"""RPM-MD: repomd.xml -> verified primary XML -> packages for the target arch.

HEAD is applied to repomd.xml, not to primary, whose name changes.
Optional GPG verification of repomd.xml.asc precedes parsing the metadata;
the primary checksum is always checked. This verifies the index's
signature, not the RPM payload.
"""
from __future__ import annotations

import asyncio
import bz2
from dataclasses import dataclass
import gzip
import hashlib
import io
import lzma
from pathlib import PurePosixPath
import re
import shutil
import subprocess
from typing import BinaryIO
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import httpx

from repowatch.processes import ProcessStream
from repowatch.gpgverify import SignatureError, verify_detached
from repowatch.parsers.base import IndexParser, PackageRef

_REPO = '{http://linux.duke.edu/metadata/repo}'
_COMMON = '{http://linux.duke.edu/metadata/common}'
_XML_BASE = '{http://www.w3.org/XML/1998/namespace}base'


@dataclass(frozen=True)
class PrimaryMetadata:
    href: str
    checksum_type: str
    checksum: str
    size: int | None
    open_checksum_type: str | None
    open_checksum: str | None
    open_size: int | None


def _relative_path(value: str | None) -> str:
    """Only a path inside upstream: warm_cache must go through our nginx.

    External/absolute location and xml:base can't be represented by the
    current single-upstream model. We don't substitute their basename or
    warm someone else's file.
    """
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError('dnf: empty or invalid location href')
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or value.startswith('/') or '\\' in value or '%' in value:
        raise ValueError(f'dnf: location must be a relative path: {value!r}')
    path = PurePosixPath(value)
    if '..' in path.parts or str(path) == '.':
        raise ValueError(f'dnf: location escapes the repository: {value!r}')
    return str(path)


def _checksum(element: ET.Element | None, required: bool = True) -> tuple[str | None, str | None]:
    if element is None:
        if required:
            raise SignatureError('dnf: repomd.xml is missing the primary checksum')
        return None, None
    algorithm = element.get('type', '').lower()
    algorithm = 'sha1' if algorithm == 'sha' else algorithm
    # SHA1/MD5 show up in older RPM-MD repos. Without GPG this is only
    # integrity — no checksum algorithm by itself attests to the owner.
    if algorithm not in {'sha256', 'sha512', 'sha384', 'sha224', 'sha1', 'md5'}:
        raise SignatureError(f'dnf: unsupported checksum type {algorithm!r}')
    digest = (element.text or '').strip().lower()
    expected_length = hashlib.new(algorithm).digest_size * 2
    if not re.fullmatch(r'[0-9a-f]{' + str(expected_length) + '}', digest):
        raise SignatureError('dnf: invalid checksum in repomd.xml')
    return algorithm, digest


def _size(element: ET.Element | None) -> int | None:
    if element is None:
        return None
    text = (element.text or '').strip()
    if not re.fullmatch(r'[0-9]+', text):
        raise ValueError('dnf: invalid primary size in repomd.xml')
    return int(text)


def _parse_repomd(raw: bytes) -> PrimaryMetadata:
    root = ET.fromstring(raw)
    if root.tag != _REPO + 'repomd':
        raise ValueError('dnf: expected a repomd root with the RPM-MD namespace')
    if any(_XML_BASE in element.attrib for element in root.iter()):
        raise ValueError('dnf: xml:base is not supported, a single upstream is required')
    primaries = [data for data in root.findall(_REPO + 'data') if data.get('type') == 'primary']
    if len(primaries) != 1:
        raise ValueError('dnf: repomd.xml must contain exactly one data type=primary (XML)')
    primary = primaries[0]
    location = primary.find(_REPO + 'location')
    href = _relative_path(location.get('href') if location is not None else None)
    algorithm, digest = _checksum(primary.find(_REPO + 'checksum'))
    assert algorithm is not None and digest is not None
    open_algorithm, open_digest = _checksum(primary.find(_REPO + 'open-checksum'), required=False)
    return PrimaryMetadata(href, algorithm, digest, _size(primary.find(_REPO + 'size')),
                           open_algorithm, open_digest, _size(primary.find(_REPO + 'open-size')))


def _verify_bytes(raw: bytes, algorithm: str | None, digest: str | None, size: int | None) -> None:
    if size is not None and len(raw) != size:
        raise SignatureError('dnf: primary size does not match repomd.xml')
    if algorithm is not None and hashlib.new(algorithm, raw).hexdigest() != digest:
        raise SignatureError('dnf: primary checksum does not match repomd.xml')


class _ZstdSubprocessStream:
    """Keep large primary XML streaming; process mechanics live in processes."""

    def __init__(self, raw: bytes):
        self._stream = ProcessStream(['zstd', '-d', '-c', '-q'], input=raw, timeout=60)

    def read(self, size: int = -1) -> bytes:
        try:
            return self._stream.read(size)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f'dnf: zstd decompression failed: {exc}') from exc

    def close(self) -> None:
        try:
            self._stream.close()
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f'dnf: zstd decompression failed: {exc}') from exc
        if self._stream.returncode != 0:
            stderr = self._stream.stderr.decode(errors='replace')[-500:]
            raise ValueError(f'dnf: zstd decompression failed: {stderr}')

    def __enter__(self) -> '_ZstdSubprocessStream':
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None:
            self._stream.abort()
        else:
            self.close()


def _open_primary(raw: bytes, href: str) -> BinaryIO:
    source = io.BytesIO(raw)
    if href.endswith('.gz'):
        return gzip.GzipFile(fileobj=source)
    if href.endswith('.xz'):
        return lzma.LZMAFile(source)
    if href.endswith('.bz2'):
        return bz2.BZ2File(source)
    if href.endswith(('.zst', '.zstd')):
        if shutil.which('zstd') is None:
            raise ValueError('dnf: primary is Zstandard-compressed — install the system "zstd" package')
        return _ZstdSubprocessStream(raw)
    if href.endswith('.xml'):
        return source
    raise ValueError(f'dnf: unsupported primary compression: {href!r}')


class _CheckedReader:
    """Computes open-checksum/size while iterparse runs, without holding the
    whole XML in memory.

    On a real Rocky primary.gz, ~23MB decompresses to ~168MB: gzip.decompress
    plus a full bytes object peaked at >400MiB before we even took a
    snapshot.
    """
    def __init__(self, stream: BinaryIO, metadata: PrimaryMetadata) -> None:
        self._stream = stream
        self._metadata = metadata
        self._digest = hashlib.new(metadata.open_checksum_type) if metadata.open_checksum_type else None
        self._size = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        self._size += len(chunk)
        if self._digest is not None:
            self._digest.update(chunk)
        return chunk

    def verify(self) -> None:
        if self._metadata.open_size is not None and self._size != self._metadata.open_size:
            raise SignatureError('dnf: primary open size does not match repomd.xml')
        if self._digest is not None and self._digest.hexdigest() != self._metadata.open_checksum:
            raise SignatureError('dnf: primary open-checksum does not match repomd.xml')


def _parse_primary(raw: bytes | BinaryIO | _CheckedReader, arch: str) -> list[PackageRef]:
    # iterparse frees nodes for already-processed packages: a large primary
    # never needs both the full ElementTree and the finished PackageRef list
    # in memory at once.
    iterator = ET.iterparse(io.BytesIO(raw) if isinstance(raw, bytes) else raw, events=('start', 'end'))
    _, root = next(iterator)
    if root.tag != _COMMON + 'metadata':
        raise ValueError('dnf: expected a metadata root with the RPM-MD namespace')
    if _XML_BASE in root.attrib:
        raise ValueError('dnf: xml:base is not supported, a single upstream is required')
    count_text = root.get('packages')
    if count_text is not None and not re.fullmatch(r'[0-9]+', count_text):
        raise ValueError('dnf: invalid packages count')
    packages: list[PackageRef] = []
    identities: set[str] = set()
    seen = 0
    for event, element in iterator:
        if event == 'start' and _XML_BASE in element.attrib:
            raise ValueError('dnf: xml:base is not supported, a single upstream is required')
        if event != 'end' or element.tag != _COMMON + 'package':
            continue
        seen += 1
        if element.get('type') != 'rpm':
            raise ValueError('dnf: unknown package type')
        name = element.findtext(_COMMON + 'name')
        package_arch = element.findtext(_COMMON + 'arch')
        version = element.find(_COMMON + 'version')
        location = element.find(_COMMON + 'location')
        if not name or not package_arch or version is None:
            raise ValueError('dnf: package missing name/arch/version')
        ver, release, epoch = version.get('ver'), version.get('rel'), version.get('epoch', '0')
        if not ver or not release or not re.fullmatch(r'[0-9]+', epoch):
            raise ValueError('dnf: invalid package EVR')
        filename = _relative_path(location.get('href') if location is not None else None)
        if package_arch in {arch, 'noarch'}:
            # arch is part of NEVRA: identical N-E-V-R for noarch and x86_64
            # must not overwrite each other in RepoSnapshot.packages.
            evr = (f'{int(epoch)}:' if int(epoch) else '') + f'{ver}-{release}.{package_arch}'
            # docs_dev/ROADMAP.md item 29 (cross-repo dedup): per-package
            # <checksum type="sha256">, same shape as apt's SHA256: and
            # pacman's SHA256SUM — only sha256 is trusted here (a repo could
            # in principle publish a different algorithm; guessing it's
            # comparable to another format's hash would risk a false match).
            checksum = element.find(_COMMON + 'checksum')
            content_hash = None
            if checksum is not None and checksum.get('type') == 'sha256' and checksum.text:
                if re.fullmatch(r'[0-9a-fA-F]{64}', checksum.text):
                    content_hash = checksum.text
            package = PackageRef(name=name, version=evr, filename=filename, content_hash=content_hash)
            if package.key in identities:
                raise ValueError(f'dnf: duplicate NEVRA {package.key!r}')
            identities.add(package.key)
            packages.append(package)
        root.clear()
    if count_text is not None and seen != int(count_text):
        raise ValueError('dnf: package count does not match metadata packages')
    return packages


def _checked_primary(raw: bytes, metadata: PrimaryMetadata, arch: str) -> list[PackageRef]:
    _verify_bytes(raw, metadata.checksum_type, metadata.checksum, metadata.size)
    with _open_primary(raw, metadata.href) as stream:
        reader = _CheckedReader(stream, metadata)
        packages = _parse_primary(reader, arch)
        reader.verify()
        return packages


class DnfParser(IndexParser):
    def index_url(self) -> str:
        return self.repo.upstream.rstrip('/') + '/repodata/repomd.xml'

    async def fetch_packages(self, client: httpx.AsyncClient) -> list[PackageRef]:
        raw = await self._http_get(client, self.index_url())
        if self.repo.verify_signature:
            signature = await self._http_get(client, self.index_url() + '.asc')
            await asyncio.to_thread(verify_detached, raw, signature, self.repo.keyring_path)
        metadata = await asyncio.to_thread(_parse_repomd, raw)
        primary = await self._http_get(client, self.repo.upstream.rstrip('/') + '/' + metadata.href)
        return await asyncio.to_thread(_checked_primary, primary, metadata, self.repo.arch)
