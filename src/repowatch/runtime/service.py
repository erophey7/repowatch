"""`repowatch supervise` — a single foreground process replacing all three
systemd units (repowatch.service, repowatch-nginx.timer,
repowatch-backup.timer) for environments without systemd (see
docs/deployment.md, "Running without systemd"). Reuses runtime.scheduler.run_forever
and nginx.apply.apply unchanged; only the periodic backup/nginx-reconciliation
loops and process lifecycle (PID file, signal handling) are new here."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import tempfile
import threading
import time
from contextlib import ExitStack
from concurrent.futures import Future
from typing import Callable
from pathlib import Path
from repowatch.backup import backup_once
from repowatch.config.models import Config
from repowatch.errors import ConfigError
from repowatch.nginx.apply import apply as nginx_apply
from repowatch.runtime.context import ServiceState
from repowatch.runtime.scheduler import run_forever

logger = logging.getLogger(__name__)


def write_pidfile(path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(str(os.getpid()))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def remove_pidfile(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)


async def _sleep_or_stop(stop: asyncio.Event, interval_seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
    except asyncio.TimeoutError:
        pass


async def backup_loop(stop: asyncio.Event, state_db: Path, backup_dir: Path,
                       interval_seconds: float, retention_days: int) -> None:
    while not stop.is_set():
        try:
            path = await asyncio.to_thread(backup_once, state_db, backup_dir, retention_days)
            logger.info('backup: %s', path)
        except Exception:
            logger.exception('scheduled backup failed')
        await _sleep_or_stop(stop, interval_seconds)


async def nginx_loop(stop: asyncio.Event, config_path: str, policy_path: str,
                      interval_seconds: float) -> None:
    while not stop.is_set():
        try:
            changed = await asyncio.to_thread(
                nginx_apply, config_path, policy_path, use_systemctl=False,
            )
            if changed:
                logger.info('nginx configuration reconciled and reloaded')
        except Exception:
            logger.exception('scheduled nginx reconciliation failed')
        await _sleep_or_stop(stop, interval_seconds)


async def supervise(
    config_path: str, config: Config, store: ServiceState, *,
    pid_file: str | None = None,
    backup_dir: str | None = None,
    backup_interval_hours: float = 24,
    backup_retention_days: int = 14,
    nginx: bool = False,
    nginx_policy: str | None = None,
    nginx_interval_seconds: float = 15,
    listeners: Listeners | None = None,
) -> None:
    if nginx and not nginx_policy:
        raise ConfigError('supervise --nginx requires --nginx-policy')
    if nginx and os.geteuid() != 0:
        raise ConfigError('supervise --nginx requires root; run it as a separate root '
                           'process, or omit --nginx and apply nginx changes another way '
                           '(see docs/deployment.md)')

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGTERM, signal.SIGINT)
    previous_signals = {sig: signal.getsignal(sig) for sig in signals}
    tasks = []
    try:
        for sig in signals:
            loop.add_signal_handler(sig, stop.set)
        if pid_file:
            write_pidfile(pid_file)
        tasks.append(asyncio.create_task(run_forever(config_path, store, initial_config=config), name='watcher'))
        if listeners is not None:
            tasks.append(asyncio.create_task(listeners.wait(), name='listeners'))
        if backup_dir:
            tasks.append(asyncio.create_task(backup_loop(
                stop, config.state_db, Path(backup_dir),
                backup_interval_hours * 3600, backup_retention_days,
            )))
        if nginx:
            tasks.append(asyncio.create_task(nginx_loop(
                stop, config_path, nginx_policy, nginx_interval_seconds,
            )))
        stopped = asyncio.create_task(stop.wait(), name='stop-signal')
        tasks.append(stopped)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            if task is not stopped and task in done:
                # Retrieve the original exception; a normal early exit is also fatal.
                task.result()
                if not stop.is_set():
                    raise RuntimeError(f'{task.get_name()} stopped unexpectedly')
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for sig in signals:
            loop.remove_signal_handler(sig)
            signal.signal(sig, previous_signals[sig])
        if pid_file:
            remove_pidfile(pid_file)


class Listeners:
    """Own prepared sockets and observe every mandatory listener thread."""

    def __init__(self) -> None:
        self.resources = ExitStack()
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.results: dict[str, Future] = {}
        self.httpd = None

    def start(self, name: str, function: Callable[[], None]) -> None:
        """Publish both exceptional and normal exits to the service monitor."""
        result = Future()
        self.results[name] = result
        def run() -> None:
            try:
                function()
            except BaseException as exc:
                result.set_exception(exc)
            else:
                result.set_result(None)
        thread = threading.Thread(target=run, name=name, daemon=True)
        thread.start()
        self.threads.append(thread)

    async def wait(self) -> None:
        """Fail the service if any mandatory listener stops, even without an error."""
        while True:
            for name, result in self.results.items():
                if result.done():
                    raise RuntimeError(f'{name} listener stopped unexpectedly') from result.exception()
            await asyncio.sleep(0.1)

    def close(self) -> None:
        """Stop listener loops before releasing their sockets."""
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=2)
        self.resources.close()

    def __enter__(self) -> Listeners:
        return self

    def __exit__(self, *args) -> None:
        self.close()


def start_listeners(config_path: str, config: Config, store: ServiceState) -> Listeners:
    """Bind every socket before starting threads; unwind partial startup on failure."""
    from repowatch.web.server import create_server
    from repowatch.runtime.syslog import open_socket, run_listener

    listeners = Listeners()
    try:
        listeners.httpd = listeners.resources.enter_context(create_server(config, store, config_path))
        sock = (listeners.resources.enter_context(open_socket(config))
                if config.syslog_listener.enabled else None)
        listeners.httpd.timeout = 0.2
        def serve_http() -> None:
            while not listeners.stop.is_set():
                listeners.httpd.handle_request()
        listeners.start('http', serve_http)
        if sock is not None:
            ready = threading.Event()
            listeners.start('syslog', lambda: run_listener(
                config_path, config, store, sock=sock, stop=listeners.stop, ready=ready))
            deadline = time.monotonic() + 30
            while not ready.wait(0.05):
                if listeners.results['syslog'].done():
                    raise RuntimeError('syslog listener failed during startup') from listeners.results['syslog'].exception()
                if time.monotonic() >= deadline:
                    raise RuntimeError('syslog listener startup timed out')
        return listeners
    except BaseException:
        listeners.close()
        raise
