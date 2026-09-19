"""Cache bookkeeping, Nix artifact ownership, warming bans, and replacements."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from repowatch.storage.database import Database, _utcnow

class CacheStore:
    """SQL operations for cache; the caller supplies the shared database."""

    def __init__(self, db: Database):
        self.db = db

    def update_nix_trust(self, repo_id: str, verify: bool, keys: list[str], *, source: str | None = None) -> None:
        """Recheck completed closures when signature policy changes."""
        policy = json.dumps([verify, sorted(set(keys)), source])
        with self.db.connect() as conn:
            previous = conn.execute("SELECT policy FROM nix_trust WHERE repo_id=?", (repo_id,)).fetchone()
            if previous is None or previous[0] != policy:
                conn.execute("UPDATE warmed_packages SET status='failed', revision=hex(randomblob(16)) WHERE repo_id=?", (repo_id,))
                conn.execute("INSERT INTO nix_trust VALUES (?, ?) ON CONFLICT(repo_id) "
                             "DO UPDATE SET policy=excluded.policy", (repo_id, policy))


    def nix_has_unknown_owners(self, repo_id: str, excluded_keys: list[str]) -> bool:
        """An undiscovered current output might reference any known artifact."""
        excluded = set(excluded_keys)
        with self.db.connect() as conn:
            return any(key not in excluded for (key,) in conn.execute(
                "SELECT p.package_key FROM repo_packages p WHERE p.repo_id=? AND NOT EXISTS "
                "(SELECT 1 FROM nix_artifacts a WHERE a.repo_id=p.repo_id AND a.package_key=p.package_key)",
                (repo_id,)))


    def record_nix_artifacts(self, repo_id: str, package_key: str, artifacts: list[dict]) -> None:
        """Preserve discovered files before warming, including failed attempts.

        Keep older encodings too so purge can remove them after a NAR URL change.
        A failed discovery must not erase the previous complete artifact list.
        """
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT INTO nix_artifacts (repo_id, package_key, filename, content_hash, size) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(repo_id, package_key, filename) DO UPDATE SET "
                "content_hash=excluded.content_hash, size=excluded.size",
                ((repo_id, package_key, a['filename'], a.get('content_hash'), a.get('size')) for a in artifacts),
            )


    def get_nix_artifacts(self, repo_id: str, package_key: str) -> list[dict]:
        with self.db.connect() as conn:
            return [dict(filename=f, content_hash=h, size=s) for f, h, s in conn.execute(
                "SELECT filename, content_hash, size FROM nix_artifacts WHERE repo_id=? AND package_key=?",
                (repo_id, package_key))]


    def touch_nix_artifact(self, repo_id: str, filename: str) -> None:
        """Refresh existing successful closures after a real artifact request.

        A metadata request alone must never create a successful closure record.
        """
        with self.db.connect() as conn:
            conn.execute("UPDATE warmed_packages SET warmed_at=?, revision=hex(randomblob(16)) WHERE repo_id=? AND status='ok' "
                         "AND package_key IN (SELECT package_key FROM nix_artifacts WHERE repo_id=? AND filename=?)",
                         (_utcnow(), repo_id, repo_id, filename))


    def nix_shared_files(self, repo_id: str, excluded_keys: list[str]) -> set[str]:
        """Files owned by current catalog entries outside a purge selection."""
        excluded = set(excluded_keys)
        with self.db.connect() as conn:
            return {filename for key, filename in conn.execute(
                "SELECT a.package_key,a.filename FROM nix_artifacts a JOIN repo_packages p "
                "ON p.repo_id=a.repo_id AND p.package_key=a.package_key WHERE a.repo_id=?", (repo_id,))
                    if key not in excluded}


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
        "prefetch", see operations/warm.py) and when a real client request was
        merely observed to succeed (source="client", see
        runtime/syslog.py) — NOT a live check of the current nginx cache
        state either way (the file could have since been evicted by
        proxy_cache_path's inactive/max_size, see nginx/render.py).

        `source` is deliberately NOT overwritten on a later call for the
        same (repo_id, package_key): it records how this package FIRST
        entered warmed_packages, which is what
        ServiceState.get_prefetch_efficiency needs — a package repowatch
        prefetched ahead of demand and only later happened to also be
        requested by a client is still a "prefetch" success, not
        reclassified as "client" just because a client eventually asked
        for it too."""
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO warmed_packages
                    (repo_id, package_key, filename, warmed_at, status, http_status, source, revision)
                VALUES (?, ?, ?, ?, ?, ?, ?, hex(randomblob(16)))
                ON CONFLICT(repo_id, package_key) DO UPDATE SET
                    filename = excluded.filename,
                    warmed_at = excluded.warmed_at,
                    status = excluded.status,
                    http_status = excluded.http_status,
                    revision = excluded.revision
                """,
                (repo_id, package_key, filename, _utcnow(), "ok" if ok else "failed", http_status, source),
            )


    def get_warmed_packages(self, repo_id: str) -> list[dict]:
        with self.db.connect() as conn:
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
        extra bookkeeping needed. Caller (nginx/render.py) resolves repo ids to
        actual local cache URIs — this module stays URL-agnostic.
        """
        with self.db.connect() as conn:
            # Do not materialize unique files in Python. Grouping removes package
            # aliases but keeps distinct hash groups even if their output tuples match.
            return conn.execute(
                """
                WITH duplicates AS (
                    SELECT filename, content_hash, MIN(repo_id) AS canonical
                    FROM repo_packages WHERE content_hash IS NOT NULL
                    GROUP BY filename, content_hash
                    HAVING COUNT(DISTINCT repo_id) > 1
                )
                SELECT p.repo_id, d.canonical, p.filename
                FROM repo_packages p JOIN duplicates d
                  ON p.filename = d.filename AND p.content_hash = d.content_hash
                WHERE p.repo_id <> d.canonical
                GROUP BY p.filename, p.content_hash, p.repo_id, d.canonical
                ORDER BY p.filename, p.content_hash, p.repo_id
                """
            ).fetchall()


    def ban_package(self, repo_id: str, package_name: str) -> None:
        """Apply ban package to one name using the bulk transaction."""
        self.ban_packages(repo_id, [package_name])


    def unban_package(self, repo_id: str, package_name: str) -> None:
        """Apply unban package to one name using the bulk transaction."""
        self.unban_packages(repo_id, [package_name])


    def ban_packages(self, repo_id: str, package_names: list[str]) -> None:
        """Bulk form of ban_package (dashboard "ban selected") — one
        connection/transaction via executemany instead of one per name, so
        selecting hundreds of packages to ban doesn't cost hundreds of
        separate SQLite commits."""
        if not package_names:
            return
        with self.db.connect() as conn:
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
        with self.db.connect() as conn:
            conn.executemany(
                "DELETE FROM prefetch_bans WHERE repo_id = ? AND package_name = ?",
                ((repo_id, name) for name in package_names),
            )


    def get_banned_packages(self, repo_id: str) -> list[str]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT package_name FROM prefetch_bans WHERE repo_id = ? ORDER BY package_name",
                (repo_id,),
            ).fetchall()
        return [r[0] for r in rows]


    def get_ban_counts(self) -> dict[str, int]:
        """Number of banned-from-auto-warm packages per repo, one query for
        all repos (for reporting.metrics.metrics_payload — a per-repo loop calling
        get_banned_packages would be one query per repo)."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT repo_id, COUNT(*) FROM prefetch_bans GROUP BY repo_id"
            ).fetchall()
        return {repo_id: count for repo_id, count in rows}


    def remove_warmed_package(self, repo_id: str, package_key: str) -> bool:
        """"Remove from warmed" — only repowatch's own bookkeeping (the
        warmed_packages entry); the actual file in the nginx cache is left
        untouched. Returns True if the row actually existed."""
        with self.db.connect() as conn:
            cur = conn.execute(
                "DELETE FROM warmed_packages WHERE repo_id = ? AND package_key = ?",
                (repo_id, package_key),
            )
            return cur.rowcount > 0


    def remove_warmed_packages(self, repo_id: str, package_keys: list[str]) -> int:
        """Delete selected rows and return the actual total, including concurrent changes."""
        with self.db.connect() as conn:
            return conn.executemany(
                "DELETE FROM warmed_packages WHERE repo_id = ? AND package_key = ?",
                ((repo_id, key) for key in package_keys),
            ).rowcount


    def find_stale_warmed(self, repo_id: str) -> list[dict]:
        """Manual cache purge (dashboard "Scan for stale entries",
        docs_dev/ROADMAP.md) — candidates for garbage: warmed_packages rows
        (repowatch believes/believed this file was fetched and cached at
        some point) whose package_key no longer exists in the CURRENT
        repo_packages snapshot for this repo (the package has since been
        removed from the upstream index). The combination of "known to have
        been cached" + "no longer a real package" is the best-supported
        "probably safe to purge" signal available without touching nginx's
        cache files directly or
        risking a false read from a live HTTP probe (see operations/warm.py).
        Does not confirm the file is STILL in nginx's cache right now —
        only an actual purge attempt (see cache.purge.purge_selected) can tell
        that for certain; this is the candidate list shown to the operator
        before they decide what to actually purge.
        """
        with self.db.connect() as conn:
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
        web.packages.purge_selected_payload)."""
        return {key: item[0] for key, item in self.get_warmed_records(repo_id, package_keys).items()}


    def get_warmed_records(self, repo_id: str, package_keys: list[str],
                           *, retention_days: int | None = None) -> dict[str, tuple[str, str]]:
        """Capture filenames and revisions before purge; optionally recheck the expiry cutoff."""
        keys = list(dict.fromkeys(package_keys))
        result = {}
        cutoff = ((datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
                  if retention_days is not None else None)
        with self.db.connect() as conn:
            for start in range(0, len(keys), 500):
                chunk = keys[start:start + 500]
                rows = conn.execute(
                    f"SELECT package_key, filename, revision FROM warmed_packages WHERE repo_id = ? "
                    f"AND package_key IN ({','.join('?' for _ in chunk)})"
                    + (" AND warmed_at < ?" if cutoff is not None else ""),
                    (repo_id, *chunk, *((cutoff,) if cutoff is not None else ())),
                )
                result.update((key, (filename, revision)) for key, filename, revision in rows)
        return result


    def remove_purged_records(self, repo_id: str, records: dict[str, tuple[str, str]],
                              resolved: list[str]) -> int:
        """Forget only the exact generation selected before network I/O."""
        with self.db.connect() as conn:
            return conn.executemany(
                "DELETE FROM warmed_packages WHERE repo_id=? AND package_key=? AND revision=?",
                ((repo_id, key, records[key][1]) for key in resolved if key in records),
            ).rowcount


    def prune_warmed_packages(self, retention_days: int) -> int:
        """Delete warmed_packages rows that haven't been updated (warmed_at)
        for longer than retention_days — usually packages long gone from
        upstream: record_snapshot no longer sees them, so warm_cache will
        never touch or refresh that row again.

        Only meaningful when syslog_listener.enabled is on (see
        operations.cleanup.prune_all, which gates calling this at all) — warmed_at is
        refreshed on every real client request (see
        runtime.syslog.run_listener) as well as on first warm, so it acts
        as a rough "still wanted" signal only where that visibility exists.
        Without it, this would just delete bookkeeping for packages
        nobody's re-warmed lately for reasons that have nothing to do with
        whether real clients still want them."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        with self.db.connect() as conn:
            cur = conn.execute("DELETE FROM warmed_packages WHERE warmed_at < ?", (cutoff,))
            conn.execute("DELETE FROM nix_artifacts WHERE NOT EXISTS "
                         "(SELECT 1 FROM repo_packages p WHERE p.repo_id=nix_artifacts.repo_id "
                         "AND p.package_key=nix_artifacts.package_key) AND NOT EXISTS "
                         "(SELECT 1 FROM warmed_packages w WHERE w.repo_id=nix_artifacts.repo_id "
                         "AND w.package_key=nix_artifacts.package_key)")
            return cur.rowcount


    def get_stale_warmed_packages(self, retention_days: int) -> list[tuple[str, str, str]]:
        """(repo_id, package_key, filename) for every row
        prune_warmed_packages(retention_days) would delete — read-only, so
        a caller that also wants to purge the real cache entry first (see
        operations.cleanup.prune_all, when nginx.enable_purge is on) can act on the
        exact same set before the bookkeeping rows disappear. Same cutoff
        computation as prune_warmed_packages — kept in sync deliberately,
        not re-derived independently, since the two must always agree on
        which rows count as stale."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT repo_id, package_key, filename FROM warmed_packages WHERE warmed_at < ?",
                (cutoff,),
            ).fetchall()
        return [(repo_id, key, filename) for repo_id, key, filename in rows]


    def get_pending_replacements(self, repo_id: str) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT q.package_key, q.targets_json, q.revision, p.filename, q.last_error, COALESCE(p.content_hash, q.expected_hash) "
                "FROM pending_replacements q LEFT JOIN repo_packages p "
                "ON p.repo_id=q.repo_id AND p.package_key=q.package_key WHERE q.repo_id=?",
                (repo_id,),
            ).fetchall()
        return [dict(package_key=key, targets=json.loads(targets), revision=revision,
                     filename=filename, last_error=error, content_hash=digest)
                for key, targets, revision, filename, error, digest in rows]


    def get_pending_replacement_counts(self) -> dict[str, int]:
        with self.db.connect() as conn:
            return dict(conn.execute("SELECT repo_id, COUNT(*) FROM pending_replacements GROUP BY repo_id"))


    def finish_replacement(self, repo_id: str, key: str, revision: str, error: str | None = None) -> None:
        with self.db.connect() as conn:
            if error is None:
                conn.execute("DELETE FROM pending_replacements WHERE repo_id=? AND package_key=? AND revision=?",
                             (repo_id, key, revision))
            else:
                conn.execute("UPDATE pending_replacements SET last_error=? WHERE repo_id=? AND package_key=? AND revision=?",
                             (error, repo_id, key, revision))
