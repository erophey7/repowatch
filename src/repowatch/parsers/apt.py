"""apt index parser: Packages(.gz) in deb822 format.

Multiple architectures are expressed as separate RepoConfig entries with
different id/arch; each architecture's snapshot and history are independent.

GPG verification (repo.verify_signature): InRelease is clearsigned, the
signature is checked with gpgv, then the expected SHA256 for the specific
Packages.gz is extracted from the verified body and compared after
download — i.e. we don't trust Packages.gz itself (nobody signs it
separately), but the chain InRelease signature -> SHA256 -> Packages.gz."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import httpx
import logging
import re
from repowatch.errors import SignatureError
from repowatch.models import PackageRef
from repowatch.parsers.base import IndexParser
from repowatch.verification.gpg import find_sha256_in_release, verify_clearsigned, _extract_clearsigned_body

logger = logging.getLogger(__name__)


class AptParser(IndexParser):
    def index_url(self) -> str:
        repo = self.repo
        # apt repository structure:
        # <upstream>/dists/<distribution>/<component>/binary-<arch>/Packages.gz
        return (
            f"{repo.upstream}/dists/{repo.distribution}/{repo.component}"
            f"/binary-{repo.arch}/Packages.gz"
        )

    def _inrelease_url(self) -> str:
        return f"{self.repo.upstream}/dists/{self.repo.distribution}/InRelease"

    async def fetch_packages(self, client: httpx.AsyncClient) -> list[PackageRef]:
        repo = self.repo
        url = self.index_url()
        expected_sha256 = None
        release_body = None
        try:
            release_raw = await self._http_get(client, self._inrelease_url())
        except httpx.HTTPStatusError as exc:
            if repo.verify_signature or exc.response.status_code not in (404, 410):
                raise
            try:
                release_body = await self._http_get(client, self._inrelease_url().removesuffix("InRelease") + "Release")
            except httpx.HTTPStatusError as release_exc:
                if release_exc.response.status_code not in (404, 410):
                    raise
        else:
            if repo.verify_signature:
                release_body = await asyncio.to_thread(verify_clearsigned, release_raw, repo.keyring_path)
            else:
                release_body = _extract_clearsigned_body(release_raw)
        if release_body is not None:
            expected_sha256 = find_sha256_in_release(
                release_body, f"{repo.component}/binary-{repo.arch}/Packages.gz")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
                raise SignatureError("Invalid SHA256 in Release")
            if re.search(rb"(?mi)^Acquire-By-Hash:\s*yes\s*$", release_body):
                url = url.rsplit("/", 1)[0] + "/by-hash/SHA256/" + expected_sha256
        logger.debug("apt: fetching %s", url)
        try:
            raw = await self._http_get(client, url)
        except httpx.HTTPStatusError as exc:
            if url == self.index_url() or exc.response.status_code not in (404, 410):
                raise
            # A mirror may advertise by-hash before syncing the object.
            # Fallback still MUST match the hash from the same Release.
            raw = await self._http_get(client, self.index_url())

        if expected_sha256 is not None:
            actual_sha256 = hashlib.sha256(raw).hexdigest()
            if actual_sha256 != expected_sha256:
                raise SignatureError(
                    f"{repo.id}: Packages.gz SHA256 does not match InRelease "
                    f"({actual_sha256} != {expected_sha256}) — possible tampering"
                )
            logger.debug("%s: Packages.gz SHA256 confirmed via InRelease", repo.id)

        return await asyncio.to_thread(_parse_packages_gz, raw)


# Fast path: only the four consumed fields are located, with a literal "\n" prefix
# so the regex engine can skip to candidate lines instead of testing every offset.
_FIRST_FIELD = re.compile(rb"(Package|Version|Filename|SHA256):([^\r\n]*)")
_LATER_FIELDS = re.compile(rb"\n(Package|Version|Filename|SHA256):([^\r\n]*)")
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")

# str.splitlines() also breaks on these. The fast path only splits on LF, so an
# index containing any of them is parsed by the reference implementation.
_ASCII_LINE_BREAKS = (b"\r", b"\x0b", b"\x0c", b"\x1c", b"\x1d", b"\x1e")
_NEL = (b"\xc2\x85",)                          # U+0085
_LINE_SEPARATORS = (b"\xe2\x80\xa8", b"\xe2\x80\xa9")  # U+2028, U+2029
# Lead-byte occurrences inspected one by one before switching to a direct
# substring search, which is slower but linear whatever the input looks like.
_LEAD_BYTE_SCAN_LIMIT = 4096
# A single C call over the whole 70 MiB document (regex scan, substring search)
# holds the GIL for up to a quarter of a second, stalling the event loop and HTTP
# threads of the same process. Scan in slices of this many bytes instead; the
# interpreter can hand the GIL over between calls.
_SCAN_SLICE = 1024 * 1024


def _parse_packages_gz(raw: bytes) -> list[PackageRef]:
    """The CPU-heavy part (gzip + parsing) — called via asyncio.to_thread,
    see fetch_packages above."""
    return _parse_packages(gzip.decompress(raw))


def _parse_packages(data: bytes) -> list[PackageRef]:
    """Extract Package/Version/Filename/SHA256 from a deb822 Packages document.

    Results are identical to _parse_packages_reference (the oracle in tests),
    including replacement decoding of invalid UTF-8: only the used fields are
    decoded, not the whole document. Documents with line breaks other than LF
    take the reference path, so unusual separators cannot change the result."""
    if _has_unusual_line_break(data):
        return _parse_packages_reference(data)

    first = _FIRST_FIELD.match(data)
    fields = _field_lines(data)
    if first is not None:
        fields.insert(0, first.groups())

    packages: list[PackageRef] = []
    name: str | None = None
    version: str | None = None
    filename: str | None = None
    content_hash: str | None = None

    for field, raw_value in fields:
        value = raw_value.decode("utf-8", errors="replace").strip()
        if field == b"Package":
            # start of a new stanza — save the previous one if it was complete
            if name and version:
                packages.append(PackageRef(name=name, version=version, filename=filename, content_hash=content_hash))
            name = value
            version = None
            filename = None
            content_hash = None
        elif field == b"Version":
            version = value
        elif field == b"Filename":
            filename = value
        else:
            # Standard per-stanza field, distinct from the by-hash SHA256 of
            # the whole Packages.gz index checked elsewhere (verification/gpg.py) —
            # this one is per package file, used for cross-repo dedup
            # (docs_dev/ROADMAP.md item 29).
            content_hash = value if _SHA256_HEX.fullmatch(value) else None

    if name and version:
        packages.append(PackageRef(name=name, version=version, filename=filename, content_hash=content_hash))

    return packages


def _field_lines(data: bytes) -> list[tuple[bytes, bytes]]:
    """(field, raw value) for every used field on a line after the first, in order.

    Slices end at an LF, and the next slice starts on that LF, which is where
    every match begins: no match is split or lost, and a value can only end at
    an LF or at the end of the data, so slicing does not change any result."""
    fields: list[tuple[bytes, bytes]] = []
    position, end = 0, len(data)
    while position < end:
        stop = data.find(b"\n", position + _SCAN_SLICE)
        if stop < 0:
            stop = end
        fields.extend(_LATER_FIELDS.findall(data, position, stop))
        position = stop
    return fields


def _find(data: bytes, needle: bytes, start: int = 0) -> int:
    """bytes.find(needle, start) in bounded slices (see _SCAN_SLICE)."""
    overlap = len(needle) - 1
    end = len(data)
    while start < end:
        stop = start + _SCAN_SLICE
        position = data.find(needle, start, min(stop + overlap, end))
        if position != -1:
            return position
        start = stop
    return -1


def _has_unusual_line_break(data: bytes) -> bool:
    """True if str.splitlines() could split the decoded text somewhere other than at LF.

    Byte-level and deliberately conservative: it looks for the UTF-8 encodings
    of the separators, and any real occurrence in the decoded text implies one
    here (a lead byte such as 0xC2 always starts a new sequence)."""
    if any(_find(data, separator) != -1 for separator in _ASCII_LINE_BREAKS):
        return True
    return (_has_encoded_break(data, b"\xc2", _NEL)
            or _has_encoded_break(data, b"\xe2", _LINE_SEPARATORS))


def _has_encoded_break(data: bytes, lead: bytes, sequences: tuple[bytes, ...]) -> bool:
    """True if any of the multi-byte `sequences` (all starting with `lead`) occurs in data."""
    inspected = 0
    position = _find(data, lead)
    while position != -1:
        if data.startswith(sequences, position):
            return True
        inspected += 1
        if inspected > _LEAD_BYTE_SCAN_LIMIT:
            return any(_find(data, sequence) != -1 for sequence in sequences)
        position = _find(data, lead, position + 1)
    return False


def _parse_packages_reference(data: bytes) -> list[PackageRef]:
    """Line-by-line parser over the fully decoded document.

    Defines the behavior the fast path must match; also handles every document
    the fast path declines."""
    text = data.decode("utf-8", errors="replace")

    packages: list[PackageRef] = []
    name: str | None = None
    version: str | None = None
    filename: str | None = None
    content_hash: str | None = None

    for line in text.splitlines():
        if line.startswith("Package:"):
            # start of a new stanza — save the previous one if it was complete
            if name and version:
                packages.append(PackageRef(name=name, version=version, filename=filename, content_hash=content_hash))
            name = line.split(":", 1)[1].strip()
            version = None
            filename = None
            content_hash = None
        elif line.startswith("Version:"):
            version = line.split(":", 1)[1].strip()
        elif line.startswith("Filename:"):
            filename = line.split(":", 1)[1].strip()
        elif line.startswith("SHA256:"):
            value = line.split(":", 1)[1].strip()
            content_hash = value if _SHA256_HEX.fullmatch(value) else None

    if name and version:
        packages.append(PackageRef(name=name, version=version, filename=filename, content_hash=content_hash))

    return packages
