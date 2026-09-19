import sqlite3

from repowatch.models import RepoSnapshot
from repowatch.runtime.context import ServiceState


def _store(tmp_path) -> ServiceState:
    return ServiceState(tmp_path / "state.sqlite3")


def test_record_snapshot_persists_names(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(
        RepoSnapshot(
            repo_id="r",
            packages={"linux-6.11.2-1": "linux-6.11.2-1-x86_64.pkg.tar.zst"},
            names={"linux-6.11.2-1": "linux"},
        )
    )
    assert store.repositories.get_names("r") == {"linux-6.11.2-1": "linux"}


def test_record_snapshot_persists_content_hash(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(RepoSnapshot(
        repo_id="r", packages={"a-1": "a.deb"}, content_hashes={"a-1": "f" * 64},
    ))
    with store.database.connect() as conn:
        assert conn.execute(
            "SELECT content_hash FROM repo_packages WHERE repo_id = 'r' AND package_key = 'a-1'"
        ).fetchone()[0] == "f" * 64


def test_record_snapshot_leaves_content_hash_null_when_not_given(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(RepoSnapshot(repo_id="r", packages={"a-1": "a.apk"}))
    with store.database.connect() as conn:
        assert conn.execute(
            "SELECT content_hash FROM repo_packages WHERE repo_id = 'r' AND package_key = 'a-1'"
        ).fetchone()[0] is None


def test_find_duplicate_files_groups_by_filename_and_hash_across_repos(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(RepoSnapshot(
        repo_id="ubuntu", packages={"a-1": "pool/main/a/a.deb"}, content_hashes={"a-1": "f" * 64},
    ))
    store.repositories.record_snapshot(RepoSnapshot(
        repo_id="debian", packages={"a-1": "pool/main/a/a.deb"}, content_hashes={"a-1": "f" * 64},
    ))
    # Same filename, DIFFERENT hash — must not be treated as a duplicate.
    store.repositories.record_snapshot(RepoSnapshot(
        repo_id="fork", packages={"b-1": "pool/main/a/a.deb"}, content_hashes={"b-1": "e" * 64},
    ))
    assert store.cache.find_duplicate_files() == [("ubuntu", "debian", "pool/main/a/a.deb")]


def test_find_duplicate_files_ignores_packages_without_a_hash(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(RepoSnapshot(repo_id="apk1", packages={"a-1": "same.apk"}))
    store.repositories.record_snapshot(RepoSnapshot(repo_id="apk2", packages={"a-1": "same.apk"}))
    assert store.cache.find_duplicate_files() == []


def test_find_duplicate_files_canonical_choice_is_stable_across_calls(tmp_path):
    store = _store(tmp_path)
    for repo_id in ("zzz", "aaa", "mmm"):
        store.repositories.record_snapshot(RepoSnapshot(
            repo_id=repo_id, packages={"a-1": "a.deb"}, content_hashes={"a-1": "f" * 64},
        ))
    result = store.cache.find_duplicate_files()
    assert result == store.cache.find_duplicate_files()
    assert all(canonical == "aaa" for _, canonical, _ in result)


def test_record_snapshot_persists_normalized_packages_and_removes_old_rows(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(
        RepoSnapshot(
            repo_id="r",
            packages={"old-1": "old-1.apk", "keep-1": "keep-1.apk"},
            names={"old-1": "old", "keep-1": "keep"},
        )
    )
    store.repositories.record_snapshot(
        RepoSnapshot(
            repo_id="r",
            packages={"keep-1": "keep-renamed.apk", "new-1": "new-1.apk"},
            names={"keep-1": "keep", "new-1": "new"},
        )
    )

    with store.database.connect() as conn:
        rows = conn.execute(
            """
            SELECT package_key, package_name, filename FROM repo_packages
            WHERE repo_id = ? ORDER BY package_key
            """,
            ("r",),
        ).fetchall()

    assert rows == [
        ("keep-1", "keep", "keep-renamed.apk"),
        ("new-1", "new", "new-1.apk"),
    ]
    assert store.repositories.get_packages("r") == {
        "keep-1": "keep-renamed.apk",
        "new-1": "new-1.apk",
    }


def test_existing_json_snapshot_is_backfilled_on_startup(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE repo_state (
                repo_id TEXT PRIMARY KEY,
                last_check TEXT NOT NULL,
                changed_at TEXT,
                packages_json TEXT NOT NULL,
                names_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO repo_state
                (repo_id, last_check, changed_at, packages_json, names_json)
            VALUES (?, ?, NULL, ?, ?)
            """,
            (
                "legacy",
                "2026-01-01T00:00:00+00:00",
                '{"musl-1": "musl-1.apk", "zlib-1": "zlib-1.apk"}',
                '{"musl-1": "musl", "zlib-1": "zlib"}',
            ),
        )

    store = ServiceState(db_path)

    assert store.repositories.get_packages("legacy") == {
        "musl-1": "musl-1.apk",
        "zlib-1": "zlib-1.apk",
    }
    assert store.repositories.get_names("legacy") == {"musl-1": "musl", "zlib-1": "zlib"}
    with store.database.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM repo_packages WHERE repo_id = ?", ("legacy",)
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT package_count FROM repo_state WHERE repo_id = ?", ("legacy",)
        ).fetchone()[0] == 2


def test_get_packages_by_keys_returns_only_requested_known_packages(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(
        RepoSnapshot(
            repo_id="r",
            packages={"a-1": "a-1.apk", "b-1": "b-1.apk", "c-1": "c-1.apk"},
        )
    )

    assert store.repositories.get_packages_by_keys("r", ["c-1", "missing", "a-1", "a-1"]) == {
        "a-1": "a-1.apk",
        "c-1": "c-1.apk",
    }
    assert store.repositories.get_packages_by_keys("r", []) == {}
    assert store.repositories.get_packages_by_keys("unknown", ["a-1"]) is None


def test_get_names_empty_when_no_snapshot_yet(tmp_path):
    store = _store(tmp_path)
    assert store.repositories.get_names("does-not-exist") == {}


def test_ban_unban_package(tmp_path):
    store = _store(tmp_path)
    assert store.cache.get_banned_packages("r") == []

    store.cache.ban_package("r", "linux-headers")
    assert store.cache.get_banned_packages("r") == ["linux-headers"]

    # banning the same name again — doesn't duplicate or fail
    store.cache.ban_package("r", "linux-headers")
    assert store.cache.get_banned_packages("r") == ["linux-headers"]

    store.cache.unban_package("r", "linux-headers")
    assert store.cache.get_banned_packages("r") == []


def test_bans_are_scoped_per_repo(tmp_path):
    store = _store(tmp_path)
    store.cache.ban_package("repo-a", "foo")
    assert store.cache.get_banned_packages("repo-a") == ["foo"]
    assert store.cache.get_banned_packages("repo-b") == []


def test_remove_warmed_package(tmp_path):
    store = _store(tmp_path)
    store.cache.record_warmed_package("r", "musl-1.2.5-r0", "musl-1.2.5-r0.apk", True, 200)
    assert len(store.cache.get_warmed_packages("r")) == 1

    removed = store.cache.remove_warmed_package("r", "musl-1.2.5-r0")
    assert removed is True
    assert store.cache.get_warmed_packages("r") == []


def test_remove_warmed_package_returns_false_when_absent(tmp_path):
    store = _store(tmp_path)
    assert store.cache.remove_warmed_package("r", "does-not-exist-1.0") is False


def test_ban_packages_bulk_dedupes_and_ignores_repeats(tmp_path):
    store = _store(tmp_path)
    store.cache.ban_package("r", "already-banned")
    store.cache.ban_packages("r", ["a", "b", "already-banned"])
    assert store.cache.get_banned_packages("r") == ["a", "already-banned", "b"]


def test_ban_packages_bulk_noop_on_empty_list(tmp_path):
    store = _store(tmp_path)
    store.cache.ban_packages("r", [])
    assert store.cache.get_banned_packages("r") == []


def test_unban_packages_bulk(tmp_path):
    store = _store(tmp_path)
    store.cache.ban_packages("r", ["a", "b", "c"])
    store.cache.unban_packages("r", ["a", "c", "never-was-banned"])
    assert store.cache.get_banned_packages("r") == ["b"]


def test_remove_warmed_packages_bulk_counts_only_existing_rows(tmp_path):
    store = _store(tmp_path)
    store.cache.record_warmed_package("r", "a-1", "a-1.apk", True, 200)
    store.cache.record_warmed_package("r", "b-1", "b-1.apk", True, 200)
    store.cache.record_warmed_package("r", "c-1", "c-1.apk", True, 200)

    removed = store.cache.remove_warmed_packages("r", ["a-1", "c-1", "does-not-exist"])

    assert removed == 2
    assert {p["package_key"] for p in store.cache.get_warmed_packages("r")} == {"b-1"}


def test_remove_warmed_packages_bulk_noop_on_empty_list(tmp_path):
    store = _store(tmp_path)
    store.cache.record_warmed_package("r", "a-1", "a-1.apk", True, 200)
    assert store.cache.remove_warmed_packages("r", []) == 0
    assert len(store.cache.get_warmed_packages("r")) == 1


def test_find_stale_warmed_finds_warmed_entries_with_no_current_package(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(RepoSnapshot("r", {"keep-1": "keep-1.deb"}))
    store.cache.record_warmed_package("r", "keep-1", "keep-1.deb", True, 200)
    # Warmed but its package has since disappeared from the index — stale.
    store.cache.record_warmed_package("r", "gone-1", "gone-1.deb", True, 200)

    stale = store.cache.find_stale_warmed("r")

    assert stale == [{"package_key": "gone-1", "filename": "gone-1.deb"}]


def test_find_stale_warmed_empty_when_nothing_warmed_or_nothing_removed(tmp_path):
    store = _store(tmp_path)
    assert store.cache.find_stale_warmed("r") == []
    store.repositories.record_snapshot(RepoSnapshot("r", {"a-1": "a.deb"}))
    store.cache.record_warmed_package("r", "a-1", "a.deb", True, 200)
    assert store.cache.find_stale_warmed("r") == []


def test_find_stale_warmed_is_scoped_per_repo(tmp_path):
    store = _store(tmp_path)
    store.cache.record_warmed_package("repo-a", "gone-1", "gone-1.deb", True, 200)
    assert store.cache.find_stale_warmed("repo-a") == [{"package_key": "gone-1", "filename": "gone-1.deb"}]
    assert store.cache.find_stale_warmed("repo-b") == []


def test_get_warmed_filenames_returns_only_the_requested_existing_keys(tmp_path):
    store = _store(tmp_path)
    store.cache.record_warmed_package("r", "a-1", "a.deb", True, 200)
    store.cache.record_warmed_package("r", "b-1", "b.deb", True, 200)

    assert store.cache.get_warmed_filenames("r", ["a-1", "does-not-exist"]) == {"a-1": "a.deb"}
    assert store.cache.get_warmed_filenames("r", []) == {}


def test_get_storage_stats_reports_db_size_and_table_counts(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(RepoSnapshot("r", {"a-1": "a.deb", "b-1": "b.deb"}))
    store.cache.record_warmed_package("r", "a-1", "a.deb", True, 200)
    store.cache.ban_package("r", "banned-name")

    stats = store.database.get_storage_stats()

    assert stats["state_db_bytes"] > 0
    assert stats["tables"]["repo_packages"] == 2
    assert stats["tables"]["warmed_packages"] == 1
    assert stats["tables"]["prefetch_bans"] == 1
    assert stats["tables"]["repo_events"] == 1  # the initial snapshot is itself a "change"
    assert stats["tables"]["request_events"] == 0


def test_record_snapshot_sets_package_count(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(
        RepoSnapshot(
            repo_id="r",
            packages={"a-1": "a-1.apk", "b-1": "b-1.apk", "c-1": "c-1.apk"},
        )
    )
    assert store.repositories.get_status("r")["package_count"] == 3


def test_record_snapshot_updates_package_count_on_second_call(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(RepoSnapshot(repo_id="r", packages={"a-1": "a-1.apk"}))
    store.repositories.record_snapshot(
        RepoSnapshot(repo_id="r", packages={"a-1": "a-1.apk", "b-1": "b-1.apk"})
    )
    assert store.repositories.get_status("r")["package_count"] == 2


def test_get_status_returns_none_package_count_when_never_checked(tmp_path):
    store = _store(tmp_path)
    assert store.repositories.get_status("does-not-exist") is None


def test_get_top_client_ips_orders_by_count_desc(tmp_path):
    store = _store(tmp_path)
    store.requests.record_request("r", "1.1.1.1", "GET", "/x", "200", "HIT")
    store.requests.record_request("r", "1.1.1.1", "GET", "/y", "200", "HIT")
    store.requests.record_request("r", "2.2.2.2", "GET", "/x", "200", "HIT")

    top = store.requests.get_top_client_ips()

    assert top[0] == {"key": "1.1.1.1", "count": 2}
    assert top[1] == {"key": "2.2.2.2", "count": 1}


def test_get_top_client_ips_filters_by_repo(tmp_path):
    store = _store(tmp_path)
    store.requests.record_request("repo-a", "1.1.1.1", "GET", "/x", "200", "HIT")
    store.requests.record_request("repo-b", "2.2.2.2", "GET", "/x", "200", "HIT")

    top = store.requests.get_top_client_ips(repo_id="repo-a")

    assert top == [{"key": "1.1.1.1", "count": 1}]


def test_get_top_client_ips_ignores_null_client_ip(tmp_path):
    store = _store(tmp_path)
    store.requests.record_request("r", None, "GET", "/x", "200", "HIT")
    assert store.requests.get_top_client_ips() == []


def test_get_top_request_paths_orders_by_count_desc(tmp_path):
    store = _store(tmp_path)
    store.requests.record_request("r", "1.1.1.1", "GET", "/popular.apk", "200", "HIT")
    store.requests.record_request("r", "2.2.2.2", "GET", "/popular.apk", "200", "HIT")
    store.requests.record_request("r", "3.3.3.3", "GET", "/rare.apk", "200", "HIT")

    top = store.requests.get_top_request_paths()

    assert top[0] == {"key": "/popular.apk", "count": 2}
    assert top[1] == {"key": "/rare.apk", "count": 1}



def test_bump_failure_increments_and_starts_unnotified(tmp_path):
    store = _store(tmp_path)
    assert store.notifications.bump_failure("r", "gpg", "bad sig") == (1, False)
    assert store.notifications.bump_failure("r", "gpg", "bad sig again") == (2, False)


def test_bump_failure_scoped_per_repo_and_kind(tmp_path):
    store = _store(tmp_path)
    store.notifications.bump_failure("r", "gpg", "x")
    store.notifications.bump_failure("r", "prefetch", "y")
    store.notifications.bump_failure("other-repo", "gpg", "z")

    assert store.notifications.bump_failure("r", "gpg", "x") == (2, False)
    assert store.notifications.bump_failure("r", "prefetch", "y") == (2, False)
    assert store.notifications.bump_failure("other-repo", "gpg", "z") == (2, False)


def test_mark_failure_notified_is_reflected_in_next_bump(tmp_path):
    store = _store(tmp_path)
    store.notifications.bump_failure("r", "gpg", "x")
    store.notifications.mark_failure_notified("r", "gpg")

    assert store.notifications.bump_failure("r", "gpg", "x") == (2, True)


def test_reset_failure_clears_series_and_reports_prior_notification(tmp_path):
    store = _store(tmp_path)
    store.notifications.bump_failure("r", "gpg", "x")
    store.notifications.mark_failure_notified("r", "gpg")

    assert store.notifications.reset_failure("r", "gpg") is True
    # the streak is fully reset — the next failure starts at 1 again, not notified
    assert store.notifications.bump_failure("r", "gpg", "x") == (1, False)


def test_reset_failure_without_prior_series_is_a_noop(tmp_path):
    store = _store(tmp_path)
    assert store.notifications.reset_failure("never-failed", "gpg") is False


def test_reset_failure_reports_false_when_series_was_never_notified(tmp_path):
    store = _store(tmp_path)
    store.notifications.bump_failure("r", "prefetch", "x")
    assert store.notifications.reset_failure("r", "prefetch") is False


def test_get_failure_counts_reflects_active_streaks_only(tmp_path):
    store = _store(tmp_path)
    store.notifications.bump_failure("r", "gpg", "x")
    store.notifications.bump_failure("r", "gpg", "x")
    store.notifications.bump_failure("r", "prefetch", "y")
    store.notifications.bump_failure("other-repo", "gpg", "z")

    assert store.notifications.get_failure_counts() == {
        ("r", "gpg"): 2,
        ("r", "prefetch"): 1,
        ("other-repo", "gpg"): 1,
    }

    store.notifications.reset_failure("r", "prefetch")
    assert ("r", "prefetch") not in store.notifications.get_failure_counts()


def test_get_ban_counts_groups_by_repo(tmp_path):
    store = _store(tmp_path)
    store.cache.ban_package("r", "musl")
    store.cache.ban_package("r", "linux-headers")
    store.cache.ban_package("other-repo", "musl")

    assert store.cache.get_ban_counts() == {"r": 2, "other-repo": 1}


def test_migration_adds_package_link_and_source_columns_to_existing_db(tmp_path):
    """A database created before item 19's package_key/package_repo_id
    (request_events), source (warmed_packages), and item 20's
    key_expires_at (repo_state) columns must upgrade in place — the CREATE
    INDEX referencing the item 19 columns lives in _migrate(), not SCHEMA,
    specifically so it doesn't run before ALTER TABLE has added them (see
    the comment on idx_request_events_package)."""
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE repo_state (
                repo_id TEXT PRIMARY KEY, last_check TEXT, changed_at TEXT,
                index_etag TEXT, index_last_modified TEXT, package_count INTEGER
            );
            CREATE TABLE repo_packages (
                repo_id TEXT, package_key TEXT, package_name TEXT, filename TEXT,
                content_hash TEXT, PRIMARY KEY(repo_id, package_key));
            CREATE TABLE repo_events (
                id INTEGER PRIMARY KEY, repo_id TEXT, ts TEXT,
                new_packages TEXT, removed_packages TEXT);
            CREATE TABLE request_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, repo_id TEXT,
                client_ip TEXT, method TEXT NOT NULL, path TEXT NOT NULL,
                status TEXT, cache_status TEXT);
            CREATE TABLE warmed_packages (
                repo_id TEXT NOT NULL, package_key TEXT NOT NULL, filename TEXT NOT NULL,
                warmed_at TEXT NOT NULL, status TEXT NOT NULL, http_status INTEGER,
                PRIMARY KEY (repo_id, package_key));
            CREATE TABLE prefetch_bans (repo_id TEXT, package_name TEXT, banned_at TEXT,
                PRIMARY KEY (repo_id, package_name));
            CREATE TABLE failure_state (repo_id TEXT, kind TEXT, consecutive_failures INTEGER,
                last_error TEXT, last_failure_at TEXT, notified INTEGER,
                PRIMARY KEY (repo_id, kind));
            CREATE TABLE host_tokens (id TEXT PRIMARY KEY, name TEXT, token_hash TEXT,
                created_at TEXT, expires_at TEXT, revoked_at TEXT, last_used_at TEXT);
        """)
        conn.execute(
            "INSERT INTO request_events (ts, repo_id, client_ip, method, path, status, cache_status) "
            "VALUES ('2026-01-01T00:00:00+00:00', 'r', '192.0.2.1', 'GET', '/x', '200', 'HIT')"
        )
        conn.execute(
            "INSERT INTO warmed_packages (repo_id, package_key, filename, warmed_at, status, http_status) "
            "VALUES ('r', 'a-1', 'a-1.deb', '2026-01-01T00:00:00+00:00', 'ok', 200)"
        )

    store = ServiceState(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert {"package_key", "package_repo_id"} <= {
            row[1] for row in conn.execute("PRAGMA table_info(request_events)")}
        assert "source" in {row[1] for row in conn.execute("PRAGMA table_info(warmed_packages)")}
        assert "key_expires_at" in {row[1] for row in conn.execute("PRAGMA table_info(repo_state)")}

    # Pre-existing rows survive with NULL for the new columns, not an error.
    assert store.requests.get_request_hit_stats() == {"r": {"total": 1, "hits": 1}}
    assert store.cache.get_warmed_packages("r")[0]["package_key"] == "a-1"

    # Re-opening (idempotent migration) and normal writes both still work.
    store2 = ServiceState(path)
    store2.cache.record_warmed_package("r", "b-1", "b-1.deb", True, 200, source="prefetch")
    assert store2.requests.get_prefetch_efficiency() == [{"repo_id": "r", "prefetched": 1, "used": 0, "ratio": 0.0}]
    store2.repositories.record_key_expiry("r", "2027-01-01T00:00:00+00:00")
    assert store2.repositories.get_repo_summaries()["r"]["key_expires_at"] == "2027-01-01T00:00:00+00:00"


def test_get_request_hit_stats_counts_total_and_hits_per_repo(tmp_path):
    store = _store(tmp_path)
    with store.database.connect() as conn:
        for repo_id, cache_status in [("r", "HIT"), ("r", "MISS"), ("r", "HIT"), (None, "MISS")]:
            conn.execute(
                "INSERT INTO request_events (ts, repo_id, client_ip, method, path, status, cache_status) "
                "VALUES (?, ?, '192.0.2.1', 'GET', '/x', '200', ?)",
                ("2026-01-01T00:00:00+00:00", repo_id, cache_status),
            )

    stats = store.requests.get_request_hit_stats()
    assert stats["r"] == {"total": 3, "hits": 2}
    assert stats[None] == {"total": 1, "hits": 0}


def test_get_requests_timeline_buckets_by_hour_and_respects_window_and_repo(tmp_path):
    store = _store(tmp_path)
    with store.database.connect() as conn:
        rows = [
            ("2026-01-01T10:05:00+00:00", "r", "HIT"),
            ("2026-01-01T10:40:00+00:00", "r", "MISS"),
            ("2026-01-01T11:05:00+00:00", "r", "HIT"),
            ("2026-01-01T11:06:00+00:00", "other", "HIT"),
        ]
        for ts, repo_id, cache_status in rows:
            conn.execute(
                "INSERT INTO request_events (ts, repo_id, client_ip, method, path, status, cache_status) "
                "VALUES (?, ?, '192.0.2.1', 'GET', '/x', '200', ?)",
                (ts, repo_id, cache_status),
            )
        # far outside any reasonable "hours" window used below
        conn.execute(
            "INSERT INTO request_events (ts, repo_id, client_ip, method, path, status, cache_status) "
            "VALUES ('2020-01-01T00:00:00+00:00', 'r', '192.0.2.1', 'GET', '/x', '200', 'HIT')"
        )

    timeline = store.requests.get_requests_timeline(hours=1_000_000)
    assert timeline[-2:] == [
        {"hour": "2026-01-01T10", "total": 2, "hits": 1},
        {"hour": "2026-01-01T11", "total": 2, "hits": 2},
    ]

    scoped = store.requests.get_requests_timeline(repo_id="r", hours=1_000_000)
    assert scoped[-2:] == [
        {"hour": "2026-01-01T10", "total": 2, "hits": 1},
        {"hour": "2026-01-01T11", "total": 1, "hits": 1},
    ]


def test_get_prefetch_efficiency_links_by_package_key_not_basename_guessing(tmp_path):
    store = _store(tmp_path)
    # Prefetched ahead of demand, later actually requested by a client —
    # counts as "used".
    store.cache.record_warmed_package("r", "used-1", "used-1.deb", True, 200, source="prefetch")
    # Prefetched, never requested — counts against the ratio.
    store.cache.record_warmed_package("r", "unused-1", "unused-1.deb", True, 200, source="prefetch")
    # First seen via a real client request, not repowatch's own prefetch —
    # excluded entirely, it was never a prefetch decision to begin with.
    store.cache.record_warmed_package("r", "client-only-1", "client-only-1.deb", True, 200, source="client")

    with store.database.connect() as conn:
        conn.execute(
            "INSERT INTO request_events (ts, repo_id, client_ip, method, path, status, cache_status, "
            "package_key, package_repo_id) VALUES "
            "('2026-01-01T00:00:00+00:00', 'r', '192.0.2.1', 'GET', '/used-1.deb', '200', 'HIT', "
            "'used-1', 'r')"
        )
        # Same basename/package_key string, but under a DIFFERENT repo — must
        # not count towards repo "r"'s efficiency (the point of matching on
        # (package_repo_id, package_key), not package_key alone).
        conn.execute(
            "INSERT INTO request_events (ts, repo_id, client_ip, method, path, status, cache_status, "
            "package_key, package_repo_id) VALUES "
            "('2026-01-01T00:00:01+00:00', 'other', '192.0.2.1', 'GET', '/unused-1.deb', '200', 'HIT', "
            "'unused-1', 'other')"
        )

    assert store.requests.get_prefetch_efficiency() == [
        {"repo_id": "r", "prefetched": 2, "used": 1, "ratio": 0.5},
    ]


def test_get_prefetch_efficiency_omits_repos_with_nothing_prefetched(tmp_path):
    store = _store(tmp_path)
    store.cache.record_warmed_package("r", "a-1", "a-1.deb", True, 200, source="client")
    assert store.requests.get_prefetch_efficiency() == []


def test_repo_summaries_skip_history_and_keep_counts(tmp_path, monkeypatch):
    store = ServiceState(tmp_path / 'state')
    store.repositories.record_snapshot(RepoSnapshot('a', {'one': 'one.rpm'}))
    store.repositories.record_snapshot(RepoSnapshot('b', {}))
    store.cache.record_warmed_package('a', 'one', 'one.rpm', True, 200)
    store.cache.record_warmed_package('orphan', 'one', 'one.rpm', True, 200)
    full = store.repositories.get_status('a')
    # Loading event JSON is forbidden on the summary path even when it grows
    # to millions of package keys after a first import.
    def unexpected_history(*args, **kwargs):
        raise AssertionError('summary must not decode history JSON')
    monkeypatch.setattr('repowatch.storage.repositories.json.loads', unexpected_history)
    summaries = store.repositories.get_repo_summaries(include_warmed=True)
    assert summaries['a'] == {key: full[key] for key in ('last_check', 'changed_at', 'package_count')} | {'warmed_count': 1, 'key_expires_at': None}
    assert summaries['b']['package_count'] == summaries['b']['warmed_count'] == 0
    assert summaries['orphan'] == {'warmed_count': 1}
    assert 'warmed_count' not in store.repositories.get_repo_summaries()['a']


def test_reader_keeps_snapshot_while_writer_commits(tmp_path):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    store = ServiceState(tmp_path / 'state')
    store.repositories.record_snapshot(RepoSnapshot('r', {'old': 'old.rpm'}))
    reader = sqlite3.connect(store.database.db_path)
    try:
        assert reader.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        reader.execute('BEGIN')
        assert reader.execute('SELECT package_key FROM repo_packages').fetchone()[0] == 'old'
        with ThreadPoolExecutor(max_workers=1) as pool:
            # In rollback-journal mode this commit cannot complete while the
            # read transaction stays open; a timeout increase would not fix it.
            pool.submit(store.repositories.record_snapshot, RepoSnapshot('r', {'new': 'new.rpm'})).result(timeout=3)
        assert reader.execute('SELECT package_key FROM repo_packages').fetchone()[0] == 'old'
        reader.commit()
        assert reader.execute('SELECT package_key FROM repo_packages').fetchone()[0] == 'new'
    finally:
        reader.close()


def test_key_expiry_does_not_create_snapshot(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_key_expiry('r', None)
    assert not store.repositories.has_snapshot('r')
    assert store.repositories.get_packages('r') is None
    assert store.repositories.get_packages_by_keys('r', []) is None
    store.repositories.record_snapshot(RepoSnapshot(repo_id='r', packages={}))
    assert store.repositories.has_snapshot('r')
    assert store.repositories.get_packages('r') == {}


def test_bulk_warmed_selection_respects_sqlite_limit(tmp_path, monkeypatch):
    from contextlib import contextmanager
    store = _store(tmp_path)
    keys = [str(n) for n in range(1100)]
    with store.database.connect() as conn:
        conn.executemany('INSERT INTO warmed_packages (repo_id,package_key,filename,warmed_at,status) '
                         "VALUES ('r',?,?,'2026-01-01','ok')", ((key, key) for key in keys))
    connect = store.database.connect
    @contextmanager
    def limited():
        with connect() as conn:
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
            yield conn
    monkeypatch.setattr(store.database, 'connect', limited)
    assert store.cache.get_warmed_filenames('r', keys + keys) == dict(zip(keys, keys))
    assert store.cache.remove_warmed_packages('r', keys + keys) == len(keys)
    assert store.cache.remove_warmed_packages('r', keys) == 0


def test_purge_does_not_forget_a_concurrent_rewarm(tmp_path, monkeypatch):
    monkeypatch.setattr('repowatch.storage.cache._utcnow', lambda: '2026-09-16T00:00:00+00:00')
    store = _store(tmp_path)
    store.cache.record_warmed_package('r', 'a', 'a.pkg', True, 200)
    records = store.cache.get_warmed_records('r', ['a'])
    store.cache.remove_warmed_package('r', 'a')
    store.cache.record_warmed_package('r', 'a', 'a.pkg', True, 200)
    assert store.cache.remove_purged_records('r', records, ['a']) == 0
    assert store.cache.get_warmed_filenames('r', ['a']) == {'a': 'a.pkg'}


def test_validators_belong_to_source_identity(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(RepoSnapshot('r', {'a': 'a.pkg'}), index_etag='same', source_identity='old')
    assert store.repositories.get_index_meta('r', 'old') == ('same', None)
    assert store.repositories.get_index_meta('r', 'new') == (None, None)


def test_read_only_database_never_initializes_or_writes(tmp_path):
    import pytest
    from repowatch.storage.database import Database
    missing = tmp_path / 'missing' / 'state.sqlite3'
    reader = Database(missing, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        with reader.connect():
            pass
    assert not missing.parent.exists()
    store = _store(tmp_path)
    reader = Database(store.database.db_path, read_only=True)
    with reader.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM repo_state').fetchone() == (0,)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            conn.execute("INSERT INTO repo_state (repo_id,last_check) VALUES ('r','')")


def test_dedup_query_matches_reference_with_aliases_and_mixed_hashes(tmp_path):
    from itertools import groupby
    store = _store(tmp_path)
    for repo_id in ('a', 'b', 'c'):
        packages = {f'pkg-{i}': f'file-{i // 2}.deb' for i in range(80)}
        hashes = {key: str(int(key.split('-')[1]) // 4 % 3) * 64
                  for key in packages if int(key.split('-')[1]) % 7}
        if repo_id == 'c':
            hashes = {key: 'e' * 64 for key in hashes}
        store.repositories.record_snapshot(RepoSnapshot(repo_id, packages, content_hashes=hashes))
    with store.database.connect() as conn:
        rows = conn.execute('SELECT filename,content_hash,repo_id FROM repo_packages '
            'WHERE content_hash IS NOT NULL GROUP BY filename,content_hash,repo_id '
            'ORDER BY filename,content_hash,repo_id').fetchall()
    expected = []
    for _, group in groupby(rows, key=lambda row: row[:2]):
        members = list(group)
        expected.extend((repo_id, members[0][2], filename) for filename, _, repo_id in members[1:])
    assert store.cache.find_duplicate_files() == expected
    assert len(expected) > 0


def test_dedup_preserves_distinct_hash_groups_with_identical_output_tuples(tmp_path):
    store = _store(tmp_path)
    for repo_id in ('a', 'b'):
        store.repositories.record_snapshot(RepoSnapshot(repo_id,
            {'v1':'same.deb','v2':'same.deb','alias':'same.deb'},
            content_hashes={'v1':'a'*64,'v2':'b'*64,'alias':'a'*64}))
    assert store.cache.find_duplicate_files() == [('b','a','same.deb'),('b','a','same.deb')]


def test_request_rollups_upgrade_pre_client_ip_history(tmp_path):
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE request_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, repo_id TEXT,
                method TEXT NOT NULL, path TEXT NOT NULL, status TEXT, cache_status TEXT);
            INSERT INTO request_events(ts,repo_id,method,path,status,cache_status)
            VALUES ('2026-01-01T00:00:00+00:00',NULL,'GET','/old','200','HIT');
        ''')
    store = ServiceState(path)
    assert store.requests.get_request_hit_stats() == {None: {'total': 1, 'hits': 1}}
    assert store.requests.get_top_client_ips() == []
    assert store.requests.get_top_request_paths() == [{'key': '/old', 'count': 1}]
    store = ServiceState(path)
    store.requests.record_request('r', '192.0.2.1', 'GET', '/new', '200', 'MISS')
    assert store.requests.get_top_client_ips() == [{'key': '192.0.2.1', 'count': 1}]
    assert store.requests.prune_requests_by_size(0) == 2
    assert store.requests.get_request_hit_stats() == {}
