import asyncio
import bz2
from dataclasses import replace
import gzip
import hashlib
import lzma
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import httpx
import pytest

from repowatch.config.models import Config
from repowatch.errors import ConfigError
from repowatch.config.models import RepoConfig
from repowatch.config.models import StatusServerConfig
from repowatch.errors import SignatureError
from repowatch.parsers import DnfParser, PARSERS
from repowatch.parsers.dnf import _checked_primary, _open_primary, _parse_primary, _parse_repomd, _relative_path
from repowatch.models import RepoSnapshot
from repowatch.runtime.context import ServiceState
from repowatch.operations.check import check_repo

FIXTURES = Path(__file__).parent / 'fixtures' / 'dnf'
PRIMARY = (FIXTURES / 'primary.xml').read_bytes()
PACKED = (FIXTURES / 'primary.xml.gz').read_bytes()
REPOMD = (FIXTURES / 'repomd.xml').read_bytes()


def repo(**overrides):
    return RepoConfig(**{'id': 'rpm-test', 'type': 'dnf', 'upstream': 'https://example.org/repo/',
                         'arch': 'x86_64', 'prefetch': False, **overrides})


def fetch(parser, repomd=REPOMD, primary=PACKED):
    requests = []
    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith('/repomd.xml'):
            return httpx.Response(200, content=repomd)
        if request.url.path.endswith('/repomd.xml.asc'):
            return httpx.Response(200, content=b'signature')
        return httpx.Response(200, content=primary)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await parser.fetch(client)
    return asyncio.run(run()), requests


def test_full_fetch_preserves_epoch_arch_and_names():
    assert PARSERS['dnf'] is DnfParser
    snapshot, requested = fetch(DnfParser(repo()))
    assert snapshot.packages == {
        'demo-2:1.4-3.el9.x86_64': 'Packages/d/demo-1.4-3.el9.x86_64.rpm',
        'demo-2:1.4-3.el9.noarch': 'Packages/d/demo-1.4-3.el9.noarch.rpm',
        'docs-3.0-1.noarch': 'Packages/d/docs-3.0-1.noarch.rpm',
    }
    assert snapshot.names['demo-2:1.4-3.el9.x86_64'] == 'demo'
    assert requested == ['/repo/repodata/repomd.xml', '/repo/repodata/primary.xml.gz']


def test_architecture_filter_is_exact_and_includes_noarch():
    snapshot, _ = fetch(DnfParser(repo(arch='aarch64')))
    assert len(snapshot.packages) == 3
    assert 'demo-2:1.4-3.el9.aarch64' in snapshot.packages
    assert not any(key.endswith(('.x86_64', '.src')) for key in snapshot.packages)


def test_head_uses_repomd_and_passes_conditional_headers():
    parser = DnfParser(repo())
    def handler(request):
        assert request.method == 'HEAD'
        assert str(request.url) == 'https://example.org/repo/repodata/repomd.xml'
        assert request.headers['If-None-Match'] == 'v1'
        return httpx.Response(304)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await parser.check_index_changed(client, 'v1', None)
            assert result.unchanged
    asyncio.run(run())


def test_signature_verified_before_metadata_parsing_or_primary_fetch(monkeypatch):
    verify = Mock()
    monkeypatch.setattr('repowatch.parsers.dnf.verify_detached', verify)
    snapshot, requested = fetch(DnfParser(repo(verify_signature=True, keyring_path='/test/keyring.gpg')))
    assert len(snapshot.packages) == 3
    verify.assert_called_once_with(REPOMD, b'signature', '/test/keyring.gpg')
    assert requested[1] == '/repo/repodata/repomd.xml.asc'
    verify.side_effect = SignatureError('bad signature')
    parser = DnfParser(repo(verify_signature=True, keyring_path='/test/keyring.gpg'))
    requested = []
    async def fake_get(client, url):
        requested.append(url)
        return b'not even XML' if url.endswith('.xml') else b'signature'
    monkeypatch.setattr(parser, '_http_get', fake_get)
    with pytest.raises(SignatureError, match='bad signature'):
        asyncio.run(parser.fetch_packages(None))
    assert len(requested) == 2


@pytest.mark.parametrize('target', ['repomd.xml', 'repomd.xml.asc', 'primary.xml.gz'])
def test_missing_metadata_never_becomes_empty_snapshot(target):
    parser = DnfParser(repo(verify_signature=target.endswith('.asc'), keyring_path='/test/keyring.gpg'))
    def handler(request):
        if request.url.path.endswith(target):
            return httpx.Response(404)
        return httpx.Response(200, content=REPOMD)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await parser.fetch(client)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())


def test_checksum_failure_without_gpg_precedes_open_primaryion(monkeypatch):
    decompress = Mock(side_effect=AssertionError('must not decompress'))
    monkeypatch.setattr('repowatch.parsers.dnf._open_primary', decompress)
    with pytest.raises(SignatureError):
        fetch(DnfParser(repo()), primary=PACKED[:-1] + bytes([PACKED[-1] ^ 1]))
    decompress.assert_not_called()


@pytest.mark.parametrize('changes', [
    {'size': 1}, {'checksum': '0' * 64}, {'open_size': 1}, {'open_checksum': '0' * 64},
])
def test_both_compressed_and_open_integrity_are_checked(changes):
    metadata = replace(_parse_repomd(REPOMD), **changes)
    with pytest.raises(SignatureError):
        _checked_primary(PACKED, metadata, 'x86_64')


@pytest.mark.parametrize('suffix,compress', [('.xml', lambda x: x), ('.xml.gz', gzip.compress),
                                             ('.xml.xz', lzma.compress), ('.xml.bz2', bz2.compress)])
def test_standard_compressions_and_optional_open_checksum(suffix, compress):
    packed = compress(PRIMARY)
    metadata = replace(_parse_repomd(REPOMD), href='repodata/primary' + suffix,
                       checksum=hashlib.sha256(packed).hexdigest(), size=None,
                       open_checksum_type=None, open_checksum=None, open_size=None)
    assert len(_checked_primary(packed, metadata, 'x86_64')) == 3


@pytest.mark.skipif(shutil.which('zstd') is None, reason="system 'zstd' binary is not installed")
def test_zstandard_when_available():
    # Compressed with the real system zstd binary, the same tool
    # _open_primary now shells out to for decompression (not
    # compression.zstd) — see parsers/dnf.py's _ZstdSubprocessStream.
    packed = subprocess.run(['zstd', '-c', '-q'], input=PRIMARY, capture_output=True, check=True).stdout
    with _open_primary(packed, 'primary.xml.zst') as stream:
        assert stream.read() == PRIMARY


def test_zstandard_missing_binary_has_actionable_error(monkeypatch):
    monkeypatch.setattr('repowatch.parsers.dnf.shutil.which', lambda name: None)
    with pytest.raises(ValueError, match='zstd'):
        _open_primary(b'anything', 'primary.xml.zst')


@pytest.mark.skipif(shutil.which('zstd') is None, reason="system 'zstd' binary is not installed")
def test_zstandard_decompression_failure_is_reported():
    with pytest.raises(ValueError, match='zstd'):
        with _open_primary(b'not actually zstd-compressed data', 'primary.xml.zst') as stream:
            stream.read()


@pytest.mark.parametrize('href', ['primary.xml.zck', 'primary.sqlite.bz2.unknown'])
def test_unsupported_compression_is_explicit(href):
    with pytest.raises(ValueError, match='compression'):
        _open_primary(b'anything', href)


@pytest.mark.parametrize('href', ['', '/', '../x', 'repodata/../../x', 'https://other.org/x', '//other.org/x',
                                  '/outside', 'x?token=secret', 'x#fragment', '%2e%2e/x', 'x\\y', 'x\ny', '.'])
def test_locations_cannot_escape_upstream(href):
    with pytest.raises(ValueError):
        _relative_path(href)


@pytest.mark.parametrize('raw', [b'<html/>', b'<repomd',
    REPOMD.replace(b'type="primary"', b'type="primary_db"'),
    REPOMD.replace(b'<revision>', b'<revision xml:base="https://other.org">'),
    REPOMD.replace(b'http://linux.duke.edu/metadata/repo', b'wrong-namespace'),
])
def test_bad_repomd_is_rejected(raw):
    with pytest.raises((ValueError, ET.ParseError)):
        _parse_repomd(raw)


@pytest.mark.parametrize('raw', [
    REPOMD.replace(b'type="sha256"', b'type="unsupported"'),
    REPOMD.replace(hashlib.sha256(PACKED).hexdigest().encode(), b'not-hex'),
    REPOMD.replace(b'<checksum ', b'<missing ').replace(b'</checksum>', b'</missing>'),
])
def test_bad_checksum_metadata_is_rejected(raw):
    with pytest.raises(SignatureError):
        _parse_repomd(raw)


@pytest.mark.parametrize('raw', [b'<html/>', PRIMARY[:-20],
    PRIMARY.replace(b'packages="5"', b'packages="6"'),
    PRIMARY.replace(b'<name>demo</name>', b'<name/>'),
    PRIMARY.replace(b'epoch="2"', b'epoch="bad"'),
    PRIMARY.replace(b'rel="3.el9"', b'rel=""'),
    PRIMARY.replace(b'<metadata ', b'<metadata xml:base="https://other.org" '),
    PRIMARY.replace(b'Packages/d/', b'../'),
])
def test_bad_primary_is_not_partially_accepted(raw):
    with pytest.raises((ValueError, ET.ParseError)):
        _parse_primary(raw, 'x86_64')


def test_duplicate_nevra_is_not_silently_overwritten():
    raw = PRIMARY.replace(b'<arch>aarch64</arch>', b'<arch>x86_64</arch>')
    with pytest.raises(ValueError, match='NEVRA'):
        _parse_primary(raw, 'x86_64')


def test_empty_repository_is_valid():
    assert _parse_primary(b'<metadata xmlns="http://linux.duke.edu/metadata/common" packages="0"/>', 'x86_64') == []


def test_watcher_preserves_snapshot_on_checksum_failure_and_recovers(tmp_path, monkeypatch):
    store = ServiceState(tmp_path / 'state')
    current = repo()
    config = Config(state_db=store.database.db_path, check_interval=300, cache_base_url='http://localhost',
                    status_server=StatusServerConfig(), repos=[current])
    store.repositories.record_snapshot(RepoSnapshot(current.id, {'old-1': 'old.rpm'}))
    original_client = httpx.AsyncClient
    corrupted = [True]
    def handler(request):
        if request.method == 'HEAD':
            return httpx.Response(200, headers={'ETag': 'new'})
        return httpx.Response(200, content=REPOMD if request.url.path.endswith('repomd.xml') else (b'bad' if corrupted[0] else PACKED))
    monkeypatch.setattr('repowatch.operations.check.httpx.AsyncClient', lambda: original_client(transport=httpx.MockTransport(handler)))
    asyncio.run(check_repo(config, current, store))
    assert store.repositories.get_packages(current.id) == {'old-1': 'old.rpm'}
    assert store.repositories.get_index_meta(current.id) == (None, None)
    with store.database.connect() as conn:
        assert conn.execute('SELECT consecutive_failures FROM failure_state').fetchone()[0] == 1
    corrupted[0] = False
    asyncio.run(check_repo(config, current, store))
    assert len(store.repositories.get_packages(current.id)) == 3
    with store.database.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM failure_state').fetchone()[0] == 0
    assert store.repositories.get_index_meta(current.id)[0] == 'new'


def test_dnf_signature_config_requires_keyring():
    with pytest.raises(ConfigError, match='keyring_path'):
        repo(verify_signature=True)


def test_primary_parser_only_requests_bounded_chunks():
    import io
    class BoundedReader(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 65536, 'primary must not be read into one giant bytes object'
            return super().read(size)
    assert len(_parse_primary(BoundedReader(PRIMARY), 'x86_64')) == 3


def test_parse_primary_reads_the_per_package_sha256_checksum():
    """docs_dev/ROADMAP.md item 29 — content_hash comes from the per-package
    <checksum type="sha256">, not the container-level checksum in repomd.xml
    (that one's already covered by _checked_primary/_parse_repomd tests)."""
    valid = 'c' * 64
    raw = (
        '<metadata xmlns="http://linux.duke.edu/metadata/common" packages="2">'
        '<package type="rpm"><name>a</name><arch>x86_64</arch>'
        '<version epoch="0" ver="1" rel="1"/>'
        f'<checksum type="sha256" pkgid="YES">{valid}</checksum>'
        '<location href="a.rpm"/></package>'
        '<package type="rpm"><name>b</name><arch>x86_64</arch>'
        '<version epoch="0" ver="1" rel="1"/>'
        # Wrong length/algorithm — must not be trusted as a real SHA256.
        '<checksum type="sha256" pkgid="YES">tooshort</checksum>'
        '<location href="b.rpm"/></package>'
        '</metadata>'
    ).encode()
    packages = {p.name: p for p in _parse_primary(raw, 'x86_64')}
    assert packages['a'].content_hash == valid
    assert packages['b'].content_hash is None


def test_duplicate_primary_metadata_is_rejected():
    root = ET.fromstring(REPOMD)
    primary = root.find('{http://linux.duke.edu/metadata/repo}data')
    root.append(primary)
    with pytest.raises(ValueError, match='exactly one'):
        _parse_repomd(ET.tostring(root))


def test_corrupt_compression_with_matching_checksum_is_rejected():
    raw = b'not gzip'
    metadata = replace(_parse_repomd(REPOMD), checksum=hashlib.sha256(raw).hexdigest(), size=len(raw))
    with pytest.raises(gzip.BadGzipFile):
        _checked_primary(raw, metadata, 'x86_64')


@pytest.mark.parametrize('algorithm', ['sha', 'sha1', 'sha224', 'sha256', 'sha384', 'sha512', 'md5'])
def test_rpm_metadata_checksum_algorithms(algorithm):
    root = ET.fromstring(REPOMD)
    for tag, data in [('checksum', PACKED), ('open-checksum', PRIMARY)]:
        element = root.find('.//{http://linux.duke.edu/metadata/repo}' + tag)
        element.set('type', algorithm)
        element.text = hashlib.new('sha1' if algorithm == 'sha' else algorithm, data).hexdigest()
    assert len(_checked_primary(PACKED, _parse_repomd(ET.tostring(root)), 'x86_64')) == 3


def test_streaming_primary_keeps_packages_across_read_boundaries():
    packages = ''.join(f'<package type="rpm"><name>p{i}</name><arch>x86_64</arch>'
                       f'<version ver="1" rel="1"/><location href="Packages/p{i}.rpm"/></package>' for i in range(10000))
    raw = f'<metadata xmlns="http://linux.duke.edu/metadata/common" packages="10000">{packages}</metadata>'.encode()
    result = _parse_primary(raw, 'x86_64')
    assert len({p.key for p in result}) == 10000
    assert result[-1].filename == 'Packages/p9999.rpm'
