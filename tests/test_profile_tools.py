"""Operational profiling stays bounded and cleans up its temporary authorization."""
import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('load_live',ROOT / 'scripts/load-live.py')
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


@pytest.mark.parametrize('url',['https://example.com','http://localhost:8085',
    'http://127.0.0.1:8085/api','http://user:secret@127.0.0.1','http://127.0.0.1?x=1'])
def test_load_rejects_non_loopback_or_ambiguous_origins(url):
    with pytest.raises(argparse.ArgumentTypeError):
        live.loopback_url(url)


def test_temporary_load_session_cleanup_preserves_existing_sessions(tmp_path):
    db = tmp_path / 'state.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE admin_sessions(secret_hash TEXT PRIMARY KEY,password_fingerprint TEXT,expires_at REAL,secure INTEGER)')
        conn.execute("INSERT INTO admin_sessions VALUES ('existing','fingerprint',9999999999,0)")
    config = SimpleNamespace(state_db=db,admin_password_hash='test-fingerprint-input')
    with pytest.raises(RuntimeError):
        with live.temporary_session(config) as cookie:
            assert cookie.startswith(live.COOKIE_NAME+'=')
            with sqlite3.connect(db) as conn:
                assert conn.execute('SELECT COUNT(*) FROM admin_sessions').fetchone()[0] == 2
            raise RuntimeError('interrupted workload')
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT secret_hash FROM admin_sessions').fetchall() == [('existing',)]
        assert conn.execute('SELECT COUNT(*) FROM sqlite_master WHERE type="table"').fetchone()[0] == 1


def test_live_load_aborts_when_health_fails(monkeypatch,capsys):
    monkeypatch.setattr(live,'process_sample',lambda pid: {'cpu_ticks':0})
    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(200,json={'healthy':False}))
        async with httpx.AsyncClient(transport=transport) as client, httpx.AsyncClient(
                transport=transport,base_url='http://127.0.0.1') as health:
            with pytest.raises(RuntimeError,match='load stopped'):
                await live.run_case(client,health,'http://127.0.0.1',['/status.json'],
                    'not-a-secret','status',1,.05,100,1,2)
    asyncio.run(run())
    result = json.loads(capsys.readouterr().out)
    assert result['errors'] == {'health_ValueError':1}


def test_synthetic_profiler_writes_only_temporary_state_and_profile_output(tmp_path):
    result = subprocess.run([sys.executable,str(ROOT/'scripts/profile-workloads.py'),
        '--samples','1','--synthetic-packages','20','--profile-dir',str(tmp_path/'profiles')],
        check=True,capture_output=True,text=True,timeout=30)
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert rows[-1]['kind'] == 'complete'
    assert {row['name'] for row in rows if 'name' in row} == {
        'synthetic_apt_gzip_parse','synthetic_gentoo_parse',
        'synthetic_snapshot_unchanged','synthetic_snapshot_100_replacements'}
    assert len(list((tmp_path/'profiles').glob('*.prof'))) == 4


def test_isolated_load_seed_uses_current_warm_schema(tmp_path):
    spec = importlib.util.spec_from_file_location('load_test', ROOT / 'scripts/load-test.py')
    load = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(load)
    store, config, snapshot = load.seed(tmp_path, repos=2, packages=4, events=3)
    assert len(config['repos']) == 2
    assert len(snapshot.packages) == 4
    with store.database.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM warmed_packages').fetchone()[0] == 4
        assert conn.execute('SELECT COUNT(*) FROM request_events').fetchone()[0] == 3
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
