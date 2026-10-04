"""Repository snapshots, changes, check state, and history."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from repowatch.models import RepoSnapshot, DiffResult
from repowatch.storage.database import Database, _utcnow

class RepositoriesStore:
    """SQL operations for repositories; the caller supplies the shared database."""

    def __init__(self, db: Database):
        self.db = db

    def record_snapshot(
        self,
        snapshot: RepoSnapshot,
        index_etag: str | None = None,
        index_last_modified: str | None = None,
        source_identity: str | None = None,
    ) -> DiffResult:
        """Compare the new snapshot against the previous one, store it, and
        return the diff.

        index_etag/index_last_modified — response headers from the HEAD
        request to the index (see IndexParser.check_index_changed), saved so
        the next cycle can skip a full index download.
        """
        now = _utcnow()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT changed_at FROM repo_state WHERE repo_id = ?",
                (snapshot.repo_id,),
            ).fetchone()
            previous = {key: (filename, digest) for key, filename, digest in conn.execute(
                "SELECT package_key, filename, content_hash FROM repo_packages WHERE repo_id = ?", (snapshot.repo_id,)
            )}
            prev_packages = {key: value[0] for key, value in previous.items()}

            new_keys = set(snapshot.packages) - set(prev_packages)
            removed_keys = set(prev_packages) - set(snapshot.packages)
            modified_keys = {
                key for key in set(snapshot.packages) & set(previous)
                if snapshot.packages[key] != previous[key][0]
                or (previous[key][1] and snapshot.content_hashes.get(key)
                    and previous[key][1] != snapshot.content_hashes[key])
            }
            changed = bool(new_keys or removed_keys or modified_keys)
            # Hash discovery/loss changes dedup routing even without a package
            # version change. Keep the user-facing diff semantics unchanged.
            catalog_changed = changed or any(
                previous[key][1] != snapshot.content_hashes.get(key)
                for key in set(previous) & set(snapshot.packages)
            )
            for key in sorted(modified_keys):
                old_filename, old_hash = previous[key]
                pending = conn.execute(
                    "SELECT targets_json FROM pending_replacements WHERE repo_id=? AND package_key=?",
                    (snapshot.repo_id, key),
                ).fetchone()
                targets = {tuple(target) for target in json.loads(pending[0])} if pending else set()
                targets.update(((snapshot.repo_id, old_filename), (snapshot.repo_id, snapshot.packages[key])))
                # Preserve the previous shared location before replacing its hash.
                if old_hash:
                    shared = conn.execute(
                        "SELECT MIN(repo_id) FROM repo_packages WHERE filename=? AND content_hash=?",
                        (old_filename, old_hash),
                    ).fetchone()[0]
                    if shared:
                        targets.add((shared, old_filename))
                conn.execute(
                    "INSERT INTO pending_replacements (repo_id, package_key, targets_json, expected_hash, revision, last_error) "
                    "VALUES (?, ?, ?, ?, ?, NULL) "
                    "ON CONFLICT(repo_id, package_key) DO UPDATE SET targets_json=excluded.targets_json, "
                    "expected_hash=excluded.expected_hash, revision=excluded.revision, last_error=NULL",
                    (snapshot.repo_id, key, json.dumps(sorted(targets)), snapshot.content_hashes.get(key), uuid.uuid4().hex),
                )
                # A previous successful warm does not confirm replacement bytes.
                conn.execute("DELETE FROM warmed_packages WHERE repo_id=? AND package_key=?", (snapshot.repo_id, key))

            changed_at = now if changed else (row[0] if row else None)

            conn.execute(
                """
                INSERT INTO repo_state
                    (repo_id, last_check, changed_at,
                     index_etag, index_last_modified, package_count, source_identity, snapshot_revision)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(repo_id) DO UPDATE SET
                    last_check = excluded.last_check,
                    changed_at = CASE WHEN ? THEN excluded.last_check ELSE repo_state.changed_at END,
                    index_etag = excluded.index_etag,
                    index_last_modified = excluded.index_last_modified,
                    package_count = excluded.package_count,
                    source_identity = excluded.source_identity,
                    snapshot_revision = repo_state.snapshot_revision + ?
                """,
                (
                    snapshot.repo_id,
                    now,
                    changed_at,
                    index_etag,
                    index_last_modified,
                    len(snapshot.packages),
                    source_identity,
                    changed,
                    int(catalog_changed),
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
                    INSERT INTO repo_events (repo_id, ts, new_pkgs_json, removed_pkgs_json, modified_pkgs_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.repo_id,
                        now,
                        json.dumps(sorted(new_keys)),
                        json.dumps(sorted(removed_keys)),
                        json.dumps(sorted(modified_keys)),
                    ),
                )

            return DiffResult(
                repo_id=snapshot.repo_id,
                changed=changed,
                new_packages=sorted(new_keys),
                removed_packages=sorted(removed_keys),
                removed_filenames={key: prev_packages[key] for key in removed_keys},
                modified_packages=sorted(modified_keys),
                modified_filenames={key: prev_packages[key] for key in modified_keys},
            )


    def get_index_meta(self, repo_id: str, source_identity: str | None = None) -> tuple[str | None, str | None]:
        """Index ETag/Last-Modified saved from the previous check."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT index_etag, index_last_modified, source_identity FROM repo_state WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
        if not row or (source_identity is not None and row[2] != source_identity):
            return None, None
        return row[0], row[1]


    def get_snapshot_revisions(self) -> dict[str, int]:
        """Compact catalog generations, independent of wall-clock precision."""
        with self.db.connect() as conn:
            return dict(conn.execute("SELECT repo_id, snapshot_revision FROM repo_state"))


    def record_key_expiry(self, repo_id: str, expires_at: str | None, *, known: bool | None = None) -> None:
        """Store signing-path diagnostics without inventing a successful index check."""
        if known is None:
            known = expires_at is not None
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO repo_state (repo_id, last_check, key_expires_at, key_expiry_known)
                VALUES (?, '', ?, ?)
                ON CONFLICT(repo_id) DO UPDATE SET key_expires_at = excluded.key_expires_at,
                    key_expiry_known = excluded.key_expiry_known""",
                (repo_id, expires_at, known),
            )


    def touch_last_check(self, repo_id: str) -> None:
        """Update only last_check — used when a HEAD check showed the index
        is unchanged and the full cycle (download/parse/diff) was skipped."""
        with self.db.connect() as conn:
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
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT repo_id, last_check, changed_at, package_count, key_expires_at, key_expiry_known FROM repo_state"
            ).fetchall()
            warmed = dict(conn.execute("SELECT repo_id, COUNT(*) FROM warmed_packages GROUP BY repo_id")) if include_warmed else {}
        result = {row[0]: {"last_check": row[1] or None, "changed_at": row[2], "package_count": row[3],
                            "key_expires_at": row[4], "key_expiry_known": bool(row[5])} for row in rows}
        if include_warmed:
            # Keep the count even for orphaned warmed rows without repo_state:
            # a per-repo count shouldn't require a snapshot to exist either.
            for repo_id in result.keys() | warmed.keys():
                result.setdefault(repo_id, {})["warmed_count"] = warmed.get(repo_id, 0)
        return result


    def get_status(self, repo_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT last_check, changed_at, package_count FROM repo_state WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
            if not row:
                return None
            last_check, changed_at, package_count = row

            last_event = conn.execute(
                """
                SELECT new_pkgs_json, removed_pkgs_json, modified_pkgs_json FROM repo_events
                WHERE repo_id = ? ORDER BY ts DESC, id DESC LIMIT 1
                """,
                (repo_id,),
            ).fetchone()

            new_pkgs = json.loads(last_event[0]) if last_event else []
            removed_pkgs = json.loads(last_event[1]) if last_event else []
            modified_pkgs = json.loads(last_event[2]) if last_event else []

            return {
                "last_check": last_check or None,
                "changed_at": changed_at,
                "last_new_packages": new_pkgs,
                "last_removed_packages": removed_pkgs,
                "last_modified_packages": modified_pkgs,
                # None for rows written before this column existed — see
                # _migrate and repos_list_payload (which has a fallback to
                # len(get_packages())).
                "package_count": package_count,
            }


    def get_names(self, repo_id: str) -> dict[str, str]:
        """{package_key: "bare package name"} — used for bans by name. An
        empty dict if there's no snapshot yet, or it predates this field."""
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT package_key, package_name FROM repo_packages
                WHERE repo_id = ? AND package_name IS NOT NULL
                """,
                (repo_id,),
            ).fetchall()
            return dict(rows)


    def get_packages(self, repo_id: str) -> dict[str, str] | None:
        """Full current package snapshot for a repository
        ({"name-version": filename}).

        None if this repo_id has never had a successful check.
        """
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM repo_state WHERE repo_id = ? AND last_check != ''",
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
        with self.db.connect() as conn:
            state_row = conn.execute(
                "SELECT 1 FROM repo_state WHERE repo_id = ? AND last_check != ''", (repo_id,)
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


    def has_snapshot(self, repo_id: str) -> bool:
        with self.db.connect() as conn:
            return conn.execute("SELECT 1 FROM repo_state WHERE repo_id = ? AND last_check != ''",
                                (repo_id,)).fetchone() is not None


    def prune_events(self, retention_days: int) -> int:
        """Delete repo_events rows older than retention_days. Returns the
        number of deleted rows (for the caller to log)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        with self.db.connect() as conn:
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
        with self.db.connect() as conn:
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


    # Tables this store owns that are keyed by repo_id — shared by every
    # method below with the sibling ones in CacheStore/NotificationsStore
    # (see operations.cleanup.find_orphaned_repos/purge_orphaned_repos).
    # request_events/request_counters/request_hourly are deliberately NOT
    # here: they're a request LOG governed by its own retention settings,
    # not repository bookkeeping — a request for a since-removed repo's URL
    # is still meaningful history, not an orphan.
    _REPO_TABLES = ('repo_state', 'repo_packages', 'repo_events', 'pending_replacements')

    def get_repo_ids_with_data(self) -> set[str]:
        """Every repo_id this store holds ANY row for, across its own
        repo-keyed tables — the read side of orphan detection (see
        operations.cleanup.find_orphaned_repos)."""
        with self.db.connect() as conn:
            return {
                repo_id
                for table in self._REPO_TABLES
                for (repo_id,) in conn.execute(f'SELECT DISTINCT repo_id FROM {table}')
            }

    def count_repo_rows(self, repo_id: str) -> dict[str, int]:
        """Row count per repo-keyed table, for previewing an orphan before
        deleting it — zero-count tables are omitted, not shown as noise."""
        with self.db.connect() as conn:
            counts = {
                table: conn.execute(f'SELECT COUNT(*) FROM {table} WHERE repo_id = ?', (repo_id,)).fetchone()[0]
                for table in self._REPO_TABLES
            }
        return {table: count for table, count in counts.items() if count}

    def delete_repo_rows(self, repo_id: str) -> dict[str, int]:
        """Delete every row this store owns for one repo_id — the write side
        of operations.cleanup.purge_orphaned_repos. Callers are responsible
        for re-checking the repo_id is still absent from config.repos right
        before calling this (see that function)."""
        with self.db.connect() as conn:
            counts = {
                table: conn.execute(f'DELETE FROM {table} WHERE repo_id = ?', (repo_id,)).rowcount
                for table in self._REPO_TABLES
            }
        return {table: count for table, count in counts.items() if count}


    def get_history(self, repo_id: str, limit: int = 20) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, new_pkgs_json, removed_pkgs_json, modified_pkgs_json FROM repo_events
                WHERE repo_id = ? ORDER BY ts DESC, id DESC LIMIT ?
                """,
                (repo_id, limit),
            ).fetchall()
        return [
            {
                "ts": ts,
                "new_packages": json.loads(new_json),
                "removed_packages": json.loads(removed_json),
                "modified_packages": json.loads(modified_json),
            }
            for ts, new_json, removed_json, modified_json in rows
        ]

