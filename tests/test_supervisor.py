import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest

from repowatch.config import ConfigError
from repowatch import supervisor


def test_write_and_remove_pidfile_round_trip(tmp_path):
    path = tmp_path / 'sub' / 'repowatch.pid'
    supervisor.write_pidfile(path)
    assert int(path.read_text()) == os.getpid()
    supervisor.remove_pidfile(path)
    assert not path.exists()


def test_remove_pidfile_missing_is_a_noop(tmp_path):
    supervisor.remove_pidfile(tmp_path / 'does-not-exist.pid')


def test_backup_loop_retries_until_stopped(monkeypatch, tmp_path):
    calls = []

    def fake_backup_once(state_db, backup_dir, retention_days):
        calls.append((state_db, backup_dir, retention_days))
        return backup_dir / 'x.sqlite3.gz'

    monkeypatch.setattr(supervisor, 'backup_once', fake_backup_once)

    async def run():
        stop = asyncio.Event()

        async def stop_after_n():
            while len(calls) < 3:
                await asyncio.sleep(0.001)
            stop.set()

        await asyncio.gather(
            supervisor.backup_loop(stop, tmp_path / 'state.sqlite3', tmp_path / 'backups', 0.001, 14),
            stop_after_n(),
        )

    asyncio.run(run())
    assert len(calls) >= 3


def test_backup_loop_survives_exceptions(monkeypatch, tmp_path):
    calls = []

    def failing_backup_once(state_db, backup_dir, retention_days):
        calls.append(1)
        raise OSError('disk full')

    monkeypatch.setattr(supervisor, 'backup_once', failing_backup_once)

    async def run():
        stop = asyncio.Event()

        async def stop_after_n():
            while len(calls) < 2:
                await asyncio.sleep(0.001)
            stop.set()

        await asyncio.gather(
            supervisor.backup_loop(stop, tmp_path / 'state.sqlite3', tmp_path / 'backups', 0.001, 14),
            stop_after_n(),
        )

    asyncio.run(run())
    assert len(calls) >= 2


def test_nginx_loop_calls_apply_with_use_systemctl_false(monkeypatch, tmp_path):
    calls = []

    def fake_apply(config_path, policy_path, *, use_systemctl):
        calls.append((config_path, policy_path, use_systemctl))
        return True

    monkeypatch.setattr(supervisor, 'nginx_apply', fake_apply)

    async def run():
        stop = asyncio.Event()

        async def stop_after_n():
            while len(calls) < 2:
                await asyncio.sleep(0.001)
            stop.set()

        await asyncio.gather(
            supervisor.nginx_loop(stop, 'config.yaml', 'policy.json', 0.001),
            stop_after_n(),
        )

    asyncio.run(run())
    assert len(calls) >= 2
    assert all(call == ('config.yaml', 'policy.json', False) for call in calls)


def test_supervise_requires_root_for_nginx(monkeypatch):
    monkeypatch.setattr(supervisor.os, 'geteuid', lambda: 1000)
    with pytest.raises(ConfigError, match='root'):
        asyncio.run(supervisor.supervise(
            'config.yaml', None, None, nginx=True, nginx_policy='policy.json',
        ))


def test_supervise_requires_nginx_policy():
    with pytest.raises(ConfigError, match='nginx-policy'):
        asyncio.run(supervisor.supervise('config.yaml', None, None, nginx=True, nginx_policy=None))


def test_supervise_subprocess_pidfile_and_clean_shutdown(tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(f"""
state_db: {tmp_path / 'state.sqlite3'}
cache_base_url: http://127.0.0.1:1
repos:
  - id: r
    type: apk
    upstream: http://127.0.0.1:1/alpine
    arch: x86_64
    prefetch: false
status_server:
  port: 0
""")
    pid_file = tmp_path / 'repowatch.pid'
    process = subprocess.Popen(
        [sys.executable, '-m', 'repowatch.cli', '-c', str(config_path), 'supervise',
         '--pid-file', str(pid_file)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        for _ in range(200):
            if pid_file.exists():
                break
            time.sleep(0.05)
        else:
            pytest.fail('pid file never appeared')
        assert int(pid_file.read_text()) == process.pid
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)
        assert process.returncode == 0
        assert not pid_file.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
