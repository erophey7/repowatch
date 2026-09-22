from repowatch.config.models import Config, RepoConfig, StatusServerConfig
from repowatch.models import RepoSnapshot
from repowatch.operations.cleanup import find_orphaned_repos, purge_orphaned_repos
from repowatch.runtime.context import ServiceState


def _store(tmp_path) -> ServiceState:
    return ServiceState(tmp_path / "state.sqlite3")


def _config(tmp_path, repos: list[RepoConfig] | None = None) -> Config:
    return Config(
        state_db=str(tmp_path / "state.sqlite3"), check_interval=300,
        cache_base_url="http://127.0.0.1:8080", status_server=StatusServerConfig(),
        repos=repos or [],
    )


def test_find_orphaned_repos_reports_repo_ids_absent_from_config(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(RepoSnapshot("kept", {"a-1": "a.deb"}))
    store.repositories.record_snapshot(RepoSnapshot("orphan", {"b-1": "b.deb"}))
    config = _config(tmp_path, [RepoConfig(id="kept", type="pacman", upstream="http://example.test", arch="x86_64", repo_name="core")])

    orphaned = find_orphaned_repos(config, store)

    assert "kept" not in orphaned
    assert orphaned["orphan"] == {"repo_state": 1, "repo_packages": 1, "repo_events": 1}


def test_find_orphaned_repos_aggregates_across_all_three_stores(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(RepoSnapshot("orphan", {"a-1": "a.deb"}))
    store.cache.record_warmed_package("orphan", "a-1", "a.deb", True, 200)
    store.cache.ban_package("orphan", "banned-name")
    store.notifications.bump_failure("orphan", "gpg", "boom")
    config = _config(tmp_path)

    orphaned = find_orphaned_repos(config, store)

    assert orphaned["orphan"] == {
        "repo_state": 1, "repo_packages": 1, "repo_events": 1,
        "warmed_packages": 1, "prefetch_bans": 1,
        "failure_state": 1,
    }


def test_find_orphaned_repos_empty_when_all_repo_ids_are_still_configured(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(RepoSnapshot("kept", {"a-1": "a.deb"}))
    config = _config(tmp_path, [RepoConfig(id="kept", type="pacman", upstream="http://example.test", arch="x86_64", repo_name="core")])

    assert find_orphaned_repos(config, store) == {}


def test_find_orphaned_repos_ignores_a_repo_id_only_present_in_request_events(tmp_path):
    # request_events has its own separate, already-implemented retention and
    # a nullable, best-effort-only repo_id — not part of this cleanup, see
    # find_orphaned_repos' own docstring.
    store = _store(tmp_path)
    store.requests.record_request("ghost", "192.0.2.1", "GET", "/x", "200", "HIT")
    config = _config(tmp_path)

    assert find_orphaned_repos(config, store) == {}


def test_purge_orphaned_repos_deletes_rows_across_all_three_stores(tmp_path):
    store = _store(tmp_path)
    store.repositories.record_snapshot(RepoSnapshot("orphan", {"a-1": "a.deb"}))
    store.cache.record_warmed_package("orphan", "a-1", "a.deb", True, 200)
    store.notifications.bump_failure("orphan", "gpg", "boom")
    config = _config(tmp_path)

    deleted = purge_orphaned_repos(config, store, ["orphan"])

    assert deleted["orphan"]["repo_state"] == 1
    assert deleted["orphan"]["warmed_packages"] == 1
    assert deleted["orphan"]["failure_state"] == 1
    assert find_orphaned_repos(config, store) == {}


def test_purge_orphaned_repos_skips_a_repo_id_re_added_to_config_since_the_scan(tmp_path):
    # Defensive re-check, same pattern as operations.cleanup.purge_items'
    # other callers: the dashboard's scan and this delete call are two
    # separate requests, and the operator could have re-added the
    # repository (same id) in between.
    store = _store(tmp_path)
    store.repositories.record_snapshot(RepoSnapshot("back", {"a-1": "a.deb"}))
    config = _config(tmp_path, [RepoConfig(id="back", type="pacman", upstream="http://example.test", arch="x86_64", repo_name="core")])

    deleted = purge_orphaned_repos(config, store, ["back"])

    assert deleted == {}
    assert store.repositories.get_status("back") is not None


def test_purge_orphaned_repos_omits_a_repo_id_with_nothing_to_delete(tmp_path):
    store = _store(tmp_path)
    config = _config(tmp_path)

    assert purge_orphaned_repos(config, store, ["never-existed"]) == {}
