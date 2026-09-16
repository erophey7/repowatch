"""Request-event persistence, retention, and statistical aggregates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from repowatch.storage.database import Database, _utcnow

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


    def get_top_client_ips(self, repo_id: str | None = None, limit: int = 10) -> list[dict]:
        """Top client_ip values by request count — for the dashboard chart
        (see reporting.statistics.requests_summary_payload). client_ip can be NULL (old
        log_format without $remote_addr, see request_events.client_ip) —
        such rows are excluded from the top, there's nothing meaningful to
        group them by."""
        with self.db.connect() as conn:
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
        with self.db.connect() as conn:
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
        match any repository, see runtime.syslog.match_repo_id), shown as
        "(unmatched)" on the dashboard side."""
        with self.db.connect() as conn:
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
        request_max_rows) — for reporting.metrics.metrics_payload. A snapshot gauge, not a
        counter: retention can shrink these numbers, they are not
        monotonically increasing. Key None groups requests whose path
        didn't match any configured repo (see runtime.syslog.match_repo_id)
        — the caller decides whether/how to surface that bucket."""
        with self.db.connect() as conn:
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
        with self.db.connect() as conn:
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


    def prune_requests(self, retention_days: int) -> int:
        """Same as prune_events but for request_events — grows much faster
        (on every client request, not just on changes), so retention is
        usually shorter."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        with self.db.connect() as conn:
            cur = conn.execute("DELETE FROM request_events WHERE ts < ?", (cutoff,))
            return cur.rowcount


    def prune_requests_by_size(self, max_rows: int) -> int:
        """Same idea as prune_events_by_size, but a GLOBAL limit, not
        per-repo: a noticeable share of request_events has repo_id IS NULL
        (path didn't match any repository, see
        runtime.syslog.match_repo_id), so "per repository" doesn't cleanly
        apply here — we just keep the N most recent requests overall."""
        with self.db.connect() as conn:
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
