"""apk index parser: APKINDEX.tar.gz -> the APKINDEX file inside it.

The APKINDEX format is line-based, records separated by a blank line,
fields:
    P:package-name
    V:version
    ...
    (there's no separate field for the full filename — it's built as
     <P>-<V>.apk)

TODO (see CLAUDE.md):
- honor the architecture from the A: field, for mixed-arch repositories.
"""

from __future__ import annotations

import asyncio
import io
import logging
import tarfile

import httpx

from repowatch.parsers.base import IndexParser, PackageRef

logger = logging.getLogger(__name__)


def _parse_apkindex_tar_gz(raw: bytes, url: str) -> list[PackageRef]:
    """The CPU-heavy part (unpacking tar.gz + line-by-line APKINDEX parsing) —
    called via asyncio.to_thread, see fetch_packages below."""
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        member = next((m for m in tar.getmembers() if m.name == "APKINDEX"), None)
        if member is None:
            logger.warning("apk: APKINDEX not found inside %s", url)
            return []

        extracted = tar.extractfile(member)
        if extracted is None:
            return []

        text = extracted.read().decode("utf-8", errors="replace")

    packages: list[PackageRef] = []
    name: str | None = None
    version: str | None = None
    for line in text.splitlines():
        if line == "":
            if name and version:
                packages.append(
                    PackageRef(name=name, version=version, filename=f"{name}-{version}.apk")
                )
            name = None
            version = None
            continue
        if line.startswith("P:"):
            name = line[2:]
        elif line.startswith("V:"):
            version = line[2:]

    if name and version:
        packages.append(PackageRef(name=name, version=version, filename=f"{name}-{version}.apk"))

    return packages


class ApkParser(IndexParser):
    def index_url(self) -> str:
        return f"{self.repo.upstream}/{self.repo.arch}/APKINDEX.tar.gz"

    async def fetch_packages(self, client: httpx.AsyncClient) -> list[PackageRef]:
        url = self.index_url()
        logger.debug("apk: fetching %s", url)
        raw = await self._http_get(client, url)
        if self.repo.verify_signature:
            from repowatch.apkverify import verify_index
            raw = await asyncio.to_thread(verify_index, raw, self.repo.apk_keys_dir, self.repo.apk_signature_backend)
        return await asyncio.to_thread(_parse_apkindex_tar_gz, raw, url)
