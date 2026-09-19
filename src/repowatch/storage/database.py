"""SQLite connections, transaction ownership, and storage statistics."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from repowatch.storage.schema import SCHEMA, _migrate, _request_rollups, _search_index
from typing import Iterator

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """SQLite initialization and transaction ownership; no runtime resources."""

    def __init__(self, db_path: str | Path, *, read_only: bool = False):
        self.db_path = Path(db_path)
        self.search_index = False
        self.read_only = read_only
        if read_only:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            # A steady stream of read-API traffic in rollback-journal mode
            # blocked the writer until the sqlite timeout (5s): a load test
            # hit "database is locked" even at 100k packages. WAL lets
            # readers keep their own snapshot while watcher/syslog commit.
            # Enabled once when the store is opened; synchronous is left
            # at its default.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            _migrate(conn)
            _request_rollups(conn)
            self.search_index = _search_index(conn)


    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = (sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
                if self.read_only else sqlite3.connect(self.db_path))
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


    def get_storage_stats(self) -> dict:
        """docs_dev/ROADMAP.md item 27 — cheap, always-safe numbers about
        state_db itself: file size (a single stat(), not a walk) and a row
        count per table. Deliberately does NOT touch the nginx package cache
        directory — that's a potentially large filesystem walk, a different
        cost class, and repowatch may not even know the real path (see
        NginxConfig.cache_dir); see api.cache_dir_stats_payload for that,
        computed on demand, not on every poll of this one.
        """
        with self.connect() as conn:
            tables = {
                name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in ("repo_packages", "repo_events", "request_events", "warmed_packages", "prefetch_bans")
            }
        return {
            "state_db_bytes": self.db_path.stat().st_size,
            "tables": tables,
        }
