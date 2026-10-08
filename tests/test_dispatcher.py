"""Slots, dispatcher and per-repository task behavior of the scheduler."""

import asyncio
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from repowatch.config.models import Config, RepoConfig, StatusServerConfig
from repowatch.errors import ConfigError
from repowatch.operations import check as operations_check
from repowatch.operations.check import CacheWork
from repowatch.operations.slots import RepoSlots
from repowatch.runtime import scheduler
from repowatch.runtime.context import ServiceState


def _config(names, **overrides) -> Config:
    repos = [RepoConfig(id=n, type="apk", upstream="https://example.org", arch="x86_64",
                        prefetch=False) for n in names]
    return Config(state_db="/tmp/unused.sqlite3", check_interval=300,
                  cache_base_url="http://127.0.0.1:8080", status_server=StatusServerConfig(),
                  repos=repos, **overrides)


def test_slots_reject_zero():
    with pytest.raises(ValueError):
        RepoSlots(0)


def test_config_rejects_check_concurrency_below_one():
    with pytest.raises(ConfigError, match="check_concurrency"):
        Config(state_db="x", check_interval=300, cache_base_url="http://h",
               status_server=StatusServerConfig(), check_concurrency=0)


def test_single_slot_lets_a_check_run_while_a_warm_is_busy():
    async def run():
        slots = RepoSlots(1)
        release = asyncio.Event()
        entered = []

        async def warm():
            async with slots.warm():
                entered.append(1)
                await release.wait()

        warms = [asyncio.create_task(warm()) for _ in range(3)]
        await asyncio.sleep(0.01)
        async with asyncio.timeout(1):
            async with slots.check():
                pass
        async with asyncio.timeout(1):  # and warming never overlaps itself
            assert len(entered) == 1
        release.set()
        await asyncio.gather(*warms)

    asyncio.run(run())


def test_single_slot_checks_are_serial():
    async def run():
        slots = RepoSlots(1)
        active = peak = 0

        async def check():
            nonlocal active, peak
            async with slots.check():
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(*(check() for _ in range(4)))
        return peak

    assert asyncio.run(run()) == 1


@pytest.mark.parametrize("size,warm_limit", [(2, 1), (3, 2), (4, 3), (5, 3), (7, 5), (8, 6), (12, 9)])
def test_warming_never_takes_the_reserved_slots(size, warm_limit):
    async def run():
        slots = RepoSlots(size)
        active = peak = 0

        async def warm():
            nonlocal active, peak
            async with slots.warm():
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1

        await asyncio.gather(*(warm() for _ in range(size * 2)))
        return peak

    assert asyncio.run(run()) == warm_limit


def test_check_runs_while_all_warming_slots_are_busy():
    async def run():
        slots = RepoSlots(4)
        release = asyncio.Event()

        async def warm():
            async with slots.warm():
                await release.wait()

        warms = [asyncio.create_task(warm()) for _ in range(6)]
        await asyncio.sleep(0.01)
        async with asyncio.timeout(1):
            async with slots.check():
                pass
        release.set()
        await asyncio.gather(*warms)

    asyncio.run(run())


def test_slow_warm_does_not_block_checks_of_other_repositories(tmp_path, monkeypatch):
    store = ServiceState(tmp_path / "state.sqlite3")
    config = _config(["slow"] + [f"r{i}" for i in range(6)], check_concurrency=2)
    checked = []

    async def fake_index(cfg, repo, st):
        checked.append(repo.id)
        return CacheWork({"k": "f"}, {}) if repo.id == "slow" else None

    async def run():
        gate = asyncio.Event()

        async def fake_apply(cfg, repo, st, work):
            await gate.wait()

        monkeypatch.setattr(operations_check, "check_index", fake_index)
        monkeypatch.setattr(operations_check, "apply_cache_work", fake_apply)
        dispatcher = scheduler.RepoDispatcher(store)
        dispatcher.reconcile(config)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(checked) == 7:
                break
        assert sorted(checked) == sorted(r.id for r in config.repos)
        assert "slow" in dispatcher._tasks  # still warming
        gate.set()
        await dispatcher.close()

    asyncio.run(run())


def test_dispatcher_never_runs_two_tasks_for_one_repository(tmp_path, monkeypatch):
    store = ServiceState(tmp_path / "state.sqlite3")
    config = _config(["r"])
    calls = []

    async def run():
        gate = asyncio.Event()

        async def fake_check(cfg, repo, st, *, slots=None):
            calls.append(repo.id)
            await gate.wait()

        monkeypatch.setattr(scheduler, "check_repo", fake_check)
        dispatcher = scheduler.RepoDispatcher(store)
        for _ in range(3):
            dispatcher.reconcile(config)
            await asyncio.sleep(0.01)
        assert calls == ["r"]
        gate.set()
        await asyncio.sleep(0.01)
        dispatcher.reconcile(config)  # reaps; not due only if last_check was set
        await dispatcher.close()

    asyncio.run(run())


def test_removed_repository_task_is_cancelled_and_edit_does_not_cancel(tmp_path, monkeypatch):
    store = ServiceState(tmp_path / "state.sqlite3")
    config = _config(["a", "b"])
    cancelled = []

    async def run():
        async def fake_check(cfg, repo, st, *, slots=None):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(repo.id)
                raise

        monkeypatch.setattr(scheduler, "check_repo", fake_check)
        dispatcher = scheduler.RepoDispatcher(store)
        dispatcher.reconcile(config)
        await asyncio.sleep(0.01)
        edited = replace(config, repos=[replace(config.repos[0], upstream="https://other.example")])
        dispatcher.reconcile(edited)
        await asyncio.sleep(0.01)
        assert cancelled == ["b"]
        dispatcher.reconcile(edited)
        assert set(dispatcher._tasks) == {"a"}
        await dispatcher.close()

    asyncio.run(run())
    assert sorted(cancelled) == ["a", "b"]


def test_dispatcher_raises_sqlite_failure_but_isolates_others(tmp_path, monkeypatch):
    store = ServiceState(tmp_path / "state.sqlite3")

    async def run():
        async def fake_check(cfg, repo, st, *, slots=None):
            if repo.id == "db":
                raise sqlite3.OperationalError("database unavailable")
            raise RuntimeError("upstream problem")

        monkeypatch.setattr(scheduler, "check_repo", fake_check)
        dispatcher = scheduler.RepoDispatcher(store)
        dispatcher.reconcile(_config(["other"]))
        await asyncio.sleep(0.01)
        dispatcher.reconcile(_config(["other"]))  # runtime error only logged
        await dispatcher.close()
        dispatcher = scheduler.RepoDispatcher(store)
        config = _config(["db"])
        dispatcher.reconcile(config)
        await asyncio.sleep(0.01)
        with pytest.raises(sqlite3.OperationalError):
            dispatcher.reconcile(config)
        await dispatcher.close()

    asyncio.run(run())


@pytest.mark.parametrize("clock_origin", [0.0, 10.0, 4000.0])
def test_prune_runs_while_repository_work_is_stuck(tmp_path, monkeypatch, clock_origin):
    store = ServiceState(tmp_path / "state.sqlite3")
    path = tmp_path / "config.yaml"
    path.write_text(f"state_db: {tmp_path / 's.db'}\ncache_base_url: http://127.0.0.1:8080\n"
                    "repos:\n  - id: r\n    type: apk\n    upstream: https://example.org\n"
                    "    arch: x86_64\n")
    pruned = []
    # Patch only the scheduler clock; asyncio keeps its real timeout clock.
    monkeypatch.setattr(scheduler, "time", SimpleNamespace(monotonic=lambda: clock_origin))

    async def run():
        stop = asyncio.Event()

        async def stuck(cfg, repo, st, *, slots=None):
            await asyncio.Event().wait()

        async def prune(cfg, st):
            pruned.append(1)
            stop.set()

        monkeypatch.setattr(scheduler, "check_repo", stuck)
        monkeypatch.setattr(scheduler, "prune_all", prune)
        monkeypatch.setattr(scheduler, "_SCHEDULER_TICK_SECONDS", 0.001)
        await asyncio.wait_for(scheduler.run_forever(str(path), store, stop=stop), timeout=3)

    asyncio.run(run())
    assert pruned


@pytest.mark.parametrize('kind', ['check', 'warm'])
def test_resize_wakes_existing_waiters_and_shrink_drains_holders(kind):
    async def run():
        slots = RepoSlots(1)
        hold = getattr(slots, kind)
        entered = []
        gates = [asyncio.Event() for _ in range(4)]

        async def job(i):
            async with hold():
                entered.append(i)
                await gates[i].wait()

        tasks = [asyncio.create_task(job(i)) for i in range(4)]
        await asyncio.sleep(0)
        assert entered == [0]
        slots.resize(4)
        await asyncio.sleep(0)
        assert len(entered) == (4 if kind == 'check' else 3)
        slots.resize(1)
        extra = asyncio.Event()

        async def waiter():
            async with hold():
                extra.set()

        waiting = asyncio.create_task(waiter())
        gates[0].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not extra.is_set()
        for gate in gates:
            gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks, waiting), 1)
        assert extra.is_set()

    asyncio.run(run())


def test_cancelled_waiter_and_holder_release_capacity():
    async def run():
        slots = RepoSlots(1)
        entered = asyncio.Event()

        async def job():
            async with slots.warm():
                entered.set()
                await asyncio.Event().wait()

        holder = asyncio.create_task(job())
        await entered.wait()
        waiter = asyncio.create_task(job())
        await asyncio.sleep(0)
        waiter.cancel()
        holder.cancel()
        await asyncio.gather(holder, waiter, return_exceptions=True)
        async with asyncio.timeout(1):
            async with slots.warm(), slots.check():
                pass

    asyncio.run(run())


def test_dispatcher_resize_and_new_repository_share_existing_pool(tmp_path, monkeypatch):
    store = ServiceState(tmp_path / 'state.sqlite3')

    async def run():
        seen, pools = [], []
        gate = asyncio.Event()

        async def check(cfg, repo, st, *, slots):
            pools.append(slots)
            async with slots.check():
                seen.append(repo.id)
                await gate.wait()

        monkeypatch.setattr(scheduler, 'check_repo', check)
        dispatcher = scheduler.RepoDispatcher(store)
        dispatcher.reconcile(_config(['a', 'b'], check_concurrency=2))
        await asyncio.sleep(0)
        dispatcher.reconcile(_config(['a', 'b', 'c'], check_concurrency=1))
        await asyncio.sleep(0)
        assert seen == ['a', 'b']
        assert len(pools) == 3 and all(pool is pools[0] for pool in pools)
        gate.set()
        await asyncio.wait_for(asyncio.gather(*dispatcher._tasks.values()), 1)
        assert seen == ['a', 'b', 'c']
        await dispatcher.close()

    asyncio.run(run())


def test_removal_cleanup_is_not_cancelled_twice_or_overlapped(tmp_path, monkeypatch):
    store = ServiceState(tmp_path / 'state.sqlite3')

    async def run():
        cleanup = asyncio.Event()
        finish = asyncio.Event()
        calls = []

        async def check(cfg, repo, st, *, slots):
            calls.append(repo.upstream)
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await finish.wait()

        monkeypatch.setattr(scheduler, 'check_repo', check)
        dispatcher = scheduler.RepoDispatcher(store)
        config = _config(['a'])
        dispatcher.reconcile(config)
        await asyncio.sleep(0)
        dispatcher.reconcile(_config([]))
        await asyncio.wait_for(cleanup.wait(), 1)
        dispatcher.reconcile(_config([]))
        edited = replace(config, repos=[replace(config.repos[0], upstream='https://new.example')])
        dispatcher.reconcile(edited)
        await asyncio.sleep(0)
        assert calls == ['https://example.org']
        assert not dispatcher._tasks['a'].done()
        finish.set()
        await asyncio.gather(*dispatcher._tasks.values(), return_exceptions=True)
        dispatcher.reconcile(edited)
        await asyncio.sleep(0)
        assert calls == ['https://example.org', 'https://new.example']
        await dispatcher.close()

    asyncio.run(run())


@pytest.mark.parametrize('daemon', [False, True])
def test_due_failure_does_not_prevent_healthy_repository(tmp_path, monkeypatch, daemon):
    store = ServiceState(tmp_path / 'state.sqlite3')
    seen = []

    def due(config, repo, store, **kwargs):
        if repo.id == 'bad':
            raise ValueError('invalid timestamp')
        return True

    async def check(config, repo, store, **kwargs):
        seen.append(repo.id)

    monkeypatch.setattr(scheduler, '_is_due', due)
    monkeypatch.setattr(scheduler, 'check_repo', check)

    async def run():
        config = _config(['bad', 'good'])
        if daemon:
            dispatcher = scheduler.RepoDispatcher(store)
            dispatcher.reconcile(config)
            await asyncio.sleep(0)
            await dispatcher.close()
        else:
            assert await scheduler.check_all(config, store) == ['bad']

    asyncio.run(run())
    assert seen == ['good']


def test_replacement_retry_releases_index_capacity_on_unchanged_head(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from repowatch.parsers.base import IndexHeadResult

    store = ServiceState(tmp_path / 'state.sqlite3')
    config = _config(['slow', 'new'], check_concurrency=1)
    monkeypatch.setattr(store.repositories, 'get_index_meta', lambda *args: ('old', None))
    monkeypatch.setattr(store.repositories, 'has_snapshot', lambda *args: True)
    monkeypatch.setattr(store.cache, 'get_pending_replacements',
                        lambda repo_id: [{'pending': True}] if repo_id == 'slow' else [])
    parser = MagicMock()

    async def unchanged(*args):
        return IndexHeadResult(unchanged=True, etag='old', last_modified=None)

    parser.check_index_changed = unchanged
    monkeypatch.setitem(operations_check.PARSERS, 'apk', lambda repo: parser)

    async def run():
        retrying = asyncio.Event()
        finish = asyncio.Event()
        slots = RepoSlots(1)

        async def retry(cfg, repo, st):
            retrying.set()
            await finish.wait()

        monkeypatch.setattr(operations_check, 'refresh_replacements', retry)
        slow = asyncio.create_task(operations_check.check_repo(config, config.repos[0], store, slots=slots))
        await asyncio.wait_for(retrying.wait(), 2)
        async with asyncio.timeout(1):
            async with slots.check():
                # Another actual index check can finish while replacements warm.
                work = await operations_check.check_index(config, config.repos[1], store)
        assert work is None
        # No-op cache phases must not queue behind the slow retry and prevent
        # subsequent checks of an otherwise idle repository.
        for _ in range(2):
            await asyncio.wait_for(operations_check.check_repo(
                config, config.repos[1], store, slots=slots), 1)
        assert not slow.done()
        finish.set()
        await slow

    asyncio.run(run())


def test_new_repository_is_checked_on_later_tick_during_slow_warm(tmp_path, monkeypatch):
    store = ServiceState(tmp_path / 'state.sqlite3')

    async def run():
        warming = asyncio.Event()
        checked = asyncio.Event()

        async def index(cfg, repo, st):
            if repo.id == 'slow':
                return CacheWork({}, {})
            checked.set()
            return None

        async def apply(cfg, repo, st, work):
            warming.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(operations_check, 'check_index', index)
        monkeypatch.setattr(operations_check, 'apply_cache_work', apply)
        dispatcher = scheduler.RepoDispatcher(store)
        dispatcher.reconcile(_config(['slow'], check_concurrency=1))
        await asyncio.wait_for(warming.wait(), 1)
        dispatcher.reconcile(_config(['slow', 'new'], check_concurrency=1))
        await asyncio.wait_for(checked.wait(), 1)
        assert not dispatcher._tasks['slow'].done()
        await dispatcher.close()
        assert not dispatcher._tasks

    asyncio.run(run())


def test_check_once_waits_for_cache_work(tmp_path, monkeypatch):
    store = ServiceState(tmp_path / 'state.sqlite3')

    async def run():
        warming, release = asyncio.Event(), asyncio.Event()

        async def index(*args):
            return CacheWork({}, {})

        async def apply(*args):
            warming.set()
            await release.wait()

        monkeypatch.setattr(operations_check, 'check_index', index)
        monkeypatch.setattr(operations_check, 'apply_cache_work', apply)
        batch = asyncio.create_task(scheduler.check_all(_config(['repo']), store))
        await asyncio.wait_for(warming.wait(), 1)
        assert not batch.done()
        release.set()
        assert await asyncio.wait_for(batch, 1) == []

    asyncio.run(run())


def test_retention_does_not_overlap_and_failure_drains_repository(tmp_path, monkeypatch):
    store = ServiceState(tmp_path / 'state.sqlite3')
    config = _config(['repo'])
    monkeypatch.setattr(scheduler, 'load_config', lambda _: config)
    monkeypatch.setattr(scheduler, '_SCHEDULER_TICK_SECONDS', 0.001)
    monkeypatch.setattr(scheduler, '_PRUNE_INTERVAL_SECONDS', 0)

    async def run():
        calls = []
        cancelled = asyncio.Event()

        async def check(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def prune(*args):
            calls.append(1)
            await asyncio.sleep(0.02)
            assert len(calls) == 1
            raise RuntimeError('retention failed')

        monkeypatch.setattr(scheduler, 'check_repo', check)
        monkeypatch.setattr(scheduler, 'prune_all', prune)
        with pytest.raises(RuntimeError, match='retention failed'):
            await asyncio.wait_for(scheduler.run_forever(str(tmp_path / 'config.yaml'), store), 1)
        assert cancelled.is_set()
        assert calls == [1]

    asyncio.run(run())


def test_retention_runs_at_startup_then_waits_a_full_interval(tmp_path, monkeypatch):
    store = ServiceState(tmp_path / 'state.sqlite3')
    config = _config([])
    clock = [0.0]
    calls = []
    monkeypatch.setattr(scheduler, 'load_config', lambda _: config)
    monkeypatch.setattr(scheduler, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(scheduler, '_SCHEDULER_TICK_SECONDS', 0.001)

    async def retention(*args):
        calls.append(clock[0])

    monkeypatch.setattr(scheduler, '_run_retention', retention)

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler.run_forever('unused', store, stop=stop))

        async def wait_for_calls(count):
            while len(calls) < count:
                await asyncio.sleep(0.001)

        try:
            await asyncio.wait_for(wait_for_calls(1), 1)
            clock[0] = 3599.0
            await asyncio.sleep(0.02)
            assert calls == [0.0]
            clock[0] = 3600.0
            await asyncio.wait_for(wait_for_calls(2), 1)
            assert calls == [0.0, 3600.0]
        finally:
            stop.set()
            await asyncio.wait_for(task, 1)

    asyncio.run(run())
