"""Watcher state is stored in sqlite: one database for the whole service."""

from __future__ import annotations

import json
import base64
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_sessions (
    secret_hash TEXT PRIMARY KEY,
    password_fingerprint TEXT NOT NULL,
    expires_at REAL NOT NULL,
    secure INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS host_tokens (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    secret_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    expires_at REAL,
    last_used_at REAL,
    revoked_at REAL
);

CREATE TABLE IF NOT EXISTS repo_state (
    repo_id       TEXT PRIMARY KEY,
    last_check    TEXT NOT NULL,
    changed_at    TEXT,
    index_etag    TEXT,           -- index ETag from the previous check (for HEAD comparison)
    index_last_modified TEXT,     -- index Last-Modified from the previous check
    package_count INTEGER,
    key_expires_at TEXT           -- soonest GPG key expiry in this repo's keyring, from the
                                   -- last check cycle (see gpgverify.soonest_key_expiry); NULL
                                   -- for apk repos, unsigned repos, or when unknown
);

-- Current snapshot: the single source of truth for package data.
CREATE TABLE IF NOT EXISTS repo_packages (
    repo_id      TEXT NOT NULL,
    package_key  TEXT NOT NULL,
    package_name TEXT,
    filename     TEXT NOT NULL,
    content_hash TEXT,
    PRIMARY KEY (repo_id, package_key)
);

CREATE INDEX IF NOT EXISTS idx_repo_packages_name
    ON repo_packages(repo_id, package_name, package_key);

CREATE TABLE IF NOT EXISTS repo_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id    TEXT NOT NULL,
    ts         TEXT NOT NULL,
    new_pkgs_json TEXT NOT NULL,   -- list of package-version strings that appeared
    removed_pkgs_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_repo_events_repo_ts ON repo_events(repo_id, ts DESC);

CREATE TABLE IF NOT EXISTS request_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    repo_id          TEXT,           -- best-effort match by path prefix, may be NULL
    client_ip        TEXT,           -- $remote_addr from nginx, may be NULL (old log_format)
    method           TEXT NOT NULL,
    path             TEXT NOT NULL,
    status           TEXT,
    cache_status     TEXT,
    package_key      TEXT,           -- set only when exactly one repo's known packages
    package_repo_id  TEXT            -- contain this basename (see match_all_package_keys);
                                      -- independent of repo_id above, which is prefix-based
                                      -- and can disagree (e.g. NULL on an ambiguous shared
                                      -- pool/) or point at a different repo entirely
);

CREATE INDEX IF NOT EXISTS idx_request_events_repo_ts ON request_events(repo_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_request_events_ts ON request_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_request_events_repo_id ON request_events(repo_id, id DESC);
-- idx_request_events_package is created in _migrate(), not here: it
-- references package_key/package_repo_id, which on an existing (pre-item-19)
-- database only exist after _migrate()'s ALTER TABLE runs — see the
-- identical reasoning on idx_repo_packages_hash below.

-- Current warm state (not an event log — one row per package, upserted on
-- every warm attempt). Packages long gone from upstream are never written
-- again — without retention the row would stay forever, hence the
-- time-based cleanup on warmed_at (see StateStore.prune_warmed_packages),
-- same as repo_events/request_events.
CREATE TABLE IF NOT EXISTS warmed_packages (
    repo_id     TEXT NOT NULL,
    package_key TEXT NOT NULL,
    filename    TEXT NOT NULL,
    warmed_at   TEXT NOT NULL,
    status      TEXT NOT NULL,   -- "ok" | "failed"
    http_status INTEGER,
    source      TEXT,            -- "prefetch" (repowatch warmed it ahead of demand) |
                                  -- "client" (first seen via a real client request) |
                                  -- NULL for rows written before this column existed
    PRIMARY KEY (repo_id, package_key)
);

CREATE INDEX IF NOT EXISTS idx_warmed_packages_repo ON warmed_packages(repo_id, warmed_at DESC);
CREATE INDEX IF NOT EXISTS idx_warmed_page ON warmed_packages(repo_id, warmed_at DESC, package_key DESC);

-- Ban on auto-warming by package name (not by a specific version — otherwise
-- the ban wouldn't survive a new release). Checked for both automatic and
-- manual warming (see prefetch.warm_cache) — the ban behaves the same
-- everywhere, no surprises.
CREATE TABLE IF NOT EXISTS prefetch_bans (
    repo_id      TEXT NOT NULL,
    package_name TEXT NOT NULL,
    banned_at    TEXT NOT NULL,
    PRIMARY KEY (repo_id, package_name)
);

-- Current "consecutive failure streak" state for a (repo_id, kind) pair —
-- not a log (that's repo_events), just the current counter, see
-- notifications.py. kind: "prefetch" (at least one failure in the last
-- warm_cache run) | "gpg" (SignatureError while checking the index).
-- The row is deleted entirely on the first success after a streak (see
-- reset_failure) — there's no separate "consecutive_failures = 0"; a
-- failure streak only makes sense while it hasn't been broken.
-- notified — whether a notification was already sent for the CURRENT
-- streak, so we don't resend on every following failed cycle (see
-- notifications.record_failure_and_maybe_notify).
CREATE TABLE IF NOT EXISTS failure_state (
    repo_id              TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    last_failure_at      TEXT,
    notified             INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo_id, kind)
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RepoSnapshot:
    """Result of parsing one repository's index at the current moment."""

    repo_id: str
    # key — a unique package identifier, usually "name-version"
    packages: dict[str, str] = field(default_factory=dict)
    # the same key -> the "bare" package name (without version) — needed
    # separately because key = f"{name}-{version}" can't be split back
    # unambiguously (both name and version may contain hyphens). Used for
    # bans by package name.
    names: dict[str, str] = field(default_factory=dict)
    # the same key -> SHA256 hex digest, only for keys where PackageRef.content_hash
    # was known from the index (docs_dev/ROADMAP.md item 29) — missing keys
    # simply aren't candidates for dedup, not an error.
    content_hashes: dict[str, str] = field(default_factory=dict)


@dataclass
class DiffResult:
    repo_id: str
    changed: bool
    new_packages: list[str]
    removed_packages: list[str]
    # {package_key: filename} for exactly the packages in removed_packages —
    # captured from the previous snapshot before its rows are deleted below.
    # Needed by watcher.check_repo/prefetch.purge_removed (docs_dev/ROADMAP.md
    # item 24) to know which file to purge from nginx's cache — the key
    # alone isn't a filename.
    removed_filenames: dict[str, str]


class StateStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
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
            self.search_index = _search_index(conn)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record_snapshot(
        self,
        snapshot: RepoSnapshot,
        index_etag: str | None = None,
        index_last_modified: str | None = None,
    ) -> DiffResult:
        """Compare the new snapshot against the previous one, store it, and
        return the diff.

        index_etag/index_last_modified — response headers from the HEAD
        request to the index (see IndexParser.check_index_changed), saved so
        the next cycle can skip a full index download.
        """
        now = _utcnow()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT changed_at FROM repo_state WHERE repo_id = ?",
                (snapshot.repo_id,),
            ).fetchone()
            prev_packages = dict(conn.execute(
                "SELECT package_key, filename FROM repo_packages WHERE repo_id = ?", (snapshot.repo_id,)
            ))

            new_keys = set(snapshot.packages) - set(prev_packages)
            removed_keys = set(prev_packages) - set(snapshot.packages)
            changed = bool(new_keys or removed_keys)

            changed_at = now if changed else (row and _prev_changed_at(conn, snapshot.repo_id))

            conn.execute(
                """
                INSERT INTO repo_state
                    (repo_id, last_check, changed_at,
                     index_etag, index_last_modified, package_count)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id) DO UPDATE SET
                    last_check = excluded.last_check,
                    changed_at = CASE WHEN ? THEN excluded.last_check ELSE repo_state.changed_at END,
                    index_etag = excluded.index_etag,
                    index_last_modified = excluded.index_last_modified,
                    package_count = excluded.package_count
                """,
                (
                    snapshot.repo_id,
                    now,
                    changed_at,
                    index_etag,
                    index_last_modified,
                    len(snapshot.packages),
                    changed,
                ),
            )

            if removed_keys:
                conn.executemany(
                    "DELETE FROM repo_packages WHERE repo_id = ? AND package_key = ?",
                    ((snapshot.repo_id, key) for key in removed_keys),
                )
            conn.executemany(
                """
                INSERT INTO repo_packages (repo_id, package_key, package_name, filename, content_hash)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, package_key) DO UPDATE SET
                    package_name = excluded.package_name,
                    filename = excluded.filename,
                    content_hash = excluded.content_hash
                WHERE package_name IS NOT excluded.package_name OR filename IS NOT excluded.filename
                    OR content_hash IS NOT excluded.content_hash
                """,
                (
                    (snapshot.repo_id, key, snapshot.names.get(key), filename, snapshot.content_hashes.get(key))
                    for key, filename in snapshot.packages.items()
                ),
            )

            if changed:
                conn.execute(
                    """
                    INSERT INTO repo_events (repo_id, ts, new_pkgs_json, removed_pkgs_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        snapshot.repo_id,
                        now,
                        json.dumps(sorted(new_keys)),
                        json.dumps(sorted(removed_keys)),
                    ),
                )

            return DiffResult(
                repo_id=snapshot.repo_id,
                changed=changed,
                new_packages=sorted(new_keys),
                removed_packages=sorted(removed_keys),
                removed_filenames={key: prev_packages[key] for key in removed_keys},
            )

    def get_index_meta(self, repo_id: str) -> tuple[str | None, str | None]:
        """Index ETag/Last-Modified saved from the previous check."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT index_etag, index_last_modified FROM repo_state WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
        if not row:
            return None, None
        return row[0], row[1]

    def record_key_expiry(self, repo_id: str, expires_at: str | None) -> None:
        """Soonest GPG key expiry for this repo's keyring, from the current
        check cycle (see gpgverify.soonest_key_expiry, watcher.check_repo).
        An upsert, not a plain UPDATE — this can run before the first
        record_snapshot()/touch_last_check() of a brand-new repository ever
        creates its repo_state row (the key-expiry check runs unconditionally
        at the top of every cycle, independent of whether the index itself
        changed, so a quiet repository that rarely changes still gets its
        key checked on schedule). The placeholder last_check this INSERT
        branch writes is immediately superseded later in the same cycle by
        the real touch_last_check()/record_snapshot() call."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO repo_state (repo_id, last_check, key_expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(repo_id) DO UPDATE SET key_expires_at = excluded.key_expires_at
                """,
                (repo_id, _utcnow(), expires_at),
            )

    def touch_last_check(self, repo_id: str) -> None:
        """Update only last_check — used when a HEAD check showed the index
        is unchanged and the full cycle (download/parse/diff) was skipped."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE repo_state SET last_check = ? WHERE repo_id = ?",
                (_utcnow(), repo_id),
            )

    def get_repo_summaries(self, *, include_warmed: bool = False) -> dict[str, dict]:
        """Counts/dates in a single connection, without the last event's JSON.

        After the first import, last_new_packages contains the whole
        repository. The dashboard/health/metrics don't use that list:
        reading it through get_status meant parsing a million keys on every
        poll. The HTTP status.json also uses this compact selection.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT repo_id, last_check, changed_at, package_count, key_expires_at FROM repo_state"
            ).fetchall()
            warmed = dict(conn.execute("SELECT repo_id, COUNT(*) FROM warmed_packages GROUP BY repo_id")) if include_warmed else {}
        result = {row[0]: {"last_check": row[1], "changed_at": row[2], "package_count": row[3],
                            "key_expires_at": row[4]} for row in rows}
        if include_warmed:
            # Keep the count even for orphaned warmed rows without repo_state:
            # a per-repo count shouldn't require a snapshot to exist either.
            for repo_id in result.keys() | warmed.keys():
                result.setdefault(repo_id, {})["warmed_count"] = warmed.get(repo_id, 0)
        return result

    def get_storage_stats(self) -> dict:
        """docs_dev/ROADMAP.md item 27 — cheap, always-safe numbers about
        state_db itself: file size (a single stat(), not a walk) and a row
        count per table. Deliberately does NOT touch the nginx package cache
        directory — that's a potentially large filesystem walk, a different
        cost class, and repowatch may not even know the real path (see
        NginxConfig.cache_dir); see api.cache_dir_stats_payload for that,
        computed on demand, not on every poll of this one.
        """
        with self._connect() as conn:
            tables = {
                name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in ("repo_packages", "repo_events", "request_events", "warmed_packages", "prefetch_bans")
            }
        return {
            "state_db_bytes": self.db_path.stat().st_size,
            "tables": tables,
        }

    def get_status(self, repo_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_check, changed_at, package_count FROM repo_state WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
            if not row:
                return None
            last_check, changed_at, package_count = row

            last_event = conn.execute(
                """
                SELECT new_pkgs_json, removed_pkgs_json FROM repo_events
                WHERE repo_id = ? ORDER BY ts DESC LIMIT 1
                """,
                (repo_id,),
            ).fetchone()

            new_pkgs = json.loads(last_event[0]) if last_event else []
            removed_pkgs = json.loads(last_event[1]) if last_event else []

            return {
                "last_check": last_check,
                "changed_at": changed_at,
                "last_new_packages": new_pkgs,
                "last_removed_packages": removed_pkgs,
                # None for rows written before this column existed — see
                # _migrate and repos_list_payload (which has a fallback to
                # len(get_packages())).
                "package_count": package_count,
            }

    def record_warmed_package(
        self,
        repo_id: str,
        package_key: str,
        filename: str,
        ok: bool,
        http_status: int | None,
        source: str = "prefetch",
    ) -> None:
        """Record the outcome of one warm attempt for a single package.
        Called both when repowatch itself actively warmed it (source=
        "prefetch", see prefetch.py) and when a real client request was
        merely observed to succeed (source="client", see
        syslog_listener.py) — NOT a live check of the current nginx cache
        state either way (the file could have since been evicted by
        proxy_cache_path's inactive/max_size, see nginx.py).

        `source` is deliberately NOT overwritten on a later call for the
        same (repo_id, package_key): it records how this package FIRST
        entered warmed_packages, which is what
        StateStore.get_prefetch_efficiency needs — a package repowatch
        prefetched ahead of demand and only later happened to also be
        requested by a client is still a "prefetch" success, not
        reclassified as "client" just because a client eventually asked
        for it too."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO warmed_packages
                    (repo_id, package_key, filename, warmed_at, status, http_status, source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, package_key) DO UPDATE SET
                    filename = excluded.filename,
                    warmed_at = excluded.warmed_at,
                    status = excluded.status,
                    http_status = excluded.http_status
                """,
                (repo_id, package_key, filename, _utcnow(), "ok" if ok else "failed", http_status, source),
            )

    def get_warmed_packages(self, repo_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT package_key, filename, warmed_at, status, http_status
                FROM warmed_packages WHERE repo_id = ? ORDER BY warmed_at DESC
                """,
                (repo_id,),
            ).fetchall()
        return [
            {
                "package_key": key,
                "filename": filename,
                "warmed_at": warmed_at,
                "status": status,
                "http_status": http_status,
            }
            for key, filename, warmed_at, status, http_status in rows
        ]

    def get_names(self, repo_id: str) -> dict[str, str]:
        """{package_key: "bare package name"} — used for bans by name. An
        empty dict if there's no snapshot yet, or it predates this field."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT package_key, package_name FROM repo_packages
                WHERE repo_id = ? AND package_name IS NOT NULL
                """,
                (repo_id,),
            ).fetchall()
            return dict(rows)

    def find_duplicate_files(self) -> list[tuple[str, str, str]]:
        """Byte-identical files across DIFFERENT repositories (docs_dev/
        ROADMAP.md item 29), identified by (filename, content_hash) — not
        by package_key, since the same file can be named differently or
        carry a different key format between distros/formats.

        Returns (duplicate_repo_id, canonical_repo_id, filename) — one row
        per repository that's NOT the canonical copy for a given
        (filename, content_hash) group. "Canonical" is just the
        alphabetically-first repo_id sharing that pair — deterministic and
        stable as long as the same set of repos keeps carrying the file, no
        extra bookkeeping needed. Caller (nginx.py) resolves repo ids to
        actual local cache URIs — this module stays URL-agnostic.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT filename, content_hash, repo_id FROM repo_packages
                WHERE content_hash IS NOT NULL
                GROUP BY filename, content_hash, repo_id
                ORDER BY filename, content_hash, repo_id
                """
            ).fetchall()
        duplicates: list[tuple[str, str, str]] = []
        for _, group in groupby(rows, key=lambda row: (row[0], row[1])):
            members = list(group)
            if len(members) < 2:
                continue
            filename, _, canonical_repo_id = members[0]
            duplicates.extend((repo_id, canonical_repo_id, filename) for _, _, repo_id in members[1:])
        return duplicates

    def ban_package(self, repo_id: str, package_name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO prefetch_bans (repo_id, package_name, banned_at)
                VALUES (?, ?, ?)
                ON CONFLICT(repo_id, package_name) DO NOTHING
                """,
                (repo_id, package_name, _utcnow()),
            )

    def unban_package(self, repo_id: str, package_name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM prefetch_bans WHERE repo_id = ? AND package_name = ?",
                (repo_id, package_name),
            )

    def ban_packages(self, repo_id: str, package_names: list[str]) -> None:
        """Bulk form of ban_package (dashboard "ban selected") — one
        connection/transaction via executemany instead of one per name, so
        selecting hundreds of packages to ban doesn't cost hundreds of
        separate SQLite commits."""
        if not package_names:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO prefetch_bans (repo_id, package_name, banned_at)
                VALUES (?, ?, ?)
                ON CONFLICT(repo_id, package_name) DO NOTHING
                """,
                ((repo_id, name, _utcnow()) for name in package_names),
            )

    def unban_packages(self, repo_id: str, package_names: list[str]) -> None:
        """Bulk form of unban_package — see ban_packages."""
        if not package_names:
            return
        with self._connect() as conn:
            conn.executemany(
                "DELETE FROM prefetch_bans WHERE repo_id = ? AND package_name = ?",
                ((repo_id, name) for name in package_names),
            )

    def get_banned_packages(self, repo_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT package_name FROM prefetch_bans WHERE repo_id = ? ORDER BY package_name",
                (repo_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def get_ban_counts(self) -> dict[str, int]:
        """Number of banned-from-auto-warm packages per repo, one query for
        all repos (for api.metrics_payload — a per-repo loop calling
        get_banned_packages would be one query per repo)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT repo_id, COUNT(*) FROM prefetch_bans GROUP BY repo_id"
            ).fetchall()
        return {repo_id: count for repo_id, count in rows}

    def remove_warmed_package(self, repo_id: str, package_key: str) -> bool:
        """"Remove from warmed" — only repowatch's own bookkeeping (the
        warmed_packages entry); the actual file in the nginx cache is left
        untouched. Returns True if the row actually existed."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM warmed_packages WHERE repo_id = ? AND package_key = ?",
                (repo_id, package_key),
            )
            return cur.rowcount > 0

    def remove_warmed_packages(self, repo_id: str, package_keys: list[str]) -> int:
        """Bulk form of remove_warmed_package (dashboard "remove selected")
        — one connection/transaction via executemany. Returns how many rows
        actually existed and were removed (executemany's own cursor.rowcount
        is not reliable per-statement, so count with a preceding SELECT)."""
        if not package_keys:
            return 0
        with self._connect() as conn:
            existing = conn.execute(
                f"SELECT COUNT(*) FROM warmed_packages WHERE repo_id = ? "
                f"AND package_key IN ({','.join('?' for _ in package_keys)})",
                (repo_id, *package_keys),
            ).fetchone()[0]
            conn.executemany(
                "DELETE FROM warmed_packages WHERE repo_id = ? AND package_key = ?",
                ((repo_id, key) for key in package_keys),
            )
            return existing

    def find_stale_warmed(self, repo_id: str) -> list[dict]:
        """Manual cache purge (dashboard "Scan for stale entries",
        docs_dev/ROADMAP.md) — candidates for garbage: warmed_packages rows
        (repowatch believes/believed this file was fetched and cached at
        some point) whose package_key no longer exists in the CURRENT
        repo_packages snapshot for this repo (the package has since been
        removed from the upstream index). The combination of "known to have
        been cached" + "no longer a real package" is the best-supported
        "probably safe to purge" signal available without touching nginx's
        cache files directly (see CLAUDE.md's "Ключевые решения" #3) or
        risking a false read from a live HTTP probe (see prefetch.py).
        Does not confirm the file is STILL in nginx's cache right now —
        only an actual purge attempt (see prefetch.purge_selected) can tell
        that for certain; this is the candidate list shown to the operator
        before they decide what to actually purge.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT w.package_key, w.filename FROM warmed_packages w
                LEFT JOIN repo_packages p ON p.repo_id = w.repo_id AND p.package_key = w.package_key
                WHERE w.repo_id = ? AND p.package_key IS NULL
                ORDER BY w.package_key
                """,
                (repo_id,),
            ).fetchall()
        return [{"package_key": key, "filename": filename} for key, filename in rows]

    def get_warmed_filenames(self, repo_id: str, package_keys: list[str]) -> dict[str, str]:
        """{package_key: filename} for exactly the given keys — used to
        re-derive filenames server-side for a purge request instead of
        trusting whatever the client echoes back (see
        api.purge_selected_payload)."""
        if not package_keys:
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT package_key, filename FROM warmed_packages WHERE repo_id = ? "
                f"AND package_key IN ({','.join('?' for _ in package_keys)})",
                (repo_id, *package_keys),
            ).fetchall()
        return dict(rows)

    def get_packages(self, repo_id: str) -> dict[str, str] | None:
        """Full current package snapshot for a repository
        ({"name-version": filename}).

        None if this repo_id has never had a successful check.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM repo_state WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
            if not row:
                return None
            return dict(conn.execute(
                "SELECT package_key, filename FROM repo_packages WHERE repo_id = ?", (repo_id,)
            ))

    def get_packages_by_keys(
        self, repo_id: str, package_keys: list[str]
    ) -> dict[str, str] | None:
        """Fetch only the requested packages, without loading the full
        snapshot.

        None means the repository has never been successfully checked;
        unknown keys are simply absent from the result. Queries are chunked
        to stay under SQLite's parameter count limit.
        """
        keys = list(dict.fromkeys(package_keys))
        with self._connect() as conn:
            state_row = conn.execute(
                "SELECT 1 FROM repo_state WHERE repo_id = ?", (repo_id,)
            ).fetchone()
            if state_row is None:
                return None
            if not keys:
                return {}

            result: dict[str, str] = {}
            for start in range(0, len(keys), 500):
                chunk = keys[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT package_key, filename FROM repo_packages
                    WHERE repo_id = ? AND package_key IN ({placeholders})
                    """,
                    (repo_id, *chunk),
                ).fetchall()
                result.update(rows)

            return result

    def get_page(self, kind: str, repo_id: str | None = None, *,
                 q: str = "", limit: int = 100, cursor: str | None = None) -> dict:
        """Bounded keyset pagination; cursors are bound to resource and filter.

        Pages reflect live state. Requests have immutable ids; warmed rows
        updated between pages can move to the first page (refresh to see them).
        """
        if not 1 <= limit <= 200 or len(q) > 200:
            raise ValueError("limit must be 1..200; q must be at most 200 characters")
        specs = {
            "packages": ("repo_packages", "package_key, package_name, filename", ["package_key"], "ASC"),
            "warmed": ("warmed_packages", "package_key, filename, warmed_at, status, http_status", ["warmed_at", "package_key"], "DESC"),
            "requests": ("request_events", "id, ts, repo_id, client_ip, method, path, status, cache_status", ["id"], "DESC"),
        }
        table, columns, keys, direction = specs[kind]
        scope = [kind, repo_id, q]
        where, args = [], []
        if repo_id is not None:
            where.append("repo_id = ?")
            args.append(repo_id)
        if kind == "warmed" and q:
            # No FTS here — warmed_packages is bounded by the count of
            # packages ever actually warmed/downloaded, not the whole
            # upstream index, so a plain substring scan stays cheap.
            where.append("(instr(lower(package_key), lower(?)) > 0 OR instr(lower(filename), lower(?)) > 0)")
            args.extend([q, q])
        if kind == "packages" and q:
            if self.search_index and len(q) >= 3 and '\x00' not in q:
                # FTS narrows candidates; the original predicate below remains
                # authoritative (SQLite lower is ASCII, FTS folds more Unicode).
                where.append('rowid IN (SELECT rowid FROM package_search WHERE package_search MATCH ?)')
                args.append('"' + q.replace('"', '""') + '"')
            # Literal substring search: '%' and '_' are not wildcards.
            where.append("(instr(lower(package_key), lower(?)) > 0 OR instr(lower(COALESCE(package_name, '')), lower(?)) > 0)")
            args.extend([q, q])
        if cursor:
            try:
                if len(cursor) > 4096:
                    raise ValueError()
                data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                values = data["after"]
                if not isinstance(values, list):
                    raise ValueError()
                if data["scope"] != scope or len(values) != len(keys):
                    raise ValueError()
                expected = int if kind == "requests" else str
                if any(type(v) is not expected for v in values):
                    raise ValueError()
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                raise ValueError("Invalid cursor for this filter") from exc
            op = ">" if direction == "ASC" else "<"
            where.append(f"({', '.join(keys)}) {op} ({', '.join('?' for _ in keys)})")
            args.extend(values)
        # Do not let SQLite scan the entire (repo_id, package_key) index to
        # preserve ordering for a rare/no-match query. Probe FTS first. For a
        # very common term keep keyset scanning: enumerating/sorting all FTS
        # matches would cost more than stopping at the first page.
        if kind == 'packages' and q and self.search_index and len(q) >= 3 and '\x00' not in q:
            with self._connect() as conn:
                matches = conn.execute('SELECT rowid FROM package_search WHERE package_search MATCH ? LIMIT 1001',
                                       ('"' + q.replace('"', '""') + '"',)).fetchall()
            if len(matches) <= 1000:
                table += ' NOT INDEXED'
            else:
                clause = 'rowid IN (SELECT rowid FROM package_search WHERE package_search MATCH ?)'
                index = where.index(clause)
                where.pop(index)
                args.pop(1 if repo_id is not None else 0)
        query = f"SELECT {columns} FROM {table}"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY " + ", ".join(f"{k} {direction}" for k in keys) + " LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(query, (*args, limit + 1)).fetchall()
        names = columns.split(", ")
        items = [dict(zip(names, row)) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = base64.urlsafe_b64encode(json.dumps({
                "scope": scope, "after": [items[-1][k] for k in keys]
            }).encode()).decode()
        return {"items": items, "next_cursor": next_cursor}

    def has_snapshot(self, repo_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM repo_state WHERE repo_id = ?",
                                (repo_id,)).fetchone() is not None

    def record_request(
        self,
        repo_id: str | None,
        client_ip: str | None,
        method: str,
        path: str,
        status: str | None,
        cache_status: str | None,
        package_key: str | None = None,
        package_repo_id: str | None = None,
    ) -> None:
        """Record one client request received via syslog_listener.
        package_key/package_repo_id are the (optional) result of matching
        this request against a specific repository's known packages (see
        syslog_listener.match_all_package_keys) — independent of repo_id,
        which is a separate, prefix-based match and may be NULL or point at
        a different repository (see request_events.package_repo_id)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO request_events
                    (ts, repo_id, client_ip, method, path, status, cache_status,
                     package_key, package_repo_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (_utcnow(), repo_id, client_ip, method, path, status, cache_status,
                 package_key, package_repo_id),
            )

    def get_recent_requests(self, repo_id: str | None = None, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            if repo_id:
                rows = conn.execute(
                    """
                    SELECT ts, repo_id, client_ip, method, path, status, cache_status
                    FROM request_events WHERE repo_id = ? ORDER BY ts DESC LIMIT ?
                    """,
                    (repo_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT ts, repo_id, client_ip, method, path, status, cache_status
                    FROM request_events ORDER BY ts DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [
            {
                "ts": ts,
                "repo_id": rid,
                "client_ip": client_ip,
                "method": method,
                "path": path,
                "status": status,
                "cache_status": cache_status,
            }
            for ts, rid, client_ip, method, path, status, cache_status in rows
        ]

    def get_top_client_ips(self, repo_id: str | None = None, limit: int = 10) -> list[dict]:
        """Top client_ip values by request count — for the dashboard chart
        (see api.requests_summary_payload). client_ip can be NULL (old
        log_format without $remote_addr, see request_events.client_ip) —
        such rows are excluded from the top, there's nothing meaningful to
        group them by."""
        with self._connect() as conn:
            if repo_id:
                rows = conn.execute(
                    """
                    SELECT client_ip, COUNT(*) AS cnt FROM request_events
                    WHERE repo_id = ? AND client_ip IS NOT NULL
                    GROUP BY client_ip ORDER BY cnt DESC LIMIT ?
                    """,
                    (repo_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT client_ip, COUNT(*) AS cnt FROM request_events
                    WHERE client_ip IS NOT NULL
                    GROUP BY client_ip ORDER BY cnt DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [{"key": key, "count": count} for key, count in rows]

    def get_top_request_paths(self, repo_id: str | None = None, limit: int = 10) -> list[dict]:
        """Top request paths by count — a simplified stand-in for "by
        package": grouped by the raw path, not by the resolved package_key
        (see request_events.package_key/get_prefetch_efficiency for the
        latter, used for a different question — "was this prefetched?" —
        not "what's most popular")."""
        with self._connect() as conn:
            if repo_id:
                rows = conn.execute(
                    """
                    SELECT path, COUNT(*) AS cnt FROM request_events
                    WHERE repo_id = ? GROUP BY path ORDER BY cnt DESC LIMIT ?
                    """,
                    (repo_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT path, COUNT(*) AS cnt FROM request_events
                    GROUP BY path ORDER BY cnt DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [{"key": key, "count": count} for key, count in rows]

    def get_requests_by_repo(self, limit: int = 20) -> list[dict]:
        """Requests per repository — including repo_id IS NULL (path didn't
        match any repository, see syslog_listener.match_repo_id), shown as
        "(unmatched)" on the dashboard side."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT repo_id, COUNT(*) AS cnt FROM request_events
                GROUP BY repo_id ORDER BY cnt DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [{"key": key, "count": count} for key, count in rows]

    def get_request_hit_stats(self) -> dict[str | None, dict[str, int]]:
        """Per-repo request count and cache-HIT count over the currently
        retained window of request_events (see prune_requests/
        request_max_rows) — for api.metrics_payload. A snapshot gauge, not a
        counter: retention can shrink these numbers, they are not
        monotonically increasing. Key None groups requests whose path
        didn't match any configured repo (see syslog_listener.match_repo_id)
        — the caller decides whether/how to surface that bucket."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT repo_id, COUNT(*) AS total,
                       SUM(CASE WHEN cache_status = 'HIT' THEN 1 ELSE 0 END) AS hits
                FROM request_events
                GROUP BY repo_id
                """
            ).fetchall()
        return {repo_id: {"total": total, "hits": hits or 0} for repo_id, total, hits in rows}

    def get_requests_timeline(self, repo_id: str | None = None, hours: int = 24) -> list[dict]:
        """Hourly request-count buckets for the last `hours` hours, oldest
        first — docs_dev/ROADMAP.md item 19's "timeline" chart. Bucketing is
        a plain substr() on the ISO-8601 `ts` (always "YYYY-MM-DDTHH:...",
        fixed width, UTC — see _utcnow), not a datetime() call: cheap and
        exact for this format, no timezone conversion needed. A snapshot
        like the rest of this module — retention pruning can make an older
        hour's bucket shrink or disappear between two calls."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        params: tuple = (cutoff,)
        where = "ts >= ?"
        if repo_id is not None:
            where += " AND repo_id = ?"
            params += (repo_id,)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT substr(ts, 1, 13) AS hour, COUNT(*) AS total,
                       SUM(CASE WHEN cache_status = 'HIT' THEN 1 ELSE 0 END) AS hits
                FROM request_events
                WHERE {where}
                GROUP BY hour ORDER BY hour
                """,
                params,
            ).fetchall()
        return [{"hour": hour, "total": total, "hits": hits or 0} for hour, total, hits in rows]

    def get_prefetch_efficiency(self) -> list[dict]:
        """Per-repo: of the packages repowatch actively prefetched ahead of
        demand (warmed_packages.source = "prefetch"), how many were later
        actually requested by a real client — docs_dev/ROADMAP.md item 19.
        Answers "was prefetching this repo worth it", as opposed to
        get_request_hit_stats (nginx's cache HIT/MISS, which also counts
        packages that became cached only because an earlier client
        requested them, not because repowatch prefetched them).

        The correlation is exact, not basename-guessing: it uses
        request_events.package_key/package_repo_id, populated by
        syslog_listener only when a request's basename unambiguously
        matches exactly one repository's known packages (see
        match_all_package_keys) — the same resolution warmed_packages
        itself relies on, so the two line up even when the plain,
        prefix-based repo_id column is NULL (e.g. several apt repos sharing
        one pool/).

        Repos with zero prefetched packages are omitted — a ratio of "0 of
        0" is not a meaningful data point, not the same thing as 0%."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT w.repo_id, COUNT(*) AS prefetched,
                       COUNT(*) FILTER (WHERE EXISTS (
                           SELECT 1 FROM request_events r
                           WHERE r.package_repo_id = w.repo_id AND r.package_key = w.package_key
                       )) AS used
                FROM warmed_packages w
                WHERE w.source = 'prefetch'
                GROUP BY w.repo_id
                """
            ).fetchall()
        return [
            {"repo_id": repo_id, "prefetched": prefetched, "used": used,
             "ratio": used / prefetched if prefetched else 0.0}
            for repo_id, prefetched, used in rows
        ]

    def prune_requests(self, retention_days: int) -> int:
        """Same as prune_events but for request_events — grows much faster
        (on every client request, not just on changes), so retention is
        usually shorter."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM request_events WHERE ts < ?", (cutoff,))
            return cur.rowcount

    def prune_events(self, retention_days: int) -> int:
        """Delete repo_events rows older than retention_days. Returns the
        number of deleted rows (for the caller to log)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM repo_events WHERE ts < ?", (cutoff,))
            return cur.rowcount

    def prune_events_by_size(self, max_rows_per_repo: int) -> int:
        """A cap on top of the time-based prune_events — for repositories
        that change so often that time-based retention alone doesn't stop
        unbounded growth. Keeps at most max_rows_per_repo most recent
        (by ts) rows PER repo_id, not globally — otherwise one very active
        repository would push out the history of the rest. Uses a
        ROW_NUMBER() window function (SQLite since 3.25, 2018) — a single
        DELETE across all repositories, no loop over repo_id."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM repo_events
                WHERE id IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY repo_id ORDER BY ts DESC
                        ) AS rn
                        FROM repo_events
                    )
                    WHERE rn > ?
                )
                """,
                (max_rows_per_repo,),
            )
            return cur.rowcount

    def prune_requests_by_size(self, max_rows: int) -> int:
        """Same idea as prune_events_by_size, but a GLOBAL limit, not
        per-repo: a noticeable share of request_events has repo_id IS NULL
        (path didn't match any repository, see
        syslog_listener.match_repo_id), so "per repository" doesn't cleanly
        apply here — we just keep the N most recent requests overall."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM request_events
                WHERE id NOT IN (
                    SELECT id FROM request_events ORDER BY ts DESC LIMIT ?
                )
                """,
                (max_rows,),
            )
            return cur.rowcount

    def prune_warmed_packages(self, retention_days: int) -> int:
        """Delete warmed_packages rows that haven't been updated (warmed_at)
        for longer than retention_days — usually packages long gone from
        upstream: record_snapshot no longer sees them, so warm_cache will
        never touch or refresh that row again.

        Only meaningful when syslog_listener.enabled is on (see
        watcher.prune_all, which gates calling this at all) — warmed_at is
        refreshed on every real client request (see
        syslog_listener.run_listener) as well as on first warm, so it acts
        as a rough "still wanted" signal only where that visibility exists.
        Without it, this would just delete bookkeeping for packages
        nobody's re-warmed lately for reasons that have nothing to do with
        whether real clients still want them."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM warmed_packages WHERE warmed_at < ?", (cutoff,))
            return cur.rowcount

    def get_stale_warmed_packages(self, retention_days: int) -> list[tuple[str, str, str]]:
        """(repo_id, package_key, filename) for every row
        prune_warmed_packages(retention_days) would delete — read-only, so
        a caller that also wants to purge the real cache entry first (see
        watcher.prune_all, when nginx.enable_purge is on) can act on the
        exact same set before the bookkeeping rows disappear. Same cutoff
        computation as prune_warmed_packages — kept in sync deliberately,
        not re-derived independently, since the two must always agree on
        which rows count as stale."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT repo_id, package_key, filename FROM warmed_packages WHERE warmed_at < ?",
                (cutoff,),
            ).fetchall()
        return [(repo_id, key, filename) for repo_id, key, filename in rows]

    def get_history(self, repo_id: str, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, new_pkgs_json, removed_pkgs_json FROM repo_events
                WHERE repo_id = ? ORDER BY ts DESC LIMIT ?
                """,
                (repo_id, limit),
            ).fetchall()
        return [
            {
                "ts": ts,
                "new_packages": json.loads(new_json),
                "removed_packages": json.loads(removed_json),
            }
            for ts, new_json, removed_json in rows
        ]

    def bump_failure(self, repo_id: str, kind: str, error_message: str) -> tuple[int, bool]:
        """Increment the consecutive-failure counter ("prefetch"/"gpg") by 1.

        Returns (new_count, was_this_streak_already_notified_BEFORE_this_call) —
        the caller (notifications.py) needs the second value to decide
        whether to send a notification right now: we check not "== threshold"
        (which would break if the counter already passed the threshold
        before the operator enabled the webhook), but "threshold reached AND
        not yet notified"."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO failure_state
                    (repo_id, kind, consecutive_failures, last_error, last_failure_at, notified)
                VALUES (?, ?, 1, ?, ?, 0)
                ON CONFLICT(repo_id, kind) DO UPDATE SET
                    consecutive_failures = failure_state.consecutive_failures + 1,
                    last_error = excluded.last_error,
                    last_failure_at = excluded.last_failure_at
                """,
                (repo_id, kind, error_message, _utcnow()),
            )
            row = conn.execute(
                "SELECT consecutive_failures, notified FROM failure_state WHERE repo_id = ? AND kind = ?",
                (repo_id, kind),
            ).fetchone()
        return row[0], bool(row[1])

    def mark_failure_notified(self, repo_id: str, kind: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE failure_state SET notified = 1 WHERE repo_id = ? AND kind = ?",
                (repo_id, kind),
            )

    def reset_failure(self, repo_id: str, kind: str) -> bool:
        """Reset the failure streak after a success (deletes the row
        entirely — see the comment on CREATE TABLE failure_state). Returns
        True if a failure notification was already sent for this streak —
        a signal for notifications.record_success_and_maybe_notify to send
        a separate "recovered" notification instead of staying silent.
        False with no write at all is the common case (the repository was
        always healthy)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT notified FROM failure_state WHERE repo_id = ? AND kind = ?",
                (repo_id, kind),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "DELETE FROM failure_state WHERE repo_id = ? AND kind = ?", (repo_id, kind)
            )
            return bool(row[0])

    def get_failure_counts(self) -> dict[tuple[str, str], int]:
        """(repo_id, kind) -> current consecutive_failures, one query for all
        repos/kinds (for api.metrics_payload). A row only exists here while
        a streak is active — reset_failure deletes it on the first success,
        so an absent (repo_id, kind) pair means "currently healthy", not
        "never failed"."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT repo_id, kind, consecutive_failures FROM failure_state"
            ).fetchall()
        return {(repo_id, kind): count for repo_id, kind, count in rows}


def _prev_changed_at(conn: sqlite3.Connection, repo_id: str) -> str | None:
    row = conn.execute(
        "SELECT changed_at FROM repo_state WHERE repo_id = ?", (repo_id,)
    ).fetchone()
    return row[0] if row else None




def _migrate(conn: sqlite3.Connection) -> None:
    """Migrate existing schemas inside the initialization transaction."""
    if 'repo_ids' not in {r[1] for r in conn.execute('PRAGMA table_info(host_tokens)')}:
        conn.execute('ALTER TABLE host_tokens ADD COLUMN repo_ids TEXT')

    if 'content_hash' not in {r[1] for r in conn.execute('PRAGMA table_info(repo_packages)')}:
        conn.execute('ALTER TABLE repo_packages ADD COLUMN content_hash TEXT')
    # Created here, not in SCHEMA, so it always runs after the column above
    # is guaranteed to exist — SCHEMA's own CREATE TABLE IF NOT EXISTS is a
    # no-op against a pre-existing table and would otherwise leave this
    # index creation racing an as-yet-missing column on upgrade.
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_repo_packages_hash '
        'ON repo_packages(filename, content_hash) WHERE content_hash IS NOT NULL'
    )

    existing = {row[1] for row in conn.execute("PRAGMA table_info(repo_state)")}
    for column in ("index_etag", "index_last_modified"):
        if column not in existing:
            conn.execute(f"ALTER TABLE repo_state ADD COLUMN {column} TEXT")
    if "package_count" not in existing:
        conn.execute("ALTER TABLE repo_state ADD COLUMN package_count INTEGER")
    if "key_expires_at" not in existing:
        conn.execute("ALTER TABLE repo_state ADD COLUMN key_expires_at TEXT")

    existing_request_events = {row[1] for row in conn.execute("PRAGMA table_info(request_events)")}
    if "client_ip" not in existing_request_events:
        conn.execute("ALTER TABLE request_events ADD COLUMN client_ip TEXT")
    if "package_key" not in existing_request_events:
        conn.execute("ALTER TABLE request_events ADD COLUMN package_key TEXT")
        conn.execute("ALTER TABLE request_events ADD COLUMN package_repo_id TEXT")
    # Same reasoning as idx_repo_packages_hash above: only safe to create
    # once package_key/package_repo_id are guaranteed to exist, which for an
    # upgraded database is only true after the ALTER TABLE calls just above.
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_request_events_package '
        'ON request_events(package_repo_id, package_key) WHERE package_key IS NOT NULL'
    )

    existing_warmed = {row[1] for row in conn.execute("PRAGMA table_info(warmed_packages)")}
    if "source" not in existing_warmed:
        conn.execute("ALTER TABLE warmed_packages ADD COLUMN source TEXT")

    if "packages_json" in existing:
        # JSON is authoritative for the dual-write release, including after
        # rollback. Rebuild every repo: counts alone cannot detect stale rows.
        names_column = "names_json" if "names_json" in existing else "NULL"
        for repo_id, raw, names_raw in conn.execute(
            f"SELECT repo_id, packages_json, {names_column} FROM repo_state"
        ).fetchall():
            packages = json.loads(raw)
            names = json.loads(names_raw or "{}")
            if not isinstance(packages, dict) or not isinstance(names, dict):
                raise ValueError(f"Invalid legacy snapshot: {repo_id}")
            if any(not isinstance(k, str) or not isinstance(v, str) for k, v in packages.items()):
                raise ValueError(f"Invalid legacy package filenames: {repo_id}")
            conn.execute("DELETE FROM repo_packages WHERE repo_id = ?", (repo_id,))
            conn.executemany(
                "INSERT INTO repo_packages (repo_id, package_key, package_name, filename) VALUES (?, ?, ?, ?)",
                ((repo_id, k, names.get(k), v) for k, v in packages.items()),
            )
            conn.execute("UPDATE repo_state SET package_count = ? WHERE repo_id = ?",
                         (len(packages), repo_id))
        conn.execute("ALTER TABLE repo_state DROP COLUMN packages_json")
        if "names_json" in existing:
            conn.execute("ALTER TABLE repo_state DROP COLUMN names_json")


def _search_index(conn: sqlite3.Connection) -> bool:
    """Transactional derived index, rebuilt once for existing databases.

    No extension is loaded. SQLite without FTS5/trigram keeps the scan path.
    External content avoids another copy of names/keys; triggers cover every
    mutation, including pruning and direct SQL imports.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='package_search'").fetchone():
        return True
    try:
        conn.execute("CREATE VIRTUAL TABLE package_search USING fts5(package_key, package_name, "
                     "content='repo_packages', content_rowid='rowid', tokenize='trigram')")
    except sqlite3.OperationalError as exc:
        if 'no such module' in str(exc) or 'no such tokenizer' in str(exc):
            return False
        raise
    conn.execute("INSERT INTO package_search(package_search) VALUES ('rebuild')")
    conn.execute("""CREATE TRIGGER package_search_insert AFTER INSERT ON repo_packages BEGIN
        INSERT INTO package_search(rowid, package_key, package_name)
        VALUES (new.rowid, new.package_key, new.package_name); END""")
    conn.execute("""CREATE TRIGGER package_search_delete AFTER DELETE ON repo_packages BEGIN
        INSERT INTO package_search(package_search, rowid, package_key, package_name)
        VALUES ('delete', old.rowid, old.package_key, old.package_name); END""")
    conn.execute("""CREATE TRIGGER package_search_update AFTER UPDATE ON repo_packages
        WHEN old.package_key IS NOT new.package_key OR old.package_name IS NOT new.package_name
        BEGIN
        INSERT INTO package_search(package_search, rowid, package_key, package_name)
        VALUES ('delete', old.rowid, old.package_key, old.package_name);
        INSERT INTO package_search(rowid, package_key, package_name)
        VALUES (new.rowid, new.package_key, new.package_name); END""")
    return True
