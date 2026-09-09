"""`repowatch supervise` — a single foreground process replacing all three
systemd units (repowatch.service, repowatch-nginx.timer,
repowatch-backup.timer) for environments without systemd (see
docs/deployment.md, "Running without systemd"). Reuses watcher.run_forever
and nginx.apply unchanged; only the periodic backup/nginx-reconciliation
loops and process lifecycle (PID file, signal handling) are new here.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import tempfile
from pathlib import Path

from repowatch.backup import backup_once
from repowatch.config import Config, ConfigError
from repowatch.nginx import apply as nginx_apply
from repowatch.state import StateStore
from repowatch.watcher import run_forever

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
    config_path: str, config: Config, store: StateStore, *,
    pid_file: str | None = None,
    backup_dir: str | None = None,
    backup_interval_hours: float = 24,
    backup_retention_days: int = 14,
    nginx: bool = False,
    nginx_policy: str | None = None,
    nginx_interval_seconds: float = 15,
) -> None:
    if nginx and not nginx_policy:
        raise ConfigError('supervise --nginx requires --nginx-policy')
    if nginx and os.geteuid() != 0:
        raise ConfigError('supervise --nginx requires root; run it as a separate root '
                           'process, or omit --nginx and apply nginx changes another way '
                           '(see docs/deployment.md)')

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    if pid_file:
        write_pidfile(pid_file)
    try:
        tasks = [asyncio.create_task(run_forever(config_path, store, initial_config=config))]
        if backup_dir:
            tasks.append(asyncio.create_task(backup_loop(
                stop, config.state_db, Path(backup_dir),
                backup_interval_hours * 3600, backup_retention_days,
            )))
        if nginx:
            tasks.append(asyncio.create_task(nginx_loop(
                stop, config_path, nginx_policy, nginx_interval_seconds,
            )))
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if pid_file:
            remove_pidfile(pid_file)
