import sqlite3
from datetime import datetime, timedelta, timezone

from repowatch.runtime.context import ServiceState


def _insert_event(store: ServiceState, repo_id: str, ts: datetime) -> None:
    with sqlite3.connect(store.database.db_path) as conn:
        conn.execute(
            "INSERT INTO repo_events (repo_id, ts, new_pkgs_json, removed_pkgs_json) "
            "VALUES (?, ?, '[]', '[]')",
            (repo_id, ts.isoformat(timespec="seconds")),
        )
        conn.commit()


def test_prune_events_removes_only_old_rows(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    now = datetime.now(timezone.utc)

    _insert_event(store, "repo-a", now - timedelta(days=100))
    _insert_event(store, "repo-a", now - timedelta(days=1))

    removed = store.repositories.prune_events(retention_days=90)

    assert removed == 1
    remaining = store.repositories.get_history("repo-a", limit=10)
    assert len(remaining) == 1


def test_prune_events_noop_when_nothing_old(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    now = datetime.now(timezone.utc)

    _insert_event(store, "repo-a", now - timedelta(days=1))

    removed = store.repositories.prune_events(retention_days=90)

    assert removed == 0
    assert len(store.repositories.get_history("repo-a", limit=10)) == 1


def _insert_warmed(store: ServiceState, repo_id: str, package_key: str, warmed_at: datetime) -> None:
    with sqlite3.connect(store.database.db_path) as conn:
        conn.execute(
            "INSERT INTO warmed_packages (repo_id, package_key, filename, warmed_at, status, http_status) "
            "VALUES (?, ?, ?, ?, 'ok', 200)",
            (repo_id, package_key, f"{package_key}.pkg", warmed_at.isoformat(timespec="seconds")),
        )
        conn.commit()


def test_prune_warmed_packages_removes_only_old_rows(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    now = datetime.now(timezone.utc)

    _insert_warmed(store, "repo-a", "old-pkg-1.0", now - timedelta(days=200))
    _insert_warmed(store, "repo-a", "recent-pkg-1.0", now - timedelta(days=1))

    removed = store.cache.prune_warmed_packages(retention_days=180)

    assert removed == 1
    remaining = store.cache.get_warmed_packages("repo-a")
    assert [r["package_key"] for r in remaining] == ["recent-pkg-1.0"]


def test_prune_warmed_packages_noop_when_nothing_old(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    now = datetime.now(timezone.utc)

    _insert_warmed(store, "repo-a", "recent-pkg-1.0", now - timedelta(days=1))

    removed = store.cache.prune_warmed_packages(retention_days=180)

    assert removed == 0
    assert len(store.cache.get_warmed_packages("repo-a")) == 1


def test_get_stale_warmed_packages_matches_what_prune_would_delete(tmp_path):
    """Read-only counterpart to prune_warmed_packages — used by
    watcher.prune_all to purge the real cache entry before the bookkeeping
    row disappears (docs_dev/ROADMAP.md item 33). Must select exactly the
    same rows prune_warmed_packages would delete, not an approximation."""
    store = ServiceState(tmp_path / "state.sqlite3")
    now = datetime.now(timezone.utc)

    _insert_warmed(store, "repo-a", "old-pkg-1.0", now - timedelta(days=200))
    _insert_warmed(store, "repo-a", "recent-pkg-1.0", now - timedelta(days=1))
    _insert_warmed(store, "repo-b", "also-old-1.0", now - timedelta(days=181))

    stale = store.cache.get_stale_warmed_packages(retention_days=180)

    assert sorted(stale) == [
        ("repo-a", "old-pkg-1.0", "old-pkg-1.0.pkg"),
        ("repo-b", "also-old-1.0", "also-old-1.0.pkg"),
    ]
    # Read-only: nothing actually removed.
    assert len(store.cache.get_warmed_packages("repo-a")) == 2


def test_get_stale_warmed_packages_empty_when_nothing_old(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    now = datetime.now(timezone.utc)
    _insert_warmed(store, "repo-a", "recent-pkg-1.0", now - timedelta(days=1))

    assert store.cache.get_stale_warmed_packages(retention_days=180) == []


def test_prune_events_by_size_keeps_only_n_most_recent_per_repo(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    now = datetime.now(timezone.utc)

    for i in range(5):
        _insert_event(store, "repo-a", now - timedelta(minutes=i))

    removed = store.repositories.prune_events_by_size(max_rows_per_repo=2)

    assert removed == 3
    remaining = store.repositories.get_history("repo-a", limit=10)
    assert len(remaining) == 2
    # what's left are the MOST RECENT ones (i=0, i=1), not the oldest
    assert remaining[0]["ts"] == (now - timedelta(minutes=0)).isoformat(timespec="seconds")
    assert remaining[1]["ts"] == (now - timedelta(minutes=1)).isoformat(timespec="seconds")


def test_prune_events_by_size_is_per_repo_not_global(tmp_path):
    """An active repository must not push out a quiet one's history — the
    limit applies to EACH repo_id separately."""
    store = ServiceState(tmp_path / "state.sqlite3")
    now = datetime.now(timezone.utc)

    for i in range(3):
        _insert_event(store, "repo-busy", now - timedelta(minutes=i))
    _insert_event(store, "repo-quiet", now - timedelta(days=10))

    removed = store.repositories.prune_events_by_size(max_rows_per_repo=2)

    assert removed == 1
    assert len(store.repositories.get_history("repo-busy", limit=10)) == 2
    assert len(store.repositories.get_history("repo-quiet", limit=10)) == 1


def test_prune_events_by_size_noop_when_under_limit(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    _insert_event(store, "repo-a", datetime.now(timezone.utc))

    removed = store.repositories.prune_events_by_size(max_rows_per_repo=10)

    assert removed == 0
    assert len(store.repositories.get_history("repo-a", limit=10)) == 1


def _insert_request(store: ServiceState, repo_id: str | None, ts: datetime) -> None:
    with sqlite3.connect(store.database.db_path) as conn:
        conn.execute(
            "INSERT INTO request_events (ts, repo_id, method, path) VALUES (?, ?, 'GET', '/x')",
            (ts.isoformat(timespec="seconds"), repo_id),
        )
        conn.commit()


def test_prune_requests_by_size_keeps_n_most_recent_globally(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    now = datetime.now(timezone.utc)

    # a mix of repositories and unmatched (repo_id NULL) requests — the
    # limit is global, per-repo doesn't apply here (see ServiceState.requests.prune_requests_by_size)
    _insert_request(store, "repo-a", now - timedelta(minutes=0))
    _insert_request(store, "repo-b", now - timedelta(minutes=1))
    _insert_request(store, None, now - timedelta(minutes=2))
    _insert_request(store, "repo-a", now - timedelta(minutes=3))

    removed = store.requests.prune_requests_by_size(max_rows=2)

    assert removed == 2
    remaining = store.requests.get_recent_requests(limit=10)
    assert len(remaining) == 2
    assert remaining[0]["ts"] == (now - timedelta(minutes=0)).isoformat(timespec="seconds")
    assert remaining[1]["ts"] == (now - timedelta(minutes=1)).isoformat(timespec="seconds")


def test_prune_requests_by_size_noop_when_under_limit(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    _insert_request(store, "repo-a", datetime.now(timezone.utc))

    removed = store.requests.prune_requests_by_size(max_rows=10)

    assert removed == 0
    assert len(store.requests.get_recent_requests(limit=10)) == 1
