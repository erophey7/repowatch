"""Gentoo/Slackware format and integration regressions; no network fixtures."""
import repowatch.operations.check as operations_check
import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import re

import httpx
import pytest

from repowatch.config.models import Config
from repowatch.errors import ConfigError
from repowatch.config.models import NginxConfig
from repowatch.config.models import RepoConfig
from repowatch.config.models import StatusServerConfig
from repowatch.errors import SignatureError
from repowatch.nginx.render import render
from repowatch.nginx.render import render_purge
from repowatch.parsers import PARSERS
from repowatch.parsers.gentoo import _parse_packages as gentoo
from repowatch.parsers.slackware import _parse_packages as slackware, _check_index
from repowatch.routing import warm_url

FIXTURES = Path(__file__).parent / 'fixtures'
GENTOO = (FIXTURES / 'gentoo/Packages').read_bytes()
SLACKWARE = (FIXTURES / 'slackware/PACKAGES.TXT').read_bytes()
ROOT = 'https://example.org/repo'


def repo(kind, **kw):
    return RepoConfig(kind, kind, ROOT, 'x86_64', **kw)


def test_gentoo_preserves_category_revision_and_build_instances():
    packages = gentoo(GENTOO, ROOT)
    assert [p.key for p in packages] == [
        'app-editors/my-editor-2.0_rc1-r2-build1',
        'app-editors/my-editor-2.0_rc1-r2-build2', 'sys-libs/zlib-1.3.1']
    assert packages[0].content_hash == 'a' * 64
    assert packages[1].content_hash is None
    assert packages[2].filename == 'sys-libs/zlib-1.3.1.tbz2'


@pytest.mark.parametrize('old,new', [
    (b'VERSION: 0', b'VERSION: 1'), (b'PACKAGES: 3', b'PACKAGES: 4'),
    (b'BUILD_ID: 1', b'BUILD_ID: -1'), (b'BUILD_ID: 2', b'BUILD_ID: 1'),
    (b'CPV: sys-libs/zlib-1.3.1', b'CPV: not-a-cpv'),
    (b'SHA256: ' + b'a' * 64, b'SHA256: invalid'),
    (b'ARCH: amd64', b'URI: https://elsewhere.test/packages'),
    (b'ARCH: amd64', b'ARCH: amd64\nARCH: amd64'),
])
def test_gentoo_rejects_incomplete_ambiguous_or_unsupported_indexes(old, new):
    with pytest.raises(ValueError):
        gentoo(GENTOO.replace(old, new), ROOT)


@pytest.mark.parametrize('path', ['../escape.gpkg.tar', '/absolute.tbz2', 'https://evil/a.tbz2',
                                  'a/%2e%2e/x.tbz2', 'a//b.tbz2', 'a/./b.tbz2', 'a/b?x.tbz2'])
def test_gentoo_rejects_unsafe_paths(path):
    raw = GENTOO.replace(b'app-editors/my-editor/my-editor-2.0_rc1-r2-1.gpkg.tar', path.encode())
    with pytest.raises(ValueError):
        gentoo(raw, ROOT)


def test_slackware_names_paths_architecture_and_builds():
    packages = slackware(SLACKWARE, 'x86_64')
    assert [p.name for p in packages] == ['my-editor', 'docs', 'firmware']
    assert packages[0].key == 'my-editor-2.0-x86_64-1_slack15.0'
    assert packages[0].filename == 'patches/packages/my-editor-2.0-x86_64-1_slack15.0.txz'
    assert all(p.content_hash is None for p in packages)


@pytest.mark.parametrize('old,new', [(b'./patches/packages', b'../escape'),
    (b'./patches/packages', b'https://evil/x'), (b'PACKAGE LOCATION:', b'LOCATION:'),
    (b'my-editor-2.0-x86_64-1_slack15.0.txz', b'invalid.txz'),
    (b'PACKAGES.TXT;', b'<html>')])
def test_slackware_rejects_malformed_entries(old, new):
    with pytest.raises(ValueError):
        slackware(SLACKWARE.replace(old, new), 'x86_64')


@pytest.mark.parametrize('kind', ['gentoo', 'slackware'])
def test_fetch_snapshot_head_warm_and_nginx_paths(kind):
    r = repo(kind, component='patches' if kind == 'slackware' else None)
    parser = PARSERS[kind](r)
    index = 'Packages' if kind == 'gentoo' else 'patches/PACKAGES.TXT'
    raw = GENTOO if kind == 'gentoo' else SLACKWARE
    def handler(request):
        assert str(request.url) == ROOT + '/' + index
        if request.method == 'HEAD':
            assert request.headers['If-None-Match'] == 'old'
            return httpx.Response(304)
        return httpx.Response(200, content=raw)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert (await parser.check_index_changed(client, 'old', None)).unchanged
            return await parser.fetch(client)
    snapshot = asyncio.run(run())
    c = Config(Path('/tmp/unused'), 300, 'http://cache', StatusServerConfig(),
               repos=[r], nginx=NginxConfig(enabled=True, enable_purge=True))
    filename = next(iter(snapshot.packages.values()))
    assert warm_url(c, r, filename) == f'http://cache/{kind}/{kind}/{filename}'
    rendered = render(c)
    assert f'location /{kind}/{kind}/' in rendered
    patterns = re.findall(r'location ~ (\S+) \{', rendered)
    assert any(re.search(p, f'/{kind}/{kind}/{index}') for p in patterns)
    assert not any(re.search(p, f'/{kind}/{kind}/{filename}') for p in patterns)
    assert f'/{kind}/{kind}/' in render_purge(c)
    custom = replace(r, url_template='/custom/{id}')
    assert warm_url(c, custom, filename) == f'http://cache/custom/{kind}/{filename}'


@pytest.mark.parametrize('mode', ['ok', 'bad-hash', 'missing', 'duplicate', 'bad-signature'])
def test_slackware_signature_covers_exact_component_index(monkeypatch, mode):
    raw = SLACKWARE
    digest = hashlib.md5(raw).hexdigest().encode()
    checksums = digest + b'  ./patches/PACKAGES.TXT\n'
    if mode == 'bad-hash': checksums = b'0' * 32 + checksums[32:]
    if mode == 'missing': checksums = checksums.replace(b'patches/', b'extra/')
    if mode == 'duplicate': checksums += checksums
    calls = []
    def verify(data, signature, keyring):
        assert data == checksums and signature == b'signature' and keyring == '/keys.gpg'
        calls.append(data)
        if mode == 'bad-signature': raise SignatureError('invalid signature')
    monkeypatch.setattr('repowatch.parsers.slackware.verify_detached', verify)
    def handler(request):
        contents = {'/repo/patches/PACKAGES.TXT': raw, '/repo/CHECKSUMS.md5': checksums,
                    '/repo/CHECKSUMS.md5.asc': b'signature'}
        return httpx.Response(200, content=contents[request.url.path])
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await PARSERS['slackware'](repo('slackware', component='patches',
                verify_signature=True, keyring_path='/keys.gpg')).fetch(client)
    if mode == 'ok': assert asyncio.run(run()).packages
    else:
        with pytest.raises(SignatureError): asyncio.run(run())
    assert len(calls) == 1


def test_config_signature_and_component_validation():
    with pytest.raises(ConfigError, match='Portage'):
        repo('gentoo', verify_signature=True, keyring_path='/keys')
    with pytest.raises(ConfigError, match='keyring_path'):
        repo('slackware', verify_signature=True)
    with pytest.raises(ConfigError, match='component'):
        repo('slackware', component='../patches')


def test_valid_empty_gentoo_and_duplicate_slackware():
    assert gentoo(b'VERSION: 0\nPACKAGES: 0\n\n', ROOT) == []
    with pytest.raises(ValueError, match='duplicate'):
        slackware(SLACKWARE + SLACKWARE, 'x86_64')


@pytest.mark.parametrize('kind', ['gentoo', 'slackware'])
def test_watcher_records_catalog_and_warms_via_cache(tmp_path, monkeypatch, kind):
    import repowatch.operations.check as watcher
    from repowatch.runtime.context import ServiceState
    r = repo(kind)
    c = Config(tmp_path / 'state.db', 300, 'http://cache', StatusServerConfig(), repos=[r])
    store = ServiceState(c.state_db)
    paths = []
    raw = GENTOO if kind == 'gentoo' else SLACKWARE
    def handler(request):
        if request.url.host == 'cache':
            paths.append(request.url.path)
            return httpx.Response(200, content=b'package payload')
        assert request.url.host == 'example.org'
        return httpx.Response(200, content=raw, headers={'ETag': 'v1'})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(handler), **kw))
    asyncio.run(operations_check.check_repo(c, r, store))
    packages = store.repositories.get_packages(r.id)
    assert len(packages) == 3
    assert sorted(paths) == sorted(f'/{kind}/{kind}/{p}' for p in packages.values())
    assert all(p['status'] == 'ok' for p in store.cache.get_warmed_packages(r.id))
    asyncio.run(operations_check.check_repo(c, r, store))
    assert len(paths) == 3


def test_slackware_components_can_share_client_prefix():
    r = repo('slackware', url_template='/slackware/15.0')
    patches = replace(r, id='patches', component='patches')
    c = Config(Path('/tmp/unused'), 300, 'http://cache', StatusServerConfig(),
               repos=[r, patches], nginx=NginxConfig(enabled=True))
    text = render(c)
    assert text.count('location /slackware/15.0/') == 1
    assert warm_url(c, r, 'patches/packages/x.txz') == warm_url(c, patches, 'patches/packages/x.txz')
