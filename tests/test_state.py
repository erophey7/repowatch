import sqlite3

from repowatch.state import RepoSnapshot, StateStore


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.sqlite3")


def test_record_snapshot_persists_names(tmp_path):
    store = _store(tmp_path)
    store.record_snapshot(
        RepoSnapshot(
            repo_id="r",
            packages={"linux-6.11.2-1": "linux-6.11.2-1-x86_64.pkg.tar.zst"},
            names={"linux-6.11.2-1": "linux"},
        )
    )
    assert store.get_names("r") == {"linux-6.11.2-1": "linux"}


def test_record_snapshot_persists_content_hash(tmp_path):
    store = _store(tmp_path)
    store.record_snapshot(RepoSnapshot(
        repo_id="r", packages={"a-1": "a.deb"}, content_hashes={"a-1": "f" * 64},
    ))
    with store._connect() as conn:
        assert conn.execute(
            "SELECT content_hash FROM repo_packages WHERE repo_id = 'r' AND package_key = 'a-1'"
        ).fetchone()[0] == "f" * 64


def test_record_snapshot_leaves_content_hash_null_when_not_given(tmp_path):
    store = _store(tmp_path)
    store.record_snapshot(RepoSnapshot(repo_id="r", packages={"a-1": "a.apk"}))
    with store._connect() as conn:
        assert conn.execute(
            "SELECT content_hash FROM repo_packages WHERE repo_id = 'r' AND package_key = 'a-1'"
        ).fetchone()[0] is None


def test_find_duplicate_files_groups_by_filename_and_hash_across_repos(tmp_path):
    store = _store(tmp_path)
    store.record_snapshot(RepoSnapshot(
        repo_id="ubuntu", packages={"a-1": "pool/main/a/a.deb"}, content_hashes={"a-1": "f" * 64},
    ))
    store.record_snapshot(RepoSnapshot(
        repo_id="debian", packages={"a-1": "pool/main/a/a.deb"}, content_hashes={"a-1": "f" * 64},
    ))
    # Same filename, DIFFERENT hash — must not be treated as a duplicate.
    store.record_snapshot(RepoSnapshot(
        repo_id="fork", packages={"b-1": "pool/main/a/a.deb"}, content_hashes={"b-1": "e" * 64},
    ))
    assert store.find_duplicate_files() == [("ubuntu", "debian", "pool/main/a/a.deb")]


def test_find_duplicate_files_ignores_packages_without_a_hash(tmp_path):
    store = _store(tmp_path)
    store.record_snapshot(RepoSnapshot(repo_id="apk1", packages={"a-1": "same.apk"}))
    store.record_snapshot(RepoSnapshot(repo_id="apk2", packages={"a-1": "same.apk"}))
    assert store.find_duplicate_files() == []


def test_find_duplicate_files_canonical_choice_is_stable_across_calls(tmp_path):
    store = _store(tmp_path)
    for repo_id in ("zzz", "aaa", "mmm"):
        store.record_snapshot(RepoSnapshot(
            repo_id=repo_id, packages={"a-1": "a.deb"}, content_hashes={"a-1": "f" * 64},
        ))
    result = store.find_duplicate_files()
    assert result == store.find_duplicate_files()
    assert all(canonical == "aaa" for _, canonical, _ in result)


def test_record_snapshot_persists_normalized_packages_and_removes_old_rows(tmp_path):
    store = _store(tmp_path)
    store.record_snapshot(
        RepoSnapshot(
            repo_id="r",
            packages={"old-1": "old-1.apk", "keep-1": "keep-1.apk"},
            names={"old-1": "old", "keep-1": "keep"},
        )
    )
    store.record_snapshot(
        RepoSnapshot(
            repo_id="r",
            packages={"keep-1": "keep-renamed.apk", "new-1": "new-1.apk"},
            names={"keep-1": "keep", "new-1": "new"},
        )
    )

    with store._connect() as conn:
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
    assert store.get_packages("r") == {
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

    store = StateStore(db_path)

    assert store.get_packages("legacy") == {
        "musl-1": "musl-1.apk",
        "zlib-1": "zlib-1.apk",
    }
    assert store.get_names("legacy") == {"musl-1": "musl", "zlib-1": "zlib"}
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM repo_packages WHERE repo_id = ?", ("legacy",)
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT package_count FROM repo_state WHERE repo_id = ?", ("legacy",)
        ).fetchone()[0] == 2


def test_get_packages_by_keys_returns_only_requested_known_packages(tmp_path):
    store = _store(tmp_path)
    store.record_snapshot(
        RepoSnapshot(
            repo_id="r",
            packages={"a-1": "a-1.apk", "b-1": "b-1.apk", "c-1": "c-1.apk"},
        )
    )

    assert store.get_packages_by_keys("r", ["c-1", "missing", "a-1", "a-1"]) == {
        "a-1": "a-1.apk",
        "c-1": "c-1.apk",
    }
    assert store.get_packages_by_keys("r", []) == {}
    assert store.get_packages_by_keys("unknown", ["a-1"]) is None


def test_get_names_empty_when_no_snapshot_yet(tmp_path):
    store = _store(tmp_path)
    assert store.get_names("does-not-exist") == {}


def test_ban_unban_package(tmp_path):
    store = _store(tmp_path)
    assert store.get_banned_packages("r") == []

    store.ban_package("r", "linux-headers")
    assert store.get_banned_packages("r") == ["linux-headers"]

    # banning the same name again — doesn't duplicate or fail
    store.ban_package("r", "linux-headers")
    assert store.get_banned_packages("r") == ["linux-headers"]

    store.unban_package("r", "linux-headers")
    assert store.get_banned_packages("r") == []


def test_bans_are_scoped_per_repo(tmp_path):
    store = _store(tmp_path)
    store.ban_package("repo-a", "foo")
    assert store.get_banned_packages("repo-a") == ["foo"]
    assert store.get_banned_packages("repo-b") == []


def test_remove_warmed_package(tmp_path):
    store = _store(tmp_path)
    store.record_warmed_package("r", "musl-1.2.5-r0", "musl-1.2.5-r0.apk", True, 200)
    assert len(store.get_warmed_packages("r")) == 1

    removed = store.remove_warmed_package("r", "musl-1.2.5-r0")
    assert removed is True
    assert store.get_warmed_packages("r") == []


def test_remove_warmed_package_returns_false_when_absent(tmp_path):
    store = _store(tmp_path)
    assert store.remove_warmed_package("r", "does-not-exist-1.0") is False


def test_ban_packages_bulk_dedupes_and_ignores_repeats(tmp_path):
    store = _store(tmp_path)
    store.ban_package("r", "already-banned")
    store.ban_packages("r", ["a", "b", "already-banned"])
    assert store.get_banned_packages("r") == ["a", "already-banned", "b"]


def test_ban_packages_bulk_noop_on_empty_list(tmp_path):
    store = _store(tmp_path)
    store.ban_packages("r", [])
    assert store.get_banned_packages("r") == []


def test_unban_packages_bulk(tmp_path):
    store = _store(tmp_path)
    store.ban_packages("r", ["a", "b", "c"])
    store.unban_packages("r", ["a", "c", "never-was-banned"])
    assert store.get_banned_packages("r") == ["b"]


def test_remove_warmed_packages_bulk_counts_only_existing_rows(tmp_path):
    store = _store(tmp_path)
    store.record_warmed_package("r", "a-1", "a-1.apk", True, 200)
    store.record_warmed_package("r", "b-1", "b-1.apk", True, 200)
    store.record_warmed_package("r", "c-1", "c-1.apk", True, 200)

    removed = store.remove_warmed_packages("r", ["a-1", "c-1", "does-not-exist"])

    assert removed == 2
    assert {p["package_key"] for p in store.get_warmed_packages("r")} == {"b-1"}


def test_remove_warmed_packages_bulk_noop_on_empty_list(tmp_path):
    store = _store(tmp_path)
    store.record_warmed_package("r", "a-1", "a-1.apk", True, 200)
    assert store.remove_warmed_packages("r", []) == 0
    assert len(store.get_warmed_packages("r")) == 1


def test_find_stale_warmed_finds_warmed_entries_with_no_current_package(tmp_path):
    store = _store(tmp_path)
    store.record_snapshot(RepoSnapshot("r", {"keep-1": "keep-1.deb"}))
    store.record_warmed_package("r", "keep-1", "keep-1.deb", True, 200)
    # Warmed but its package has since disappeared from the index — stale.
    store.record_warmed_package("r", "gone-1", "gone-1.deb", True, 200)

    stale = store.find_stale_warmed("r")

    assert stale == [{"package_key": "gone-1", "filename": "gone-1.deb"}]


def test_find_stale_warmed_empty_when_nothing_warmed_or_nothing_removed(tmp_path):
    store = _store(tmp_path)
    assert store.find_stale_warmed("r") == []
    store.record_snapshot(RepoSnapshot("r", {"a-1": "a.deb"}))
    store.record_warmed_package("r", "a-1", "a.deb", True, 200)
    assert store.find_stale_warmed("r") == []


def test_find_stale_warmed_is_scoped_per_repo(tmp_path):
    store = _store(tmp_path)
    store.record_warmed_package("repo-a", "gone-1", "gone-1.deb", True, 200)
    assert store.find_stale_warmed("repo-a") == [{"package_key": "gone-1", "filename": "gone-1.deb"}]
    assert store.find_stale_warmed("repo-b") == []


def test_get_warmed_filenames_returns_only_the_requested_existing_keys(tmp_path):
    store = _store(tmp_path)
    store.record_warmed_package("r", "a-1", "a.deb", True, 200)
    store.record_warmed_package("r", "b-1", "b.deb", True, 200)

    assert store.get_warmed_filenames("r", ["a-1", "does-not-exist"]) == {"a-1": "a.deb"}
    assert store.get_warmed_filenames("r", []) == {}


def test_get_storage_stats_reports_db_size_and_table_counts(tmp_path):
    store = _store(tmp_path)
    store.record_snapshot(RepoSnapshot("r", {"a-1": "a.deb", "b-1": "b.deb"}))
    store.record_warmed_package("r", "a-1", "a.deb", True, 200)
    store.ban_package("r", "banned-name")

    stats = store.get_storage_stats()

    assert stats["state_db_bytes"] > 0
    assert stats["tables"]["repo_packages"] == 2
    assert stats["tables"]["warmed_packages"] == 1
    assert stats["tables"]["prefetch_bans"] == 1
    assert stats["tables"]["repo_events"] == 1  # the initial snapshot is itself a "change"
    assert stats["tables"]["request_events"] == 0


def test_record_snapshot_sets_package_count(tmp_path):
    store = _store(tmp_path)
    store.record_snapshot(
        RepoSnapshot(
            repo_id="r",
            packages={"a-1": "a-1.apk", "b-1": "b-1.apk", "c-1": "c-1.apk"},
        )
    )
    assert store.get_status("r")["package_count"] == 3


def test_record_snapshot_updates_package_count_on_second_call(tmp_path):
    store = _store(tmp_path)
    store.record_snapshot(RepoSnapshot(repo_id="r", packages={"a-1": "a-1.apk"}))
    store.record_snapshot(
        RepoSnapshot(repo_id="r", packages={"a-1": "a-1.apk", "b-1": "b-1.apk"})
    )
    assert store.get_status("r")["package_count"] == 2


def test_get_status_returns_none_package_count_when_never_checked(tmp_path):
    store = _store(tmp_path)
    assert store.get_status("does-not-exist") is None


def test_get_top_client_ips_orders_by_count_desc(tmp_path):
    store = _store(tmp_path)
    store.record_request("r", "1.1.1.1", "GET", "/x", "200", "HIT")
    store.record_request("r", "1.1.1.1", "GET", "/y", "200", "HIT")
    store.record_request("r", "2.2.2.2", "GET", "/x", "200", "HIT")

    top = store.get_top_client_ips()

    assert top[0] == {"key": "1.1.1.1", "count": 2}
    assert top[1] == {"key": "2.2.2.2", "count": 1}


def test_get_top_client_ips_filters_by_repo(tmp_path):
    store = _store(tmp_path)
    store.record_request("repo-a", "1.1.1.1", "GET", "/x", "200", "HIT")
    store.record_request("repo-b", "2.2.2.2", "GET", "/x", "200", "HIT")

    top = store.get_top_client_ips(repo_id="repo-a")

    assert top == [{"key": "1.1.1.1", "count": 1}]


def test_get_top_client_ips_ignores_null_client_ip(tmp_path):
    store = _store(tmp_path)
    store.record_request("r", None, "GET", "/x", "200", "HIT")
    assert store.get_top_client_ips() == []


def test_get_top_request_paths_orders_by_count_desc(tmp_path):
    store = _store(tmp_path)
    store.record_request("r", "1.1.1.1", "GET", "/popular.apk", "200", "HIT")
    store.record_request("r", "2.2.2.2", "GET", "/popular.apk", "200", "HIT")
    store.record_request("r", "3.3.3.3", "GET", "/rare.apk", "200", "HIT")

    top = store.get_top_request_paths()

    assert top[0] == {"key": "/popular.apk", "count": 2}
    assert top[1] == {"key": "/rare.apk", "count": 1}


def test_get_requests_by_repo_includes_unmatched_null_repo(tmp_path):
    store = _store(tmp_path)
    store.record_request("repo-a", "1.1.1.1", "GET", "/x", "200", "HIT")
    store.record_request(None, "1.1.1.1", "GET", "/unmatched", "200", "HIT")
    store.record_request(None, "1.1.1.1", "GET", "/unmatched2", "200", "HIT")

    by_repo = store.get_requests_by_repo()

    assert {"key": "repo-a", "count": 1} in by_repo
    assert {"key": None, "count": 2} in by_repo


def test_bump_failure_increments_and_starts_unnotified(tmp_path):
    store = _store(tmp_path)
    assert store.bump_failure("r", "gpg", "bad sig") == (1, False)
    assert store.bump_failure("r", "gpg", "bad sig again") == (2, False)


def test_bump_failure_scoped_per_repo_and_kind(tmp_path):
    store = _store(tmp_path)
    store.bump_failure("r", "gpg", "x")
    store.bump_failure("r", "prefetch", "y")
    store.bump_failure("other-repo", "gpg", "z")

    assert store.bump_failure("r", "gpg", "x") == (2, False)
    assert store.bump_failure("r", "prefetch", "y") == (2, False)
    assert store.bump_failure("other-repo", "gpg", "z") == (2, False)


def test_mark_failure_notified_is_reflected_in_next_bump(tmp_path):
    store = _store(tmp_path)
    store.bump_failure("r", "gpg", "x")
    store.mark_failure_notified("r", "gpg")

    assert store.bump_failure("r", "gpg", "x") == (2, True)


def test_reset_failure_clears_series_and_reports_prior_notification(tmp_path):
    store = _store(tmp_path)
    store.bump_failure("r", "gpg", "x")
    store.mark_failure_notified("r", "gpg")

    assert store.reset_failure("r", "gpg") is True
    # the streak is fully reset — the next failure starts at 1 again, not notified
    assert store.bump_failure("r", "gpg", "x") == (1, False)


def test_reset_failure_without_prior_series_is_a_noop(tmp_path):
    store = _store(tmp_path)
    assert store.reset_failure("never-failed", "gpg") is False


def test_reset_failure_reports_false_when_series_was_never_notified(tmp_path):
    store = _store(tmp_path)
    store.bump_failure("r", "prefetch", "x")
    assert store.reset_failure("r", "prefetch") is False


def test_get_failure_counts_reflects_active_streaks_only(tmp_path):
    store = _store(tmp_path)
    store.bump_failure("r", "gpg", "x")
    store.bump_failure("r", "gpg", "x")
    store.bump_failure("r", "prefetch", "y")
    store.bump_failure("other-repo", "gpg", "z")

    assert store.get_failure_counts() == {
        ("r", "gpg"): 2,
        ("r", "prefetch"): 1,
        ("other-repo", "gpg"): 1,
    }

    store.reset_failure("r", "prefetch")
    assert ("r", "prefetch") not in store.get_failure_counts()


def test_get_ban_counts_groups_by_repo(tmp_path):
    store = _store(tmp_path)
    store.ban_package("r", "musl")
    store.ban_package("r", "linux-headers")
    store.ban_package("other-repo", "musl")

    assert store.get_ban_counts() == {"r": 2, "other-repo": 1}


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

    store = StateStore(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert {"package_key", "package_repo_id"} <= {
            row[1] for row in conn.execute("PRAGMA table_info(request_events)")}
        assert "source" in {row[1] for row in conn.execute("PRAGMA table_info(warmed_packages)")}
        assert "key_expires_at" in {row[1] for row in conn.execute("PRAGMA table_info(repo_state)")}

    # Pre-existing rows survive with NULL for the new columns, not an error.
    assert store.get_request_hit_stats() == {"r": {"total": 1, "hits": 1}}
    assert store.get_warmed_packages("r")[0]["package_key"] == "a-1"

    # Re-opening (idempotent migration) and normal writes both still work.
    store2 = StateStore(path)
    store2.record_warmed_package("r", "b-1", "b-1.deb", True, 200, source="prefetch")
    assert store2.get_prefetch_efficiency() == [{"repo_id": "r", "prefetched": 1, "used": 0, "ratio": 0.0}]
    store2.record_key_expiry("r", "2027-01-01T00:00:00+00:00")
    assert store2.get_repo_summaries()["r"]["key_expires_at"] == "2027-01-01T00:00:00+00:00"


def test_get_request_hit_stats_counts_total_and_hits_per_repo(tmp_path):
    store = _store(tmp_path)
    with store._connect() as conn:
        for repo_id, cache_status in [("r", "HIT"), ("r", "MISS"), ("r", "HIT"), (None, "MISS")]:
            conn.execute(
                "INSERT INTO request_events (ts, repo_id, client_ip, method, path, status, cache_status) "
                "VALUES (?, ?, '192.0.2.1', 'GET', '/x', '200', ?)",
                ("2026-01-01T00:00:00+00:00", repo_id, cache_status),
            )

    stats = store.get_request_hit_stats()
    assert stats["r"] == {"total": 3, "hits": 2}
    assert stats[None] == {"total": 1, "hits": 0}


def test_get_requests_timeline_buckets_by_hour_and_respects_window_and_repo(tmp_path):
    store = _store(tmp_path)
    with store._connect() as conn:
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

    timeline = store.get_requests_timeline(hours=1_000_000)
    assert timeline[-2:] == [
        {"hour": "2026-01-01T10", "total": 2, "hits": 1},
        {"hour": "2026-01-01T11", "total": 2, "hits": 2},
    ]

    scoped = store.get_requests_timeline(repo_id="r", hours=1_000_000)
    assert scoped[-2:] == [
        {"hour": "2026-01-01T10", "total": 2, "hits": 1},
        {"hour": "2026-01-01T11", "total": 1, "hits": 1},
    ]


def test_get_prefetch_efficiency_links_by_package_key_not_basename_guessing(tmp_path):
    store = _store(tmp_path)
    # Prefetched ahead of demand, later actually requested by a client —
    # counts as "used".
    store.record_warmed_package("r", "used-1", "used-1.deb", True, 200, source="prefetch")
    # Prefetched, never requested — counts against the ratio.
    store.record_warmed_package("r", "unused-1", "unused-1.deb", True, 200, source="prefetch")
    # First seen via a real client request, not repowatch's own prefetch —
    # excluded entirely, it was never a prefetch decision to begin with.
    store.record_warmed_package("r", "client-only-1", "client-only-1.deb", True, 200, source="client")

    with store._connect() as conn:
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

    assert store.get_prefetch_efficiency() == [
        {"repo_id": "r", "prefetched": 2, "used": 1, "ratio": 0.5},
    ]


def test_get_prefetch_efficiency_omits_repos_with_nothing_prefetched(tmp_path):
    store = _store(tmp_path)
    store.record_warmed_package("r", "a-1", "a-1.deb", True, 200, source="client")
    assert store.get_prefetch_efficiency() == []


def test_repo_summaries_skip_history_and_keep_counts(tmp_path, monkeypatch):
    store = StateStore(tmp_path / 'state')
    store.record_snapshot(RepoSnapshot('a', {'one': 'one.rpm'}))
    store.record_snapshot(RepoSnapshot('b', {}))
    store.record_warmed_package('a', 'one', 'one.rpm', True, 200)
    store.record_warmed_package('orphan', 'one', 'one.rpm', True, 200)
    full = store.get_status('a')
    # Loading event JSON is forbidden on the summary path even when it grows
    # to millions of package keys after a first import.
    def unexpected_history(*args, **kwargs):
        raise AssertionError('summary must not decode history JSON')
    monkeypatch.setattr('repowatch.state.json.loads', unexpected_history)
    summaries = store.get_repo_summaries(include_warmed=True)
    assert summaries['a'] == {key: full[key] for key in ('last_check', 'changed_at', 'package_count')} | {'warmed_count': 1, 'key_expires_at': None}
    assert summaries['b']['package_count'] == summaries['b']['warmed_count'] == 0
    assert summaries['orphan'] == {'warmed_count': 1}
    assert 'warmed_count' not in store.get_repo_summaries()['a']


def test_reader_keeps_snapshot_while_writer_commits(tmp_path):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    store = StateStore(tmp_path / 'state')
    store.record_snapshot(RepoSnapshot('r', {'old': 'old.rpm'}))
    reader = sqlite3.connect(store.db_path)
    try:
        assert reader.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        reader.execute('BEGIN')
        assert reader.execute('SELECT package_key FROM repo_packages').fetchone()[0] == 'old'
        with ThreadPoolExecutor(max_workers=1) as pool:
            # In rollback-journal mode this commit cannot complete while the
            # read transaction stays open; a timeout increase would not fix it.
            pool.submit(store.record_snapshot, RepoSnapshot('r', {'new': 'new.rpm'})).result(timeout=3)
        assert reader.execute('SELECT package_key FROM repo_packages').fetchone()[0] == 'old'
        reader.commit()
        assert reader.execute('SELECT package_key FROM repo_packages').fetchone()[0] == 'new'
    finally:
        reader.close()
