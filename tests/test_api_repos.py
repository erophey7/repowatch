"""Tests for the /api/repos* routes — calling the plain functions directly,
without a real HTTP server (opening sockets is unreliable in this sandbox:
even loopback HTTP gets intercepted by a transparent proxy layer)."""

from unittest.mock import patch

from datetime import datetime, timedelta, timezone

import pytest

from repowatch.api import (
    STATIC_DIR,
    add_repo_payload,
    ban_package_payload,
    banned_packages_payload,
    cache_dir_stats,
    delete_repo_payload,
    healthz_payload,
    metrics_payload,
    prefetch_efficiency_payload,
    purge_candidates_payload,
    purge_selected_payload,
    remove_warmed_package_payload,
    repos_list_payload,
    requests_summary_payload,
    safe_config_payload,
    stats_payload,
    status_payload,
    unban_package_payload,
    update_repo_payload,
    update_safe_config_payload,
    warm_packages_payload,
)
from repowatch.auth import hash_password, verify_password
from repowatch.access import AdminSession, digest
from repowatch.config import load_config
from repowatch.state import RepoSnapshot, StateStore


def _write_config(tmp_path, repos_yaml: str, admin_password: str | None = None):
    config_path = tmp_path / "config.yaml"
    hash_line = f"admin_password_hash: {hash_password(admin_password)}\n" if admin_password else ""
    config_path.write_text(
        f"""
state_db: {tmp_path / "state.sqlite3"}
cache_base_url: http://127.0.0.1:8080
{hash_line}repos:
{repos_yaml}
"""
    )
    return config_path



def _session(config_path, password):
    current = load_config(config_path)
    if not password or not current.admin_password_hash or not verify_password(password, current.admin_password_hash):
        return None
    return AdminSession(digest(current.admin_password_hash), 'csrf', float('inf'))


def _setup(tmp_path, admin_password: str | None = None):
    config_path = _write_config(
        tmp_path,
        """  - id: alpine-test
    type: apk
    upstream: https://example.org/alpine/v3.20/main
    arch: x86_64
""",
        admin_password=admin_password,
    )
    config = load_config(config_path)
    store = StateStore(config.state_db)
    return config_path, store


def test_dashboard_html_exists_and_mentions_repowatch():
    html = (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
    assert "repowatch" in html


def test_repos_list_payload_lists_configured_repo(tmp_path):
    config_path, store = _setup(tmp_path)

    status, data = repos_list_payload(config_path, store)

    assert status == 200
    assert len(data) == 1
    assert data[0]["id"] == "alpine-test"
    assert data[0]["type"] == "apk"
    assert data[0]["package_count"] == 0
    assert data[0]["warmed_count"] == 0
    assert data[0]["browse_url"] == "http://127.0.0.1:8080/alpine/v3.20/main/x86_64/"
    assert data[0]["last_check"] is None


def test_repos_list_payload_exposes_key_expiry_and_warning_flag(tmp_path):
    config_path, store = _setup(tmp_path)

    status, data = repos_list_payload(config_path, store)
    assert status == 200
    assert data[0]["key_expires_at"] is None
    assert data[0]["key_expiring_soon"] is False

    far_future = (datetime.now(timezone.utc) + timedelta(days=200)).isoformat(timespec="seconds")
    store.record_key_expiry("alpine-test", far_future)
    status, data = repos_list_payload(config_path, store)
    assert data[0]["key_expires_at"] == far_future
    assert data[0]["key_expiring_soon"] is False

    soon = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(timespec="seconds")
    store.record_key_expiry("alpine-test", soon)
    status, data = repos_list_payload(config_path, store)
    assert data[0]["key_expires_at"] == soon
    assert data[0]["key_expiring_soon"] is True


def test_repos_list_payload_reflects_recorded_snapshot(tmp_path):
    config_path, store = _setup(tmp_path)
    store.record_snapshot(
        RepoSnapshot(repo_id="alpine-test", packages={"musl-1.2.5-r0": "musl-1.2.5-r0.apk"})
    )

    status, data = repos_list_payload(config_path, store)

    assert status == 200
    assert data[0]["package_count"] == 1
    assert data[0]["last_check"] is not None


def test_repos_list_payload_falls_back_to_computed_count_when_package_count_is_null(tmp_path):
    """Rows written before the package_count column existed (or inserted by
    hand via SQL) read it as NULL — repos_list_payload must compute
    package_count from packages_json instead of returning None/0."""
    config_path, store = _setup(tmp_path)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO repo_state (repo_id, last_check, changed_at, package_count) "
            "VALUES (?, ?, NULL, NULL)",
            ("alpine-test", "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO repo_packages (repo_id, package_key, package_name, filename) VALUES (?, ?, ?, ?)",
            ("alpine-test", "musl-1.2.5-r0", "musl", "musl-1.2.5-r0.apk"),
        )

    status, data = repos_list_payload(config_path, store)

    assert status == 200
    assert data[0]["package_count"] == 1


def test_repos_list_payload_500_on_broken_config(tmp_path):
    config_path, store = _setup(tmp_path)
    config_path.write_text("this is not valid yaml: [unclosed")

    status, data = repos_list_payload(config_path, store)

    assert status == 500
    assert "error" in data


NEW_REPO_BODY = {
    "id": "arch-extra-test",
    "type": "pacman",
    "upstream": "https://example.org/extra/os/x86_64",
    "arch": "x86_64",
    "repo_name": "extra",
}


def test_add_repo_disabled_without_admin_password(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password=None)

    status, data = add_repo_payload(config_path, _session(config_path, "whatever"), NEW_REPO_BODY)

    assert status == 501
    assert "error" in data


def test_add_repo_rejects_missing_password(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    status, data = add_repo_payload(config_path, _session(config_path, None), NEW_REPO_BODY)

    assert status == 401


def test_add_repo_rejects_wrong_password(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    status, data = add_repo_payload(config_path, _session(config_path, "wrong-password"), NEW_REPO_BODY)

    assert status == 401


def test_add_repo_succeeds_with_correct_password_and_persists(tmp_path):
    config_path, store = _setup(tmp_path, admin_password="secret123")

    status, data = add_repo_payload(config_path, _session(config_path, "secret123"), NEW_REPO_BODY)

    assert status == 201
    assert data["id"] == "arch-extra-test"

    # actually written to disk and picked up on the next read
    reloaded = load_config(config_path)
    assert reloaded.repo_by_id("arch-extra-test") is not None

    list_status, repos = repos_list_payload(config_path, store)
    assert list_status == 200
    ids = {r["id"] for r in repos}
    assert {"alpine-test", "arch-extra-test"} == ids


def test_add_repo_persists_manual_group(tmp_path):
    config_path, store = _setup(tmp_path, admin_password="secret123")

    status, data = add_repo_payload(config_path, _session(config_path, "secret123"), {**NEW_REPO_BODY, "group": "staging"})

    assert status == 201

    reloaded = load_config(config_path)
    assert reloaded.repo_by_id("arch-extra-test").group == "staging"

    list_status, repos = repos_list_payload(config_path, store)
    assert list_status == 200
    by_id = {r["id"]: r for r in repos}
    assert by_id["arch-extra-test"]["config"]["group"] == "staging"
    assert by_id["alpine-test"]["config"]["group"] is None


def test_add_repo_rejects_duplicate_id(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    duplicate = {**NEW_REPO_BODY, "id": "alpine-test"}
    status, data = add_repo_payload(config_path, _session(config_path, "secret123"), duplicate)

    assert status == 409


def test_add_repo_rejects_invalid_body(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    invalid = {"id": "broken", "type": "pacman"}  # missing the required repo_name/upstream/arch
    status, data = add_repo_payload(config_path, _session(config_path, "secret123"), invalid)

    assert status == 400
    assert "error" in data


def test_warm_packages_payload_disabled_without_admin_password(tmp_path):
    config_path, store = _setup(tmp_path, admin_password=None)

    status, data = warm_packages_payload(
        config_path, store, "alpine-test", _session(config_path, "whatever"), {"package_keys": ["x"]}
    )

    assert status == 501


def test_warm_packages_payload_unknown_repo_404(tmp_path):
    config_path, store = _setup(tmp_path, admin_password="secret123")

    status, data = warm_packages_payload(
        config_path, store, "does-not-exist", _session(config_path, "secret123"), {"package_keys": ["x"]}
    )

    assert status == 404


def test_warm_packages_payload_warms_only_known_keys(tmp_path):
    config_path, store = _setup(tmp_path, admin_password="secret123")
    store.record_snapshot(
        RepoSnapshot(
            repo_id="alpine-test",
            packages={"musl-1.2.5-r0": "musl-1.2.5-r0.apk", "zlib-1.3-r0": "zlib-1.3-r0.apk"},
        )
    )

    with patch("repowatch.api.warm_cache") as mock_warm_cache:
        status, data = warm_packages_payload(
            config_path, store, "alpine-test", _session(config_path, "secret123"),
            {"package_keys": ["musl-1.2.5-r0", "does-not-exist-1.0-r0"]},
        )

    assert status == 200
    assert data["warmed"] == ["musl-1.2.5-r0"]
    assert data["not_found"] == ["does-not-exist-1.0-r0"]
    mock_warm_cache.assert_called_once()
    # force=True — manual warming must not depend on repo.prefetch
    assert mock_warm_cache.call_args.kwargs.get("force") is True


def test_banned_packages_payload_unknown_repo_404(tmp_path):
    _config_path, store = _setup(tmp_path)

    status, data = banned_packages_payload(_config_path, store, "does-not-exist")

    assert status == 404


def test_banned_packages_payload_empty_by_default(tmp_path):
    config_path, store = _setup(tmp_path)
    store.record_snapshot(RepoSnapshot(repo_id="alpine-test", packages={}))

    status, data = banned_packages_payload(config_path, store, "alpine-test")

    assert status == 200
    assert data == []


def test_ban_package_disabled_without_admin_password(tmp_path):
    config_path, store = _setup(tmp_path, admin_password=None)

    status, data = ban_package_payload(
        config_path, store, "alpine-test", _session(config_path, "whatever"), {"package_name": "musl"}
    )

    assert status == 501


def test_ban_package_rejects_wrong_password(tmp_path):
    config_path, store = _setup(tmp_path, admin_password="secret123")

    status, data = ban_package_payload(
        config_path, store, "alpine-test", _session(config_path, "wrong"), {"package_name": "musl"}
    )

    assert status == 401


def test_ban_package_unknown_repo_404(tmp_path):
    config_path, store = _setup(tmp_path, admin_password="secret123")

    status, data = ban_package_payload(
        config_path, store, "does-not-exist", _session(config_path, "secret123"), {"package_name": "musl"}
    )

    assert status == 404


@pytest.mark.parametrize("body", [{}, {"package_name": "musl"}, {"package_names": []}, {"package_names": "musl"}])
def test_ban_package_requires_a_nonempty_package_names_list_in_body(tmp_path, body):
    config_path, store = _setup(tmp_path, admin_password="secret123")

    status, data = ban_package_payload(config_path, store, "alpine-test", _session(config_path, "secret123"), body)

    assert status == 400


def test_ban_then_unban_package_roundtrip(tmp_path):
    config_path, store = _setup(tmp_path, admin_password="secret123")

    status, data = ban_package_payload(
        config_path, store, "alpine-test", _session(config_path, "secret123"), {"package_names": ["musl"]}
    )
    assert status == 200
    assert data["banned"] == ["musl"]

    list_status, banned = banned_packages_payload(config_path, store, "alpine-test")
    assert banned == ["musl"]

    status, data = unban_package_payload(
        config_path, store, "alpine-test", _session(config_path, "secret123"), {"package_names": ["musl"]}
    )
    assert status == 200
    assert data["banned"] == []


def test_ban_then_unban_multiple_packages_at_once(tmp_path):
    """The dashboard's "ban selected"/"unban selected" bulk actions send more
    than one name in a single request — must not just handle a 1-element list."""
    config_path, store = _setup(tmp_path, admin_password="secret123")
    names = ["musl", "busybox", "openssl"]

    status, data = ban_package_payload(
        config_path, store, "alpine-test", _session(config_path, "secret123"), {"package_names": names}
    )
    assert status == 200
    assert data["banned"] == sorted(names)

    status, data = unban_package_payload(
        config_path, store, "alpine-test", _session(config_path, "secret123"), {"package_names": ["musl", "openssl"]}
    )
    assert status == 200
    assert data["banned"] == ["busybox"]


def test_remove_warmed_package_disabled_without_admin_password(tmp_path):
    config_path, store = _setup(tmp_path, admin_password=None)

    status, data = remove_warmed_package_payload(
        config_path, store, "alpine-test", _session(config_path, "whatever"), {"package_key": "musl-1.2.5-r0"}
    )

    assert status == 501


def test_remove_warmed_package_removes_existing_entry(tmp_path):
    config_path, store = _setup(tmp_path, admin_password="secret123")
    store.record_warmed_package("alpine-test", "musl-1.2.5-r0", "musl-1.2.5-r0.apk", True, 200)

    status, data = remove_warmed_package_payload(
        config_path, store, "alpine-test", _session(config_path, "secret123"), {"package_keys": ["musl-1.2.5-r0"]}
    )

    assert status == 200
    assert data["removed"] == 1
    assert store.get_warmed_packages("alpine-test") == []


def test_remove_multiple_warmed_packages_at_once(tmp_path):
    config_path, store = _setup(tmp_path, admin_password="secret123")
    store.record_warmed_package("alpine-test", "a-1", "a-1.apk", True, 200)
    store.record_warmed_package("alpine-test", "b-1", "b-1.apk", True, 200)
    store.record_warmed_package("alpine-test", "c-1", "c-1.apk", True, 200)

    status, data = remove_warmed_package_payload(
        config_path, store, "alpine-test", _session(config_path, "secret123"),
        {"package_keys": ["a-1", "c-1", "does-not-exist"]},
    )

    assert status == 200
    assert data["removed"] == 2  # only a-1/c-1 actually existed
    assert {p["package_key"] for p in store.get_warmed_packages("alpine-test")} == {"b-1"}


@pytest.mark.parametrize("body", [{}, {"package_key": "x"}, {"package_keys": []}, {"package_keys": "x"}])
def test_remove_warmed_package_requires_a_nonempty_package_keys_list(tmp_path, body):
    config_path, store = _setup(tmp_path, admin_password="secret123")

    status, data = remove_warmed_package_payload(
        config_path, store, "alpine-test", _session(config_path, "secret123"), body)

    assert status == 400


def test_remove_warmed_package_stays_bookkeeping_only_when_purge_is_off(tmp_path):
    config_path, store = _setup_purge(tmp_path, enable_purge=False)
    store.record_warmed_package("alpine-test", "musl-1.2.5-r0", "musl-1.2.5-r0.apk", True, 200)

    status, data = remove_warmed_package_payload(
        config_path, store, "alpine-test", _session(config_path, "secret123"),
        {"package_keys": ["musl-1.2.5-r0"]},
    )

    assert status == 200
    assert data == {"removed": 1}  # no purge_results — no purge mechanism to call
    assert store.get_warmed_packages("alpine-test") == []


def test_remove_warmed_package_also_purges_when_enable_purge_is_on(tmp_path, monkeypatch):
    """Real behavior gap pointed out by the user (2026-09-13): "Remove from
    warmed" only ever edited the warmed_packages bookkeeping row, leaving
    the real cached file on disk untouched — exactly the disconnect
    docs_dev/ROADMAP.md item 33 flagged (an unwarmed-but-still-cached file
    becomes invisible to future stale-scans, since find_stale_warmed()
    needs the row to find it). Now, when purge is available, un-warming
    also evicts the real entry via the same prefetch.purge_selected() call
    "Purge selected" uses — same conservative bookkeeping rule too: only
    confirmed-gone keys ("purged"/"not_cached") lose their tracking row, an
    errored one stays trackable for a retry."""
    config_path, store = _setup_purge(tmp_path)
    store.record_warmed_package("alpine-test", "purged-1", "purged-1.apk", True, 200)
    store.record_warmed_package("alpine-test", "already-gone-1", "already-gone-1.apk", True, 200)
    store.record_warmed_package("alpine-test", "flaky-1", "flaky-1.apk", True, 200)

    async def fake_purge_selected(config, repo, items):
        assert items == {
            "purged-1": "purged-1.apk",
            "already-gone-1": "already-gone-1.apk",
            "flaky-1": "flaky-1.apk",
        }
        return {"purged-1": "purged", "already-gone-1": "not_cached", "flaky-1": "error (timeout)"}

    monkeypatch.setattr("repowatch.api.purge_selected", fake_purge_selected)

    status, data = remove_warmed_package_payload(
        config_path, store, "alpine-test", _session(config_path, "secret123"),
        {"package_keys": ["purged-1", "already-gone-1", "flaky-1", "not-actually-warmed"]},
    )

    assert status == 200
    assert data["purge_results"] == {"purged-1": "purged", "already-gone-1": "not_cached", "flaky-1": "error (timeout)"}
    assert data["removed"] == 2  # purged-1, already-gone-1 (+ not-actually-warmed, never tracked)
    remaining = {p["package_key"] for p in store.get_warmed_packages("alpine-test")}
    assert remaining == {"flaky-1"}  # only the errored one survives for a retry


def test_remove_warmed_package_uses_cache_probe_when_enable_cache_probe_is_on(tmp_path, monkeypatch):
    """Same mechanism-selection as purge_selected_payload (2026-09-14)."""
    config_path, store = _setup_purge(tmp_path, enable_cache_probe=True)
    store.record_warmed_package("alpine-test", "a-1", "a-1.apk", True, 200)

    def boom(*a, **kw):
        raise AssertionError("prefetch.purge_selected must not be called when enable_cache_probe is on")
    monkeypatch.setattr("repowatch.api.purge_selected", boom)

    async def fake_purge_selected_raw(config, repo, items):
        return {"a-1": "purged"}
    monkeypatch.setattr("repowatch.cache_probe.purge_selected_raw", fake_purge_selected_raw)

    status, data = remove_warmed_package_payload(
        config_path, store, "alpine-test", _session(config_path, "secret123"), {"package_keys": ["a-1"]},
    )

    assert status == 200
    assert data["purge_results"] == {"a-1": "purged"}
    assert store.get_warmed_packages("alpine-test") == []


def test_remove_warmed_package_unknown_repo_404(tmp_path):
    config_path, store = _setup(tmp_path, admin_password="secret123")

    status, data = remove_warmed_package_payload(
        config_path, store, "does-not-exist", _session(config_path, "secret123"), {"package_key": "x"}
    )

    assert status == 404


def test_healthz_ok_when_never_checked(tmp_path):
    """A fresh repository (never checked) is not considered stale — that's
    the expected state right after startup/adding, not a failure."""
    config_path, store = _setup(tmp_path)

    status, data = healthz_payload(config_path, store)

    assert status == 200
    assert data == {"ok": True}


def test_healthz_ok_right_after_check(tmp_path):
    config_path, store = _setup(tmp_path)
    store.record_snapshot(RepoSnapshot(repo_id="alpine-test", packages={}))

    status, data = healthz_payload(config_path, store)

    assert status == 200
    assert data == {"ok": True}


def _set_last_check(store, repo_id: str, when) -> None:
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO repo_state (repo_id, last_check, changed_at) "
            "VALUES (?, ?, NULL) "
            "ON CONFLICT(repo_id) DO UPDATE SET last_check = excluded.last_check",
            (repo_id, when.isoformat(timespec="seconds")),
        )


def test_healthz_503_when_repo_stale(tmp_path):
    config_path, store = _setup(tmp_path)
    # check_interval defaults to 300s, threshold is 3x=900s; 1000s ago is already stale
    _set_last_check(store, "alpine-test", datetime.now(timezone.utc) - timedelta(seconds=1000))

    status, data = healthz_payload(config_path, store)

    assert status == 503
    assert data["ok"] is False
    assert data["stale_repos"][0]["repo_id"] == "alpine-test"


def test_healthz_ok_when_stale_but_within_threshold(tmp_path):
    config_path, store = _setup(tmp_path)
    _set_last_check(store, "alpine-test", datetime.now(timezone.utc) - timedelta(seconds=100))

    status, data = healthz_payload(config_path, store)

    assert status == 200
    assert data == {"ok": True}


def test_healthz_ok_on_broken_config(tmp_path):
    config_path, store = _setup(tmp_path)
    config_path.write_text("this is not valid yaml: [unclosed")

    status, data = healthz_payload(config_path, store)

    assert status == 200
    assert data["ok"] is True
    assert "warning" in data


def test_metrics_payload_includes_repo_gauges(tmp_path):
    config_path, store = _setup(tmp_path)
    store.record_snapshot(
        RepoSnapshot(repo_id="alpine-test", packages={"musl-1.2.5-r0": "musl-1.2.5-r0.apk"})
    )
    store.record_warmed_package("alpine-test", "musl-1.2.5-r0", "musl-1.2.5-r0.apk", True, 200)

    status, body = metrics_payload(config_path, store)

    assert status == 200
    assert '# TYPE repowatch_repo_package_count gauge' in body
    assert 'repowatch_repo_package_count{repo_id="alpine-test"} 1' in body
    assert 'repowatch_repo_warmed_count{repo_id="alpine-test"} 1' in body
    assert "repowatch_repo_last_check_timestamp_seconds" in body
    assert "repowatch_repo_changed_at_timestamp_seconds" in body
    assert "repowatch_healthy 1" in body


def test_metrics_payload_falls_back_to_computed_count_when_package_count_is_null(tmp_path):
    config_path, store = _setup(tmp_path)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO repo_state (repo_id, last_check, changed_at, package_count) "
            "VALUES (?, ?, NULL, NULL)",
            ("alpine-test", "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO repo_packages (repo_id, package_key, package_name, filename) VALUES (?, ?, ?, ?)",
            ("alpine-test", "musl-1.2.5-r0", "musl", "musl-1.2.5-r0.apk"),
        )

    status, body = metrics_payload(config_path, store)

    assert status == 200
    assert 'repowatch_repo_package_count{repo_id="alpine-test"} 1' in body


def test_metrics_payload_healthy_zero_when_repo_stale(tmp_path):
    config_path, store = _setup(tmp_path)
    _set_last_check(store, "alpine-test", datetime.now(timezone.utc) - timedelta(seconds=1000))

    status, body = metrics_payload(config_path, store)

    assert status == 200
    assert "repowatch_healthy 0" in body


def test_metrics_payload_extended_gauges(tmp_path):
    config_path, store = _setup(tmp_path)
    # syslog_listener.enabled defaults to True since 2026-09-14 — turn it
    # off explicitly to test the "no request gauges" case below.
    config_path.write_text(config_path.read_text() + "\nsyslog_listener:\n  enabled: false\n")
    _set_last_check(store, "alpine-test", datetime.now(timezone.utc) - timedelta(seconds=1000))
    store.bump_failure("alpine-test", "gpg", "bad signature")
    store.bump_failure("alpine-test", "gpg", "bad signature")
    store.ban_package("alpine-test", "musl")

    status, body = metrics_payload(config_path, store)

    assert status == 200
    assert 'repowatch_repo_stale{repo_id="alpine-test"} 1' in body
    assert 'repowatch_repo_consecutive_failures{repo_id="alpine-test",kind="gpg"} 2' in body
    assert 'repowatch_repo_consecutive_failures{repo_id="alpine-test",kind="prefetch"} 0' in body
    assert 'repowatch_repo_banned_packages{repo_id="alpine-test"} 1' in body
    assert 'repowatch_repos_by_type{type="apk"} 1' in body
    assert "repowatch_state_db_bytes " in body
    # syslog_listener disabled — no per-repo request gauges emitted.
    assert "repowatch_repo_requests" not in body


def test_metrics_payload_key_expiry_gauges(tmp_path):
    config_path, store = _setup(tmp_path)

    status, body = metrics_payload(config_path, store)
    assert status == 200
    # never checked yet — no key_expires_at recorded, gauges absent for this repo
    assert 'repowatch_repo_key_expires_at_timestamp_seconds{repo_id="alpine-test"}' not in body
    assert 'repowatch_repo_key_expiring_soon{repo_id="alpine-test"}' not in body

    soon = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(timespec="seconds")
    store.record_key_expiry("alpine-test", soon)
    status, body = metrics_payload(config_path, store)
    assert 'repowatch_repo_key_expires_at_timestamp_seconds{repo_id="alpine-test"}' in body
    assert 'repowatch_repo_key_expiring_soon{repo_id="alpine-test"} 1' in body

    far_future = (datetime.now(timezone.utc) + timedelta(days=200)).isoformat(timespec="seconds")
    store.record_key_expiry("alpine-test", far_future)
    status, body = metrics_payload(config_path, store)
    assert 'repowatch_repo_key_expiring_soon{repo_id="alpine-test"} 0' in body


def test_metrics_payload_includes_request_gauges_only_when_syslog_listener_enabled(tmp_path):
    config_path, store = _setup(tmp_path)
    # syslog_listener.enabled defaults to True since 2026-09-14 — start
    # from explicitly off to actually exercise the "disabled" half below.
    config_path.write_text(config_path.read_text() + "\nsyslog_listener:\n  enabled: false\n")
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO request_events (ts, repo_id, client_ip, method, path, status, cache_status) "
            "VALUES (?, ?, ?, 'GET', '/alpine/x', '200', ?)",
            ("2026-01-01T00:00:00+00:00", "alpine-test", "192.0.2.1", "HIT"),
        )
        conn.execute(
            "INSERT INTO request_events (ts, repo_id, client_ip, method, path, status, cache_status) "
            "VALUES (?, ?, ?, 'GET', '/alpine/y', '200', ?)",
            ("2026-01-01T00:00:01+00:00", "alpine-test", "192.0.2.1", "MISS"),
        )

    status, body = metrics_payload(config_path, store)
    assert status == 200
    assert "repowatch_repo_requests" not in body

    raw = config_path.read_text()
    config_path.write_text(raw + "\nsyslog_listener:\n  enabled: true\n")

    status, body = metrics_payload(config_path, store)
    assert status == 200
    assert 'repowatch_repo_requests{repo_id="alpine-test"} 2' in body
    assert 'repowatch_repo_requests_cache_hit{repo_id="alpine-test"} 1' in body


def test_metrics_payload_returns_500_on_broken_config(tmp_path):
    config_path, store = _setup(tmp_path)
    config_path.write_text("this is not valid yaml: [unclosed")

    status, body = metrics_payload(config_path, store)

    assert status == 500
    assert isinstance(body, str)


def test_metrics_payload_escapes_label_values(tmp_path):
    config_path, store = _setup(tmp_path)
    # a repo_id with a quote is valid per RepoConfig (it's just a str), it
    # must be safely escaped in the label, not break the format
    config_path.write_text(
        f"""
state_db: {tmp_path / "state.sqlite3"}
cache_base_url: http://127.0.0.1:8080
repos:
  - id: 'weird"repo'
    type: apk
    upstream: https://example.org
    arch: x86_64
"""
    )

    status, body = metrics_payload(config_path, store)

    assert status == 200
    assert 'repo_id="weird\\"repo"' in body


def test_requests_summary_payload_shape(tmp_path):
    _config_path, store = _setup(tmp_path)
    store.record_request("alpine-test", "1.1.1.1", "GET", "/x.apk", "200", "HIT")
    store.record_request("alpine-test", "1.1.1.1", "GET", "/x.apk", "200", "HIT")
    store.record_request("alpine-test", "2.2.2.2", "GET", "/y.apk", "200", "MISS")

    status, data = requests_summary_payload(store, repo_id=None)

    assert status == 200
    assert data["by_client_ip"][0] == {"key": "1.1.1.1", "count": 2}
    assert data["by_path"][0] == {"key": "/x.apk", "count": 2}
    assert {"key": "alpine-test", "count": 3} in data["by_repo"]
    assert data["timeline"][0]["total"] == 3
    assert data["timeline"][0]["hits"] == 2
    assert {"repo_id": "alpine-test", "total": 3, "hits": 2} in data["cache_hit_stats"]


def test_requests_summary_payload_timeline_respects_repo_filter(tmp_path):
    _config_path, store = _setup(tmp_path)
    store.record_request("alpine-test", "1.1.1.1", "GET", "/x.apk", "200", "HIT")
    store.record_request("other-repo", "9.9.9.9", "GET", "/z.apk", "200", "HIT")

    status, data = requests_summary_payload(store, repo_id="alpine-test")

    assert status == 200
    assert sum(bucket["total"] for bucket in data["timeline"]) == 1


def test_prefetch_efficiency_payload(tmp_path):
    _config_path, store = _setup(tmp_path)
    assert prefetch_efficiency_payload(store) == (200, {"items": []})

    store.record_warmed_package("alpine-test", "musl-1.2.5-r0", "musl-1.2.5-r0.apk", True, 200, source="prefetch")
    status, data = prefetch_efficiency_payload(store)
    assert status == 200
    assert data == {"items": [{"repo_id": "alpine-test", "prefetched": 1, "used": 0, "ratio": 0.0}]}


def test_requests_summary_payload_filters_by_repo(tmp_path):
    _config_path, store = _setup(tmp_path)
    store.record_request("alpine-test", "1.1.1.1", "GET", "/x.apk", "200", "HIT")
    store.record_request("other-repo", "9.9.9.9", "GET", "/z.apk", "200", "HIT")

    status, data = requests_summary_payload(store, repo_id="alpine-test")

    assert status == 200
    assert data["by_client_ip"] == [{"key": "1.1.1.1", "count": 1}]
    assert data["by_path"] == [{"key": "/x.apk", "count": 1}]
    # by_repo ignores the filter — it's already broken down by repository
    assert {"key": "other-repo", "count": 1} in data["by_repo"]


UPDATE_REPO_BODY = {
    "type": "apk",
    "upstream": "https://example.org/alpine/v3.20/main",
    "arch": "x86_64",
    "prefetch": False,
    "check_interval": 60,
}


def test_update_repo_disabled_without_admin_password(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password=None)

    status, data = update_repo_payload(config_path, _session(config_path, "whatever"), "alpine-test", UPDATE_REPO_BODY)

    assert status == 501


def test_update_repo_rejects_wrong_password(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    status, data = update_repo_payload(config_path, _session(config_path, "wrong"), "alpine-test", UPDATE_REPO_BODY)

    assert status == 401


def test_update_repo_unknown_repo_404(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    status, data = update_repo_payload(config_path, _session(config_path, "secret123"), "does-not-exist", UPDATE_REPO_BODY)

    assert status == 404


def test_update_repo_rejects_id_change(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    status, data = update_repo_payload(
        config_path, _session(config_path, "secret123"), "alpine-test", {**UPDATE_REPO_BODY, "id": "renamed"}
    )

    assert status == 400
    assert "id" in data["error"]


def test_update_repo_rejects_invalid_body(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    invalid = {"type": "apt", "upstream": "https://example.org", "arch": "x86_64"}  # missing distribution/component
    status, data = update_repo_payload(config_path, _session(config_path, "secret123"), "alpine-test", invalid)

    assert status == 400
    assert "error" in data


def test_update_repo_succeeds_and_persists(tmp_path):
    config_path, store = _setup(tmp_path, admin_password="secret123")

    status, data = update_repo_payload(config_path, _session(config_path, "secret123"), "alpine-test", UPDATE_REPO_BODY)

    assert status == 200
    assert data["id"] == "alpine-test"

    reloaded = load_config(config_path)
    updated = reloaded.repo_by_id("alpine-test")
    assert updated.prefetch is False
    assert updated.check_interval == 60

    list_status, repos = repos_list_payload(config_path, store)
    assert list_status == 200
    assert repos[0]["check_interval"] == 60
    assert repos[0]["prefetch"] is False


def test_update_repo_can_set_and_clear_manual_group(tmp_path):
    config_path, store = _setup(tmp_path, admin_password="secret123")

    status, _data = update_repo_payload(
        config_path, _session(config_path, "secret123"), "alpine-test", {**UPDATE_REPO_BODY, "group": "staging"}
    )
    assert status == 200
    assert load_config(config_path).repo_by_id("alpine-test").group == "staging"

    # empty/missing field in the body — "ungrouped" again, doesn't keep the old value
    status, _data = update_repo_payload(config_path, _session(config_path, "secret123"), "alpine-test", UPDATE_REPO_BODY)
    assert status == 200
    assert load_config(config_path).repo_by_id("alpine-test").group is None


def test_delete_repo_disabled_without_admin_password(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password=None)

    status, data = delete_repo_payload(config_path, _session(config_path, "whatever"), "alpine-test")

    assert status == 501


def test_delete_repo_rejects_wrong_password(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    status, data = delete_repo_payload(config_path, _session(config_path, "wrong"), "alpine-test")

    assert status == 401


def test_delete_repo_unknown_repo_404(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    status, data = delete_repo_payload(config_path, _session(config_path, "secret123"), "does-not-exist")

    assert status == 404


def test_delete_repo_rejects_deleting_last_repo(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    status, data = delete_repo_payload(config_path, _session(config_path, "secret123"), "alpine-test")

    assert status == 400
    assert "error" in data
    # the config must be left untouched
    reloaded = load_config(config_path)
    assert reloaded.repo_by_id("alpine-test") is not None


def test_delete_repo_succeeds_and_persists(tmp_path):
    config_path, store = _setup(tmp_path, admin_password="secret123")
    add_status, _ = add_repo_payload(config_path, _session(config_path, "secret123"), NEW_REPO_BODY)
    assert add_status == 201

    status, data = delete_repo_payload(config_path, _session(config_path, "secret123"), "alpine-test")

    assert status == 200
    assert data["deleted"] == "alpine-test"

    reloaded = load_config(config_path)
    assert reloaded.repo_by_id("alpine-test") is None
    assert reloaded.repo_by_id("arch-extra-test") is not None

    list_status, repos = repos_list_payload(config_path, store)
    assert list_status == 200
    assert {r["id"] for r in repos} == {"arch-extra-test"}


def test_safe_config_payload_returns_current_values(tmp_path):
    config_path, _store = _setup(tmp_path)

    status, data = safe_config_payload(config_path)

    assert status == 200
    assert data["check_interval"] == 300
    assert data["check_concurrency"] == 8
    assert data["prefetch_bandwidth_limit"] is None
    assert data["cache_base_url"] == "http://127.0.0.1:8080"
    assert data["key_expiry_warning_days"] == 30


def test_update_safe_config_disabled_without_admin_password(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password=None)

    status, data = update_safe_config_payload(config_path, _session(config_path, "whatever"), {"check_interval": 60})

    assert status == 501


def test_update_safe_config_rejects_wrong_password(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    status, data = update_safe_config_payload(config_path, _session(config_path, "wrong"), {"check_interval": 60})

    assert status == 401


def test_update_safe_config_rejects_unknown_field(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    status, data = update_safe_config_payload(
        config_path, _session(config_path, "secret123"), {"admin_password_hash": "whatever"}
    )

    assert status == 400
    assert "admin_password_hash" in data["error"]


def test_update_safe_config_rejects_state_db_field(tmp_path):
    """state_db is only read at startup — an edit through the dashboard
    would silently do nothing until a restart, so it's not in
    SAFE_CONFIG_FIELDS."""
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    status, data = update_safe_config_payload(config_path, _session(config_path, "secret123"), {"state_db": "/tmp/x.sqlite3"})

    assert status == 400


def test_update_safe_config_rejects_invalid_value(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    status, data = update_safe_config_payload(config_path, _session(config_path, "secret123"), {"check_interval": "not-a-number"})

    assert status == 400
    assert "check_interval" in data["error"]


def test_update_safe_config_succeeds_and_persists(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")

    status, data = update_safe_config_payload(
        config_path, _session(config_path, "secret123"),
        {
            "check_interval": 60, "check_concurrency": 3, "prefetch_bandwidth_limit": 5,
            "event_max_rows_per_repo": 10000, "request_max_rows": 1000000,
        },
    )

    assert status == 200
    assert data["check_interval"] == 60
    assert data["check_concurrency"] == 3
    assert data["prefetch_bandwidth_limit"] == 5.0
    assert data["event_max_rows_per_repo"] == 10000
    assert data["request_max_rows"] == 1000000

    reloaded = load_config(config_path)
    assert reloaded.check_interval == 60
    assert reloaded.check_concurrency == 3
    assert reloaded.prefetch_bandwidth_limit == 5.0
    assert reloaded.event_max_rows_per_repo == 10000
    assert reloaded.request_max_rows == 1000000


def test_update_safe_config_clears_size_retention_with_empty_string(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")
    update_safe_config_payload(config_path, _session(config_path, "secret123"), {"event_max_rows_per_repo": 10000})

    status, data = update_safe_config_payload(config_path, _session(config_path, "secret123"), {"event_max_rows_per_repo": ""})

    assert status == 200
    assert data["event_max_rows_per_repo"] is None


def test_update_safe_config_partial_update_leaves_other_fields(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")
    update_safe_config_payload(config_path, _session(config_path, "secret123"), {"check_interval": 60})

    status, data = update_safe_config_payload(config_path, _session(config_path, "secret123"), {"event_retention_days": 30})

    assert status == 200
    assert data["event_retention_days"] == 30
    assert data["check_interval"] == 60  # the previous change wasn't lost

    # repositories were not affected by editing global fields
    reloaded = load_config(config_path)
    assert reloaded.repo_by_id("alpine-test") is not None


def test_update_safe_config_clears_optional_field_with_empty_string(tmp_path):
    config_path, _store = _setup(tmp_path, admin_password="secret123")
    update_safe_config_payload(config_path, _session(config_path, "secret123"), {"public_cache_url": "http://192.0.2.10:8080"})

    status, data = update_safe_config_payload(config_path, _session(config_path, "secret123"), {"public_cache_url": ""})

    assert status == 200
    assert data["public_cache_url"] is None


def test_sustained_gets_from_same_ip_have_no_request_quota(tmp_path):
    from unittest.mock import Mock
    from repowatch.api import make_handler
    config_path, store = _setup(tmp_path)
    handler_class = make_handler(load_config(config_path), store, config_path)
    handler = object.__new__(handler_class)
    handler.client_address = ('192.0.2.1', 1234)
    handler.path = '/status.json'
    handler._json = Mock()
    from email.message import Message
    from repowatch.access import AccessStore
    handler.headers = Message()
    handler.headers['Host'] = 'localhost'
    handler.headers['Authorization'] = 'Bearer ' + AccessStore(store).create_token('test')['token']
    handler.client_address = ('127.0.0.1', 1234)
    handler.connection = object()

    for _ in range(250):
        handler.do_GET()
    assert handler._json.call_count == 250
    assert all(call.kwargs.get('status', 200) == 200 for call in handler._json.call_args_list)


def test_dnf_repo_can_be_added_and_has_cache_browse_url(tmp_path):
    config_path, store = _setup(tmp_path, admin_password='secret')
    status, result = add_repo_payload(config_path, _session(config_path, 'secret'), {
        'id': 'rpm-test', 'type': 'dnf', 'upstream': 'https://example.org/repo',
        'arch': 'x86_64', 'prefetch': False,
    })
    assert status == 201
    status, repos = repos_list_payload(config_path, store)
    assert status == 200
    assert next(repo for repo in repos if repo['id'] == 'rpm-test')['browse_url'].endswith('/rpm/rpm-test/')
    assert '<option value="dnf">' in (STATIC_DIR / 'dashboard.html').read_text()


def test_dashboard_health_and_metrics_do_not_read_package_change_lists(tmp_path, monkeypatch):
    config_path, store = _setup(tmp_path)
    store.record_snapshot(RepoSnapshot('alpine-test', {'one-1': 'one.apk'}))
    def unexpected_full_status(*args, **kwargs):
        raise AssertionError('lightweight API must not fetch full event lists')
    monkeypatch.setattr(store, 'get_status', unexpected_full_status)
    assert repos_list_payload(config_path, store)[0] == 200
    assert healthz_payload(config_path, store)[0] == 200
    assert metrics_payload(config_path, store)[0] == 200


def test_minimal_status_lifecycle(tmp_path):
    config_path, store = _setup(tmp_path)
    assert status_payload(config_path, store) == (200, {
        'alpine-test': {'last_check': None, 'changed_at': None, 'stale': True},
    })
    store.record_snapshot(RepoSnapshot('alpine-test', {'one-1': 'one.apk'}))
    code, first = status_payload(config_path, store, 'alpine-test')
    assert code == 200
    assert set(first) == {'last_check', 'changed_at', 'stale'}
    assert first['changed_at'] and first['stale'] is False
    store.touch_last_check('alpine-test')
    assert status_payload(config_path, store, 'alpine-test')[1]['changed_at'] == first['changed_at']
    with store._connect() as conn:
        conn.execute('UPDATE repo_state SET last_check = ?', ('2000-01-01T00:00:00+00:00',))
    assert status_payload(config_path, store, 'alpine-test')[1]['stale'] is True
    store.record_snapshot(RepoSnapshot('orphan', {'one': 'one.apk'}))
    assert set(status_payload(config_path, store)[1]) == {'alpine-test'}
    assert status_payload(config_path, store, 'orphan')[0] == 404
    config_path.write_text('repos: [')
    assert status_payload(config_path, store)[0] == 503


def test_status_routes_ignore_large_history(tmp_path, monkeypatch):
    import json
    from unittest.mock import Mock
    from repowatch.api import make_handler
    config_path, store = _setup(tmp_path)
    store.record_snapshot(RepoSnapshot('alpine-test', {'one': 'one.apk'}))
    # A million historical keys must not be loaded by either status route.
    with store._connect() as conn:
        conn.execute('UPDATE repo_events SET new_pkgs_json = ?', (json.dumps(['package'] * 1_000_000),))
    def forbidden(*args, **kwargs):
        raise AssertionError('status must not load full history')
    monkeypatch.setattr(store, 'get_status', forbidden)
    handler = object.__new__(make_handler(load_config(config_path), store, config_path))
    handler._json = Mock()
    from email.message import Message
    from repowatch.access import AccessStore
    handler.headers = Message()
    handler.headers['Host'] = 'localhost'
    handler.headers['Authorization'] = 'Bearer ' + AccessStore(store).create_token('test')['token']
    handler.client_address = ('127.0.0.1', 1234)
    handler.connection = object()

    for path in ('/status.json', '/status/alpine-test.json'):
        handler.path = path
        handler.do_GET()
        payload = handler._json.call_args.args[0]
        assert handler._json.call_args.kwargs['status'] == 200
        assert len(json.dumps(payload)) < 250
    handler.path = '/status/missing.json'
    handler.do_GET()
    assert handler._json.call_args.kwargs['status'] == 404


def test_alt_schema_saved_and_conflicting_route_rejected_atomically(tmp_path):
    path, store = _setup(tmp_path, admin_password='pw')
    session = _session(path, 'pw')
    body = {'id':'alt', 'type':'apt-rpm', 'upstream':'https://example.test/p11/x86_64',
            'arch':'x86_64', 'component':'classic', 'url_template':'/{distro}/{branch}/{arch}/',
            'url_variables':{'distro':'altlinux','branch':'p11'}, 'prefetch':False}
    assert add_repo_payload(path, session, body)[0] == 201
    current = load_config(path).repo_by_id('alt')
    assert current.url_variables == body['url_variables']
    status, payload = repos_list_payload(path, store)
    alt = next(item for item in payload if item['id']=='alt')
    assert alt['browse_url'].endswith('/altlinux/p11/x86_64/RPMS.classic/') or alt['browse_url'].endswith('/altlinux/p11/x86_64/RPMS.classic')
    before = path.read_bytes()
    status, result = add_repo_payload(path, session, {**body,'id':'collision','upstream':'https://other.test/p11/x86_64'})
    assert status == 400
    assert 'conflicting' in result['error']
    assert path.read_bytes() == before


def test_stats_payload_reports_db_size_without_walking_the_cache_dir_by_default(tmp_path):
    config_path, store = _setup(tmp_path)
    store.record_snapshot(RepoSnapshot("alpine-test", {"a-1": "a.apk"}))

    status, payload = stats_payload(config_path, store)

    assert status == 200
    assert payload["state_db_bytes"] > 0
    assert payload["tables"]["repo_packages"] == 1
    assert "cache_dir" not in payload


def test_stats_payload_cache_dir_is_none_when_not_configured(tmp_path):
    config_path, store = _setup(tmp_path)

    status, payload = stats_payload(config_path, store, include_cache_dir=True)

    assert status == 200
    assert payload["cache_dir"] is None


def test_stats_payload_walks_the_real_cache_dir_when_configured(tmp_path):
    cache_dir = tmp_path / "nginx-cache"
    (cache_dir / "1" / "23").mkdir(parents=True)
    (cache_dir / "1" / "23" / "somefile").write_bytes(b"x" * 100)
    (cache_dir / "1" / "23" / "otherfile").write_bytes(b"y" * 50)
    config_path = _write_config(
        tmp_path,
        f"""  - id: alpine-test
    type: apk
    upstream: https://example.org/alpine/v3.20/main
    arch: x86_64
nginx:
  enabled: true
  cache_dir: {cache_dir}
""",
    )
    store = StateStore(load_config(config_path).state_db)

    status, payload = stats_payload(config_path, store, include_cache_dir=True)

    assert status == 200
    assert payload["cache_dir"] == {"path": str(cache_dir), "size_bytes": 150, "file_count": 2}


def test_cache_dir_stats_raises_for_a_missing_path(tmp_path):
    with pytest.raises(OSError):
        cache_dir_stats(str(tmp_path / "does-not-exist"))


def test_cache_dir_stats_flags_undercount_from_inaccessible_subdirectories(tmp_path):
    """Real production finding (2026-09-10): nginx creates proxy_cache_path's
    levels=1:2 subdirectories 0700, owned by the nginx worker user — the
    repowatch service user (a DIFFERENT, unprivileged user by design) cannot
    read into them at all. os.walk()'s default onerror is a silent no-op,
    which made a permission-blocked, actually-41GB cache report "0 bytes" —
    indistinguishable from genuinely empty. Must surface the gap instead."""
    cache_dir = tmp_path / "nginx-cache"
    readable = cache_dir / "0"
    readable.mkdir(parents=True)
    (readable / "somefile").write_bytes(b"x" * 100)
    blocked = cache_dir / "1"
    blocked.mkdir()
    (blocked / "hidden").write_bytes(b"y" * 999)
    blocked.chmod(0o000)
    try:
        result = cache_dir_stats(str(cache_dir))
        assert result["size_bytes"] == 100  # only the readable subdirectory counted
        assert result["file_count"] == 1
        assert result["inaccessible_directories"] == 1
    finally:
        blocked.chmod(0o755)  # tmp_path cleanup needs to be able to remove it


def test_stats_payload_surfaces_a_missing_cache_dir_as_an_error_not_zero(tmp_path):
    missing = tmp_path / "does-not-exist"
    config_path = _write_config(
        tmp_path,
        f"""  - id: alpine-test
    type: apk
    upstream: https://example.org/alpine/v3.20/main
    arch: x86_64
nginx:
  enabled: true
  cache_dir: {missing}
""",
    )
    store = StateStore(load_config(config_path).state_db)

    status, payload = stats_payload(config_path, store, include_cache_dir=True)

    assert status == 200
    assert "error" in payload["cache_dir"]


def test_stats_payload_prefers_cache_probe_when_enabled(tmp_path):
    """docs_dev/ROADMAP.md item 8 — hooked up to "Calculate cache directory
    size" (item 27): with nginx.enable_cache_probe on, the njs-based
    ground-truth scan is used instead of os.walk(), and it works even
    without nginx.cache_dir set locally (unlike the os.walk() path) since
    it only needs cache_base_url, which is always present."""
    import httpx
    from unittest.mock import patch

    config_path = _write_config(
        tmp_path,
        """  - id: alpine-test
    type: apk
    upstream: https://example.org/alpine/v3.20/main
    arch: x86_64
nginx:
  enabled: true
  enable_cache_probe: true
""",
    )
    store = StateStore(load_config(config_path).state_db)

    def handler(request):
        if request.url.params.get("dir") == "c/29":
            return httpx.Response(200, json=[
                {"file": "f1", "key": "k1", "size": 100},
                {"file": "f2", "key": None, "error": "bad header"},
            ])
        return httpx.Response(200, json=[])

    real_async_client = httpx.AsyncClient
    with patch("repowatch.cache_probe.httpx.AsyncClient",
               lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
        status, payload = stats_payload(config_path, store, include_cache_dir=True)

    assert status == 200
    assert payload["cache_dir"]["source"] == "cache_probe"
    assert payload["cache_dir"]["size_bytes"] == 100
    assert payload["cache_dir"]["file_count"] == 2
    assert payload["cache_dir"]["unreadable_keys"] == 1
    # No nginx.cache_dir configured — falls back to cache_base_url for display.
    assert payload["cache_dir"]["path"] == "http://127.0.0.1:8080"


def test_stats_payload_reports_cache_probe_unreachable_as_an_error_not_zero(tmp_path):
    """Same principle as test_stats_payload_surfaces_a_missing_cache_dir_as_
    an_error_not_zero, for the cache_probe path: if nginx.enable_cache_probe
    is set but the endpoint can't actually be reached (module not loaded
    yet, nginx down), every one of the 4096 leaf requests would otherwise
    fail identically and get silently swallowed by full_inventory() — must
    not look like "the cache is empty"."""
    import httpx
    from unittest.mock import patch

    config_path = _write_config(
        tmp_path,
        """  - id: alpine-test
    type: apk
    upstream: https://example.org/alpine/v3.20/main
    arch: x86_64
nginx:
  enabled: true
  enable_cache_probe: true
""",
    )
    store = StateStore(load_config(config_path).state_db)

    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    real_async_client = httpx.AsyncClient
    with patch("repowatch.cache_probe.httpx.AsyncClient",
               lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
        status, payload = stats_payload(config_path, store, include_cache_dir=True)

    assert status == 200
    assert "error" in payload["cache_dir"]


def _setup_purge(tmp_path, admin_password="secret123", enable_purge=True, enable_cache_probe=False):
    config_path = _write_config(
        tmp_path,
        f"""  - id: alpine-test
    type: apk
    upstream: https://example.org/alpine/v3.20/main
    arch: x86_64
nginx:
  enabled: true
  enable_purge: {"true" if enable_purge else "false"}
  enable_cache_probe: {"true" if enable_cache_probe else "false"}
""",
        admin_password=admin_password,
    )
    store = StateStore(load_config(config_path).state_db)
    return config_path, store


def test_purge_candidates_payload_lists_stale_warmed_entries_and_enable_purge_flag(tmp_path):
    config_path, store = _setup_purge(tmp_path)
    store.record_snapshot(RepoSnapshot("alpine-test", {"keep-1": "keep-1.apk"}))
    store.record_warmed_package("alpine-test", "keep-1", "keep-1.apk", True, 200)
    store.record_warmed_package("alpine-test", "gone-1", "gone-1.apk", True, 200)

    status, payload = purge_candidates_payload(config_path, store, "alpine-test")

    assert status == 200
    assert payload["enable_purge"] is True
    assert payload["candidates"] == [{"package_key": "gone-1", "filename": "gone-1.apk"}]


def test_purge_candidates_payload_reports_enable_purge_false(tmp_path):
    config_path, store = _setup_purge(tmp_path, enable_purge=False)
    status, payload = purge_candidates_payload(config_path, store, "alpine-test")
    assert status == 200
    assert payload["enable_purge"] is False


def test_purge_candidates_payload_unknown_repo_404(tmp_path):
    config_path, store = _setup_purge(tmp_path)
    status, payload = purge_candidates_payload(config_path, store, "does-not-exist")
    assert status == 404


def test_purge_selected_payload_disabled_without_admin_password(tmp_path):
    config_path, store = _setup_purge(tmp_path, admin_password=None)
    status, payload = purge_selected_payload(
        config_path, store, "alpine-test", _session(config_path, "whatever"), {"package_keys": ["x"]}
    )
    assert status == 501


def test_purge_selected_payload_rejects_wrong_password(tmp_path):
    config_path, store = _setup_purge(tmp_path)
    status, payload = purge_selected_payload(
        config_path, store, "alpine-test", _session(config_path, "wrong"), {"package_keys": ["x"]}
    )
    assert status == 401


def test_purge_selected_payload_unknown_repo_404(tmp_path):
    config_path, store = _setup_purge(tmp_path)
    status, payload = purge_selected_payload(
        config_path, store, "does-not-exist", _session(config_path, "secret123"), {"package_keys": ["x"]}
    )
    assert status == 404


def test_purge_selected_payload_400_when_enable_purge_is_off(tmp_path):
    config_path, store = _setup_purge(tmp_path, enable_purge=False)
    store.record_warmed_package("alpine-test", "gone-1", "gone-1.apk", True, 200)

    status, payload = purge_selected_payload(
        config_path, store, "alpine-test", _session(config_path, "secret123"), {"package_keys": ["gone-1"]}
    )

    assert status == 400
    assert "enable_purge" in payload["error"]


@pytest.mark.parametrize("body", [{}, {"package_key": "x"}, {"package_keys": []}, {"package_keys": "x"}])
def test_purge_selected_payload_requires_a_nonempty_package_keys_list(tmp_path, body):
    config_path, store = _setup_purge(tmp_path)
    status, payload = purge_selected_payload(
        config_path, store, "alpine-test", _session(config_path, "secret123"), body
    )
    assert status == 400


def test_purge_selected_payload_purges_and_cleans_up_warmed_bookkeeping(tmp_path, monkeypatch):
    """End-to-end through the payload function (not the real network) —
    filenames are re-derived from warmed_packages, never trusted from the
    request body, and a confirmed outcome (purged/not_cached) removes the
    now-meaningless warmed_packages row so a re-scan doesn't show it again."""
    config_path, store = _setup_purge(tmp_path)
    store.record_warmed_package("alpine-test", "purged-1", "purged-1.apk", True, 200)
    store.record_warmed_package("alpine-test", "already-gone-1", "already-gone-1.apk", True, 200)
    store.record_warmed_package("alpine-test", "flaky-1", "flaky-1.apk", True, 200)

    async def fake_purge_selected(config, repo, items):
        assert items == {
            "purged-1": "purged-1.apk",
            "already-gone-1": "already-gone-1.apk",
            "flaky-1": "flaky-1.apk",
            # a client-supplied filename for a key not actually in
            # warmed_packages must never reach here at all (re-derived
            # server-side, absent keys silently drop out of `items`)
        }
        return {"purged-1": "purged", "already-gone-1": "not_cached", "flaky-1": "error (timeout)"}

    monkeypatch.setattr("repowatch.api.purge_selected", fake_purge_selected)

    status, payload = purge_selected_payload(
        config_path, store, "alpine-test", _session(config_path, "secret123"),
        {"package_keys": ["purged-1", "already-gone-1", "flaky-1", "not-actually-warmed"]},
    )

    assert status == 200
    assert payload["results"] == {"purged-1": "purged", "already-gone-1": "not_cached", "flaky-1": "error (timeout)"}
    assert payload["not_found"] == ["not-actually-warmed"]
    remaining = {p["package_key"] for p in store.get_warmed_packages("alpine-test")}
    assert remaining == {"flaky-1"}  # only the errored one survives for a retry


def test_purge_selected_payload_uses_cache_probe_when_enable_cache_probe_is_on(tmp_path, monkeypatch):
    """By direct user request (2026-09-14): "по умолчанию пурж через
    дашборд работает стандартным механизмом, но если включен cache_probe
    применяем purge_raw" — with nginx.enable_cache_probe on, "Purge
    selected" must go through cache_probe.purge_selected_raw() instead of
    prefetch.purge_selected(), not just fall back to it."""
    config_path, store = _setup_purge(tmp_path, enable_cache_probe=True)
    store.record_warmed_package("alpine-test", "a-1", "a-1.apk", True, 200)

    def boom(*a, **kw):
        raise AssertionError("prefetch.purge_selected must not be called when enable_cache_probe is on")
    monkeypatch.setattr("repowatch.api.purge_selected", boom)

    calls = []
    async def fake_purge_selected_raw(config, repo, items):
        calls.append((repo.id, items))
        return {"a-1": "purged"}
    monkeypatch.setattr("repowatch.cache_probe.purge_selected_raw", fake_purge_selected_raw)

    status, payload = purge_selected_payload(
        config_path, store, "alpine-test", _session(config_path, "secret123"), {"package_keys": ["a-1"]},
    )

    assert status == 200
    assert payload["results"] == {"a-1": "purged"}
    assert calls == [("alpine-test", {"a-1": "a-1.apk"})]
    assert store.get_warmed_packages("alpine-test") == []


@pytest.mark.parametrize('action', [purge_selected_payload, remove_warmed_package_payload])
def test_raw_purge_failure_keeps_warmed_record_until_both_keys_confirmed(tmp_path, monkeypatch, action):
    import httpx
    config_path = _write_config(tmp_path, '''  - id: core
    type: pacman
    upstream: https://mirror.test/core/os/x86_64
    repo_name: core
    arch: x86_64
nginx:
  enabled: true
  enable_purge: true
  enable_cache_probe: true
  enable_dedup: true
''', admin_password='secret123')
    store = StateStore(load_config(config_path).state_db)
    store.record_warmed_package('core', 'foo', 'foo.pkg.tar.zst', True, 200)
    responses = iter([404, 503, 404, 200])
    def handler(request):
        return httpx.Response(next(responses))
    real_client = httpx.AsyncClient
    monkeypatch.setattr('repowatch.cache_probe.httpx.AsyncClient',
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    session = _session(config_path, 'secret123')
    status, payload = action(config_path, store, 'core', session, {'package_keys': ['foo']})
    result_field = 'results' if action is purge_selected_payload else 'purge_results'
    assert status == 200
    assert payload[result_field]['foo'] == 'error (HTTP 503)'
    assert [row['package_key'] for row in store.get_warmed_packages('core')] == ['foo']
    status, payload = action(config_path, store, 'core', session, {'package_keys': ['foo']})
    assert status == 200
    assert payload[result_field]['foo'] == 'purged'
    assert store.get_warmed_packages('core') == []
