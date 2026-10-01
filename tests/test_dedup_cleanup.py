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
