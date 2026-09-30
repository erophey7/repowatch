"""Physical storage conservation and shared availability across repositories/groups."""

from dataclasses import replace

import pytest

from repowatch.cache.probe import Inventory, ScanEntry
from repowatch.reporting import completeness
from repowatch.reporting.storage_usage import storage_usage
from repowatch.routing import CacheKeyBuilder
from test_completeness import setup, snapshot


def shared(tmp_path, *, groups=('shared', 'shared'), dedup=True):
    repos = [dict(id=name, type='dnf', upstream=f'https://{name}.example/repo',
                  arch='x86_64', group=group) for name, group in zip(('a', 'b'), groups)]
    path, config, store = setup(tmp_path, repos, dedup=dedup)
    for repo in config.repos:
        snapshot(config, store, repo.id, {'pkg': 'pkg.rpm'}, {'pkg': 'a' * 64})
    return path, config, store


def report(config, store, entries):
    builder = CacheKeyBuilder(config)
    catalogs = store.queries.completeness_catalogs([repo.id for repo in config.repos], include_warmed=True)
    return storage_usage(config, catalogs, entries, builder, completeness._dedup_keys(config, store, builder))


def file(config, repo, name, size, *, dedup=None):
    key = CacheKeyBuilder(config).for_repo(config.repo_by_id(repo))(name, dedup=dedup)
    return ScanEntry('0/00', repo + name, key, size)


def conserved(usage):
    assert sum(row['physical_bytes'] for row in usage['repositories']) == usage['attributed_bytes']
    assert sum(row['physical_bytes'] for row in usage['groups']) == usage['attributed_bytes']
    assert usage['attributed_bytes'] + usage['unattributed_bytes'] == usage['total_bytes']
    assert sum(row['physical_files'] for row in usage['repositories']) + usage['unattributed_files'] == usage['file_count']


def test_shared_copy_counted_once_physically_and_once_per_group(tmp_path):
    _, config, store = shared(tmp_path)
    usage = report(config, store, Inventory([file(config, 'a', 'pkg.rpm', 100)]))
    assert [r['physical_bytes'] for r in usage['repositories']] == [100, 0]
    assert [r['available_bytes'] for r in usage['repositories']] == [100, 100]
    assert usage['groups'][0]['available_bytes'] == 100
    assert usage['groups'][0]['physical_bytes'] == 100
    assert usage['inventory_complete']
    conserved(usage)


def test_distinct_groups_overlap_only_in_available_bytes(tmp_path):
    _, config, store = shared(tmp_path, groups=('one', 'two'))
    usage = report(config, store, Inventory([file(config, 'a', 'pkg.rpm', 123)]))
    assert [g['available_bytes'] for g in usage['groups']] == [123, 123]
    assert sum(g['physical_bytes'] for g in usage['groups']) == 123
    conserved(usage)


def test_unused_duplicate_and_obsolete_warm_copy_still_take_physical_space(tmp_path):
    _, config, store = shared(tmp_path)
    store.cache.record_warmed_package('b', 'old', 'old.rpm', True, 200)
    usage = report(config, store, Inventory([
        file(config, 'a', 'pkg.rpm', 100), file(config, 'b', 'pkg.rpm', 100),
        file(config, 'b', 'old.rpm', 50, dedup=False),
        ScanEntry('0/00', 'unknown', 'unrelated-key', 25),
    ]))
    assert [r['physical_bytes'] for r in usage['repositories']] == [100, 150]
    assert [r['available_bytes'] for r in usage['repositories']] == [100, 100]
    assert usage['unattributed_bytes'] == 25
    assert usage['total_bytes'] == 275
    conserved(usage)


def test_aliases_and_repeated_closure_files_are_not_double_counted(tmp_path):
    _, config, store = shared(tmp_path, dedup=False)
    snapshot(config, store, 'a', {'one': 'pkg.rpm', 'alias': 'pkg.rpm'})
    usage = report(config, store, Inventory([file(config, 'a', 'pkg.rpm', 100)]))
    assert usage['repositories'][0]['available_files'] == 1
    assert usage['repositories'][0]['available_bytes'] == 100
    assert usage['repositories'][1]['available_bytes'] == 0
    conserved(usage)


def test_partial_inventory_retains_known_bytes_without_guessing_sizes(tmp_path):
    _, config, store = shared(tmp_path)
    usage = report(config, store, Inventory([
        file(config, 'a', 'pkg.rpm', None),
        ScanEntry('1/00', 'unreadable', None, 40, error='read failed'),
    ], failed_leaves=1))
    assert usage['total_bytes'] == usage['unattributed_bytes'] == 40
    assert usage['unreadable_sizes'] == usage['unreadable_keys'] == usage['failed_leaves'] == 1
    assert not usage['inventory_complete']
    assert usage['repositories'][0]['physical_files'] == 1
    conserved(usage)


def test_no_snapshot_or_changed_source_leaves_bytes_unattributed(tmp_path):
    _, config, store = setup(tmp_path)
    entry = file(config, 'r', 'one.apk', 80)
    usage = report(config, store, Inventory([entry]))
    assert usage['repositories'][0]['reason'] == 'no_snapshot'
    assert usage['groups'][0]['unknown_repositories'] == 1
    snapshot(config, store)
    changed = replace(config, repos=[replace(config.repos[0], upstream='https://other.example/alpine')])
    usage = report(changed, store, Inventory([entry]))
    assert usage['repositories'][0]['reason'] == 'source_changed'
    assert usage['unattributed_bytes'] == 80
    conserved(usage)


def test_shared_direct_route_has_stable_owner_independent_of_config_order(tmp_path):
    repos = [dict(id=name, type='apt', upstream='https://example.org/debian', arch='amd64',
                  distribution=suite, component='main', group=None)
             for name, suite in [('b', 'testing'), ('a', 'stable')]]
    _, config, store = setup(tmp_path, repos)
    for repo in config.repos:
        snapshot(config, store, repo.id, {'one': 'pool/main/pkg.deb'})
    usage = report(config, store, Inventory([file(config, 'a', 'pool/main/pkg.deb', 100)]))
    assert {r['repo_id']: r['physical_bytes'] for r in usage['repositories']} == {'a': 100, 'b': 0}
    assert usage['groups'][0]['group'] is None
    assert usage['groups'][0]['available_bytes'] == 100
    conserved(usage)


def test_nix_shared_artifacts_count_once_without_requiring_complete_closure(tmp_path):
    _, config, store = setup(tmp_path)
    repo = replace(config.repos[0], type='nix', arch='x86_64-linux', nix_source='https://example.org/source.tar.gz')
    config = replace(config, repos=[repo])
    snapshot(config, store, packages={'root1': 'one.narinfo', 'root2': 'two.narinfo'})
    for root in ['root1', 'root2']:
        store.cache.record_nix_artifacts('r', root, [{'filename': 'nar/shared.xz'}])
    usage = report(config, store, Inventory([file(config, 'r', 'nar/shared.xz', 100)]))
    assert usage['repositories'][0]['available_files'] == 1
    assert usage['repositories'][0]['available_bytes'] == 100
    conserved(usage)


@pytest.mark.parametrize('empty', [False, True])
def test_usage_opt_in_uses_one_inventory_even_with_no_repositories(tmp_path, monkeypatch, empty):
    path, config, store = setup(tmp_path, [] if empty else None)
    calls = []
    async def scan(*args):
        calls.append(1)
        return Inventory([ScanEntry('0/00', 'file', 'unattributed', 10)])
    monkeypatch.setattr(completeness, '_inventory', scan)
    status, payload = completeness.completeness_payload(path, store, include_usage=True)
    assert status == 200 and calls == [1]
    assert payload['storage_usage']['total_bytes'] == 10
    assert payload['storage_usage']['unattributed_bytes'] == 10
