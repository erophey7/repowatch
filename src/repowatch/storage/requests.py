"""Request-event persistence, retention, and statistical aggregates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from repowatch.storage.database import Database, _utcnow

# Rows removed per transaction by the request pruners (see _delete_requests).
_PRUNE_CHUNK = 5000


class RequestsStore:
    """SQL operations for requests; the caller supplies the shared database."""

    def __init__(self, db: Database):
        self.db = db

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
        runtime.syslog.match_all_package_keys) — independent of repo_id,
        which is a separate, prefix-based match and may be NULL or point at
        a different repository (see request_events.package_repo_id)."""
        with self.db.connect() as conn:
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
        with self.db.connect() as conn:
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


    def _top(self, kind: str, repo_id: str | None, limit: int) -> list[dict]:
        """Highest counts of one rollup kind, globally or for one repository."""
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT key, total FROM request_counters
                WHERE kind = ? AND repo_key = ? AND total > 0
                ORDER BY total DESC, key LIMIT ?
                """,
                (kind, f"r:{repo_id}" if repo_id else "*", limit),
            ).fetchall()
        return [{"key": key, "count": count} for key, count in rows]


    def get_top_client_ips(self, repo_id: str | None = None, limit: int = 10) -> list[dict]:
        """Top client_ip values by request count — for the dashboard chart
        (see reporting.statistics.requests_summary_payload). client_ip can be NULL (old
        log_format without $remote_addr, see request_events.client_ip) —
        such rows are excluded from the top, there's nothing meaningful to
        group them by. Read from request_counters (see storage.schema):
        the cost depends on the number of distinct addresses, not on how many
        requests are retained."""
        return self._top("ip", repo_id, limit)


    def get_top_request_paths(self, repo_id: str | None = None, limit: int = 10) -> list[dict]:
        """Top request paths by count — a simplified stand-in for "by
        package": grouped by the raw path, not by the resolved package_key
        (see request_events.package_key/get_prefetch_efficiency for the
        latter, used for a different question — "was this prefetched?" —
        not "what's most popular"). Counts equal those grouped over
        request_events, read from request_counters."""
        return self._top("path", repo_id, limit)


    def get_request_hit_stats(self) -> dict[str | None, dict[str, int]]:
        """Per-repo request count and cache-HIT count over the currently
        retained window of request_events (see prune_requests/
        request_max_rows) — for reporting.metrics.metrics_payload. A snapshot gauge, not a
        counter: retention can shrink these numbers, they are not
        monotonically increasing. Key None groups requests whose path
        didn't match any configured repo (see runtime.syslog.match_repo_id)
        — the caller decides whether/how to surface that bucket."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT repo_key, total, hits FROM request_counters WHERE kind = 'repo' AND total > 0"
            ).fetchall()
        return {(repo_key[2:] if repo_key else None): {"total": total, "hits": hits}
                for repo_key, total, hits in rows}


    def get_requests_timeline(self, repo_id: str | None = None, hours: int = 24) -> list[dict]:
        """Hourly request-count buckets for the last `hours` hours, oldest
        first — docs_dev/ROADMAP.md item 19's "timeline" chart. Read from
        request_hourly, whose bucket is the 'YYYY-MM-DDTHH' prefix of the ISO-8601
        `ts` (fixed width, UTC — see _utcnow). Buckets are whole hours: the
        oldest one includes the requests from before the exact cut-off within
        that hour. A snapshot like the rest of this module — retention pruning
        can make an older hour's bucket shrink or disappear between two calls."""
        first_hour = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")[:13]
        where, params = "hour >= ? AND total > 0", (first_hour,)
        if repo_id is not None:
            if not repo_id:
                return []
            where += " AND repo_key = ?"
            params += (f"r:{repo_id}",)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT hour, SUM(total), SUM(hits) FROM request_hourly
                WHERE {where} GROUP BY hour ORDER BY hour
                """,
                params,
            ).fetchall()
        return [{"hour": hour, "total": total, "hits": hits} for hour, total, hits in rows]


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
        with self.db.connect() as conn:
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


    def _delete_requests(self, where: str, params: tuple) -> int:
        """Delete matching request_events rows in bounded transactions.

        The triggers in storage.schema move each row out of the rollups, which
        makes a delete roughly ten times dearer than a bare one; one statement
        over a large backlog would hold the write lock (blocking every
        incoming request record) for its whole duration. Each chunk commits
        separately."""
        deleted = 0
        while True:
            with self.db.connect() as conn:
                count = conn.execute(
                    f"DELETE FROM request_events WHERE id IN "
                    f"(SELECT id FROM request_events WHERE {where} LIMIT ?)",
                    (*params, _PRUNE_CHUNK),
                ).rowcount
            deleted += count
            if count < _PRUNE_CHUNK:
                return deleted


    def _sweep_rollups(self) -> None:
        """Drop rollup rows whose count fell to zero (readers already ignore
        them; this only stops them accumulating)."""
        with self.db.connect() as conn:
            conn.execute("DELETE FROM request_counters WHERE total <= 0")
            conn.execute("DELETE FROM request_hourly WHERE total <= 0")


    def prune_requests(self, retention_days: int) -> int:
        """Same as prune_events but for request_events — grows much faster
        (on every client request, not just on changes), so retention is
        usually shorter. Blocking and proportional to the rows removed: an
        asynchronous caller should run it in a thread."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        deleted = self._delete_requests("ts < ?", (cutoff,))
        if deleted:
            self._sweep_rollups()
        return deleted


    def prune_requests_by_size(self, max_rows: int) -> int:
        """Same idea as prune_events_by_size, but a GLOBAL limit, not
        per-repo: a noticeable share of request_events has repo_id IS NULL
        (path didn't match any repository, see
        runtime.syslog.match_repo_id), so "per repository" doesn't cleanly
        apply here — we just keep the N most recent requests overall.

        "Most recent" is (ts DESC, id ASC), which the ts index provides without a
        sort; rows sharing a timestamp are therefore kept lowest id first. The
        row at position N is the boundary: it and everything after it goes.
        A row recorded in the same second as the boundary while this runs can
        be removed with them, which only matters for limits below one second of
        traffic. Blocking, like prune_requests."""
        with self.db.connect() as conn:
            boundary = conn.execute(
                "SELECT ts, id FROM request_events ORDER BY ts DESC, id ASC LIMIT 1 OFFSET ?",
                (max_rows,),
            ).fetchone()
        if boundary is None:
            return 0
        boundary_ts, boundary_id = boundary
        deleted = self._delete_requests("ts < ?", (boundary_ts,))
        deleted += self._delete_requests("ts = ? AND id >= ?", (boundary_ts, boundary_id))
        if deleted:
            self._sweep_rollups()
        return deleted
