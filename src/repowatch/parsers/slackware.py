"""Slackware PACKAGES.TXT with optional signed CHECKSUMS.md5 verification."""

from __future__ import annotations

import asyncio
import hashlib
import httpx
import re
from repowatch.errors import SignatureError
from repowatch.models import PackageRef
from repowatch.parsers.base import IndexParser, safe_package_path
from repowatch.verification.gpg import verify_detached

def _parse_packages(raw: bytes, arch: str) -> list[PackageRef]:
    text = raw.decode('utf-8')
    if not text.lstrip().startswith('PACKAGES.TXT;'):
        raise ValueError('slackware: missing PACKAGES.TXT header')
    if not re.search(r'^Total size of all packages \(compressed\): +[0-9]+ MB$', text, re.MULTILINE):
        raise ValueError('slackware: missing package size summary')
    packages = []
    keys = set()
    for block in re.split(r'^PACKAGE NAME: *', text, flags=re.MULTILINE)[1:]:
        filename = block.splitlines()[0].strip()
        safe_package_path(filename)
        if '/' in filename or not filename.endswith(('.tgz', '.txz', '.tlz', '.tbz')):
            raise ValueError('slackware: invalid package filename')
        parts = filename[:-4].rsplit('-', 3)
        if len(parts) != 4 or not all(parts):
            raise ValueError('slackware: expected name-version-arch-build filename')
        name, version, package_arch, build = parts
        # Description lines are not metadata, even when they resemble fields.
        metadata = block.split('PACKAGE DESCRIPTION:', 1)[0]
        locations = re.findall(r'^PACKAGE LOCATION: *(\S+)\s*$', metadata, re.MULTILINE)
        if len(locations) != 1:
            raise ValueError('slackware: missing or duplicate package location')
        location = locations[0]
        if location.startswith('./'):
            location = location[2:]
        path = safe_package_path(f'{location}/{filename}' if location else filename)
        if package_arch not in (arch, 'noarch', 'fw'):
            continue
        package = PackageRef(name, f'{version}-{package_arch}-{build}', path)
        if package.key in keys:
            raise ValueError('slackware: duplicate package identity; configure components separately')
        keys.add(package.key)
        packages.append(package)
    return packages


def _check_index(raw: bytes, checksums: bytes, index_path: str) -> None:
    matches = []
    for line in checksums.decode('utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-fA-F]{32}) [ *](.+)', line)
        if match:
            filename = match[2]
            if filename.startswith('./'):
                filename = filename[2:]
            if filename == index_path:
                matches.append(match[1].lower())
    # MD5 is the upstream format, not a SHA256 suitable for deduplication.
    digest = hashlib.md5(raw, usedforsecurity=False).hexdigest()
    if len(matches) != 1 or matches[0] != digest:
        raise SignatureError(f'slackware: missing, ambiguous or mismatched checksum for {index_path}')


class SlackwareParser(IndexParser):
    def index_path(self) -> str:
        return f'{self.repo.component}/PACKAGES.TXT' if self.repo.component else 'PACKAGES.TXT'

    def index_url(self) -> str:
        return self.repo.upstream.rstrip('/') + '/' + self.index_path()

    async def fetch_packages(self, client: httpx.AsyncClient) -> list[PackageRef]:
        raw = await self._http_get(client, self.index_url())
        if self.repo.verify_signature:
            root = self.repo.upstream.rstrip('/')
            checksums = await self._http_get(client, root + '/CHECKSUMS.md5')
            signature = await self._http_get(client, root + '/CHECKSUMS.md5.asc')
            await asyncio.to_thread(verify_detached, checksums, signature, self.repo.keyring_path)
            await asyncio.to_thread(_check_index, raw, checksums, self.index_path())
        return await asyncio.to_thread(_parse_packages, raw, self.repo.arch)
