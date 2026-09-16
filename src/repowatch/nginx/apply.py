"""Publish nginx configuration transactionally with validation and rollback."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from repowatch.config.load import load_config
from repowatch.errors import ConfigError
from repowatch.nginx.policy import _check_policy_owner
from repowatch.nginx.render import render, render_purge, render_probe_js, render_probe_conf, resolve_dedup_pairs, render_dedup
from repowatch.storage.cache import CacheStore
from repowatch.storage.database import Database

def _write(path: Path, data: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _reload(nginx: str, nginx_conf: str, *, use_systemctl: bool) -> None:
    if use_systemctl:
        subprocess.run(['systemctl', 'reload', 'nginx.service'], check=True, capture_output=True, text=True, timeout=30)
    else:
        subprocess.run([nginx, '-s', 'reload', '-c', nginx_conf], check=True, capture_output=True, text=True, timeout=30)


def apply(config_path: str, policy_path: str, *, force: bool = False, use_systemctl: bool = True) -> bool:
    """Root timer/manual CLI share one transaction. No shell or writable code.

    active/previous/status/lock live in a root-owned directory outside the
    service's writable config/state directories. Site symlink is installed once
    by the operator. A failed candidate never replaces the last working backup.

    use_systemctl=False reloads nginx directly (`nginx -s reload`) instead of
    going through `systemctl reload nginx.service` — for the no-systemd
    standalone path (see runtime/service.py); the systemd-managed CLI/timer path
    keeps the default.
    """
    policy_file = Path(policy_path)
    _check_policy_owner(policy_file)
    policy = json.loads(policy_file.read_text())
    directory = policy_file.parent
    active = directory / 'active.conf'
    previous = directory / 'previous.conf'
    purge_path = directory / 'purge.conf'
    previous_purge = directory / 'previous-purge.conf'
    dedup_path = directory / 'dedup.map'
    previous_dedup = directory / 'previous-dedup.map'
    probe_conf_path = directory / 'probe.conf'
    previous_probe_conf = directory / 'previous-probe.conf'
    probe_js_path = directory / 'probe.js'
    previous_probe_js = directory / 'previous-probe.js'
    status = directory / 'status.json'
    link = Path(policy['site_link'])
    if not link.is_symlink() or link.resolve() != active.resolve():
        raise ConfigError('nginx site symlink does not point to the managed active.conf')
    with (directory / 'apply.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = load_config(config_path)
        if not config.nginx.enabled:
            return False
        if config.nginx.cache_dir is not None and config.nginx.cache_dir != policy['cache_dir']:
            raise ConfigError(
                f"config.yaml declares nginx.cache_dir={config.nginx.cache_dir!r}, but the "
                f"installed policy actually uses cache_dir={policy['cache_dir']!r} — "
                f"these must match (see docs/configuration.md#nginx). Either update "
                f"nginx.cache_dir in config.yaml to the real path, or change the real one with "
                f"CACHE_DIR=... make install && sudo make activate."
            )
        candidate = render(config, cache_dir=policy['cache_dir'], access_log=policy['access_log'],
                            purge_conf=str(purge_path), dedup_conf=str(dedup_path),
                            probe_conf=str(probe_conf_path), probe_js=str(probe_js_path))
        # render_purge()/render_dedup()/render_probe_conf()/render_probe_js()
        # are written unconditionally (empty/comment-only when their flag is
        # off) — render() only ever `include`s/`js_import`s them when the
        # corresponding flag is on, so an idle file on disk changes nothing;
        # this avoids conditionally creating/deleting a file across toggles.
        purge_candidate = render_purge(config)
        # find_duplicate_files() is a real query over repo_packages — only
        # run it when dedup is actually on, so leaving it off costs nothing
        # extra on every apply cycle (this timer runs every 15s).
        dedup_rows = (CacheStore(Database(config.state_db, read_only=True)).find_duplicate_files()
                      if config.nginx.enable_dedup and config.state_db.is_file() else [])
        dedup_candidate = render_dedup(config, resolve_dedup_pairs(config, dedup_rows))
        probe_conf_candidate = render_probe_conf(config)
        probe_js_candidate = render_probe_js(config, cache_dir=policy['cache_dir'])
        digest = hashlib.sha256((
            candidate + purge_candidate + dedup_candidate + probe_conf_candidate + probe_js_candidate
        ).encode()).hexdigest()
        old = active.read_text() if active.exists() else None
        old_purge = purge_path.read_text() if purge_path.exists() else None
        old_dedup = dedup_path.read_text() if dedup_path.exists() else None
        old_probe_conf = probe_conf_path.read_text() if probe_conf_path.exists() else None
        old_probe_js = probe_js_path.read_text() if probe_js_path.exists() else None
        if (old == candidate and old_purge == purge_candidate and old_dedup == dedup_candidate
                and old_probe_conf == probe_conf_candidate and old_probe_js == probe_js_candidate
                and not force):
            return False
        nginx = policy['nginx_binary']
        command = [nginx, '-t', '-c', policy['nginx_conf']]
        files = [(purge_path, purge_candidate, old_purge),
                 (dedup_path, dedup_candidate, old_dedup),
                 (probe_conf_path, probe_conf_candidate, old_probe_conf),
                 (probe_js_path, probe_js_candidate, old_probe_js),
                 (active, candidate, old)]
        published = []
        try:
            for path, content, original in files:
                _write(path, content)
                published.append((path, original))
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
            _reload(nginx, policy['nginx_conf'], use_systemctl=use_systemctl)
        except (OSError, subprocess.SubprocessError) as exc:
            logging.getLogger(__name__).error('nginx apply failed: %s', getattr(exc, 'stderr', str(exc)))
            rollback_errors = []
            for path, original in reversed(published):
                try:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        _write(path, original)
                except OSError as rollback:
                    rollback_errors.append(rollback)
            if not rollback_errors and old is not None:
                # Reload may have partially succeeded before its command failed.
                try:
                    subprocess.run(command, check=True, capture_output=True, timeout=30)
                    _reload(nginx, policy['nginx_conf'], use_systemctl=use_systemctl)
                except (OSError, subprocess.SubprocessError) as rollback:
                    rollback_errors.append(rollback)
            try:
                _write(status, json.dumps({'ok': False, 'candidate': digest,
                                          'rollback_failed': bool(rollback_errors)}) + '\n')
            except OSError:
                logging.getLogger(__name__).exception('failed to write nginx apply status')
            if rollback_errors:
                raise ConfigError('nginx apply and rollback failed; inspect journal') from rollback_errors[0]
            raise ConfigError('nginx apply failed; previous configuration restored') from exc
        if old is not None:
            _write(previous, old)
        if old_purge is not None:
            _write(previous_purge, old_purge)
        if old_dedup is not None:
            _write(previous_dedup, old_dedup)
        if old_probe_conf is not None:
            _write(previous_probe_conf, old_probe_conf)
        if old_probe_js is not None:
            _write(previous_probe_js, old_probe_js)
        _write(status, json.dumps({'ok': True, 'active': digest}) + '\n')
        return True
