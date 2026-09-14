"""Trial container initialization and private nginx configuration contracts."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
from unittest.mock import Mock

import pytest
import yaml

from repowatch.config import ConfigError

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


bootstrap = module('compose_init', 'docker/compose/init.py')
supervisor = module('compose_nginx', 'docker/nginx/run.py')
SEED = ROOT / 'docker/compose/config.example.yaml'


def test_trial_network_and_storage_boundaries():
    services = yaml.safe_load((ROOT / 'docker-compose.dev.yml').read_text())['services']
    assert services['init']['network_mode'] == 'none'
    assert services['repowatch']['network_mode'] == 'service:nginx'
    assert services['repowatch']['depends_on']['nginx']['condition'] == 'service_healthy'
    assert services['nginx']['depends_on']['init']['condition'] == 'service_completed_successfully'
    assert services['nginx']['ports'] == ['127.0.0.1:18080:8080', '127.0.0.1:18085:8085']
    assert './config:/etc/repowatch:ro' in services['nginx']['volumes']
    for service in services.values():
        assert not service.get('privileged')
        assert not any('docker.sock' in mount for mount in service['volumes'])
        assert 'build' not in service
        assert all(mount.startswith('./') for mount in service['volumes'])
    assert 'volumes' not in yaml.safe_load((ROOT / 'docker-compose.dev.yml').read_text())
    assert services['repowatch']['user'] == '10001:${REPOWATCH_GID:-10001}'
    assert services['nginx']['user'] == '0:${REPOWATCH_GID:-10001}'


def test_initialize_preserves_existing_configuration_and_database(tmp_path):
    config, state, nix = [tmp_path / name for name in ('config', 'state', 'nix')]
    previous = os.umask(0o022)
    try:
        bootstrap.initialize(config, state, nix, SEED, owner=None)
        data = (config / 'config.yaml').read_text().replace('Compose trial', 'Existing trial')
        (config / 'config.yaml').write_text(data)
        import sqlite3
        with sqlite3.connect(state / 'state.sqlite3') as db:
            db.execute('CREATE TABLE trial_marker (value TEXT)')
            db.execute("INSERT INTO trial_marker VALUES ('preserved')")
        bootstrap.initialize(config, state, nix, SEED, owner=None)
        assert (config / 'config.yaml').read_text() == data
        with sqlite3.connect(state / 'state.sqlite3') as db:
            assert db.execute('SELECT value FROM trial_marker').fetchone() == ('preserved',)
        assert state.stat().st_mode & 0o7777 == 0o2770
    finally:
        os.umask(previous)


def test_initialize_rejects_symlink(tmp_path):
    config = tmp_path / 'config'
    config.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        bootstrap.initialize(config, tmp_path / 'state', tmp_path / 'nix', SEED, owner=None)


@pytest.mark.parametrize('replacement', [
    ('state_db: /var/lib/repowatch/state.sqlite3', 'state_db: /tmp/other.sqlite3'),
    ('listen: \'8080\'', 'listen: \'80\''),
    ('cache_base_url: http://127.0.0.1:8080', 'cache_base_url: http://elsewhere:8080'),
    ('cache_dir: /var/cache/nginx/repowatch', 'cache_dir: /tmp/cache'),
])
def test_invalid_snapshot_preserves_last_valid_input(tmp_path, replacement):
    source, destination = tmp_path / 'config.yaml', tmp_path / 'input.yaml'
    source.write_text(SEED.read_text())
    supervisor.snapshot(source, destination)
    original = destination.read_bytes()
    source.write_text(source.read_text().replace(*replacement))
    with pytest.raises(ConfigError):
        supervisor.snapshot(source, destination)
    assert destination.read_bytes() == original
    assert set(tmp_path.iterdir()) == {source, destination}


@pytest.mark.parametrize('timeout', [False, True])
def test_shutdown_uses_nginx_graceful_signal(timeout):
    process = Mock()
    process.poll.return_value = None
    if timeout:
        process.wait.side_effect = [subprocess.TimeoutExpired('nginx', 20), 0]
    supervisor.shutdown(process)
    process.send_signal.assert_called_once_with(signal.SIGQUIT)
    assert process.kill.called == timeout


def test_bind_mount_ownership_and_cache_initialization(tmp_path, monkeypatch):
    changes = []
    monkeypatch.setattr(bootstrap.os, 'chown', lambda path, uid, gid: changes.append((path, uid, gid)))
    for name in ('setgroups', 'setgid', 'setuid'):
        monkeypatch.setattr(bootstrap.os, name, lambda *_: None)
    config, state, nix, cache = [tmp_path / name for name in ('config', 'state', 'nix', 'cache')]
    cache.mkdir()
    cached = cache / 'existing-package'
    cached.write_bytes(b'keep')
    previous = os.umask(0o022)
    try:
        bootstrap.initialize(config, state, nix, SEED, owner=(10001, 1234), cache_dir=cache)
    finally:
        os.umask(previous)
    assert (config, 10001, 1234) in changes
    assert (config / 'keys', 10001, 1234) in changes
    assert (cache, 33, 1234) in changes
    assert cached.read_bytes() == b'keep'
    assert not any(path == cached for path, _, _ in changes)
    assert cache.stat().st_mode & 0o7777 == 0o2770


@pytest.mark.parametrize('target', ['keys', 'cache'])
def test_bind_initializer_rejects_symlink_before_changing_paths(tmp_path, target):
    config = tmp_path / 'config'
    config.mkdir()
    cache = tmp_path / 'cache'
    path = config / 'keys' if target == 'keys' else cache
    path.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        bootstrap.initialize(config, tmp_path / 'state', tmp_path / 'nix', SEED,
                             owner=None, cache_dir=cache)
    assert not (config / 'config.yaml').exists()
    assert not (tmp_path / 'state').exists()


@pytest.mark.parametrize('gid', [-1, 0, 2**32])
def test_invalid_bind_group_does_not_create_directories(tmp_path, gid):
    with pytest.raises(ValueError, match='UID/GID'):
        bootstrap.initialize(tmp_path / 'config', tmp_path / 'state', tmp_path / 'nix',
                             SEED, owner=(10001, gid))
    assert list(tmp_path.iterdir()) == []



@pytest.mark.skipif(not (ROOT / 'docker-compose.release.yml').exists(),
                    reason='release Compose draft stays local until release preparation')
def test_release_is_standalone_and_preserves_development_runtime_contract():
    development = yaml.safe_load((ROOT / 'docker-compose.dev.yml').read_text())
    release = yaml.safe_load((ROOT / 'docker-compose.release.yml').read_text())
    assert release['name'] != development['name']
    assert release['services'].keys() == development['services'].keys()
    for name, service in release['services'].items():
        assert service['pull_policy'] == 'always'
        repository = 'repowatch-nginx' if name == 'nginx' else 'repowatch'
        assert service['image'].startswith('docker.io/${DOCKERHUB_NAMESPACE:?')
        assert '/' + repository + ':${REPOWATCH_VERSION:?' in service['image']
        assert all(mount.startswith(('./config:', './data/')) for mount in service['volumes'])
        expected = dict(development['services'][name])
        expected['image'] = service['image']
        expected['pull_policy'] = 'always'
        expected['volumes'] = [mount for mount in expected['volumes']
                               if not mount.startswith('./docker/')]
        assert service == expected
    assert release['services']['init']['image'] == release['services']['repowatch']['image']
    assert 'volumes' not in release
