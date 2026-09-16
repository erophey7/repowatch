"""Shared scheduling across repositories, threads, loops and policy changes."""
import repowatch.operations.warm as operations_warm
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import math
import threading
import time

import pytest
import yaml

from repowatch.bandwidth import BandwidthBudget, _Request, scheduled_limit
from repowatch.config.models import Config
from repowatch.errors import ConfigError
from repowatch.config.models import RepoConfig
from repowatch.config.models import StatusServerConfig
from repowatch.config.load import load_config
from repowatch.runtime.context import ServiceState
from repowatch.operations.warm import warm_cache


def config(tmp_path, **kwargs):
    return Config(tmp_path / 'state.sqlite', 300, 'http://cache.test', StatusServerConfig(), **kwargs)


def repo(name='r', limit=None):
    return RepoConfig(name, 'apk', 'https://upstream.test', 'x86_64', prefetch_bandwidth_limit=limit)


def window(start='09:00', end='20:00', limit=10, days=None):
    return dict(start=start, end=end, limit=limit, days=days or ['mon'])


@pytest.mark.parametrize('value', [0, -1, True, '100', math.inf, math.nan])
def test_invalid_limits_rejected(tmp_path, value):
    with pytest.raises(ConfigError):
        config(tmp_path, prefetch_bandwidth_limit=value)
    with pytest.raises(ConfigError):
        repo(limit=value)


@pytest.mark.parametrize('windows', [None, {}, [dict(start='09:00', end='10:00')],
    [window(start='9:00')], [window(end='25:00')], [window(end='09:00')],
    [window(limit=0)], [window(days=['MON'])], [window(days=['mon','mon'])],
    [dict(window(), extra=True)], [window(),window(start='19:00',end='21:00')],
    [window('23:00','02:00',days=['sun']),window('01:00','03:00',days=['mon'])]])
def test_invalid_schedule_rejected(tmp_path, windows):
    with pytest.raises(ConfigError):
        config(tmp_path, prefetch_bandwidth_schedule=windows)


def test_unknown_timezone_rejected(tmp_path):
    with pytest.raises(ConfigError):
        config(tmp_path, prefetch_bandwidth_timezone='Not/AZone')


def test_overnight_week_wrap_boundaries_and_unlimited_window(tmp_path):
    c = config(tmp_path, prefetch_bandwidth_limit=100,
               prefetch_bandwidth_schedule=[window('23:00','02:00',limit=None,days=['sun'])])
    for stamp, expected in [('2026-09-13T22:59:00',100), ('2026-09-13T23:00:00',None),
                            ('2026-09-14T01:59:00',None), ('2026-09-14T02:00:00',100)]:
        assert scheduled_limit(c, datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)) == expected


def test_timezone_dst_repeated_hour_and_full_day(tmp_path):
    c = config(tmp_path, prefetch_bandwidth_limit=100, prefetch_bandwidth_timezone='America/New_York',
               prefetch_bandwidth_schedule=[window('01:00','02:00',10,['sun'])])
    for hour in (5,6):
        assert scheduled_limit(c, datetime(2026,11,1,hour,30,tzinfo=timezone.utc)) == 10
    assert scheduled_limit(c, datetime(2026,11,1,7,0,tzinfo=timezone.utc)) == 100
    c = replace(c, prefetch_bandwidth_schedule=[window('00:00','24:00',20,['mon'])])
    assert scheduled_limit(c, datetime(2026,9,15,3,59,tzinfo=timezone.utc)) == 20


def test_repository_cannot_raise_global_ceiling(tmp_path):
    c = config(tmp_path, prefetch_bandwidth_limit=10)
    assert c.effective_prefetch_bandwidth_limit(repo(limit=100)) == 10


def test_shared_budget_across_threads_and_event_loops(tmp_path):
    r1, r2 = repo('one'), repo('two')
    c = config(tmp_path, repos=[r1,r2], prefetch_bandwidth_limit=100)
    budget = BandwidthBudget()
    handles = [budget.limiter(c, r1), budget.limiter(c, r2)]
    barrier = threading.Barrier(2)
    def consume(handle):
        barrier.wait()
        asyncio.run(handle.consume(30))
    start = time.monotonic()
    with ThreadPoolExecutor(2) as executor:
        list(executor.map(consume, handles))
    assert time.monotonic() - start >= 0.55


def test_slow_repository_does_not_block_others_and_same_repo_shares_cap(tmp_path):
    c = config(tmp_path, repos=[repo('slow',10),repo('fast')], prefetch_bandwidth_limit=100)
    b = BandwidthBudget(); b.limiter(c,c.repos[0])
    a, other, fast = _Request('slow',100), _Request('slow',100), _Request('fast',100)
    b._requests.extend([a,other,fast])
    for i in range(11):
        b._step(i/10)
    assert a.remaining + other.remaining >= 189.9  # Together at most 10 bytes.
    assert fast.remaining <= 11  # Slow repo's cap must not serialize everyone.


def test_schedule_change_updates_pending_work_without_old_rate_credit(tmp_path):
    c = config(tmp_path, prefetch_bandwidth_limit=1000,
               prefetch_bandwidth_schedule=[window('09:00','20:00',10)])
    b = BandwidthBudget(); b.limiter(c,repo()); pending = _Request('r',1000)
    b._requests.append(pending)
    b._step(0, datetime(2026,9,14,8,59,59,tzinfo=timezone.utc))
    b._step(0.1, datetime(2026,9,14,8,59,59,tzinfo=timezone.utc))
    assert pending.remaining == 900
    b._step(0.2, datetime(2026,9,14,9,0,0,tzinfo=timezone.utc))
    assert pending.remaining == 900
    b._step(0.3, datetime(2026,9,14,9,0,0,tzinfo=timezone.utc))
    assert pending.remaining == pytest.approx(899)


def test_cancellation_removes_waiter_without_blocking_next_operation(tmp_path):
    async def scenario():
        b = BandwidthBudget(); c=config(tmp_path,prefetch_bandwidth_limit=100)
        handle=b.limiter(c,repo())
        task=asyncio.create_task(handle.consume(100000))
        await asyncio.sleep(0.06);task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not b._requests
        await asyncio.wait_for(handle.consume(1),1)
    asyncio.run(scenario())


def test_live_reload_and_invalid_file_retains_last_valid_policy(tmp_path):
    path=tmp_path/'config.yaml'
    raw=dict(state_db=str(tmp_path/'s.sqlite'),cache_base_url='http://cache.test',repos=[dict(id='r',type='apk',upstream='https://upstream.test',arch='x86_64')],prefetch_bandwidth_limit=10)
    path.write_text(yaml.safe_dump(raw))
    b=BandwidthBudget();b.limiter(load_config(path),repo());b.bind(path);b._refresh()
    assert b._config.prefetch_bandwidth_limit == 10
    raw['prefetch_bandwidth_limit']=100
    path.write_text(yaml.safe_dump(raw));b._next_reload=0;b._refresh()
    assert b._config.prefetch_bandwidth_limit == 100
    path.write_text('bad: [');b._next_reload=0;b._refresh()
    assert b._config.prefetch_bandwidth_limit == 100


def test_manual_and_automatic_warm_share_budget(tmp_path,monkeypatch):
    import repowatch.operations.warm as prefetch
    r1,r2=repo('one'),repo('two');c=config(tmp_path,repos=[r1,r2],prefetch_bandwidth_limit=100)
    store=ServiceState(c.state_db)
    async def fake(client,url,limiter):
        await limiter.consume(30)
        return True,200
    monkeypatch.setattr(operations_warm,'download_package',fake)
    async def scenario():
        await asyncio.gather(warm_cache(c,r1,store,{'one':'one.apk'}),
                             warm_cache(c,r2,store,{'two':'two.apk'},force=True))
    start=time.monotonic();asyncio.run(scenario())
    assert time.monotonic()-start >= 0.55


def test_high_rate_is_not_capped_by_http_chunk_size(tmp_path):
    c = config(tmp_path, prefetch_bandwidth_limit=10 * 1024 * 1024)
    b = BandwidthBudget(); b.limiter(c, repo())
    request = _Request('r', 1024 * 1024)
    b._requests.append(request)
    b._step(0)
    b._step(0.05)
    assert request.remaining == 512 * 1024


def test_no_unlimited_credit_survives_enabling_a_cap(tmp_path):
    b = BandwidthBudget(); c = config(tmp_path)
    b.limiter(c, repo()); first = _Request('r',100)
    b._requests.append(first); b._step(0)
    assert first.remaining == 0
    b.limiter(replace(c,prefetch_bandwidth_limit=10),repo())
    second = _Request('r',100); b._requests.append(second); b._step(1)
    assert second.remaining == 100


def test_running_warm_observes_edited_policy_without_restart(tmp_path):
    path = tmp_path/'config.yaml'
    raw = dict(state_db=str(tmp_path/'s.sqlite'),cache_base_url='http://cache.test',
               repos=[dict(id='r',type='apk',upstream='https://upstream.test',arch='x86_64')],
               prefetch_bandwidth_limit=10)
    path.write_text(yaml.safe_dump(raw))
    b = BandwidthBudget(); handle = b.limiter(load_config(path),repo()); b.bind(path)
    async def scenario():
        task = asyncio.create_task(handle.consume(10000))
        await asyncio.sleep(0.08)
        assert not task.done()
        raw['prefetch_bandwidth_limit'] = None
        path.write_text(yaml.safe_dump(raw))
        await asyncio.wait_for(task,2.5)
    asyncio.run(scenario())


def test_default_utc_does_not_require_system_timezone_database(tmp_path,monkeypatch):
    import repowatch.bandwidth as module
    def fail(*args):
        raise AssertionError('UTC must not load external timezone data')
    monkeypatch.setattr(module,'ZoneInfo',fail)
    c=config(tmp_path,prefetch_bandwidth_schedule=[window('00:00','24:00',10)])
    assert scheduled_limit(c,datetime(2026,9,14,tzinfo=timezone.utc)) == 10


def test_oversized_warm_response_still_spends_consumed_bytes():
    import httpx
    from repowatch.cache.transport import download_package
    consumed = []
    class Limiter:
        async def consume(self, size): consumed.append(size)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200,content=b'oversized'))) as client:
            assert await download_package(client,'http://cache.test/file',Limiter(),expected_size=1) == (False,200)
    asyncio.run(scenario())
    assert consumed == [9]
