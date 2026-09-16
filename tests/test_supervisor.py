import repowatch.runtime.service as runtime_service
import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest

from repowatch.errors import ConfigError
import repowatch.runtime.service as supervisor


def test_write_and_remove_pidfile_round_trip(tmp_path):
    path = tmp_path / 'sub' / 'repowatch.pid'
    runtime_service.write_pidfile(path)
    assert int(path.read_text()) == os.getpid()
    runtime_service.remove_pidfile(path)
    assert not path.exists()


def test_remove_pidfile_missing_is_a_noop(tmp_path):
    runtime_service.remove_pidfile(tmp_path / 'does-not-exist.pid')


def test_backup_loop_retries_until_stopped(monkeypatch, tmp_path):
    calls = []

    def fake_backup_once(state_db, backup_dir, retention_days):
        calls.append((state_db, backup_dir, retention_days))
        return backup_dir / 'x.sqlite3.gz'

    monkeypatch.setattr(runtime_service, 'backup_once', fake_backup_once)

    async def run():
        stop = asyncio.Event()

        async def stop_after_n():
            while len(calls) < 3:
                await asyncio.sleep(0.001)
            stop.set()

        await asyncio.gather(
            runtime_service.backup_loop(stop, tmp_path / 'state.sqlite3', tmp_path / 'backups', 0.001, 14),
            stop_after_n(),
        )

    asyncio.run(run())
    assert len(calls) >= 3


def test_backup_loop_survives_exceptions(monkeypatch, tmp_path):
    calls = []

    def failing_backup_once(state_db, backup_dir, retention_days):
        calls.append(1)
        raise OSError('disk full')

    monkeypatch.setattr(runtime_service, 'backup_once', failing_backup_once)

    async def run():
        stop = asyncio.Event()

        async def stop_after_n():
            while len(calls) < 2:
                await asyncio.sleep(0.001)
            stop.set()

        await asyncio.gather(
            runtime_service.backup_loop(stop, tmp_path / 'state.sqlite3', tmp_path / 'backups', 0.001, 14),
            stop_after_n(),
        )

    asyncio.run(run())
    assert len(calls) >= 2


def test_nginx_loop_calls_apply_with_use_systemctl_false(monkeypatch, tmp_path):
    calls = []

    def fake_apply(config_path, policy_path, *, use_systemctl):
        calls.append((config_path, policy_path, use_systemctl))
        return True

    monkeypatch.setattr(runtime_service, 'nginx_apply', fake_apply)

    async def run():
        stop = asyncio.Event()

        async def stop_after_n():
            while len(calls) < 2:
                await asyncio.sleep(0.001)
            stop.set()

        await asyncio.gather(
            runtime_service.nginx_loop(stop, 'config.yaml', 'policy.json', 0.001),
            stop_after_n(),
        )

    asyncio.run(run())
    assert len(calls) >= 2
    assert all(call == ('config.yaml', 'policy.json', False) for call in calls)


def test_supervise_requires_root_for_nginx(monkeypatch):
    monkeypatch.setattr(runtime_service.os, 'geteuid', lambda: 1000)
    with pytest.raises(ConfigError, match='root'):
        asyncio.run(runtime_service.supervise(
            'config.yaml', None, None, nginx=True, nginx_policy='policy.json',
        ))


def test_supervise_requires_nginx_policy():
    with pytest.raises(ConfigError, match='nginx-policy'):
        asyncio.run(runtime_service.supervise('config.yaml', None, None, nginx=True, nginx_policy=None))


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
syslog_listener:
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
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode(errors='replace'))
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


@pytest.mark.parametrize('normal_exit', [False, True])
def test_supervise_detects_watcher_exit_and_cleans_up(tmp_path, monkeypatch, normal_exit):
    from repowatch.config import load_config
    from repowatch.runtime.context import ServiceState
    config = load_config('docker/compose/config.example.yaml')
    store = ServiceState(tmp_path / 'state.db')
    pid = tmp_path / 'service.pid'
    async def watcher(*args, **kwargs):
        if not normal_exit:
            raise RuntimeError('watcher failure')
    monkeypatch.setattr(runtime_service, 'run_forever', watcher)
    before = signal.getsignal(signal.SIGTERM)
    async def run():
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(runtime_service.supervise('unused', config, store,
                                                             pid_file=str(pid)), timeout=2)
    asyncio.run(run())
    assert not pid.exists()
    assert signal.getsignal(signal.SIGTERM) == before


def test_supervise_cancellation_drains_children(tmp_path, monkeypatch):
    from repowatch.config import load_config
    from repowatch.runtime.context import ServiceState
    config = load_config('docker/compose/config.example.yaml')
    store = ServiceState(tmp_path / 'state.db')
    entered, finished = [], []
    async def watcher(*args, **kwargs):
        entered.append(True)
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(True)
    monkeypatch.setattr(runtime_service, 'run_forever', watcher)
    async def run():
        task = asyncio.create_task(runtime_service.supervise('unused', config, store))
        while not entered:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == [True]
    asyncio.run(run())


@pytest.mark.parametrize('protocol', ['http', 'syslog'])
def test_listener_bind_failure_unwinds_sockets(tmp_path, protocol):
    import socket
    from dataclasses import replace
    from repowatch.config import load_config
    from repowatch.runtime.context import ServiceState
    config = load_config('docker/compose/config.example.yaml')
    config = replace(config, status_server=replace(config.status_server, bind='127.0.0.1', port=0),
                     syslog_listener=replace(config.syslog_listener, bind='127.0.0.1', port=0))
    store = ServiceState(tmp_path / 'state.db')
    kind = socket.SOCK_STREAM if protocol == 'http' else socket.SOCK_DGRAM
    with socket.socket(socket.AF_INET, kind) as occupied:
        occupied.bind(('127.0.0.1', 0))
        if protocol == 'http':
            occupied.listen()
            config = replace(config, status_server=replace(config.status_server, port=occupied.getsockname()[1]))
        else:
            config = replace(config, syslog_listener=replace(config.syslog_listener, port=occupied.getsockname()[1]))
        with pytest.raises(OSError):
            runtime_service.start_listeners('unused', config, store)


def test_failed_listener_stops_supervisor_and_cleans_up(tmp_path, monkeypatch):
    from dataclasses import replace
    from repowatch.config import load_config
    from repowatch.runtime.context import ServiceState
    import repowatch.runtime.syslog as syslog
    config = load_config('docker/compose/config.example.yaml')
    config = replace(config, status_server=replace(config.status_server, port=0),
                     syslog_listener=replace(config.syslog_listener, port=0))
    store = ServiceState(tmp_path / 'state.db')
    def fail(*args, **kwargs):
        kwargs['ready'].set()
        raise OSError('listener failure')
    monkeypatch.setattr(syslog, 'run_listener', fail)
    async def watcher(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(runtime_service, 'run_forever', watcher)
    with runtime_service.start_listeners('unused', config, store) as listeners:
        async def run():
            with pytest.raises(RuntimeError, match='syslog'):
                await asyncio.wait_for(runtime_service.supervise('unused', config, store,
                                                                 listeners=listeners), timeout=2)
        asyncio.run(run())
    assert all(not thread.is_alive() for thread in listeners.threads)


def test_syslog_initialization_failure_aborts_startup(tmp_path, monkeypatch):
    from dataclasses import replace
    from repowatch.config import load_config
    from repowatch.runtime.context import ServiceState
    import repowatch.runtime.syslog as syslog
    config = load_config('docker/compose/config.example.yaml')
    config = replace(config, status_server=replace(config.status_server, port=0),
                     syslog_listener=replace(config.syslog_listener, port=0))
    store = ServiceState(tmp_path / 'state.db')
    def fail(*args, **kwargs):
        raise OSError('cannot read initial index')
    monkeypatch.setattr(syslog, 'refresh_package_indexes', fail)
    with pytest.raises(RuntimeError, match='startup') as caught:
        runtime_service.start_listeners('unused', config, store)
    assert isinstance(caught.value.__cause__, OSError)


def test_stop_signal_allows_periodic_loop_to_return_normally(tmp_path, monkeypatch):
    from repowatch.config import load_config
    from repowatch.runtime.context import ServiceState
    config = load_config('docker/compose/config.example.yaml')
    store = ServiceState(tmp_path / 'state.db')
    async def watcher(*args, **kwargs):
        await asyncio.Event().wait()
    async def backup(stop, *args):
        # Simulate the signal callback setting the shared event just before
        # the periodic loop observes it and returns in the same event-loop turn.
        stop.set()
    monkeypatch.setattr(runtime_service, 'run_forever', watcher)
    monkeypatch.setattr(runtime_service, 'backup_loop', backup)
    asyncio.run(asyncio.wait_for(runtime_service.supervise(
        'unused', config, store, backup_dir=str(tmp_path / 'backups')), timeout=2))
