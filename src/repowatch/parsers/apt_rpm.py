"""ALT APT-RPM: signed base/release + concatenated RPM headers in pkglist.

Binary layout is the RPM header format (big endian index and data sections).
Only package identity/location tags are decoded; no RPM library or command is
needed. Compressed indexes are parsed as streams, not inflated into one blob.
"""
from __future__ import annotations

import asyncio
import bz2
import gzip
import hashlib
import io
import lzma
import re
import struct
from typing import BinaryIO

import httpx

from repowatch.gpgverify import SignatureError, verify_detached
from repowatch.parsers.base import IndexParser, PackageRef

_MAGIC = b'\x8e\xad\xe8\x01\x00\x00\x00\x00'
# rpm tag numbers and APT-RPM custom filename/directory tags.
_STRINGS = {1000, 1001, 1002, 1022, 1155, 1000000, 1000010}
_INTS = {1003, 1006}
_MAX_EXPANDED = 512 * 1024 * 1024


def release_body(raw: bytes, keyring: str | None = None) -> bytes:
    marker = b'-----BEGIN PGP SIGNATURE-----'
    body, sep, signature = raw.partition(marker)
    if keyring:
        if not sep:
            raise SignatureError('apt-rpm: release has no appended OpenPGP signature')
        # The exact bytes preceding the marker are signed, including the
        # final blank line. Do not strip/canonicalize the release payload.
        verify_detached(body, sep + signature, keyring)
    return body


def checksums(body: bytes) -> dict[str, tuple[str, str, int]]:
    entries: dict[str, tuple[str, str, int]] = {}
    algorithm = None
    rank = {'sha256': 1, 'blake2b': 2}
    for line in body.decode('utf-8', errors='strict').splitlines():
        if not line.startswith((' ', '\t')):
            algorithm = {'BLAKE2b:': 'blake2b', 'SHA256:': 'sha256'}.get(line)
            continue
        if algorithm is None:
            continue
        fields = line.split()
        if len(fields) != 3:
            raise SignatureError('apt-rpm: malformed checksum entry')
        digest, size, path = fields
        length = 128 if algorithm == 'blake2b' else 64
        if not re.fullmatch(r'[0-9a-fA-F]{' + str(length) + '}', digest) or not size.isdigit():
            raise SignatureError('apt-rpm: malformed checksum/size')
        entry = (algorithm, digest.lower(), int(size))
        if path in entries:
            previous = entries[path]
            if previous[0] == algorithm:
                raise SignatureError('apt-rpm: duplicate checksum entry')
            if rank[previous[0]] > rank[algorithm]:
                continue
        entries[path] = entry
    return entries


def _check(raw: bytes, expected: tuple[str, str, int]) -> None:
    algorithm, digest, size = expected
    if len(raw) != size or hashlib.new(algorithm, raw).hexdigest() != digest:
        raise SignatureError('apt-rpm: pkglist checksum/size mismatch')


class _Reader:
    def __init__(self, stream: BinaryIO, expected: tuple[str, str, int] | None):
        self.stream = stream
        self.expected = expected
        self.count = 0
        self.hasher = hashlib.new(expected[0]) if expected else None

    def read(self, size: int) -> bytes:
        data = self.stream.read(size)
        self.count += len(data)
        if self.count > _MAX_EXPANDED or (self.expected and self.count > self.expected[2]):
            raise SignatureError('apt-rpm: expanded pkglist exceeds declared/maximum size')
        if self.hasher:
            self.hasher.update(data)
        return data

    def finish(self) -> None:
        if self.expected and (self.count != self.expected[2] or self.hasher.hexdigest() != self.expected[1]):
            raise SignatureError('apt-rpm: expanded pkglist checksum/size mismatch')


def _relative(value: str) -> str:
    if (not re.fullmatch(r'[A-Za-z0-9_+~./-]+', value)
            or value.startswith('/') or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('apt-rpm: unsafe package location')
    return value


def parse_pkglist(raw: bytes, compression: str, arch: str, component: str,
                  expected: tuple[str, str, int] | None = None) -> list[PackageRef]:
    source = io.BytesIO(raw)
    opener = {'.xz': lzma.LZMAFile, '.bz2': bz2.BZ2File}
    if compression == '.gz':
        stream = gzip.GzipFile(fileobj=source, mode='rb')
    else:
        stream = opener[compression](source, mode='rb') if compression else source
    packages: list[PackageRef] = []
    seen: set[str] = set()
    with stream:
        reader = _Reader(stream, expected)
        while True:
            header = reader.read(16)
            if not header:
                break
            if len(header) != 16 or header[:8] != _MAGIC:
                raise ValueError('apt-rpm: truncated/invalid RPM header')
            count, size = struct.unpack('>II', header[8:])
            if not 1 <= count <= 65536 or not 1 <= size <= 64 * 1024 * 1024:
                raise ValueError('apt-rpm: unreasonable RPM header dimensions')
            index = reader.read(count * 16)
            data = reader.read(size)
            if len(index) != count * 16 or len(data) != size:
                raise ValueError('apt-rpm: truncated RPM index/data')
            values: dict[int, str | int] = {}
            tags: set[int] = set()
            for tag, kind, offset, items in struct.iter_unpack('>4I', index):
                if tag in tags or offset >= size:
                    raise ValueError('apt-rpm: duplicate tag or invalid offset')
                tags.add(tag)
                if tag in _STRINGS:
                    if kind != 6 or items != 1:
                        raise ValueError('apt-rpm: invalid string tag type/count')
                    end = data.find(b'\0', offset)
                    if end < 0:
                        raise ValueError('apt-rpm: unterminated string')
                    values[tag] = data[offset:end].decode('utf-8', errors='strict')
                elif tag in _INTS:
                    if kind != 4 or items != 1 or offset + 4 > size:
                        raise ValueError('apt-rpm: invalid integer tag')
                    values[tag] = struct.unpack_from('>I', data, offset)[0]
            if any(not values.get(tag) for tag in (1000, 1001, 1002, 1022, 1000000)):
                raise ValueError('apt-rpm: missing package identity/location')
            package_arch = values[1022]
            filename = _relative(values[1000000])
            directory = _relative(values.get(1000010, 'RPMS.' + component))
            if '/' in filename or not filename.endswith('.rpm'):
                raise ValueError('apt-rpm: filename must be a basename ending in .rpm')
            if package_arch not in (arch, 'noarch'):
                continue
            epoch = values.get(1003, 0)
            version = (str(epoch) + ':' if epoch else '') + values[1001] + '-' + values[1002]
            if values.get(1155):
                version += ':' + values[1155]
            if values.get(1006):
                version += '@' + str(values[1006])
            package = PackageRef(str(values[1000]), version + '.' + str(package_arch), directory + '/' + filename)
            if package.key in seen:
                raise ValueError('apt-rpm: duplicate package identity')
            seen.add(package.key)
            packages.append(package)
        reader.finish()
    return packages


class AptRpmParser(IndexParser):
    def index_url(self) -> str:
        # HEAD follows release: a change of compression/index filenames must
        # not be hidden by validators of an old pkglist URL.
        return self.repo.upstream.rstrip('/') + '/base/release'

    async def fetch_packages(self, client: httpx.AsyncClient) -> list[PackageRef]:
        release = await self._http_get(client, self.index_url())
        body = await asyncio.to_thread(release_body, release,
                                       self.repo.keyring_path if self.repo.verify_signature else None)
        entries = checksums(body)
        base = 'base/pkglist.' + self.repo.component
        for suffix in ('.xz', '.bz2', '.gz', ''):
            if base + suffix in entries:
                break
        else:
            raise SignatureError('apt-rpm: release has no SHA256/BLAKE2b entry for pkglist')
        raw = await self._http_get(client, self.repo.upstream.rstrip('/') + '/' + base + suffix)
        await asyncio.to_thread(_check, raw, entries[base + suffix])
        return await asyncio.to_thread(parse_pkglist, raw, suffix, self.repo.arch,
                                       self.repo.component, entries.get(base))
