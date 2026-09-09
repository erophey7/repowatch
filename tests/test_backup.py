import gzip
import os
import sqlite3
import time

from repowatch.backup import backup_once


def _make_state_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute('CREATE TABLE t (x INTEGER)')
    conn.execute('INSERT INTO t VALUES (1)')
    conn.commit()
    conn.close()


def test_backup_once_produces_valid_gzipped_sqlite(tmp_path):
    state_db = tmp_path / 'state.sqlite3'
    _make_state_db(state_db)
    backup_dir = tmp_path / 'backups'

    result = backup_once(state_db, backup_dir, retention_days=14)

    assert result.parent == backup_dir
    assert result.name.endswith('.sqlite3.gz')
    with gzip.open(result, 'rb') as compressed:
        restored = backup_dir / 'restored.sqlite3'
        restored.write_bytes(compressed.read())
    conn = sqlite3.connect(str(restored))
    assert conn.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    assert conn.execute('SELECT x FROM t').fetchall() == [(1,)]
    conn.close()


def test_backup_once_prunes_old_backups_but_keeps_new_ones(tmp_path):
    state_db = tmp_path / 'state.sqlite3'
    _make_state_db(state_db)
    backup_dir = tmp_path / 'backups'
    backup_dir.mkdir()

    old = backup_dir / 'state-old.sqlite3.gz'
    old.write_bytes(b'old')
    old_time = time.time() - 30 * 86400
    os.utime(old, (old_time, old_time))

    recent = backup_dir / 'state-recent.sqlite3.gz'
    recent.write_bytes(b'recent')
    recent_time = time.time() - 1 * 86400
    os.utime(recent, (recent_time, recent_time))

    backup_once(state_db, backup_dir, retention_days=14)

    assert not old.exists()
    assert recent.exists()


def test_backup_once_creates_backup_dir(tmp_path):
    state_db = tmp_path / 'state.sqlite3'
    _make_state_db(state_db)
    backup_dir = tmp_path / 'nested' / 'backups'

    result = backup_once(state_db, backup_dir, retention_days=14)

    assert result.exists()
