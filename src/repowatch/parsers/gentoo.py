"""Gentoo binhost Packages version 0; package signatures belong to Portage."""

from __future__ import annotations

import asyncio
import httpx
import re
from repowatch.models import PackageRef
from repowatch.parsers.base import IndexParser, safe_package_path

# Decoding and splitting the whole 20 MiB index in single C calls holds the GIL for
# a quarter of a second at a time; work through it in slices of this many bytes.
_SCAN_SLICE = 1024 * 1024
_CPV = re.compile(r'([A-Za-z0-9_+.-]+/[A-Za-z0-9_+.-]+)-([0-9]+(?:\.[0-9]+)*[a-z]?(?:_(?:alpha|beta|pre|rc|p)[0-9]*)*(?:-r[0-9]+)?)')


def _lines(raw: bytes):
    """The lines of raw.decode('utf-8').splitlines(), then one empty line, one slice at a time.

    A slice ends right after an LF, so no line (or "\\r\\n" pair, or multi-byte
    character: UTF-8 continuation bytes are never 0x0A) is split between slices
    and every separator str.splitlines() knows behaves as in a single call."""
    position, end = 0, len(raw)
    while position < end:
        stop = raw.find(b'\n', position + _SCAN_SLICE)
        stop = end if stop < 0 else stop + 1
        yield from raw[position:stop].decode('utf-8').splitlines()
        position = stop
    yield ''


def _parse_packages(raw: bytes, upstream: str) -> list[PackageRef]:
    records = []
    current: dict[str, str] = {}
    for line in _lines(raw):
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        key, sep, value = line.partition(': ')
        if not sep or key in current:
            raise ValueError('gentoo: malformed or duplicate index field')
        current[key] = value.strip()
    if not records or records[0].get('VERSION') != '0':
        raise ValueError('gentoo: expected Packages index version 0')
    header, *entries = records
    if header.get('URI', upstream).rstrip('/') != upstream.rstrip('/'):
        raise ValueError('gentoo: alternate index URI is unsupported; use a binhost with local package paths')
    if not header.get('PACKAGES', '').isdigit() or int(header['PACKAGES']) != len(entries):
        raise ValueError('gentoo: package count does not match the index')
    packages = []
    keys = set()
    for entry in entries:
        cpv = entry.get('CPV', '')
        match = _CPV.fullmatch(cpv)
        if not match:
            raise ValueError(f'gentoo: invalid CPV: {cpv!r}')
        name, version = match.groups()
        build = entry.get('BUILD_ID', '')
        if build:
            if not build.isascii() or not build.isdigit() or int(build) < 1:
                raise ValueError('gentoo: invalid BUILD_ID')
            version += f'-build{int(build)}'
        filename = safe_package_path(entry.get('PATH') or f'{cpv}.tbz2')
        if not filename.endswith(('.gpkg.tar', '.tbz2', '.xpak')):
            raise ValueError('gentoo: unsupported binary package suffix')
        digest = entry.get('SHA256')
        if digest is not None and not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
            raise ValueError('gentoo: invalid SHA256')
        package = PackageRef(name, version, filename, digest.lower() if digest else None)
        if package.key in keys:
            raise ValueError('gentoo: duplicate package instance')
        keys.add(package.key)
        packages.append(package)
    return packages


class GentooParser(IndexParser):
    def index_url(self) -> str:
        return self.repo.upstream.rstrip('/') + '/Packages'

    async def fetch_packages(self, client: httpx.AsyncClient) -> list[PackageRef]:
        raw = await self._http_get(client, self.index_url())
        return await asyncio.to_thread(_parse_packages, raw, self.repo.upstream)
