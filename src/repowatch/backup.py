"""Pure-Python online backup of state.sqlite3 — for repowatch supervise/backup
(see runtime/service.py, cli/main.py), which must not assume the `scripts/` tree (and
its `sqlite3`/`gzip` CLI binaries) is present on disk, unlike the systemd
timer path (scripts/backup-state.sh, left untouched, still used there).

Same operation as backup-state.sh: SQLite's own online backup API (not a
plain file copy, which risks capturing the file mid-transaction/WAL), then
gzip, then age-based rotation of old backups."""

from __future__ import annotations

import gzip
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_RETENTION_SECONDS_PER_DAY = 86400


def backup_once(state_db: Path, backup_dir: Path, retention_days: int) -> Path:
    """Back up state_db into backup_dir as a gzip-compressed, timestamped
    copy, then delete backups in backup_dir older than retention_days.
    Returns the path of the new backup."""
    state_db = Path(state_db)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    dest = backup_dir / f'state-{timestamp}.sqlite3'
    dest_gz = dest.with_suffix(dest.suffix + '.gz')

    source = sqlite3.connect(str(state_db))
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    with open(dest, 'rb') as raw, gzip.open(dest_gz, 'wb') as compressed:
        while chunk := raw.read(1024 * 1024):
            compressed.write(chunk)
    dest.unlink()

    _prune(backup_dir, retention_days)
    return dest_gz


def _prune(backup_dir: Path, retention_days: int) -> None:
    cutoff = time.time() - retention_days * _RETENTION_SECONDS_PER_DAY
    for candidate in backup_dir.glob('state-*.sqlite3.gz'):
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
        except OSError:
            logger.warning('failed to prune old backup %s', candidate, exc_info=True)
