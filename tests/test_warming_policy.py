"""Whitelist/blacklist behavior and integration with the existing warm path."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from repowatch.config.models import Config
from repowatch.errors import ConfigError
from repowatch.config.models import RepoConfig
from repowatch.config.models import StatusServerConfig
from repowatch.operations.warm import warm_cache
from repowatch.models import RepoSnapshot
from repowatch.runtime.context import ServiceState
from repowatch.warming_policy import WarmingPolicy


def repo(**kw):
    return RepoConfig('r', 'dnf', 'https://example.org/repo', 'x86_64', **kw)


@pytest.mark.parametrize('white,black,name,allowed', [
    ([], [], 'anything', True), (['linux-*'], [], 'linux-headers', True),
    (['linux-*'], [], 'bash', False), (['linux-*'], ['*-debug'], 'linux-debug', False),
    ([], ['*-debug'], 'bash', True), ([], ['*-debug'], 'bash-debug', False),
    (['lib?'], [], 'liba', True), (['lib?'], [], 'libabc', False),
    (['lib[12]'], [], 'lib2', True), (['lib[!12]'], [], 'lib3', True),
    (['Linux-*'], [], 'linux-headers', False),
    (['app-editors/*'], [], 'app-editors/vim', True),
    (['hello:*'], [], 'hello:out', True), (['hello:*'], ['*:dev'], 'hello:dev', False),
    (['literal['], [], 'literal[', True),
])
def test_selection_semantics(tmp_path, white, black, name, allowed):
    store = ServiceState(tmp_path / 'state')
    store.repositories.record_snapshot(RepoSnapshot('r', {'key-with-version': 'file'}, {'key-with-version': name}))
    policy = WarmingPolicy(repo(prefetch_whitelist=white, prefetch_blacklist=black), store)
    assert policy.allows('key-with-version') is allowed


def test_exact_bans_win_and_are_not_interpreted_as_globs(tmp_path):
    store = ServiceState(tmp_path / 'state')
    store.repositories.record_snapshot(RepoSnapshot('r', {'a': 'a', 'b': 'b'}, {'a': 'foo', 'b': 'foo*'}))
    store.cache.ban_package('r', 'foo*')
    policy = WarmingPolicy(repo(prefetch_whitelist=['*']), store)
    assert policy.allows('a')
    assert not policy.allows('b')
    assert not policy.allows('missing')


@pytest.mark.parametrize('white,black,allowed', [([], [], True), (['*'], [], False), ([], ['foo*'], False)])
def test_missing_names_cannot_bypass_filters(tmp_path, white, black, allowed):
    policy = WarmingPolicy(repo(prefetch_whitelist=white, prefetch_blacklist=black), ServiceState(tmp_path / 'state'))
    assert policy.allows('foo-1') is allowed


@pytest.mark.parametrize('field', ['prefetch_whitelist', 'prefetch_blacklist'])
@pytest.mark.parametrize('value', [None, '*', {}, True, [None], [1], [''], [' foo'], ['foo '],
    ['foo\nbar'], ['foo\x00'], ['x' * 257], ['*'] * 129])
def test_invalid_lists_rejected(field, value):
    with pytest.raises(ConfigError, match=field):
        repo(**{field: value})


@pytest.mark.parametrize('force', [False, True])
def test_automatic_and_manual_warm_apply_policy_before_requests(tmp_path, monkeypatch, force):
    r = repo(prefetch=not force, prefetch_whitelist=['linux-*'], prefetch_blacklist=['*-debug'])
    store = ServiceState(tmp_path / 'state')
    names = {'kernel': 'linux-image', 'debug': 'linux-debug', 'headers': 'linux-headers', 'shell': 'bash'}
    files = {key: f'{key}.rpm' for key in names}
    store.repositories.record_snapshot(RepoSnapshot('r', files, names))
    store.cache.ban_package('r', 'linux-headers')
    c = Config(tmp_path / 'state', 300, 'http://cache.test', StatusServerConfig(), repos=[r])
    warm = AsyncMock(return_value=(True, 200))
    monkeypatch.setattr('repowatch.operations.warm.download_package', warm)
    result = asyncio.run(warm_cache(c, r, store, files, force=force))
    assert result == {'kernel': True}
    assert warm.await_count == 1
    assert warm.call_args.args[1] == 'http://cache.test/rpm/r/kernel.rpm'
    assert [p['package_key'] for p in store.cache.get_warmed_packages('r')] == ['kernel']
    assert store.repositories.get_packages('r') == files
    # Clearing lists affects the next operation; exact bans remain independent.
    r = replace(r, prefetch_whitelist=[], prefetch_blacklist=[])
    assert set(asyncio.run(warm_cache(c, r, store, files, force=force))) == {'kernel', 'debug', 'shell'}
