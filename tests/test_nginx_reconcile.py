"""Regression coverage for cached reconciliation, invalidation and recovery."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

import repowatch.nginx.apply as apply_module
import repowatch.nginx.reconcile as reconcile
from repowatch.config.models import Config, NginxConfig, RepoConfig, StatusServerConfig
from repowatch.errors import ConfigError
from repowatch.models import RepoSnapshot
from repowatch.runtime.context import ServiceState


@pytest.fixture
def installation(tmp_path, monkeypatch):
    store = ServiceState(tmp_path / 'state.sqlite3')
    repos = [RepoConfig(name, 'apt', 'https://example.org/' + name, 'amd64',
                        distribution='stable', component='main') for name in ('a', 'b')]
    current = [Config(store.database.db_path, 300, 'http://localhost:8080', StatusServerConfig(),
                      repos=repos, nginx=NginxConfig(enabled=True, enable_dedup=True,
                                                    enable_purge=True, enable_cache_probe=True))]
    for repo in repos:
        store.repositories.record_snapshot(RepoSnapshot(repo.id, {'v1': 'pool/pkg.deb'},
                                           content_hashes={'v1': 'a' * 64}))
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps(dict(site_link=str(tmp_path / 'site.conf'),
        cache_dir='/var/cache/nginx/repo', access_log='/var/log/nginx/repo.log',
        nginx_conf='/etc/nginx/nginx.conf', nginx_binary='/usr/sbin/nginx')))
    (tmp_path / 'site.conf').symlink_to(tmp_path / 'active.conf')
    monkeypatch.setattr(apply_module, '_check_policy_owner', lambda p: None)
    monkeypatch.setattr(apply_module, 'load_config', lambda p: current[0])
    calls = []
    monkeypatch.setattr(apply_module.subprocess, 'run', lambda args, **kw: calls.append(args))
    return current, store, policy, calls


def run(installation, **kwargs):
    return apply_module.apply('config', str(installation[2]), **kwargs)


def test_unchanged_checks_and_unrelated_writes_skip_duplicate_query(installation, monkeypatch):
    current, store, policy, calls = installation
    assert run(installation)
    store.repositories.touch_last_check('a')
    store.cache.record_warmed_package('a', 'v1', 'pool/pkg.deb', True, 200)
    def unexpected(*args):
        raise AssertionError('unchanged catalogs must not run the duplicate query')
    monkeypatch.setattr(apply_module.CacheStore, 'find_duplicate_files', unexpected)
    assert not run(installation)
    assert len(calls) == 2


@pytest.mark.parametrize('name', ['active.conf', 'purge.conf', 'dedup.map', 'probe.conf', 'probe.js'])
@pytest.mark.parametrize('damage', ['missing', 'changed', 'invalid_utf8'])
def test_each_damaged_output_is_repaired(installation, name, damage):
    assert run(installation)
    path = installation[2].parent / name
    original = path.read_bytes()
    if damage == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'\xff broken' if damage == 'invalid_utf8' else b'# damaged\n')
    assert run(installation)
    assert path.read_bytes() == original
    assert not run(installation)


@pytest.mark.parametrize('digest', [None, 'b' * 64])
def test_changed_or_lost_hash_rebuilds_map(installation, digest):
    assert run(installation)
    store = installation[1]
    old_revision = store.repositories.get_snapshot_revisions()['b']
    store.repositories.record_snapshot(RepoSnapshot('b', {'v1': 'pool/pkg.deb'},
                                       content_hashes={'v1': digest} if digest else {}))
    assert store.repositories.get_snapshot_revisions()['b'] > old_revision
    assert run(installation)
    assert 'pool/pkg.deb' not in (installation[2].parent / 'dedup.map').read_text()


def test_newly_discovered_hash_invalidates_without_new_package_event(installation):
    store = installation[1]
    store.repositories.record_snapshot(RepoSnapshot('b', {'v1': 'pool/pkg.deb'}))
    assert run(installation)
    result = store.repositories.record_snapshot(RepoSnapshot('b', {'v1': 'pool/pkg.deb'},
                                                content_hashes={'v1': 'a' * 64}))
    assert not result.changed
    assert run(installation)
    assert 'pool/pkg.deb' in (installation[2].parent / 'dedup.map').read_text()


@pytest.mark.parametrize('kind', ['config', 'policy', 'renderer', 'force', 'status', 'database'])
def test_other_inputs_invalidate(installation, monkeypatch, kind):
    current, store, policy, calls = installation
    assert run(installation)
    seen = []
    original = apply_module.CacheStore.find_duplicate_files
    def query(self):
        seen.append(1)
        return original(self)
    monkeypatch.setattr(apply_module.CacheStore, 'find_duplicate_files', query)
    if kind == 'config':
        current[0] = replace(current[0], nginx=replace(current[0].nginx, package_ttl=172800))
    elif kind == 'policy':
        data = json.loads(policy.read_text()); data['access_log'] = '/var/log/nginx/new.log'
        policy.write_text(json.dumps(data))
    elif kind == 'renderer':
        monkeypatch.setattr(reconcile, 'renderer_digest', lambda: 'new renderer')
    elif kind == 'status':
        (policy.parent / 'status.json').write_text('broken JSON')
    elif kind == 'database':
        import sqlite3
        target = policy.parent / 'replacement.sqlite3'
        with sqlite3.connect(store.database.db_path) as source, sqlite3.connect(target) as dest:
            source.backup(dest)
        target.replace(store.database.db_path)
    run(installation, force=kind == 'force')
    assert seen == [1]
    assert not run(installation)
    assert seen == [1]


def test_failed_reload_is_retried_even_when_files_match(installation, monkeypatch):
    import subprocess
    assert run(installation)
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ['nginx'])
    monkeypatch.setattr(apply_module.subprocess, 'run', fail)
    with pytest.raises(ConfigError):
        run(installation, force=True)
    calls = []
    monkeypatch.setattr(apply_module.subprocess, 'run', lambda args, **kw: calls.append(args))
    assert run(installation)
    assert len(calls) == 2
    assert not run(installation)


def test_catalog_change_during_render_is_not_cached(installation, monkeypatch):
    original = apply_module.render
    def render(*args, **kwargs):
        installation[1].repositories.record_snapshot(RepoSnapshot('b', {}))
        return original(*args, **kwargs)
    monkeypatch.setattr(apply_module, 'render', render)
    assert run(installation)
    assert json.loads((installation[2].parent / 'status.json').read_text())['inputs'] is None
    monkeypatch.setattr(apply_module, 'render', original)
    assert run(installation)
    assert not run(installation)


def test_repository_removal_and_dedup_toggle_remove_old_routes(installation):
    current, store, policy, calls = installation
    assert run(installation)
    current[0] = replace(current[0], repos=current[0].repos[:1])
    assert run(installation)
    assert 'pool/pkg.deb' not in (policy.parent / 'dedup.map').read_text()
    current[0] = replace(current[0], nginx=replace(current[0].nginx, enable_dedup=False))
    assert run(installation)
    assert not run(installation)


def test_package_removal_invalidates_catalog(installation):
    assert run(installation)
    installation[1].repositories.record_snapshot(RepoSnapshot('b', {}))
    assert run(installation)
    assert 'pool/pkg.deb' not in (installation[2].parent / 'dedup.map').read_text()


def test_renderer_fingerprint_includes_imported_code_and_probe_template(tmp_path, monkeypatch):
    root = tmp_path / 'repowatch'
    (root / 'nginx').mkdir(parents=True)
    monkeypatch.setattr(reconcile, '__file__', str(root / 'nginx/reconcile.py'))
    routing = root / 'routing.py'; routing.write_text('old routing')
    template = root / 'nginx/probe.js'; template.write_text('old probe')
    initial = reconcile.renderer_digest()
    routing.write_text('new routing')
    changed = reconcile.renderer_digest()
    assert changed != initial
    template.write_text('new probe')
    assert reconcile.renderer_digest() != changed


def test_moving_bytes_between_outputs_does_not_preserve_integrity(installation):
    assert run(installation)
    directory = installation[2].parent
    active = directory / 'active.conf'; purge = directory / 'purge.conf'
    original_active = active.read_bytes(); original_purge = purge.read_bytes()
    first, rest = original_purge.split(b'\n', 1)
    active.write_bytes(original_active + first + b'\n')
    purge.write_bytes(rest)
    assert active.read_bytes() + purge.read_bytes() == original_active + original_purge
    assert run(installation)
    assert active.read_bytes() == original_active and purge.read_bytes() == original_purge
