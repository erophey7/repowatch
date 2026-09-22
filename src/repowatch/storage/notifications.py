"""Persistent failure streaks and notification acknowledgements."""

from __future__ import annotations

from repowatch.storage.database import Database, _utcnow

class NotificationsStore:
    """SQL operations for notifications; the caller supplies the shared database."""

    def __init__(self, db: Database):
        self.db = db

    def bump_failure(self, repo_id: str, kind: str, error_message: str) -> tuple[int, bool]:
        """Increment the consecutive-failure counter ("prefetch"/"gpg") by 1.

        Returns (new_count, was_this_streak_already_notified_BEFORE_this_call) —
        the caller (notifications.py) needs the second value to decide
        whether to send a notification right now: we check not "== threshold"
        (which would break if the counter already passed the threshold
        before the operator enabled the webhook), but "threshold reached AND
        not yet notified"."""
        with self.db.connect() as conn:
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
        with self.db.connect() as conn:
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
        with self.db.connect() as conn:
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


    # The CacheStore/RepositoriesStore siblings return a dict of
    # {table: count} since they each own several repo-keyed tables; this
    # store owns exactly one (failure_state, keyed by (repo_id, kind)), so a
    # plain int is enough — operations.cleanup.find_orphaned_repos/
    # purge_orphaned_repos wrap it under the 'failure_state' key themselves.
    def get_repo_ids_with_data(self) -> set[str]:
        with self.db.connect() as conn:
            return {repo_id for (repo_id,) in conn.execute('SELECT DISTINCT repo_id FROM failure_state')}

    def count_repo_rows(self, repo_id: str) -> int:
        with self.db.connect() as conn:
            return conn.execute('SELECT COUNT(*) FROM failure_state WHERE repo_id = ?', (repo_id,)).fetchone()[0]

    def delete_repo_rows(self, repo_id: str) -> int:
        with self.db.connect() as conn:
            return conn.execute('DELETE FROM failure_state WHERE repo_id = ?', (repo_id,)).rowcount


    def get_failure_counts(self) -> dict[tuple[str, str], int]:
        """(repo_id, kind) -> current consecutive_failures, one query for all
        repos/kinds (for reporting.metrics.metrics_payload). A row only exists here while
        a streak is active — reset_failure deletes it on the first success,
        so an absent (repo_id, kind) pair means "currently healthy", not
        "never failed"."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT repo_id, kind, consecutive_failures FROM failure_state"
            ).fetchall()
        return {(repo_id, kind): count for repo_id, kind, count in rows}
