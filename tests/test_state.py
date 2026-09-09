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
    assert summaries['a'] == {key: full[key] for key in ('last_check', 'changed_at', 'package_count')} | {'warmed_count': 1}
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
