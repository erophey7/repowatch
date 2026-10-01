"""Demand survives package versions; automatic warming never subscribes a catalog."""

import asyncio
from dataclasses import replace
from threading import Event
from unittest.mock import AsyncMock

import pytest

from repowatch.config.models import Config, RepoConfig, StatusServerConfig
from repowatch.models import RepoSnapshot
from repowatch.operations import check, warm, replacements
from repowatch.parsers.base import IndexHeadResult
from repowatch.runtime.context import ServiceState
from repowatch.runtime import syslog
from repowatch.runtime.scheduler import RepoDispatcher


@pytest.fixture
def state(tmp_path):
    repo = RepoConfig('r', 'apk', 'https://example.org/alpine', 'x86_64')
    config = Config(tmp_path / 'state.sqlite', 300, 'http://cache.test', StatusServerConfig(), repos=[repo])
    return config, repo, ServiceState(config.state_db)


def snap(repo, version='1', *, names=('curl', 'wget'), digest=None):
    return RepoSnapshot(repo.id, {f'{n}-{version}': f'{n}-{version}.apk' for n in names},
                        {f'{n}-{version}': n for n in names},
                        {f'{n}-{version}': digest for n in names} if digest else {})


def save(store, repo, version='1', **kwargs):
    store.repositories.record_snapshot(snap(repo, version, **kwargs), source_identity=repo.catalog_identity())


def parser(monkeypatch, repo, snapshot):
    cls = check.PARSERS[repo.type]
    monkeypatch.setattr(cls, 'check_index_changed', AsyncMock(return_value=IndexHeadResult(False, None, None)))
    monkeypatch.setattr(cls, 'fetch', AsyncMock(return_value=snapshot))


def test_first_snapshot_and_subsequent_unrelated_changes_do_not_download(state, monkeypatch):
    config, repo, store = state
    download = AsyncMock(side_effect=AssertionError('catalog discovery must not download packages'))
    monkeypatch.setattr(warm, 'download_package', download)
    for version in ('1', '2'):
        parser(monkeypatch, repo, snap(repo, version))
        asyncio.run(check.check_repo(config, repo, store))
    assert store.repositories.get_packages(repo.id) == snap(repo, '2').packages
    assert len(store.repositories.get_history(repo.id)) == 2
    assert store.cache.interested_keys(repo.id, repo.catalog_identity()) == set()


def test_manual_selection_subscribes_name_and_warms_only_its_updates(state, monkeypatch):
    config, repo, store = state
    save(store, repo)
    download = AsyncMock(return_value=(True, 200))
    monkeypatch.setattr(warm, 'download_package', download)
    assert asyncio.run(warm.warm_selected(config, repo, store, ['curl-1']))['warmed'] == ['curl-1']
    store.cache.remove_warmed_package(repo.id, 'curl-1')
    store = ServiceState(config.state_db)
    parser(monkeypatch, repo, snap(repo, '2', names=('curl', 'wget', 'firefox')))
    asyncio.run(check.check_repo(config, repo, store))
    assert [call.args[1].rsplit('/', 1)[-1] for call in download.await_args_list] == ['curl-1.apk', 'curl-2.apk']
    assert store.cache.interested_keys(repo.id, repo.catalog_identity()) == {'curl-2'}


def test_failed_manual_request_still_expresses_interest_but_excluded_selection_does_not(state, monkeypatch):
    config, repo, store = state
    save(store, repo)
    repo = replace(repo, prefetch=False, prefetch_blacklist=['wget'])
    monkeypatch.setattr(warm, 'download_package', AsyncMock(return_value=(False, 503)))
    result = asyncio.run(warm.warm_selected(config, repo, store, ['curl-1', 'wget-1', 'unknown']))
    assert result == dict(warmed=[], failed=['curl-1'], skipped=['wget-1'], not_found=['unknown'])
    assert store.cache.interested_keys(repo.id, repo.catalog_identity()) == {'curl-1'}


def test_previous_auto_warm_and_whitelist_alone_never_establish_interest(state, monkeypatch):
    config, repo, store = state
    save(store, repo)
    store.cache.record_warmed_package(repo.id, 'curl-1', 'curl-1.apk', True, 200, source='prefetch')
    repo = replace(repo, prefetch_whitelist=['curl'])
    parser(monkeypatch, repo, snap(repo, '2'))
    work = asyncio.run(check.check_index(config, repo, store))
    assert work.warm == {}


def test_source_changes_do_not_carry_interest_to_a_different_repository(state):
    _, repo, store = state
    save(store, repo)
    store.cache.record_interest(repo.id, ['curl-1'], repo.catalog_identity())
    changed = replace(repo, upstream='https://different.example/alpine')
    save(store, changed, '2')
    store.cache.record_interest(repo.id, ['wget-2'], repo.catalog_identity())
    assert store.cache.interested_keys(repo.id, changed.catalog_identity()) == set()
    assert store.cache.interested_keys(repo.id, repo.catalog_identity()) == set()


def test_empty_initial_snapshot_does_not_make_later_population_a_subscription(state, monkeypatch):
    config, repo, store = state
    save(store, repo, names=())
    parser(monkeypatch, repo, snap(repo, '2'))
    assert asyncio.run(check.check_index(config, repo, store)).warm == {}


def test_disabled_prefetch_still_records_indexes_but_does_not_download_updates(state, monkeypatch):
    config, repo, store = state
    save(store, repo)
    store.cache.record_interest(repo.id, ['curl-1'], repo.catalog_identity())
    repo = replace(repo, prefetch=False)
    parser(monkeypatch, repo, snap(repo, '2'))
    assert asyncio.run(check.check_index(config, repo, store)).warm == {}
    assert store.cache.interested_keys(repo.id, repo.catalog_identity()) == {'curl-2'}


@pytest.mark.parametrize('interested', [False, True])
def test_modified_same_path_always_invalidates_but_only_demanded_names_warm(state, monkeypatch, interested):
    config, repo, store = state
    config = replace(config, nginx=replace(config.nginx, enable_purge=True))
    save(store, repo, digest='a' * 64)
    if interested:
        store.cache.record_interest(repo.id, ['curl-1'], repo.catalog_identity())
    save(store, repo, digest='b' * 64)
    purge = AsyncMock(side_effect=lambda config, repo, items: dict.fromkeys(items, 'purged'))
    warmer = AsyncMock(return_value={'curl-1': True})
    monkeypatch.setattr(replacements, 'purge_selected', purge)
    monkeypatch.setattr(replacements, 'warm_cache', warmer)
    asyncio.run(replacements.refresh_replacements(config, repo, store))
    assert purge.await_count == 2
    assert warmer.await_count == int(interested)
    if interested:
        assert warmer.await_args.args[3] == {'curl-1': 'curl-1.apk'}


def test_live_disable_stops_queued_downloads_but_manual_warming_still_works(state, monkeypatch):
    config, repo, store = state
    config = replace(config, prefetch_concurrency=1)
    save(store, repo)
    calls = []
    async def download(client, url, limiter):
        calls.append(url)
        store.automatic_warm_repos = {repo.id: replace(repo, prefetch=False)}
        return True, 200
    monkeypatch.setattr(warm, 'download_package', download)
    result = asyncio.run(warm.warm_cache(config, repo, store, snap(repo).packages))
    assert result == {'curl-1': True}
    assert len(calls) == 1
    assert asyncio.run(warm.warm_selected(config, repo, store, ['wget-1']))['warmed'] == ['wget-1']


def test_dispatcher_publishes_opt_out_without_cancelling_checks(state, monkeypatch):
    config, repo, store = state
    async def run():
        checker = AsyncMock(return_value=None)
        monkeypatch.setattr(check, 'check_index', checker)
        dispatcher = RepoDispatcher(store)
        dispatcher.reconcile(replace(config, repos=[replace(repo, prefetch=False)]))
        assert not store.automatic_warm_enabled(repo)
        await asyncio.sleep(.01)
        checker.assert_awaited_once()
        await dispatcher.close()
    asyncio.run(run())


def test_migration_uses_only_successful_attributable_gets_and_runs_once(state):
    config, repo, store = state
    save(store, repo, names=('used', 'head', 'failed', 'auto', 'manual', 'ambiguous'))
    for name, method, status, owner in [('used', 'GET', '200', repo.id), ('head', 'HEAD', '200', repo.id),
            ('failed', 'GET', '404', repo.id), ('ambiguous', 'GET', '200', None)]:
        store.requests.record_request(repo.id, '127.0.0.1', method, '/file', status, 'HIT', name+'-1', owner)
    for name in ('auto', 'manual'):
        store.cache.record_warmed_package(repo.id, name+'-1', name+'.apk', True, 200, source='prefetch')
    with store.database.connect() as conn:
        conn.execute('DROP TABLE package_interest')
    store = ServiceState(config.state_db)
    assert store.cache.interested_keys(repo.id, repo.catalog_identity()) == {'used-1'}
    with store.database.connect() as conn:
        conn.execute('DELETE FROM package_interest')
    assert ServiceState(config.state_db).cache.interested_keys(repo.id, repo.catalog_identity()) == set()


@pytest.mark.parametrize('method,status,prefetch,expected', [('GET', '200', '0', {'curl-1'}),
    ('HEAD', '200', '0', set()), ('GET', '404', '0', set()), ('GET', '200', '1', set())])
def test_syslog_only_client_downloads_establish_interest(state, monkeypatch, method, status, prefetch, expected):
    config, repo, store = state
    save(store, repo)
    # A prefetched package later used by a client must become interesting even
    # though warmed.source intentionally retains its original value.
    store.cache.record_warmed_package(repo.id, 'curl-1', 'curl-1.apk', True, 200)
    stop = Event()
    class Packets:
        def settimeout(self, value): pass
        def recvfrom(self, size):
            stop.set()
            return (f'<134>Oct 2 12:00:00 host repowatch: 127.0.0.1 {method} /alpine/x86_64/curl-1.apk {status} HIT {prefetch}'.encode(), ('127.0.0.1', 1))
        def close(self): pass
    monkeypatch.setattr(syslog, 'load_config', lambda path: config)
    syslog.run_listener('unused', config, store, sock=Packets(), stop=stop)
    assert store.cache.interested_keys(repo.id, repo.catalog_identity()) == expected


def test_orphan_cleanup_removes_interest_but_warm_retention_does_not(state):
    _, repo, store = state
    save(store, repo)
    store.cache.record_interest(repo.id, ['curl-1'], repo.catalog_identity())
    store.cache.prune_warmed_packages(0)
    assert store.cache.count_repo_rows(repo.id)['package_interest'] == 1
    assert store.cache.delete_repo_rows(repo.id)['package_interest'] == 1
    assert store.cache.interested_keys(repo.id, repo.catalog_identity()) == set()


def test_nix_does_not_warm_undemanded_roots_on_first_or_unchanged_catalog(state, monkeypatch):
    from test_nix import repo as nix_repo
    config, _, store = state
    repo = nix_repo()
    config = replace(config, repos=[repo])
    snapshot = RepoSnapshot(repo.id, {'root': 'a.narinfo', 'other': 'b.narinfo'},
                            {'root': 'hello:out', 'other': 'firefox:out'})
    parser(monkeypatch, repo, snapshot)
    assert asyncio.run(check.check_index(config, repo, store)).warm == {}
    assert asyncio.run(check.check_index(config, repo, store)).warm == {}
    store.cache.record_interest(repo.id, ['root'], repo.catalog_identity())
    assert asyncio.run(check.check_index(config, repo, store)).warm == {'root': 'a.narinfo'}
    rebuilt = RepoSnapshot(repo.id, {'root-v2': 'c.narinfo', 'other': 'b.narinfo'},
                           {'root-v2': 'hello:out', 'other': 'firefox:out'})
    parser(monkeypatch, repo, rebuilt)
    assert asyncio.run(check.check_index(config, repo, store)).warm == {'root-v2': 'c.narinfo'}


def test_nix_artifact_observation_tracks_known_roots_without_marking_closure_complete(state):
    from test_nix import repo as nix_repo
    _, _, store = state
    repo = nix_repo()
    store.repositories.record_snapshot(RepoSnapshot(repo.id, {'root': 'a.narinfo'}, {'root': 'hello:out'}),
                                        source_identity=repo.catalog_identity())
    store.cache.record_nix_artifacts(repo.id, 'root', [{'filename': 'nar/archive.nar'}])
    store.cache.record_artifact_interest(repo.id, 'nar/unknown.nar', repo.catalog_identity())
    assert store.cache.interested_keys(repo.id, repo.catalog_identity()) == set()
    store.cache.record_artifact_interest(repo.id, 'nar/archive.nar', repo.catalog_identity())
    assert store.cache.interested_keys(repo.id, repo.catalog_identity()) == {'root'}
    assert store.cache.get_warmed_packages(repo.id) == []


def test_interest_requires_catalog_names_and_rejects_stale_source_registration(state):
    _, repo, store = state
    store.repositories.record_snapshot(RepoSnapshot(repo.id, {'unknown-version': 'unknown.apk'}),
                                        source_identity=repo.catalog_identity())
    store.cache.record_interest(repo.id, ['unknown-version'], repo.catalog_identity())
    assert store.cache.interested_keys(repo.id, repo.catalog_identity()) == set()
    save(store, repo)
    store.cache.record_interest(repo.id, ['curl-1'], 'old-source')
    assert store.cache.interested_keys(repo.id, repo.catalog_identity()) == set()
