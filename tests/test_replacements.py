"""Same-version replacements: durable state, invalidation order, and actual bytes."""
import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from repowatch import watcher
from repowatch.api import status_payload
from repowatch.config import Config, RepoConfig, NginxConfig, StatusServerConfig
from repowatch.parsers.base import IndexHeadResult
from repowatch.state import RepoSnapshot, StateStore


def snapshot(filename='foo.rpm', digest=None):
    return RepoSnapshot('r', {'foo-1': filename}, {'foo-1': 'foo'},
                        {'foo-1': digest} if digest else {})


@pytest.mark.parametrize('old_file,new_file,old_hash,new_hash,modified', [
    ('foo.rpm', 'bar.rpm', None, None, True),
    ('foo.rpm', 'foo.rpm', 'a' * 64, 'b' * 64, True),
    ('foo.rpm', 'foo.rpm', None, 'b' * 64, False),
    ('foo.rpm', 'foo.rpm', 'a' * 64, None, False),
    ('foo.rpm', 'foo.rpm', 'a' * 64, 'a' * 64, False),
])
def test_replacement_diff_and_history(tmp_path, old_file, new_file, old_hash, new_hash, modified):
    store = StateStore(tmp_path / 'state')
    store.record_snapshot(snapshot(old_file, old_hash))
    diff = store.record_snapshot(snapshot(new_file, new_hash))
    assert diff.changed is modified
    assert diff.modified_packages == (['foo-1'] if modified else [])
    assert not diff.new_packages and not diff.removed_packages
    assert bool(store.get_pending_replacements('r')) is modified
    if modified:
        assert store.get_history('r')[0]['modified_packages'] == ['foo-1']
        assert store.get_status('r')['last_modified_packages'] == ['foo-1']


def test_pending_survives_restart_and_new_replacement_preserves_old_targets(tmp_path):
    path = tmp_path / 'state'
    store = StateStore(path)
    store.record_snapshot(snapshot('a.rpm'))
    store.record_snapshot(snapshot('b.rpm'))
    old = store.get_pending_replacements('r')[0]
    store = StateStore(path)
    assert store.get_pending_replacements('r')[0] == old
    store.record_snapshot(snapshot('c.rpm'))
    current = store.get_pending_replacements('r')[0]
    assert set(map(tuple, current['targets'])) == {('r', 'a.rpm'), ('r', 'b.rpm'), ('r', 'c.rpm')}
    store.finish_replacement('r', 'foo-1', old['revision'])
    assert store.get_pending_replacements('r')
    store.record_snapshot(RepoSnapshot('r'))
    # Removal must not discard unpurged historical filenames.
    assert store.get_pending_replacements('r')[0]['filename'] is None


def test_old_event_schema_migrates_without_losing_history(tmp_path):
    path = tmp_path / 'state'
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE repo_events (id INTEGER PRIMARY KEY, repo_id TEXT, ts TEXT, new_pkgs_json TEXT, removed_pkgs_json TEXT)")
        conn.execute("INSERT INTO repo_events VALUES (1, 'r', '2020-01-01', '[\"foo-1\"]', '[]')")
    store = StateStore(path)
    assert store.get_history('r')[0]['modified_packages'] == []
    assert store.get_history('r')[0]['new_packages'] == ['foo-1']


@pytest.mark.parametrize('renamed', [False, True])
@pytest.mark.parametrize('first_failure', ['none', 'purge', 'hash', 'warm'])
def test_watcher_replacement_purges_before_warming_and_retries_on_unchanged_head(
        tmp_path, monkeypatch, renamed, first_failure):
    before, after = b'old package', b'new package'
    digest = lambda data: hashlib.sha256(data).hexdigest()
    store = StateStore(tmp_path / 'state')
    repo = RepoConfig('r', 'dnf', 'https://mirror.test/repo', 'x86_64')
    config = Config(tmp_path / 'state', 300, 'http://cache.test', StatusServerConfig(), repos=[repo],
                    nginx=NginxConfig(enabled=True, enable_purge=True, enable_cache_probe=True))
    new_file = 'bar.rpm' if renamed else 'foo.rpm'
    store.record_snapshot(snapshot('foo.rpm', digest(before)))
    parser = watcher.PARSERS['dnf']
    monkeypatch.setattr(parser, 'check_index_changed', AsyncMock(side_effect=[
        IndexHeadResult(False, 'new', None), IndexHeadResult(True, 'new', None)]))
    fetch = AsyncMock(return_value=snapshot(new_file, digest(after)))
    monkeypatch.setattr(parser, 'fetch', fetch)
    events = []
    failure = [first_failure]
    cache = {'foo.rpm': before, 'bar.rpm': before}
    def handler(request):
        if request.url.path == '/purge-raw':
            filename = request.url.params['key'].rsplit('/', 1)[-1]
            events.append(('purge', filename))
            if failure[0] == 'purge':
                failure[0] = 'none'
                return httpx.Response(503)
            cached = cache.pop(filename, None)
            return httpx.Response(200 if cached else 404)
        events.append(('warm', request.url.path))
        if failure[0] == 'warm':
            failure[0] = 'none'
            return httpx.Response(503)
        if failure[0] == 'hash':
            failure[0] = 'none'
            cache[new_file] = before
        data = cache.setdefault(new_file, after)
        return httpx.Response(200, content=data)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    asyncio.run(watcher.check_repo(config, repo, store))
    if first_failure != 'none':
        assert store.get_pending_replacements('r')
        if first_failure == 'purge':
            assert not any(kind == 'warm' for kind, _ in events)
        store = StateStore(tmp_path / 'state')
        asyncio.run(watcher.check_repo(config, repo, store))
        assert fetch.await_count == 1  # the unchanged cycle still retries pending work
    assert not store.get_pending_replacements('r')
    assert cache[new_file] == after
    assert events[0][0] == 'purge'
    assert store.get_warmed_packages('r')[0]['status'] == 'ok'
    assert store.get_history('r')[0]['modified_packages'] == ['foo-1']


def test_disabled_purge_exposes_pending_without_warming_old_hit(tmp_path, monkeypatch):
    store = StateStore(tmp_path / 'state')
    store.record_snapshot(snapshot('old.rpm'))
    store.record_snapshot(snapshot('new.rpm'))
    repo = RepoConfig('r', 'dnf', 'https://mirror.test/repo', 'x86_64')
    config = Config(tmp_path / 'state', 300, 'http://cache.test', StatusServerConfig(), repos=[repo])
    warm = AsyncMock()
    monkeypatch.setattr(watcher, 'warm_cache', warm)
    asyncio.run(watcher.refresh_replacements(config, repo, store))
    warm.assert_not_called()
    assert 'enable_purge' in store.get_pending_replacements('r')[0]['last_error']
    path = tmp_path / 'config.yaml'
    path.write_text(f'cache_base_url: http://cache.test\nstate_db: {tmp_path / "state"}\nrepos:\n  - id: r\n    type: dnf\n    upstream: https://mirror.test/repo\n    arch: x86_64\n')
    assert status_payload(path, store)[1]['r']['pending_replacements'] == 1


@pytest.mark.parametrize('policy', ['disabled', 'banned', 'removed'])
def test_replacement_invalidates_without_overriding_warming_policy(tmp_path, monkeypatch, policy):
    store = StateStore(tmp_path / 'state')
    store.record_snapshot(snapshot('old.rpm'))
    store.record_snapshot(snapshot('new.rpm'))
    if policy == 'removed':
        store.record_snapshot(RepoSnapshot('r'))
    if policy == 'banned':
        store.ban_package('r', 'foo')
    repo = RepoConfig('r', 'dnf', 'https://mirror.test/repo', 'x86_64', prefetch=policy != 'disabled')
    config = Config(tmp_path / 'state', 300, 'http://cache.test', StatusServerConfig(), repos=[repo],
                    nginx=NginxConfig(enable_purge=True))
    calls = []
    async def purge(config, repo, items):
        calls.extend(items.values())
        return {key: 'purged' for key in items}
    monkeypatch.setattr(watcher, 'purge_selected', purge)
    warm = AsyncMock()
    monkeypatch.setattr(watcher, 'warm_cache', warm)
    asyncio.run(watcher.refresh_replacements(config, repo, store))
    warm.assert_not_called()
    assert set(calls) == {'old.rpm', 'new.rpm'}
    assert not store.get_pending_replacements('r')


def test_pending_replacement_keeps_known_hash_if_metadata_temporarily_loses_it(tmp_path):
    store = StateStore(tmp_path / 'state')
    store.record_snapshot(snapshot(digest='a' * 64))
    store.record_snapshot(snapshot(digest='b' * 64))
    diff = store.record_snapshot(snapshot())
    assert not diff.changed
    assert store.get_pending_replacements('r')[0]['content_hash'] == 'b' * 64


def test_replacement_preserves_previous_canonical_location(tmp_path):
    store = StateStore(tmp_path / 'state')
    store.record_snapshot(RepoSnapshot('a', {'foo-1': 'foo.rpm'}, content_hashes={'foo-1': 'a' * 64}))
    store.record_snapshot(snapshot(digest='a' * 64))
    store.record_snapshot(snapshot(digest='b' * 64))
    assert ['a', 'foo.rpm'] in store.get_pending_replacements('r')[0]['targets']
