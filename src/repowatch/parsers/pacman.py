"""pacman index parser: <repo>.db.tar.gz — a tar with <pkg>-<ver>/desc entries.

The desc format is blocks like:
    %NAME%
    package-name
    %VERSION%
    1.2.3-1
    %FILENAME%
    package-name-1.2.3-1-x86_64.pkg.tar.zst

TODO (see CLAUDE.md):
- support .files.tar.gz, if package file-list contents are ever needed.

GPG verification (repo.verify_signature): official Arch mirrors ship a
detached signature <repo>.db.tar.gz.sig alongside the index — we download
both files and verify with gpgv BEFORE unpacking/parsing."""

from __future__ import annotations

import asyncio
import httpx
import io
import logging
import re
import tarfile
from repowatch.models import PackageRef
from repowatch.parsers.base import IndexParser
from repowatch.verification.gpg import verify_detached

logger = logging.getLogger(__name__)


def _parse_desc(desc_text: str) -> dict[str, str]:
    """%KEY%\\nvalue\\n\\n... -> {"KEY": "value"} (only the first value line)."""
    fields: dict[str, str] = {}
    lines = desc_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("%") and line.endswith("%"):
            key = line.strip("%")
            i += 1
            value_lines = []
            while i < len(lines) and lines[i] != "":
                value_lines.append(lines[i])
                i += 1
            fields[key] = "\n".join(value_lines)
        i += 1
    return fields


def _parse_db_tar_gz(raw: bytes) -> list[PackageRef]:
    """The CPU-heavy part (unpacking tar.gz + parsing every desc) — called
    via asyncio.to_thread, see fetch_packages below."""
    packages: list[PackageRef] = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.name.endswith("/desc"):
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            fields = _parse_desc(extracted.read().decode("utf-8", errors="replace"))
            name = fields.get("NAME")
            version = fields.get("VERSION")
            filename = fields.get("FILENAME")
            # docs_dev/ROADMAP.md item 29 (cross-repo dedup): SHA256SUM is a
            # standard desc field, same algorithm/hex-digest shape as apt's
            # per-stanza SHA256: and dnf's <checksum type="sha256">.
            sha256sum = fields.get("SHA256SUM")
            content_hash = sha256sum if sha256sum and re.fullmatch(r"[0-9a-fA-F]{64}", sha256sum) else None
            if name and version:
                packages.append(PackageRef(name=name, version=version, filename=filename, content_hash=content_hash))

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
