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


def _parse_packages_gz(raw: bytes) -> list[PackageRef]:
    """The CPU-heavy part (gzip + line-by-line parsing) — called via
    asyncio.to_thread, see fetch_packages above."""
    text = gzip.decompress(raw).decode("utf-8", errors="replace")

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
            # Standard per-stanza field, distinct from the by-hash SHA256 of
            # the whole Packages.gz index checked elsewhere (verification/gpg.py) —
            # this one is per package file, used for cross-repo dedup
            # (docs_dev/ROADMAP.md item 29).
            value = line.split(":", 1)[1].strip()
            content_hash = value if re.fullmatch(r"[0-9a-fA-F]{64}", value) else None

    if name and version:
        packages.append(PackageRef(name=name, version=version, filename=filename, content_hash=content_hash))

    return packages
