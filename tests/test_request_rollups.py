"""Request statistics are read from trigger-maintained rollups (storage.schema).

The reference for every aggregate is a plain GROUP BY over request_events, the way
the dashboard used to compute them."""

import asyncio
import random
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from repowatch.config.models import Config, StatusServerConfig
from repowatch.operations.cleanup import prune_all
from repowatch.reporting.statistics import requests_summary_payload
from repowatch.runtime.context import ServiceState
from repowatch.storage import requests as requests_module
from repowatch.storage.database import Database

REPOS = ["repo-a", "repo-b", "*", "", None]  # ids that must not collide with the rollup scopes
IPS = ["10.0.0.1", "10.0.0.2", "10.0.0.3", None]
PATHS = [f"/pool/p{i}.deb" for i in range(6)]


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _raw_insert(path, ts, repo_id, client_ip, request_path, cache_status):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO request_events (ts, repo_id, client_ip, method, path, status, cache_status) "
            "VALUES (?, ?, ?, 'GET', ?, '200', ?)",
            (ts, repo_id, client_ip, request_path, cache_status),
        )


def _reference(store: ServiceState, hours: int = 24 * 400) -> dict:
    """The old aggregation queries, run over the raw log."""
    with sqlite3.connect(store.database.db_path) as conn:
        def top(column, repo_id, extra=""):
            where = "WHERE " + column + " IS NOT NULL" + extra
            params: tuple = ()
            if repo_id:
                where += " AND repo_id = ?"
                params = (repo_id,)
            return [
                {"key": key, "count": count}
                for key, count in conn.execute(
                    f"SELECT {column}, COUNT(*) c FROM request_events {where} "
                    f"GROUP BY {column} ORDER BY c DESC, {column} LIMIT 10", params)
            ]

        hits = {
            repo: {"total": total, "hits": hit}
            for repo, total, hit in conn.execute(
                "SELECT repo_id, COUNT(*), SUM(CASE WHEN cache_status = 'HIT' THEN 1 ELSE 0 END) "
                "FROM request_events GROUP BY repo_id")
        }
        hourly = {}
        for repo in {None, *[r for r in REPOS if r]}:
            clause, params = ("repo_id = ?", (repo,)) if repo else ("1", ())
            hourly[repo] = [
                {"hour": hour, "total": total, "hits": hit}
                for hour, total, hit in conn.execute(
                    f"SELECT substr(ts, 1, 13), COUNT(*), SUM(CASE WHEN cache_status = 'HIT' THEN 1 ELSE 0 END) "
                    f"FROM request_events WHERE {clause} GROUP BY 1 ORDER BY 1", params)
            ]
    return {
        "ips": {repo: top("client_ip", repo) for repo in {None, *[r for r in REPOS if r]}},
        "paths": {repo: top("path", repo) for repo in {None, *[r for r in REPOS if r]}},
        "hits": hits,
        "hourly": hourly,
    }


def _actual(store: ServiceState) -> dict:
    repos = {None, *[r for r in REPOS if r]}
    hours = 24 * 400
    return {
        "ips": {repo: store.requests.get_top_client_ips(repo_id=repo) for repo in repos},
        "paths": {repo: store.requests.get_top_request_paths(repo_id=repo) for repo in repos},
        "hits": store.requests.get_request_hit_stats(),
        "hourly": {repo: store.requests.get_requests_timeline(repo_id=repo, hours=hours) for repo in repos},
    }


def _assert_matches_log(store: ServiceState) -> None:
    expected, actual = _reference(store), _actual(store)
    # Both sides order ties by key, so the top lists must be identical, not just equal as sets.
    assert actual == expected


def test_rollups_match_the_log_through_random_writes_and_prunes(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    rng = random.Random(20260919)
    now = datetime.now(timezone.utc)
    for step in range(300):
        action = rng.random()
        ts = _iso(now - timedelta(hours=rng.randrange(0, 60), minutes=rng.randrange(60)))
        repo, ip, path = rng.choice(REPOS), rng.choice(IPS), rng.choice(PATHS)
        status = rng.choice(["HIT", "HIT", "MISS", None])
        if action < 0.6:
            _raw_insert(store.database.db_path, ts, repo, ip, path, status)
        elif action < 0.75:
            store.requests.record_request(repo, ip, "GET", path, "200", status)
        elif action < 0.85:
            with sqlite3.connect(store.database.db_path) as conn:  # direct deletes, no store involved
                conn.execute("DELETE FROM request_events WHERE id IN "
                             "(SELECT id FROM request_events ORDER BY RANDOM() LIMIT 3)")
        elif action < 0.93:
            store.requests.prune_requests(retention_days=1)
        else:
            store.requests.prune_requests_by_size(rng.randrange(0, 40))
        if step % 25 == 0:
            _assert_matches_log(store)
    _assert_matches_log(store)


def test_reserved_looking_repository_ids_do_not_collide_with_scopes(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    store.requests.record_request("*", "10.0.0.1", "GET", "/star", "200", "HIT")
    store.requests.record_request("", "10.0.0.2", "GET", "/empty", "200", "MISS")
    store.requests.record_request(None, "10.0.0.3", "GET", "/none", "200", "HIT")
    store.requests.record_request("repo-a", "10.0.0.4", "GET", "/a", "200", "HIT")

    assert store.requests.get_request_hit_stats() == {
        "*": {"total": 1, "hits": 1}, "": {"total": 1, "hits": 0},
        None: {"total": 1, "hits": 1}, "repo-a": {"total": 1, "hits": 1},
    }
    assert store.requests.get_top_request_paths(repo_id="*") == [{"key": "/star", "count": 1}]
    assert len(store.requests.get_top_request_paths()) == 4  # the global scope sees every request


def test_summary_and_metrics_reads_never_touch_the_request_log(tmp_path, monkeypatch):
    store = ServiceState(tmp_path / "state.sqlite3")
    for i in range(50):
        store.requests.record_request("repo-a", f"10.0.0.{i % 5}", "GET", f"/p{i % 7}", "200", "HIT")

    denied = []
    original = Database.connect

    @contextmanager
    def guarded(self):
        with original(self) as conn:
            def authorize(action, arg1, arg2, database, source):
                if action == sqlite3.SQLITE_READ and arg1 == "request_events":
                    denied.append(source)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            yield conn

    monkeypatch.setattr(Database, "connect", guarded)
    status, payload = requests_summary_payload(store, "repo-a")
    requests_summary_payload(store, None)
    store.requests.get_request_hit_stats()

    assert status == 200 and not denied
    assert payload["by_client_ip"][0]["count"] == 10
    assert payload["cache_hit_stats"] == [{"repo_id": "repo-a", "total": 50, "hits": 50}]


def test_existing_history_is_backfilled_once(tmp_path):
    path = tmp_path / "old.sqlite3"
    ServiceState(path)  # creates the current schema
    with sqlite3.connect(path) as conn:  # simulate a database from before the rollups existed
        for trigger in ("request_rollup_insert", "request_rollup_ip_insert",
                        "request_rollup_delete", "request_rollup_ip_delete"):
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute("DROP TABLE request_counters")
        conn.execute("DROP TABLE request_hourly")
    now = _iso(datetime.now(timezone.utc))
    for repo, ip, request_path, status in [("repo-a", "1.1.1.1", "/x", "HIT"), ("repo-a", "1.1.1.1", "/x", "MISS"),
                                           (None, None, "/y", None), ("repo-b", "2.2.2.2", "/x", "HIT")]:
        _raw_insert(path, now, repo, ip, request_path, status)

    store = ServiceState(path)
    _assert_matches_log(store)
    assert store.requests.get_request_hit_stats()["repo-a"] == {"total": 2, "hits": 1}

    ServiceState(path)  # reopening must not count the history again
    _assert_matches_log(store)
    store.requests.record_request("repo-a", "1.1.1.1", "GET", "/x", "200", "HIT")
    assert store.requests.get_top_request_paths()[0] == {"key": "/x", "count": 4}


def test_interrupted_backfill_leaves_no_partial_rollups(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    ServiceState(path)
    with sqlite3.connect(path) as conn:
        for trigger in ("request_rollup_insert", "request_rollup_ip_insert",
                        "request_rollup_delete", "request_rollup_ip_delete"):
            conn.execute(f"DROP TRIGGER {trigger}")
    _raw_insert(path, _iso(datetime.now(timezone.utc)), "repo-a", "1.1.1.1", "/x", "HIT")

    from repowatch.storage import schema
    monkeypatch.setattr(schema, "_rollup_triggers", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        ServiceState(path)
    monkeypatch.undo()

    store = ServiceState(path)  # nothing was half-applied, so the next start builds it in full
    _assert_matches_log(store)


def test_prune_leaves_no_zero_rows_and_reports_removed_rows(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    old = _iso(datetime.now(timezone.utc) - timedelta(days=30))
    for i in range(12):
        _raw_insert(store.database.db_path, old, "repo-a", "1.1.1.1", f"/p{i}", "HIT")

    assert store.requests.prune_requests(retention_days=7) == 12

    assert store.requests.get_request_hit_stats() == {}
    with sqlite3.connect(store.database.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM request_counters").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM request_hourly").fetchone() == (0,)


def test_chunked_deletes_give_the_same_result(tmp_path, monkeypatch):
    monkeypatch.setattr(requests_module, "_PRUNE_CHUNK", 3)
    store = ServiceState(tmp_path / "state.sqlite3")
    old = _iso(datetime.now(timezone.utc) - timedelta(days=30))
    for i in range(20):
        _raw_insert(store.database.db_path, old, "repo-a", "1.1.1.1", "/old", "HIT")
    for i in range(7):
        store.requests.record_request("repo-b", "2.2.2.2", "GET", "/new", "200", "MISS")

    assert store.requests.prune_requests(retention_days=7) == 20  # exact multiple of no chunk boundary

    _assert_matches_log(store)
    assert store.requests.get_request_hit_stats() == {"repo-b": {"total": 7, "hits": 0}}


def test_size_limit_keeps_newest_rows_and_lowest_ids_within_a_second(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(3):  # three requests in the same second, then older ones
        _raw_insert(store.database.db_path, _iso(base), "repo-a", "1.1.1.1", f"/same{i}", "HIT")
    for i in range(1, 4):
        _raw_insert(store.database.db_path, _iso(base - timedelta(minutes=i)), "repo-a", "1.1.1.1", f"/old{i}", "HIT")

    assert store.requests.prune_requests_by_size(max_rows=2) == 4

    kept = sorted(row["path"] for row in store.queries.get_page("requests", None, limit=10)["items"])
    assert kept == ["/same0", "/same1"]
    _assert_matches_log(store)
    assert store.requests.prune_requests_by_size(max_rows=2) == 0
    assert store.requests.prune_requests_by_size(max_rows=50) == 0
    assert store.requests.prune_requests_by_size(max_rows=0) == 2
    assert store.requests.get_request_hit_stats() == {}


def test_timeline_uses_whole_hour_buckets_and_repo_filter(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    now = datetime.now(timezone.utc)
    cutoff_hour = (now - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    _raw_insert(store.database.db_path, _iso(cutoff_hour + timedelta(minutes=1)), "repo-a", "1.1.1.1", "/a", "HIT")
    _raw_insert(store.database.db_path, _iso(cutoff_hour + timedelta(minutes=59)), "repo-b", "1.1.1.1", "/b", "MISS")
    _raw_insert(store.database.db_path, _iso(cutoff_hour - timedelta(hours=1)), "repo-a", "1.1.1.1", "/c", "HIT")

    everything = store.requests.get_requests_timeline(hours=3)
    assert everything == [{"hour": _iso(cutoff_hour)[:13], "total": 2, "hits": 1}]
    assert store.requests.get_requests_timeline(repo_id="repo-b", hours=3) == [
        {"hour": _iso(cutoff_hour)[:13], "total": 1, "hits": 0}]
    assert store.requests.get_requests_timeline(repo_id="", hours=3) == []
    assert store.requests.get_requests_timeline(repo_id="missing", hours=3) == []


def test_prune_all_applies_retention_off_the_event_loop_and_updates_aggregates(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    old = _iso(datetime.now(timezone.utc) - timedelta(days=30))
    _raw_insert(store.database.db_path, old, "repo-a", "1.1.1.1", "/old", "HIT")
    store.requests.record_request("repo-a", "1.1.1.1", "GET", "/new", "200", "HIT")
    config = Config(state_db=str(tmp_path / "state.sqlite3"), check_interval=300,
                    cache_base_url="http://127.0.0.1:8080", status_server=StatusServerConfig(),
                    request_retention_days=7, request_max_rows=None)

    asyncio.run(prune_all(config, store))

    assert store.requests.get_top_request_paths() == [{"key": "/new", "count": 1}]
    assert store.requests.get_request_hit_stats() == {"repo-a": {"total": 1, "hits": 1}}
