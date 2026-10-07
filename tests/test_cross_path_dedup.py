"""Different index paths may share bytes, but never override conflicting URL owners."""
import asyncio
from dataclasses import replace
import re
from unittest.mock import AsyncMock

import httpx
import pytest

from repowatch.cache import probe, purge
from repowatch.cache.dedup import accepted_pairs, dedup_keys
from repowatch.nginx.render import render
from repowatch.operations.cleanup import purge_items
from repowatch.operations.dedup_cleanup import make_plan
from repowatch.reporting import completeness
from repowatch.routing import CacheKeyBuilder, package_path
from test_completeness import setup, snapshot
from test_storage_usage import file, report


def catalogs(tmp_path):
    repos = [dict(id='a', type='apt', arch='amd64', upstream='http://deb.test/debian',
                  distribution='trixie', component='contrib'),
             dict(id='b', type='apt', arch='amd64', upstream='http://security.test/debian-security',
                  distribution='trixie-security', component='contrib')]
    path, config, store = setup(tmp_path, repos, dedup=True)
    for repo, filename in [('a', 'pool/contrib/z/zfs.deb'), ('b', 'pool/updates/contrib/z/zfs.deb')]:
        snapshot(config, store, repo, {'zfs': filename}, {'zfs': 'a' * 64})
    return path, config, store


def test_cross_path_routes_accounting_cleanup_and_manual_purge(tmp_path, monkeypatch):
    _, config, store = catalogs(tmp_path)
    rows = store.cache.find_duplicate_files()
    assert rows == [('b', 'a', 'pool/updates/contrib/z/zfs.deb', 'pool/contrib/z/zfs.deb')]
    source, target = config.repos[1], config.repos[0]
    filename, canonical = rows[0][2:]
    pairs = accepted_pairs(config, store.cache, rows)
    assert pairs == [(package_path(source, filename), package_path(target, canonical))]
    builder = CacheKeyBuilder(config)
    canonical_key = builder.for_repo(target)(canonical)
    keys = dedup_keys(config, rows, builder, pairs=pairs)
    assert keys[package_path(source, filename).lower()] == canonical_key
    inventory = probe.Inventory([file(config, 'a', canonical, 100)])
    usage = report(config, store, inventory)
    assert [r['physical_bytes'] for r in usage['repositories']] == [100, 0]
    assert [r['available_bytes'] for r in usage['repositories']] == [100, 100]
    with store.queries.catalog_snapshot(['a', 'b']) as data:
        assert [r['cached_packages'] for r in completeness.summarize(config, data, inventory, builder, keys)] == [1, 1]
    plan = make_plan(config, store)
    assert builder.for_repo(source)(filename) in plan.candidates
    assert plan.candidates[builder.for_repo(source)(filename)]['canonical_key'] == canonical_key
    assert canonical_key not in plan.candidates
    remove = AsyncMock(return_value={'zfs': 'purged'})
    monkeypatch.setattr(probe, 'purge_selected_raw', remove)
    asyncio.run(purge_items(config, source, {'zfs': filename}, store))
    assert remove.call_args.kwargs['canonical_keys'] == {filename: canonical_key}


@pytest.mark.parametrize('conflict', [None, 'b' * 64])
@pytest.mark.parametrize('owner', ['a', 'b'])
def test_unrelated_shared_route_owner_blocks_both_ends(tmp_path, conflict, owner):
    _, config, store = catalogs(tmp_path)
    repo = replace(config.repo_by_id(owner), id='other', distribution='other-suite')
    config = replace(config, repos=[*config.repos, repo])
    filename = 'pool/contrib/z/zfs.deb' if owner == 'a' else 'pool/updates/contrib/z/zfs.deb'
    snapshot(config, store, 'other', {'other': filename}, {'other': conflict} if conflict else {})
    assert not accepted_pairs(config, store.cache, store.cache.find_duplicate_files())


@pytest.mark.parametrize('extension', ['deb', 'udeb', 'ddeb'])
def test_apt_payload_under_dists_uses_package_location(tmp_path, extension):
    _, config, _ = catalogs(tmp_path)
    text = render(config)
    patterns = re.findall(r'    location ~ (.+) \{', text)
    payload = '/debian/dists/trixie/contrib/binary-amd64/package.' + extension
    assert not any(re.search(pattern, payload) for pattern in patterns)
    for name in ('InRelease', 'Release', 'Release.gpg', 'contrib/binary-amd64/Packages.xz',
                 'contrib/binary-amd64/by-hash/SHA256/abcdef', 'contrib/i18n/Translation-en.xz'):
        assert any(re.search(pattern, '/debian/dists/trixie/' + name) for pattern in patterns)
    package_block = text.split('    location /debian/ {', 1)[1].split('\n    }', 1)[0]
    assert 'proxy_cache_key "$scheme$proxy_host$uri";' in package_block


def test_empty_timeout_is_identifiable_in_raw_purge():
    async def run():
        def fail(request):
            raise httpx.ReadTimeout('')
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            return await probe.purge_raw(client, 'http://localhost', 'key')
    assert asyncio.run(run()) == 'error (ReadTimeout)'


def test_empty_timeout_is_identifiable_in_automatic_purge(tmp_path, monkeypatch, caplog):
    _, config, _ = catalogs(tmp_path)
    config = replace(config, nginx=replace(config.nginx, enable_purge=True))
    original = httpx.AsyncClient
    def fail(request):
        raise httpx.ReadTimeout('')
    monkeypatch.setattr(purge.httpx, 'AsyncClient',
                        lambda **kwargs: original(transport=httpx.MockTransport(fail)))
    asyncio.run(purge.purge_removed(config, config.repos[0], {'zfs': 'zfs.deb'}))
    assert 'purge failed (ReadTimeout) for zfs.deb' in caplog.text


@pytest.mark.parametrize('canonical_exists', [False, True])
def test_cross_path_cleanup_requires_existing_canonical(tmp_path, monkeypatch, canonical_exists):
    from repowatch.operations.dedup_cleanup import remove_copies
    _, config, store = catalogs(tmp_path)
    source = CacheKeyBuilder(config).for_repo(config.repos[1])('pool/updates/contrib/z/zfs.deb')
    plan = make_plan(config, store, selected={source})
    inspect = AsyncMock(side_effect=[probe.ProbeResult(True, 100, generation=plan.generation),
        probe.ProbeResult(canonical_exists, 100, generation=plan.generation)])
    remove = AsyncMock(return_value='purged')
    monkeypatch.setattr(probe, 'probe', inspect)
    monkeypatch.setattr(probe, 'purge_raw', remove)
    result = asyncio.run(remove_copies(plan, store, [source]))
    assert result['results'][source] == ('purged' if canonical_exists else 'skipped')
    assert remove.await_count == int(canonical_exists)
    assert inspect.await_args_list[1].args[2] == plan.candidates[source]['canonical_key']
