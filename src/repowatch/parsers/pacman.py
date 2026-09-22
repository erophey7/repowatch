"""pacman index parser: <repo>.db.tar.gz — a tar with <pkg>-<ver>/desc entries.

The desc format is blocks like:
    %NAME%
    package-name
    %VERSION%
    1.2.3-1
    %FILENAME%
    package-name-1.2.3-1-x86_64.pkg.tar.zst

TODO:
- support .files.tar.gz, if package file-list contents are ever needed.

GPG verification (repo.verify_signature): official Arch mirrors ship a
detached signature <repo>.db.tar.gz.sig alongside the index — we download
both files and verify with gpgv BEFORE unpacking/parsing."""

from __future__ import annotations

import asyncio
import gzip
import httpx
import io
import logging
import re
import tarfile
import zlib
from repowatch.models import PackageRef
from repowatch.parsers.base import IndexParser
from repowatch.verification.gpg import verify_detached

logger = logging.getLogger(__name__)


def _parse_desc(desc_text: str) -> dict[str, str]:
    """%KEY%\\nvalue\\n\\n... -> {"KEY": "value"}: a key line, then every line up to
    the next empty line is its value (lines inside a value are never keys)."""
    fields: dict[str, str] = {}
    lines = iter(desc_text.splitlines())
    for line in lines:
        if line.startswith("%") and line.endswith("%"):
            key = line.strip("%")
            value_lines = []
            for value_line in lines:  # also consumes the terminating empty line
                if value_line == "":
                    break
                value_lines.append(value_line)
            fields[key] = "\n".join(value_lines)
    return fields


def _package(desc: bytes) -> PackageRef | None:
    """The package a desc member describes, or None if it lacks a name or version."""
    fields = _parse_desc(desc.decode("utf-8", errors="replace"))
    name = fields.get("NAME")
    version = fields.get("VERSION")
    filename = fields.get("FILENAME")
    # Cross-repo dedup: SHA256SUM is a
    # standard desc field, same algorithm/hex-digest shape as apt's
    # per-stanza SHA256: and dnf's <checksum type="sha256">.
    sha256sum = fields.get("SHA256SUM")
    content_hash = sha256sum if sha256sum and re.fullmatch(r"[0-9a-fA-F]{64}", sha256sum) else None
    if name and version:
        return PackageRef(name=name, version=version, filename=filename, content_hash=content_hash)
    return None


class _Unusual(Exception):
    """The archive is outside what the direct reader handles; use tarfile."""


# Decompressed bytes fetched per read while walking the archive.
_CHUNK = 256 * 1024
_BLOCK = 512
# What tarfile's nti() reads from a numeric header field: optional blanks, octal
# digits, then blanks/NULs. Anything else (base-256 sizes, garbage) is left to tarfile.
_NUMBER = re.compile(rb"[ ]*[0-7]*[\0 ]*")
# (start, end) of the numeric fields tarfile parses and validates in every header
# but this reader does not use: mode, uid, gid, mtime, devmajor, devminor. They are
# nearly identical from header to header, so each distinct combination is checked once.
_IGNORED_NUMERIC = ((100, 108), (108, 116), (116, 124), (136, 148), (329, 337), (337, 345))
_MAX_CHECKED_COMBINATIONS = 16


class _Blocks:
    """Sequential reader over a decompressed stream with a refillable buffer."""

    def __init__(self, stream) -> None:
        """Wrap a decompressed byte stream; nothing is read until take()/skip()."""
        self._stream = stream
        self._buffer = b""
        self._start = 0

    def take(self, size: int) -> bytes:
        """Up to `size` bytes; fewer only at the end of the stream."""
        available = len(self._buffer) - self._start
        while available < size:
            chunk = self._stream.read(_CHUNK)
            if not chunk:
                break
            self._buffer = self._buffer[self._start:] + chunk
            self._start = 0
            available = len(self._buffer)
        end = self._start + min(size, available)
        data = self._buffer[self._start:end]
        self._start = end
        return data

    def skip(self, size: int) -> None:
        """Discard `size` bytes; _Unusual if the stream ends first."""
        while size > 0:
            taken = len(self.take(min(size, _CHUNK)))
            if not taken:
                raise _Unusual("truncated archive")
            size -= taken


def _octal(field: bytes) -> int:
    """The value of a plain octal header field; _Unusual for anything tarfile alone could interpret."""
    if not _NUMBER.fullmatch(field):
        raise _Unusual("unusual numeric field")
    digits = field.strip(b" \0")
    return int(digits, 8) if digits else 0


def _desc_members(raw: bytes):
    """Yield the content of every regular '<dir>/desc' member, in archive order.

    Handles plain POSIX/GNU tar headers only and raises _Unusual for anything
    else (long-name and pax extensions, links, sparse files, odd numeric fields,
    bad checksums, a missing end marker, ...), so a caller can hand the archive
    to tarfile, which then decides exactly as it always did. Everything tarfile
    validates in a header that this reader ignores is validated here too."""
    try:
        stream = gzip.GzipFile(fileobj=io.BytesIO(raw))
        blocks = _Blocks(stream)
        checked: set[bytes] = set()
        while True:
            head = blocks.take(_BLOCK)
            if len(head) != _BLOCK:
                raise _Unusual("no end-of-archive marker")
            if not any(head):
                return  # end of archive: tarfile stops at the first zero block too
            ignored = head[100:124] + head[136:148] + head[329:345]
            if ignored not in checked:
                for start, end in _IGNORED_NUMERIC:
                    _octal(head[start:end])
                if len(checked) < _MAX_CHECKED_COMBINATIONS:
                    checked.add(ignored)
            size = _octal(head[124:136])
            if sum(head) - sum(head[148:156]) + 256 != _octal(head[148:156]):
                raise _Unusual("checksum not the plain unsigned sum")
            magic = head[257:265]
            posix = magic == b"ustar\x0000"
            if not (posix or magic == b"ustar  \x00"):
                raise _Unusual("not a ustar/GNU header")
            name = head[:100].split(b"\0", 1)[0]
            if head[345]:
                if not posix:
                    raise _Unusual("GNU header with an atime/ctime area")
                name = head[345:500].split(b"\0", 1)[0] + b"/" + name
            kind = head[156:157]
            if kind == b"5":
                continue  # directory: tarfile never reads data for it
            if kind not in (b"0", b"\0") or (kind == b"\0" and name.endswith(b"/")):
                raise _Unusual("member type handled by tarfile")
            if name.endswith(b"/desc"):
                body = blocks.take(size)
                if len(body) != size:
                    raise _Unusual("truncated member")
                blocks.skip(-size % _BLOCK)
                yield body
            else:
                blocks.skip(size + -size % _BLOCK)
    except (OSError, EOFError, zlib.error) as exc:  # damaged gzip stream
        raise _Unusual(str(exc)) from exc


def _parse_db_tar_gz(raw: bytes) -> list[PackageRef]:
    """The CPU-heavy part (unpacking tar.gz + parsing every desc) — called
    via asyncio.to_thread, see fetch_packages below.

    Walks the archive with a small reader that streams the decompressed bytes
    (tarfile's member table for ~30000 entries is the dominant cost); for any
    archive that reader declines, the original tarfile implementation runs."""
    packages: list[PackageRef] = []
    try:
        for desc in _desc_members(raw):
            package = _package(desc)
            if package is not None:
                packages.append(package)
    except _Unusual:
        return _parse_db_tar_gz_reference(raw)
    return packages


def _parse_db_tar_gz_reference(raw: bytes) -> list[PackageRef]:
    """Parse through tarfile. Defines the behavior _desc_members must match and
    handles every archive it declines."""
    packages: list[PackageRef] = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.name.endswith("/desc"):
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            package = _package(extracted.read())
            if package is not None:
                packages.append(package)
    return packages


class PacmanParser(IndexParser):
    def index_url(self) -> str:
        return f"{self.repo.upstream}/{self.repo.repo_name}.db.tar.gz"

    async def fetch_packages(self, client: httpx.AsyncClient) -> list[PackageRef]:
        repo = self.repo
        url = self.index_url()
        logger.debug("pacman: fetching %s", url)
        raw = await self._http_get(client, url)

        if repo.verify_signature:
            sig = await self._http_get(client, url + ".sig")
            # gpgv verification is blocking, run it in a separate
            # thread so it doesn't stall every other concurrently checked repo
            await asyncio.to_thread(verify_detached, raw, sig, repo.keyring_path)
            logger.debug("%s: signature %s.sig verified", repo.id, repo.repo_name)

        return await asyncio.to_thread(_parse_db_tar_gz, raw)
