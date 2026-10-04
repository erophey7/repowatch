"""Obsolete-copy cleanup preserves current routes and defers on stale evidence."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from repowatch.cache import probe
from repowatch.nginx import render
from repowatch.operations import dedup_cleanup as cleanup
from repowatch.web import dedup_cleanup as api
from test_storage_usage import shared
from test_completeness import snapshot


@pytest.fixture
def state(tmp_path):
    path, config, store = shared(tmp_path)
    data = yaml.safe_load(path.read_text())
    data['nginx'].update(enabled=True, enable_purge=True)
    path.write_text(yaml.safe_dump(data))
    config = cleanup.load_config(path)
    yield path, config, store


def mocks(monkeypatch, plan, *, exists=True, generation=None):
    inspect = AsyncMock(return_value=probe.ProbeResult(exists, 100, generation=generation or plan.generation))
    purge = AsyncMock(return_value='purged')
    monkeypatch.setattr(probe, 'probe', inspect)
    monkeypatch.setattr(probe, 'purge_raw', purge)
    return inspect, purge


def test_only_obsolete_keys_are_removed_without_touching_warmed(state, monkeypatch):
    path, config, store = state
    store.cache.record_warmed_package('b', 'pkg', 'pkg.rpm', True, 200)
    plan = cleanup.make_plan(config, store)
    assert plan.candidates
    assert all(row['key'] != row['canonical_key'] for row in plan.candidates.values())
    before = store.cache.get_warmed_packages('b')
    _, purge = mocks(monkeypatch, plan)
    result = asyncio.run(cleanup.remove_copies(plan, store, list(plan.candidates), config_path=path))
    assert result['observed_removed_bytes'] == len(plan.candidates) * 100
    assert purge.await_count == len(plan.candidates)
    assert store.cache.get_warmed_packages('b') == before
    for call in purge.await_args_list:
        assert call.args[2] in plan.candidates
        assert call.kwargs == {'generation': plan.generation}


@pytest.mark.parametrize('exists,generation', [(False, None), (True, 'old')])
def test_missing_copy_or_unapplied_generation_never_purges(state, monkeypatch, exists, generation):
    _, config, store = state
    plan = cleanup.make_plan(config, store)
    _, purge = mocks(monkeypatch, plan, exists=exists, generation=generation)
    result = asyncio.run(cleanup.remove_copies(plan, store, list(plan.candidates)))
    purge.assert_not_awaited()
    assert bool(result['stopped']) == bool(generation)


def test_missing_canonical_never_purges(state, monkeypatch):
    _, config, store = state
    plan = cleanup.make_plan(config, store)
    inspect, purge = mocks(monkeypatch, plan)
    inspect.side_effect = [probe.ProbeResult(True, 100, generation=plan.generation),
                           probe.ProbeResult(False, generation=plan.generation)]
    result = asyncio.run(cleanup.remove_copies(plan, store, list(plan.candidates)[:1]))
    assert list(result['results'].values()) == ['skipped']
    purge.assert_not_awaited()


@pytest.mark.parametrize('change', ['catalog', 'config', 'invalid'])
def test_changes_during_probe_stop_before_deletion(state, monkeypatch, change):
    path, config, store = state
    plan = cleanup.make_plan(config, store)
    inspect, purge = mocks(monkeypatch, plan)
    async def inspect_changed(*args):
        if change == 'catalog':
            snapshot(config, store, 'b', {'pkg': 'different.rpm'})
        else:
            path.write_text('[' if change == 'invalid' else path.read_text() + '\ncheck_interval: 99\n')
        return probe.ProbeResult(True, 100, generation=plan.generation)
    inspect.side_effect = inspect_changed
    result = asyncio.run(cleanup.remove_copies(plan, store, list(plan.candidates), config_path=path))
    assert result['stopped']
    purge.assert_not_awaited()


def test_pending_replacement_blocks_canonical_and_duplicates(state):
    _, config, store = state
    snapshot(config, store, 'a', {'pkg': 'pkg.rpm'}, {'pkg': 'b' * 64})
    snapshot(config, store, 'b', {'pkg': 'pkg.rpm'}, {'pkg': 'b' * 64})
    assert not cleanup.make_plan(config, store).candidates


def test_unknown_requested_key_is_not_trusted(state, monkeypatch):
    _, config, store = state
    plan = cleanup.make_plan(config, store)
    _, purge = mocks(monkeypatch, plan)
    result = asyncio.run(cleanup.remove_copies(plan, store, ['arbitrary-client-key']))
    assert result['results'] == {'arbitrary-client-key': 'skipped'}
    purge.assert_not_awaited()


def test_partial_result_survives_nginx_generation_change(state, monkeypatch):
    _, config, store = state
    plan = cleanup.make_plan(config, store)
    assert len(plan.candidates) >= 2
    _, purge = mocks(monkeypatch, plan)
    purge.side_effect = ['purged', 'error (HTTP 409)']
    result = asyncio.run(cleanup.remove_copies(plan, store, list(plan.candidates)))
    assert result['observed_removed_bytes'] == 100
    assert result['stopped'] == 'nginx generation changed'
    assert purge.await_count == 2


def test_automatic_pass_rotates_and_does_not_scan_inventory(state, monkeypatch):
    _, config, store = state
    plan = cleanup.make_plan(config, store)
    _, purge = mocks(monkeypatch, plan)
    inventory = AsyncMock(side_effect=AssertionError('automatic cleanup must not scan'))
    monkeypatch.setattr(probe, 'full_inventory', inventory)
    monkeypatch.setattr(cleanup, 'AUTOMATIC_BATCH', 1)
    asyncio.run(cleanup.automatic_cleanup(config, store))
    asyncio.run(cleanup.automatic_cleanup(config, store))
    assert len({call.args[2] for call in purge.await_args_list}) == 2
    inventory.assert_not_awaited()
    assert not store.dedup_cleanup_lock.locked()


def test_disabled_or_busy_automatic_cleanup_does_nothing(state, monkeypatch):
    _, config, store = state
    monkeypatch.setattr(cleanup, 'make_plan', lambda *args: pytest.fail('should not run'))
    asyncio.run(cleanup.automatic_cleanup(replace(config, nginx=replace(config.nginx, enable_purge=False)), store))
    store.dedup_cleanup_lock.acquire()
    try:
        asyncio.run(cleanup.automatic_cleanup(config, store))
    finally:
        store.dedup_cleanup_lock.release()


def test_preview_uses_inventory_and_rejects_partial_results(state, monkeypatch):
    path, config, store = state
    plan = cleanup.make_plan(config, store)
    mocks(monkeypatch, plan)
    row = next(iter(plan.candidates.values()))
    entries = [probe.ScanEntry('0/00', str(i), key, 123)
               for i, key in enumerate([row['key'], row['canonical_key']])]
    scan = AsyncMock(return_value=probe.Inventory(entries))
    monkeypatch.setattr(probe, 'full_inventory', scan)
    status, data = api.dedup_cleanup_payload(path, store)
    assert status == 200 and data['total_candidates'] == 1 and data['total_bytes'] == 123
    scan.return_value = probe.Inventory(entries, failed_leaves=1)
    assert api.dedup_cleanup_payload(path, store)[0] == 409
    assert not store.dedup_cleanup_lock.locked()


def test_post_requires_admin_and_checks_generation(state, monkeypatch):
    path, config, store = state
    assert api.dedup_cleanup_payload(path, store, body={'keys': ['x'], 'generation': 'old'})[0] == 501
    monkeypatch.setattr(api, 'load_admin_config', lambda *args: (config, None))
    assert api.dedup_cleanup_payload(path, store, body=[])[0] == 400
    assert api.dedup_cleanup_payload(path, store, body={'keys': [], 'generation': 'old'})[0] == 400
    assert api.dedup_cleanup_payload(path, store, body={'keys': ['x'], 'generation': 'old'})[0] == 409


def test_generated_guard_and_probe_agree_and_track_routes(state):
    _, config, store = state
    pairs = render.resolve_dedup_pairs(config, store.cache.find_duplicate_files())
    generation = render.dedup_generation(config, pairs)
    assert generation in render.render_probe_js(config, dedup_pairs=pairs)
    assert f'"~^{generation}$" 1;' in render.render(config, dedup_pairs=pairs)
    assert 'if ($repowatch_cleanup_generation_valid = 0) { return 409; }' in render.render_purge(config)
    assert generation != render.dedup_generation(config, [])
    assert generation != render.dedup_generation(replace(config, nginx=replace(config.nginx, cache_key_version='other')), pairs)


def test_conditional_purge_keeps_literal_key_and_passes_generation():
    seen = []
    async def run():
        def respond(request):
            seen.append(str(request.url))
            return httpx.Response(409)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await probe.purge_raw(client, 'http://localhost', 'v1:httpserver/pool/pkg', generation='a' * 64)
    assert asyncio.run(run()) == 'error (HTTP 409)'
    assert seen == ['http://localhost/purge-raw?key=v1:httpserver/pool/pkg&cleanup_generation=' + 'a' * 64]


def test_streamed_windows_cover_all_candidates_and_preserve_targets(state, monkeypatch):
    _, config, store = state
    packages = {f'p{i}': f'p{i}.rpm' for i in range(31)}
    hashes = {key: f'{i:064x}' for i, key in enumerate(packages)}
    for repo in config.repos:
        snapshot(config, store, repo.id, packages, hashes)
    store.cache.record_warmed_package('b', 'p30', 'p30.rpm', True, 200)
    expected = cleanup.make_plan(config, store).candidates
    monkeypatch.setattr(store.queries, 'completeness_catalogs',
                        lambda *args: pytest.fail('must stream catalog evidence'))
    seen, cursor, priority = {}, '', ''
    for _ in range(len(expected)):
        plan = cleanup.make_plan(config, store, limit=5, after=cursor, priority_after=priority)
        assert len(plan.candidates) <= 5
        seen.update(plan.candidates)
        cursor, priority = plan.cursor, plan.priority_cursor
    assert seen == expected
    assert cleanup.make_plan(config, store, selected=set(expected) | {'invented'}).candidates == expected


def test_bounded_window_checks_blockers_outside_selection(state):
    _, config, store = state
    expected = cleanup.make_plan(config, store)
    key = next(iter(expected.candidates))
    # An outdated owner must block deletion even when the client requests only
    # one otherwise plausible physical key.
    with store.database.connect() as conn:
        conn.execute("UPDATE repo_state SET source_identity='outdated'")
    assert not cleanup.make_plan(config, store, selected={key}).candidates


def test_planning_rejects_catalog_change_between_streams(state, monkeypatch):
    _, config, store = state
    original = store.queries.cleanup_rows
    calls = 0
    def rows(ids):
        nonlocal calls
        yield from original(ids)
        calls += 1
        if calls == 1:
            snapshot(config, store, 'b', {'new': 'new.rpm'})
    monkeypatch.setattr(store.queries, 'cleanup_rows', rows)
    with pytest.raises(cleanup.CleanupChanged):
        cleanup.make_plan(config, store, limit=1)


def test_absent_keys_are_reconsidered_after_expiry(state, monkeypatch):
    _, config, store = state
    plan = cleanup.make_plan(config, store)
    inspect, purge = mocks(monkeypatch, plan, exists=False)
    asyncio.run(cleanup.automatic_cleanup(config, store))
    first = inspect.await_count
    assert first and len(store.dedup_cleanup_recent) == len(plan.candidates)
    asyncio.run(cleanup.automatic_cleanup(config, store))
    assert inspect.await_count == first
    # A new client can populate a previously absent key during this interval.
    # Once the bounded negative evidence expires the normal probe discovers it.
    store.dedup_cleanup_recent = dict.fromkeys(store.dedup_cleanup_recent, 0)
    inspect.return_value = probe.ProbeResult(True, 100, generation=plan.generation)
    asyncio.run(cleanup.automatic_cleanup(config, store))
    assert purge.await_count == len(plan.candidates)


def test_time_budget_resumes_unprocessed_keys_before_next_window(state, monkeypatch):
    _, config, store = state
    plan = cleanup.make_plan(config, store)
    keys = list(plan.candidates)
    assert len(keys) > 1
    calls = []
    async def remove(plan, store, selected, **kwargs):
        calls.append(selected)
        done = selected[:1]
        return dict(results=dict.fromkeys(done, 'skipped'), absent_keys=[],
                    observed_removed_bytes=0, stopped='time budget reached' if len(selected) > 1 else None,
                    counts=dict(examined=1, purged=0, eligible=0, source_missing=0, canonical_missing=1))
    monkeypatch.setattr(cleanup, 'remove_copies', remove)
    for _ in keys:
        asyncio.run(cleanup.automatic_cleanup(config, store))
    assert [call[0] for call in calls] == keys
    assert store.dedup_cleanup_pending is None


def test_progress_survives_catalog_change_but_negative_evidence_does_not(state, monkeypatch):
    _, config, store = state
    plan = cleanup.make_plan(config, store)
    mocks(monkeypatch, plan, exists=False)
    monkeypatch.setattr(cleanup, 'AUTOMATIC_BATCH', 1)
    asyncio.run(cleanup.automatic_cleanup(config, store))
    old_cursor = store.dedup_cleanup_cursor
    store.dedup_cleanup_recent['sentinel'] = float('inf')
    with store.database.connect() as conn:
        conn.execute('UPDATE repo_state SET snapshot_revision=snapshot_revision+1')
    asyncio.run(cleanup.automatic_cleanup(config, store))
    assert 'sentinel' not in store.dedup_cleanup_recent
    assert store.dedup_cleanup_cursor != old_cursor


def test_cleanup_reports_missing_source_and_missing_canonical_separately(state, monkeypatch):
    _, config, store = state
    plan = cleanup.make_plan(config, store)
    inspect, purge = mocks(monkeypatch, plan)
    inspect.side_effect = [probe.ProbeResult(False, generation=plan.generation),
                          probe.ProbeResult(True, 100, generation=plan.generation),
                          probe.ProbeResult(True, 100, generation=plan.generation),
                          probe.ProbeResult(False, generation=plan.generation)]
    keys = list(plan.candidates)[:2]
    result = asyncio.run(cleanup.remove_copies(plan, store, keys))
    assert result['counts'] == dict(examined=2, source_missing=1, canonical_missing=1, eligible=0, purged=0)
    assert result['absent_keys'] == keys[:1]
    purge.assert_not_awaited()


def test_priority_keeps_first_owner_attribution_for_shared_routes(tmp_path):
    from test_completeness import setup
    repos = [dict(id=name, type='apt', upstream='https://example.org/' + directory,
                  arch='amd64', distribution='stable', component='main')
             for name, directory in [('0', 'debian'), ('a', 'ubuntu'), ('b', 'ubuntu')]]
    _, config, store = setup(tmp_path, repos, dedup=True)
    for repo in config.repos:
        snapshot(config, store, repo.id, {'p': 'pool/p.deb'}, {'p': 'a' * 64})
    # The obsolete Ubuntu key has two owners; only the later one has a warm
    # hint. Its display attribution must remain the first owner in either plan.
    store.cache.record_warmed_package('b', 'p', 'pool/p.deb', True, 200)
    expected = cleanup.make_plan(config, store).candidates
    assert expected and {row['repo_id'] for row in expected.values()} == {'a'}
    assert cleanup.make_plan(config, store, limit=2).candidates == expected


def test_nix_artifact_protects_a_key_selected_from_another_repository(state):
    from repowatch.config.models import RepoConfig
    from repowatch.routing import CacheKeyBuilder
    _, config, store = state
    nix = RepoConfig('n', 'nix', 'https://b.example/repo', 'x86_64-linux',
                     nix_source='https://example.org/nixpkgs.tar.gz')
    config = replace(config, repos=[*config.repos, nix])
    snapshot(config, store, 'n', {'root': 'root.narinfo'})
    builder = CacheKeyBuilder(config)
    artifact_key = builder.for_repo(nix)('pkg.rpm')
    assert artifact_key in cleanup.make_plan(config, store).candidates
    store.cache.record_nix_artifacts('n', 'root', [{'filename': 'pkg.rpm', 'content_hash': None, 'size': 10}])
    assert artifact_key not in cleanup.make_plan(config, store, selected={artifact_key}).candidates
