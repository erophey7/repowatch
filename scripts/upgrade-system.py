#!/usr/bin/env python3
"""Offline system upgrade with an exact installation/SQLite rollback.

Run from the reviewed release source with a target-compatible wheelhouse.
No network, /opt assumptions, cache deletion or unrelated nginx tree restore.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import json
import importlib.util
import logging
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

from install import Layout, absolute, check, install, activate

UNITS = ('repowatch.service', 'repowatch-status.service', 'repowatch-check.timer',
         'repowatch-check.service', 'repowatch-backup.timer', 'repowatch-backup.service',
         'repowatch-nginx.timer', 'repowatch-nginx.service')


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def service_state():
    result = {}
    for unit in (*UNITS, 'nginx.service'):
        active = subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True).stdout.strip()
        enabled = subprocess.run(['systemctl', 'is-enabled', unit], capture_output=True, text=True).stdout.strip()
        if active in ('activating', 'deactivating', 'reloading'):
            raise ValueError(f'{unit} is transitioning; retry after it finishes')
        result[unit] = {'active': active == 'active', 'enabled': enabled}
    return result


def plan(layout, wheelhouse):
    """Read-only preconditions for `make upgrade-plan` (no `--apply`) —
    added 2026-09-14 after a documentation-accuracy review found the docs
    claimed this ran "the same validation upgrade --apply would", when the
    actual code did nothing beyond printing the resolved manifest paths:
    exit 0 even with a missing config.yaml, missing venv, and missing
    wheelhouse (reproduced by hand before this existed).

    This is still NOT the full validation --apply performs — it doesn't
    build a candidate venv or actually resolve the new release's
    dependencies, since that needs real disk I/O this dry-run mode
    shouldn't require. It's a genuine, if cheaper, set of checks: does the
    config this upgrade would use actually load in the CURRENTLY installed
    venv, does that venv exist at all, does the wheelhouse have exactly one
    application wheel, and are the services --apply requires to already be
    running actually running. Returns a list of problem strings — empty
    means these preconditions look fine, not that the upgrade is
    guaranteed to succeed.
    """
    problems = []
    python = layout.venv / 'bin/python'
    if not python.is_file():
        problems.append(f'installed venv not found: {layout.venv}')
    elif not layout.config.is_file():
        problems.append(f'config.yaml not found: {layout.config}')
    else:
        try:
            probe(python, layout.config)
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            problems.append(f'config.yaml does not load in the installed venv: {exc}')
    app = list(wheelhouse.glob('repowatch-*.whl')) if wheelhouse.is_dir() else []
    if len(app) != 1:
        problems.append(f'wheelhouse must contain exactly one repowatch-*.whl, found {len(app)}: {wheelhouse}')
    if layout.with_nginx and not shutil.which('nginx'):
        problems.append('nginx not found on PATH but with_nginx is set')
    try:
        states = service_state()
    except ValueError as exc:
        problems.append(str(exc))
    else:
        if not states.get('repowatch.service', {}).get('active'):
            problems.append('repowatch.service is not active — upgrade --apply requires it running first')
        if layout.with_nginx and not states.get('nginx.service', {}).get('active'):
            problems.append('nginx.service is not active — upgrade --apply requires it running first')
    return problems


def stop():
    # Stop schedules first; then drain even a oneshot that fired after inspection.
    loaded = [u for u in UNITS if subprocess.run(['systemctl', 'show', '-p', 'LoadState', '--value', u], capture_output=True, text=True).stdout.strip() not in ('not-found', '')]
    for suffix in ('.timer', '.service'):
        selected = [u for u in loaded if u.endswith(suffix)]
        if selected: run(['systemctl', 'stop', *selected])


def resume(states):
    for unit, state in states.items():
        enabled = state['enabled']
        if enabled in ('enabled', 'enabled-runtime'):
            run(['systemctl', 'enable', *(['--runtime'] if enabled.endswith('-runtime') else []), unit])
        elif enabled == 'disabled':
            run(['systemctl', 'disable', unit])
    wanted = [u for u, state in states.items() if state['active']]
    if wanted:
        run(['systemctl', 'start', *wanted])


def targets(layout, source):
    paths = [layout.venv.parent, layout.share, layout.libexec, layout.cli, layout.config.parent]
    names = {p.name for p in (layout.share / 'systemd').iterdir()}
    names.update(p.name for p in (source / 'systemd').iterdir())
    paths.extend(Path(layout.systemd_unit_dir) / n for n in sorted(names))
    if layout.with_nginx:
        paths.extend([layout.nginx_policy.parent, Path(layout.nginx_enabled_dir) / 'repowatch.conf'])
    return paths


def snapshot_files(paths, backup):
    present = []
    with tarfile.open(backup / 'installation.tar.gz', 'w:gz') as archive:
        for path in paths:
            if path.exists() or path.is_symlink():
                archive.add(path, arcname=str(path).lstrip('/'))
                present.append(str(path))
    (backup / 'paths.json').write_text(json.dumps({'targets': list(map(str, paths)), 'present': present}))


def restore_files(paths, backup):
    for path in paths:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    # This archive was created by this process in a private root-owned directory,
    # never accepted from a remote request or user-selected archive.
    with tarfile.open(backup / 'installation.tar.gz') as archive:
        archive.extractall('/', filter='fully_trusted')


def database_backup(db, backup):
    metadata = db.stat()
    with sqlite3.connect(db) as src, sqlite3.connect(backup / 'state.sqlite3') as dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('database integrity check failed')
    return {'uid': metadata.st_uid, 'gid': metadata.st_gid, 'mode': metadata.st_mode & 0o777}


def restore_database(db, backup, metadata):
    for suffix in ('-wal', '-shm'):
        Path(str(db) + suffix).unlink(missing_ok=True)
    shutil.copy2(backup / 'state.sqlite3', db)
    os.chown(db, metadata['uid'], metadata['gid'])
    os.chmod(db, metadata['mode'])


def probe(python, config):
    # Read config only, never instantiate ServiceState against the live database.
    code = ('import json,sys; from repowatch.config import load_config; '
            'c=load_config(sys.argv[1]); print(json.dumps({"db":str(c.state_db),'
            '"bind":c.status_server.bind,"port":c.status_server.port,'
            '"tls":bool(c.status_server.tls_cert_path)}))')
    return json.loads(run([python, '-B', '-c', code, config], capture_output=True, text=True).stdout)


def smoke(layout, info):
    run([layout.venv / 'bin/python', '-m', 'pip', 'check'])
    # A dedicated public health endpoint may be 503 for stale upstreams. Both
    # codes establish that our HTTP service answers; malformed JSON does not.
    host = info['bind']
    if host in ('0.0.0.0', '::'): host = '127.0.0.1'
    if ':' in host: host = '[' + host + ']'
    import ssl
    import urllib.error
    context = ssl._create_unverified_context() if info['tls'] else None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPSHandler(context=context))
    url = f'{"https" if info["tls"] else "http"}://{host}:{info["port"]}/healthz'
    for attempt in range(30):
        try:
            try:
                response = opener.open(url, timeout=2)
            except urllib.error.HTTPError as exc:
                if exc.code != 503: raise
                response = exc
            with response:
                payload = json.load(response)
                if not isinstance(payload, dict) or 'healthy' not in payload:
                    raise ValueError('invalid health response')
            break
        except (OSError, ValueError):
            if attempt == 29: raise
            time.sleep(.2)
    with sqlite3.connect(info['db']) as conn:
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('upgraded database integrity check failed')


def upgrade(layout, source, wheels):
    if layout.destdir or not layout.with_systemd:
        raise ValueError('upgrade requires an activated systemd installation, without DESTDIR')
    if os.geteuid() != 0:
        raise ValueError('upgrade requires root')
    if check(layout, for_install=False).failures:
        raise ValueError('preflight failed')
    states = service_state()
    if layout.with_nginx and not states.get('nginx.service', {}).get('active'):
        raise ValueError('nginx.service must be active before automated upgrade')
    if not states['repowatch.service']['active']:
        raise ValueError('repowatch.service must be active before automated upgrade')
    original = layout.config.read_bytes()
    info = probe(layout.venv / 'bin/python', layout.config)
    db = absolute(info['db'])
    paths = targets(layout, source)
    if any(db == p or p in db.parents for p in paths):
        raise ValueError('state_db must be outside installation/config directories')
    # Resolve every runtime dependency and validate the candidate BEFORE downtime.
    with tempfile.TemporaryDirectory(prefix='repowatch-upgrade-') as temporary:
        candidate = Path(temporary) / 'venv'
        run([sys.executable, '-m', 'venv', candidate])
        app = list(wheels.glob('repowatch-*.whl'))
        if len(app) != 1: raise ValueError('exactly one application wheel required')
        run([candidate / 'bin/python', '-m', 'pip', 'install', '--no-index', '--find-links', wheels, app[0]])
        if probe(candidate / 'bin/python', layout.config) != info:
            raise ValueError('candidate changes installation/config interpretation')
        run([candidate / 'bin/python', '-m', 'pip', 'check'])
        backup = layout.state / 'backups' / ('upgrade-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f'))
        backup.mkdir(mode=0o700)
        print(f'BACKUP {backup}', flush=True)
        metadata = None
        files_saved = False
        try:
            stop()
            # Service-writable YAML is captured only after all writers stop.
            if layout.config.read_bytes() != original:
                raise ValueError('configuration changed during preparation; retry')
            snapshot_files(paths, backup)
            files_saved = True
            metadata = database_backup(db, backup)
            (backup / 'metadata.json').write_text(json.dumps({'layout': asdict(layout), 'services': states,
                'database': str(db), 'database_metadata': metadata}, indent=2))
            install(layout, source, wheels)
            if layout.config.read_bytes() != original:
                raise ValueError('installer changed existing YAML')
            activate(layout)
            if layout.with_nginx:
                run([layout.cli, '-c', layout.config, 'nginx-apply', '--policy', layout.nginx_policy])
            smoke(layout, info)
            # Restore prior enablement and leave previously inactive units stopped.
            stop(); resume(states)
            smoke(layout, info)
            print(f'UPGRADE_OK {backup}', flush=True)
        except BaseException:
            print(f'ROLLBACK {backup}', flush=True)
            stop()
            if files_saved: restore_files(paths, backup)
            if metadata: restore_database(db, backup, metadata)
            run(['systemctl', 'daemon-reload'])
            if layout.with_nginx:
                run(['nginx', '-t', '-c', layout.nginx_conf])
                run(['systemctl', 'reload', 'nginx'])
            resume(states)
            raise


def ensure_interpreter(layout):
    """Ubuntu may provide venv/ensurepip but omit pip from system Python."""
    if importlib.util.find_spec('pip') is not None:
        return
    installed = layout.venv / 'bin/python'
    if not installed.is_file() or os.path.abspath(sys.executable) == str(installed):
        raise ValueError('pip unavailable in both current and installed Python')
    os.execv(str(installed), [str(installed), *sys.argv])


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path('/usr/local/share/repowatch/install.json'))
    parser.add_argument('--wheelhouse', type=Path, required=True)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--apply', action='store_true', help='perform upgrade; otherwise print plan only')
    args = parser.parse_args()
    layout = Layout(**json.loads(args.manifest.read_text()))
    for value in asdict(layout).values():
        if isinstance(value, str) and value: absolute(value)
    if args.manifest.resolve() != (layout.share / 'install.json').resolve():
        parser.error('manifest location does not match its prefix')
    print(json.dumps({'layout': asdict(layout), 'wheelhouse': str(args.wheelhouse.resolve()),
                      'source': str(args.source.resolve()), 'apply': args.apply}, indent=2))
    if args.apply:
        ensure_interpreter(layout)
        # Serialize upgrades independently from nginx-apply's own lock.
        import hashlib
        lock_path = Path('/run/lock') / ('repowatch-upgrade-' + hashlib.sha256(layout.prefix.encode()).hexdigest()[:16] + '.lock')
        with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def interrupted(signum, frame): raise KeyboardInterrupt(f'signal {signum}')
            signal.signal(signal.SIGTERM, interrupted)
            upgrade(layout, args.source.resolve(), args.wheelhouse.resolve())
    else:
        problems = plan(layout, args.wheelhouse.resolve())
        if problems:
            for problem in problems:
                print(f'PROBLEM {problem}')
            sys.exit(1)
        print('PLAN_OK')


if __name__ == '__main__':
    main()
