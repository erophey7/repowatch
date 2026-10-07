"""Physical package coverage, independent of warm bookkeeping and status polling."""

import asyncio
from dataclasses import replace

import httpx
import pytest
import yaml

from repowatch.cache.probe import Inventory, ScanEntry
from repowatch.config.load import load_config
from repowatch.models import RepoSnapshot
from repowatch.reporting import completeness
from repowatch.routing import CacheKeyBuilder
from repowatch.runtime.context import ServiceState


def setup(tmp_path, repos=None, dedup=False):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(dict(
        state_db=str(tmp_path / 'state.db'), cache_base_url='http://127.0.0.1:8080',
        nginx=dict(enable_cache_probe=True, enable_dedup=dedup),
        repos=repos if repos is not None else [dict(id='r', type='apk', arch='x86_64',
                                                   upstream='https://example.org/alpine')],
    )))
    config = load_config(path)
    return path, config, ServiceState(config.state_db)


def snapshot(config, store, repo_id='r', packages=None, hashes=None):
    store.repositories.record_snapshot(
        RepoSnapshot(repo_id, packages if packages is not None else {'one': 'one.apk', 'two': 'two.apk'},
                     content_hashes=hashes or {}),
        source_identity=config.repo_by_id(repo_id).catalog_identity())


def inventory(monkeypatch, config, files=(), *, failed=0, extra=()):
    builder = CacheKeyBuilder(config)
    entries = [ScanEntry('0/00', 'file', builder.for_repo(config.repo_by_id(repo))(name))
               for repo, name in files]
    async def scan(*args):
        return Inventory(entries + list(extra), failed)
    monkeypatch.setattr(completeness, '_inventory', scan)


def test_counts_actual_current_packages_not_warmed_rows(tmp_path, monkeypatch):
    path, config, store = setup(tmp_path)
    snapshot(config, store)
    store.cache.record_warmed_package('r', 'two', 'two.apk', True, 200)
    store.cache.ban_package('r', 'one')
    inventory(monkeypatch, config, [('r', 'one.apk'), ('r', 'obsolete.apk')])
    status, result = completeness.completeness_payload(path, store)
    row = result['items'][0]
    assert status == 200
    assert (row['total_packages'], row['cached_packages'], row['missing_packages'], row['percent']) == (2, 1, 1, 50)
    assert row['state'] == 'partial'
    assert result['source'] == 'cache_inventory'


@pytest.mark.parametrize('failed,extra', [(1, []), (0, [ScanEntry('a/bc', 'bad', None, error='unreadable')])])
def test_partial_scan_reports_unknown_not_false_missing(tmp_path, monkeypatch, failed, extra):
    path, config, store = setup(tmp_path)
    snapshot(config, store)
    inventory(monkeypatch, config, [('r', 'one.apk')], failed=failed, extra=extra)
    status, result = completeness.completeness_payload(path, store)
    row = result['items'][0]
    assert status == 200
    assert (row['cached_packages'], row['unknown_packages'], row['missing_packages']) == (1, 1, 0)
    assert row['percent'] is None and row['state'] == 'unknown'


def test_empty_and_never_checked_are_distinct(tmp_path, monkeypatch):
    path, config, store = setup(tmp_path)
    inventory(monkeypatch, config)
    assert completeness.completeness_payload(path, store)[1]['items'][0]['reason'] == 'no_snapshot'
    snapshot(config, store, packages={})
    row = completeness.completeness_payload(path, store)[1]['items'][0]
    assert row['state'] == 'empty' and row['total_packages'] == 0 and row['percent'] is None


def test_source_change_and_pending_replacement_cannot_report_complete(tmp_path, monkeypatch):
    path, config, store = setup(tmp_path)
    snapshot(config, store, packages={'one': 'one.apk'}, hashes={'one': 'a' * 64})
    snapshot(config, store, packages={'one': 'one.apk'}, hashes={'one': 'b' * 64})
    inventory(monkeypatch, config, [('r', 'one.apk')])
    row = completeness.completeness_payload(path, store)[1]['items'][0]
    assert row['unknown_packages'] == 1 and row['percent'] is None
    raw = yaml.safe_load(path.read_text())
    raw['repos'][0]['upstream'] = 'https://other.example/alpine'
    path.write_text(yaml.safe_dump(raw))
    assert completeness.completeness_payload(path, store)[1]['items'][0]['reason'] == 'source_changed'


def test_dedup_only_counts_the_routed_canonical_copy(tmp_path, monkeypatch):
    repos = [dict(id=n, type='dnf', upstream=f'https://{n}.example/repo', arch='x86_64') for n in ['a', 'b']]
    path, config, store = setup(tmp_path, repos, dedup=True)
    for repo in repos:
        snapshot(config, store, repo['id'], {'pkg': 'pkg.rpm'}, {'pkg': 'a' * 64})
    inventory(monkeypatch, config, [('b', 'pkg.rpm')])
    assert all(row['cached_packages'] == 0 for row in completeness.completeness_payload(path, store)[1]['items'])
    inventory(monkeypatch, config, [('a', 'pkg.rpm')])
    assert all(row['percent'] == 100 for row in completeness.completeness_payload(path, store)[1]['items'])


def test_old_key_version_does_not_count(tmp_path, monkeypatch):
    path, config, store = setup(tmp_path)
    snapshot(config, store)
    old = replace(config, nginx=replace(config.nginx, cache_key_version='obsolete'))
    inventory(monkeypatch, old, [('r', 'one.apk'), ('r', 'two.apk')])
    assert completeness.completeness_payload(path, store)[1]['items'][0]['percent'] == 0


@pytest.mark.parametrize('change', ['catalog', 'config'])
def test_changing_inputs_reject_stale_measurement_and_release_lock(tmp_path, monkeypatch, change):
    path, config, store = setup(tmp_path)
    snapshot(config, store)
    async def scan(*args):
        if change == 'catalog':
            snapshot(config, store, packages={'new': 'new.apk'})
        else:
            raw = yaml.safe_load(path.read_text())
            raw['repos'] = []
            path.write_text(yaml.safe_dump(raw))
        return Inventory([])
    monkeypatch.setattr(completeness, '_inventory', scan)
    assert completeness.completeness_payload(path, store)[0] == 409
    assert not store.completeness_lock.locked()


def test_disabled_unreachable_and_overlapping_scan(tmp_path, monkeypatch):
    path, config, store = setup(tmp_path)
    disabled = replace(config, nginx=replace(config.nginx, enable_cache_probe=False))
    assert completeness.completeness_payload(path, store, current=disabled)[0] == 409
    with store.completeness_lock:
        assert completeness.completeness_payload(path, store)[0] == 409
    async def scan(*args):
        raise httpx.ConnectError('unreachable')
    monkeypatch.setattr(completeness, '_inventory', scan)
    assert completeness.completeness_payload(path, store)[0] == 502
    assert not store.completeness_lock.locked()


def test_no_repositories_skips_scan(tmp_path, monkeypatch):
    path, config, store = setup(tmp_path, [])
    async def scan(*args):
        pytest.fail('empty configuration should not scan')
    monkeypatch.setattr(completeness, '_inventory', scan)
    status, result = completeness.completeness_payload(path, store)
    assert status == 200 and result['items'] == []


def test_nix_requires_known_closure_not_just_root_metadata(tmp_path, monkeypatch):
    path, config, store = setup(tmp_path)
    # Construct the reporting input directly: parser/discovery validity is tested
    # separately; reporting must never start Nix CLI or discover from upstream.
    repo = replace(config.repos[0], type='nix', arch='x86_64-linux',
                   nix_source='https://example.org/source.tar.gz')
    config = replace(config, repos=[repo])
    catalog = {'r': dict(last_check='now', source_identity=repo.catalog_identity(),
                        packages=[('root', 'root.narinfo')], pending=set(), artifacts={})}
    builder = CacheKeyBuilder(config)
    entries = [ScanEntry('0/00', 'f', builder.for_repo(repo)(name))
               for name in ['root.narinfo', 'nar/body.xz']]
    def row():
        return completeness.summarize(config, catalog, Inventory(entries), builder, {})[0]
    assert row()['unknown_packages'] == 1
    catalog['r']['artifacts']['root'] = {'root.narinfo', 'nar/body.xz'}
    assert row()['percent'] == 100
    entries.pop()
    assert row()['unknown_packages'] == 1 and row()['percent'] is None


def test_inventory_uses_only_scan_endpoint_and_does_not_download_packages(tmp_path, monkeypatch):
    from repowatch.cache import probe
    path, config, store = setup(tmp_path)
    snapshot(config, store, packages={'one': 'one.apk', 'alias': 'one.apk'})
    key = CacheKeyBuilder(config).for_repo(config.repos[0])('one.apk')
    requests = []
    def transport(request):
        requests.append(request)
        assert request.url.path == '/cache-scan'
        return httpx.Response(200, json=[{'file': 'abc', 'key': key, 'size': 100}])
    client = httpx.AsyncClient
    monkeypatch.setattr(completeness.httpx, 'AsyncClient',
                        lambda: client(transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(probe, 'LEAF_DIRS', ['0/00'])
    status, payload = completeness.completeness_payload(path, store)
    assert status == 200 and payload['items'][0]['cached_packages'] == 2
    assert len(requests) == 2  # Reachability check and full (one-leaf fixture) scan.
    assert store.cache.get_warmed_packages('r') == []


def test_disabled_dedup_does_not_count_another_repository_copy(tmp_path, monkeypatch):
    repos = [dict(id=n, type='dnf', upstream=f'https://{n}.example/repo', arch='x86_64') for n in ['a', 'b']]
    path, config, store = setup(tmp_path, repos)
    for repo in repos:
        snapshot(config, store, repo['id'], {'pkg': 'pkg.rpm'}, {'pkg': 'a' * 64})
    inventory(monkeypatch, config, [('a', 'pkg.rpm')])
    rows = completeness.completeness_payload(path, store)[1]['items']
    assert [row['percent'] for row in rows] == [100, 0]


def test_pending_canonical_replacement_also_marks_dependants_unknown(tmp_path, monkeypatch):
    repos = [dict(id=n, type='dnf', upstream=f'https://{n}.example/repo', arch='x86_64') for n in ['a', 'b']]
    path, config, store = setup(tmp_path, repos, dedup=True)
    snapshot(config, store, 'a', {'pkg': 'pkg.rpm'}, {'pkg': 'a' * 64})
    for repo in repos:
        snapshot(config, store, repo['id'], {'pkg': 'pkg.rpm'}, {'pkg': 'b' * 64})
    inventory(monkeypatch, config, [('a', 'pkg.rpm')])
    rows = completeness.completeness_payload(path, store)[1]['items']
    assert all(row['unknown_packages'] == 1 and row['percent'] is None for row in rows)


def test_catalog_snapshot_is_repeatable_and_isolated_from_concurrent_updates(tmp_path):
    path, config, store = setup(tmp_path)
    snapshot(config, store, packages={'one': 'one.apk'})
    with store.queries.catalog_snapshot(['r'], include_warmed=True) as catalogs:
        rows = catalogs['r']['packages']
        assert len(rows) == 1
        assert list(rows) == [('one', 'one.apk')]
        snapshot(config, store, packages={'two': 'two.apk', 'three': 'three.apk'})
        assert len(rows) == 1
        assert list(rows) == [('one', 'one.apk')]
    with store.queries.catalog_snapshot(['r']) as catalogs:
        assert len(catalogs['r']['packages']) == 2


def test_inventory_precedes_catalog_transaction_and_materialization_is_unused(tmp_path, monkeypatch):
    from contextlib import contextmanager
    path, config, store = setup(tmp_path)
    snapshot(config, store)
    scanned = False
    active = False
    original = store.queries.catalog_snapshot
    @contextmanager
    def reader(*args, **kwargs):
        nonlocal active
        assert scanned
        active = True
        try:
            with original(*args, **kwargs) as catalogs:
                yield catalogs
        finally:
            active = False
    async def scan(*args):
        nonlocal scanned
        assert not active
        scanned = True
        return Inventory([])
    monkeypatch.setattr(completeness, '_inventory', scan)
    monkeypatch.setattr(store.queries, 'catalog_snapshot', reader)
    monkeypatch.setattr(store.queries, 'completeness_catalogs',
                        lambda *a, **kw: pytest.fail('must not materialize catalog'))
    status, result = completeness.completeness_payload(path, store, include_usage=True)
    assert status == 200 and result['items'][0]['missing_packages'] == 2
    assert result['storage_usage']['total_bytes'] == 0 and not active


def test_change_during_accounting_rejects_result_and_releases_scan_lock(tmp_path, monkeypatch):
    path, config, store = setup(tmp_path)
    snapshot(config, store)
    inventory(monkeypatch, config)
    original = completeness.storage_usage
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        snapshot(config, store, packages={'new': 'new.apk'})
        return result
    monkeypatch.setattr(completeness, 'storage_usage', changed)
    assert completeness.completeness_payload(path, store, include_usage=True)[0] == 409
    assert not store.completeness_lock.locked()


def test_completeness_and_dedup_share_admission(tmp_path):
    from repowatch.web.dedup_cleanup import dedup_cleanup_payload
    path, config, store = setup(tmp_path)
    config = replace(config, nginx=replace(config.nginx, enabled=True,
                                          enable_dedup=True, enable_purge=True))
    with store.dedup_cleanup_lock:
        assert completeness.completeness_payload(path, store, current=config)[0] == 409
    with store.completeness_lock:
        assert dedup_cleanup_payload(path, store, current=config)[0] == 409
