import asyncio
from datetime import datetime, timedelta, timezone

from repowatch import watcher
from repowatch.config import Config, RepoConfig, StatusServerConfig
from repowatch.gpgverify import SignatureError
from repowatch.parsers.base import IndexHeadResult
from repowatch.state import RepoSnapshot, StateStore
from repowatch.watcher import _is_due, check_all, check_repo


def _config(**overrides) -> Config:
    return Config(
        state_db="/tmp/unused.sqlite3",
        check_interval=300,
        cache_base_url="http://127.0.0.1:8080",
        status_server=StatusServerConfig(),
        **overrides,
    )


def test_is_due_true_when_never_checked(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    repo = RepoConfig(id="r", type="apk", upstream="https://example.org", arch="x86_64")
    assert _is_due(_config(), repo, store) is True


def test_is_due_false_right_after_check(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.record_snapshot(RepoSnapshot(repo_id="r", packages={}))
    repo = RepoConfig(
        id="r", type="apk", upstream="https://example.org", arch="x86_64", check_interval=300,
    )
    assert _is_due(_config(), repo, store) is False


def test_is_due_respects_per_repo_short_interval(tmp_path, monkeypatch):
    """Regression for the feature itself: a repository with a short
    check_interval must be considered "due" even when the global interval
    is much longer and the last check was recent."""
    store = StateStore(tmp_path / "state.sqlite3")

    old_check = (datetime.now(timezone.utc) - timedelta(seconds=40)).isoformat(timespec="seconds")
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO repo_state (repo_id, last_check, changed_at) "
            "VALUES (?, ?, NULL)",
            ("fast-repo", old_check),
        )

    fast_repo = RepoConfig(
        id="fast-repo", type="apk", upstream="https://example.org", arch="x86_64",
        check_interval=30,
    )
    slow_repo = RepoConfig(
        id="fast-repo", type="apk", upstream="https://example.org", arch="x86_64",
        check_interval=300,
    )

    config = _config()  # check_interval=300 by default
    # same last_check (40s ago): due for its own short interval (30s),
    # not yet due for the long default (300s)
    assert _is_due(config, fast_repo, store) is True
    assert _is_due(config, slow_repo, store) is False


def test_check_all_checks_due_repos_concurrently(tmp_path, monkeypatch):
    """check_all no longer checks repositories strictly one at a time (see
    watcher.check_all) — a slow/hung upstream for one repo must not delay
    checking the rest in the same tick."""
    store = StateStore(tmp_path / "state.sqlite3")
    repos = [
        RepoConfig(id=f"repo-{i}", type="apk", upstream="https://example.org", arch="x86_64")
        for i in range(4)
    ]
    config = _config(repos=repos, check_concurrency=4)

    active = 0
    max_active = 0

    async def fake_check_repo(cfg, repo, st):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)  # keep the task "busy" so others get a chance to start
        active -= 1

    monkeypatch.setattr("repowatch.watcher.check_repo", fake_check_repo)

    asyncio.run(check_all(config, store))

    assert max_active > 1


def test_check_all_skips_repos_not_due(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.sqlite3")
    store.record_snapshot(RepoSnapshot(repo_id="r", packages={}))
    repo = RepoConfig(
        id="r", type="apk", upstream="https://example.org", arch="x86_64", check_interval=300,
    )
    config = _config(repos=[repo])

    calls = []

    async def fake_check_repo(cfg, repo, st):
        calls.append(repo.id)

    monkeypatch.setattr("repowatch.watcher.check_repo", fake_check_repo)

    asyncio.run(check_all(config, store))

    assert calls == []


class _SignatureErrorParser:
    """A stub parser whose fetch() always fails with SignatureError — for
    testing the separate except branch in check_repo (see notifications.py)."""

    def __init__(self, repo):
        self.repo = repo

    async def check_index_changed(self, client, prev_etag, prev_last_modified):
        return IndexHeadResult(unchanged=False, etag=None, last_modified=None)

    async def fetch(self, client):
        raise SignatureError("BADSIG")


class _SucceedingParser:
    def __init__(self, repo):
        self.repo = repo

    async def check_index_changed(self, client, prev_etag, prev_last_modified):
        return IndexHeadResult(unchanged=False, etag=None, last_modified=None)

    async def fetch(self, client):
        from repowatch.state import RepoSnapshot
        return RepoSnapshot(repo_id=self.repo.id, packages={})


def test_check_repo_records_gpg_failure_on_signature_error(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.sqlite3")
    repo = RepoConfig(id="r", type="apk", upstream="https://example.org", arch="x86_64")
    config = _config(repos=[repo])

    monkeypatch.setitem(watcher.PARSERS, "apk", _SignatureErrorParser)

    asyncio.run(check_repo(config, repo, store))

    # bump_failure on an already-existing streak returns count 2 — meaning
    # check_repo already recorded this streak's first failure
    assert store.bump_failure("r", "gpg", "peek") == (2, False)


def test_check_repo_resets_gpg_failure_after_success_when_verify_signature_enabled(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.sqlite3")
    store.bump_failure("r", "gpg", "previous failure")
    repo = RepoConfig(
        id="r", type="pacman", upstream="https://example.org", arch="x86_64", repo_name="core",
        verify_signature=True, keyring_path="/fake/keyring.gpg",
    )
    config = _config(repos=[repo])

    monkeypatch.setitem(watcher.PARSERS, "pacman", _SucceedingParser)

    asyncio.run(check_repo(config, repo, store))

    # the streak is fully reset — the next failure starts the count at 1 again
    assert store.bump_failure("r", "gpg", "peek") == (1, False)


def test_check_repo_success_without_prior_gpg_failure_creates_no_failure_row(tmp_path, monkeypatch):
    """A successful check_repo without verify_signature unconditionally
    calls reset_failure (see the fix below), but that doesn't create a row
    where there was no streak — bump_failure from scratch afterward still
    returns (1, False), not some inherited counter."""
    store = StateStore(tmp_path / "state.sqlite3")
    repo = RepoConfig(id="r", type="pacman", upstream="https://example.org", arch="x86_64", repo_name="core")
    config = _config(repos=[repo])

    monkeypatch.setitem(watcher.PARSERS, "pacman", _SucceedingParser)

    asyncio.run(check_repo(config, repo, store))

    assert store.bump_failure("r", "gpg", "peek") == (1, False)


def test_check_repo_end_to_end_version_churn_updates_packages_and_history(tmp_path, monkeypatch):
    """Regression for the full fetch -> diff -> record path (not just
    record_snapshot() given an already-built dict directly, see other tests
    in test_state.py): a real version bump between two check_repo() cycles
    (e.g. upstream rebuilds linux-headers at a new version) must make the
    old version actually disappear from get_packages() and be reflected in
    history — not just leave both versions sitting side by side."""
    calls = []

    class _ChurningParser:
        """Same package set on every call except linux-headers, which
        bumps version on the second call — an unrelated package
        (stable-pkg) is included to prove it survives untouched."""

        def __init__(self, repo):
            self.repo = repo

        async def check_index_changed(self, client, prev_etag, prev_last_modified):
            return IndexHeadResult(unchanged=False, etag=None, last_modified=None)

        async def fetch(self, client):
            calls.append(1)
            headers_version = "linux-headers-6.11.2-1" if len(calls) == 1 else "linux-headers-6.12.0-1"
            packages = {
                headers_version: f"{headers_version}-x86_64.pkg.tar.zst",
                "stable-pkg-1.0-1": "stable-pkg-1.0-1-x86_64.pkg.tar.zst",
            }
            return RepoSnapshot(repo_id=self.repo.id, packages=packages)

    store = StateStore(tmp_path / "state.sqlite3")
    repo = RepoConfig(
        id="r", type="pacman", upstream="https://example.org", arch="x86_64",
        repo_name="core", prefetch=False,
    )
    config = _config(repos=[repo])
    monkeypatch.setitem(watcher.PARSERS, "pacman", _ChurningParser)

    asyncio.run(check_repo(config, repo, store))
    after_first = store.get_packages("r")
    assert after_first == {
        "linux-headers-6.11.2-1": "linux-headers-6.11.2-1-x86_64.pkg.tar.zst",
        "stable-pkg-1.0-1": "stable-pkg-1.0-1-x86_64.pkg.tar.zst",
    }

    asyncio.run(check_repo(config, repo, store))
    after_second = store.get_packages("r")
    # the old version is genuinely gone, not just shadowed by the new one
    assert "linux-headers-6.11.2-1" not in after_second
    assert after_second == {
        "linux-headers-6.12.0-1": "linux-headers-6.12.0-1-x86_64.pkg.tar.zst",
        "stable-pkg-1.0-1": "stable-pkg-1.0-1-x86_64.pkg.tar.zst",
    }

    # history: two events (the first snapshot counts as "changed" too, see
    # record_snapshot). Both check_repo() calls happen within the same
    # second in a fast test, so repo_events.ts (second-precision) can tie —
    # aggregate across entries instead of trusting ORDER BY ts to separate
    # them, which is exactly what production never has to do at real
    # check_interval spacing.
    history = store.get_history("r", limit=10)
    assert len(history) == 2
    all_new = {pkg for entry in history for pkg in entry["new_packages"]}
    all_removed = {pkg for entry in history for pkg in entry["removed_packages"]}
    assert all_new == {"linux-headers-6.11.2-1", "stable-pkg-1.0-1", "linux-headers-6.12.0-1"}
    assert all_removed == {"linux-headers-6.11.2-1"}

    summary = store.get_repo_summaries()["r"]
    assert summary["package_count"] == 2
    assert summary["changed_at"] is not None


def test_check_repo_calls_purge_removed_with_removed_filenames(tmp_path, monkeypatch):
    """docs_dev/ROADMAP.md item 24: check_repo must hand the REMOVED
    package's filename (not just its key) to purge_removed the moment the
    diff confirms it's gone — purge_removed itself no-ops unless
    nginx.enable_purge is set, this test is only about the wiring."""
    calls = []

    class _ChurningParser:
        def __init__(self, repo):
            self.repo = repo

        async def check_index_changed(self, client, prev_etag, prev_last_modified):
            return IndexHeadResult(unchanged=False, etag=None, last_modified=None)

        async def fetch(self, client):
            calls.append(1)
            if len(calls) == 1:
                packages = {"linux-headers-6.11.2-1": "linux-headers-6.11.2-1-x86_64.pkg.tar.zst"}
            else:
                packages = {}
            return RepoSnapshot(repo_id=self.repo.id, packages=packages)

    store = StateStore(tmp_path / "state.sqlite3")
    repo = RepoConfig(
        id="r", type="pacman", upstream="https://example.org", arch="x86_64",
        repo_name="core", prefetch=False,
    )
    config = _config(repos=[repo])
    monkeypatch.setitem(watcher.PARSERS, "pacman", _ChurningParser)

    purge_calls = []
    async def fake_purge_removed(cfg, r, removed):
        purge_calls.append((r.id, removed))
    monkeypatch.setattr(watcher, "purge_removed", fake_purge_removed)

    asyncio.run(check_repo(config, repo, store))
    assert purge_calls == []  # nothing removed yet on the first check

    asyncio.run(check_repo(config, repo, store))
    assert purge_calls == [("r", {"linux-headers-6.11.2-1": "linux-headers-6.11.2-1-x86_64.pkg.tar.zst"})]


def test_run_forever_invokes_check_all_and_prune_all_on_its_own_timer(tmp_path, monkeypatch):
    """Regression for the wiring itself: prune_all/check_all are each
    covered individually elsewhere, but nothing previously exercised that
    run_forever's own loop (watcher.py's _SCHEDULER_TICK_SECONDS /
    _PRUNE_INTERVAL_SECONDS) actually calls them on schedule."""
    store = StateStore(tmp_path / "state.sqlite3")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"state_db: {tmp_path / 'state.sqlite3'}\n"
        "cache_base_url: http://127.0.0.1:8080\n"
        "repos:\n"
        "  - id: r\n"
        "    type: apk\n"
        "    upstream: https://example.org\n"
        "    arch: x86_64\n"
    )

    check_all_calls = []
    prune_calls = []

    async def fake_check_all(cfg, st):
        check_all_calls.append(1)

    def fake_prune_all(cfg, st):
        prune_calls.append(1)

    monkeypatch.setattr(watcher, "check_all", fake_check_all)
    monkeypatch.setattr(watcher, "prune_all", fake_prune_all)
    # Tiny tick/prune intervals so the loop fires several times fast,
    # instead of relying on the real 10s tick / 3600s prune interval or on
    # host uptime (time.monotonic()'s origin is unspecified) to happen to
    # already exceed _PRUNE_INTERVAL_SECONDS on the first iteration.
    monkeypatch.setattr(watcher, "_SCHEDULER_TICK_SECONDS", 0.001)
    monkeypatch.setattr(watcher, "_PRUNE_INTERVAL_SECONDS", 0.001)

    async def run():
        stop = asyncio.Event()

        async def stop_after_a_few_prunes():
            while len(prune_calls) < 3:
                await asyncio.sleep(0.001)
            stop.set()

        await asyncio.gather(
            watcher.run_forever(str(config_path), store, stop=stop),
            stop_after_a_few_prunes(),
        )

    asyncio.run(run())

    assert len(prune_calls) >= 3
    assert len(check_all_calls) >= 3


def test_check_repo_resets_gpg_failure_after_success_even_without_verify_signature(tmp_path, monkeypatch):
    """Regression: AptParser cross-checks Packages.gz's SHA256 against
    InRelease (by-hash) and can raise SignatureError even with
    verify_signature=False (see parsers/apt.py) — before the fix, the reset
    in check_repo was gated on `if repo.verify_signature`, so for such
    repositories the "gpg" streak never cleared after a failure, even once
    the repository fetched successfully again. repo.verify_signature=False
    here is deliberate."""
    store = StateStore(tmp_path / "state.sqlite3")
    store.bump_failure("r", "gpg", "previous failure (e.g. SHA256 mismatch)")
    repo = RepoConfig(id="r", type="pacman", upstream="https://example.org", arch="x86_64", repo_name="core")
    config = _config(repos=[repo])

    monkeypatch.setitem(watcher.PARSERS, "pacman", _SucceedingParser)

    asyncio.run(check_repo(config, repo, store))

    # the streak is fully reset — the next failure starts the count at 1 again
    assert store.bump_failure("r", "gpg", "peek") == (1, False)
