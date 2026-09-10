import socket
import threading
import time

from unittest.mock import patch

from repowatch.config import RepoConfig
from repowatch.state import RepoSnapshot, StateStore
from repowatch.syslog_listener import (
    basename_index,
    match_all_package_keys,
    match_repo_id,
    parse_syslog_line,
    refresh_package_indexes,
    run_listener,
)


def test_parses_rfc3164_style_datagram_with_pid():
    raw = b"<134>Sep  5 12:00:00 myhost repowatch[12345]: 203.0.113.7 GET /arch/core/os/x86_64/linux-6.11.2-1-x86_64.pkg.tar.zst 200 HIT"
    parsed = parse_syslog_line(raw)

    assert parsed is not None
    assert parsed.client_ip == "203.0.113.7"
    assert parsed.method == "GET"
    assert parsed.path == "/arch/core/os/x86_64/linux-6.11.2-1-x86_64.pkg.tar.zst"
    assert parsed.status == "200"
    assert parsed.cache_status == "HIT"


def test_parses_datagram_without_pid():
    raw = b"<134>Sep  5 12:00:00 myhost repowatch: 203.0.113.7 GET /debian/pool/main/z/zlib/zlib1g_1.2.13-1_amd64.deb 200 MISS"
    parsed = parse_syslog_line(raw)

    assert parsed is not None
    assert parsed.path == "/debian/pool/main/z/zlib/zlib1g_1.2.13-1_amd64.deb"
    assert parsed.cache_status == "MISS"


def test_missing_optional_fields_are_none():
    raw = b"<134>Sep  5 12:00:00 myhost repowatch: 203.0.113.7 GET /alpine/x86_64/musl-1.2.5-r0.apk"
    parsed = parse_syslog_line(raw)

    assert parsed is not None
    assert parsed.status is None
    assert parsed.cache_status is None


def test_is_prefetch_field_parsed_when_present():
    raw = b"<134>Sep  5 12:00:00 myhost repowatch: 127.0.0.1 GET /debian/dists/bookworm/InRelease 200 MISS 1"
    parsed = parse_syslog_line(raw)

    assert parsed is not None
    assert parsed.is_prefetch is True


def test_is_prefetch_defaults_to_false_when_field_absent_or_zero():
    older_format = b"<134>Sep  5 12:00:00 myhost repowatch: 203.0.113.7 GET /debian/pool/main/z/zlib.deb 200 HIT"
    assert parse_syslog_line(older_format).is_prefetch is False
    explicit_zero = b"<134>Sep  5 12:00:00 myhost repowatch: 203.0.113.7 GET /debian/pool/main/z/zlib.deb 200 HIT 0"
    assert parse_syslog_line(explicit_zero).is_prefetch is False


def test_garbage_input_returns_none_not_exception():
    assert parse_syslog_line(b"") is None
    assert parse_syslog_line(b"complete garbage, no tag here") is None
    assert parse_syslog_line(b"<134>Sep  5 12:00:00 myhost repowatch: onlyonefield") is None
    assert parse_syslog_line(b"<134>Sep  5 12:00:00 myhost repowatch: 203.0.113.7 GET") is None
    assert parse_syslog_line(b"\xff\xfe\x00\x01") is None


def _pacman_repo(id_, repo_name, arch="x86_64"):
    return RepoConfig(
        id=id_, type="pacman", upstream="https://example.org", arch=arch, repo_name=repo_name
    )


def test_match_repo_id_unique_prefix():
    repos = [_pacman_repo("arch-core", "core"), _pacman_repo("arch-extra", "extra")]
    assert match_repo_id("/arch/core/os/x86_64/linux-6.11.2-1-x86_64.pkg.tar.zst", repos) == "arch-core"
    assert match_repo_id("/arch/extra/os/x86_64/vim-9.1-1-x86_64.pkg.tar.zst", repos) == "arch-extra"


def test_match_repo_id_no_match_returns_none():
    repos = [_pacman_repo("arch-core", "core")]
    assert match_repo_id("/debian/pool/main/b/bash/bash_5.2-1_amd64.deb", repos) is None


def test_match_repo_id_ambiguous_prefix_returns_none():
    # two repositories with the same repo_name/arch produce the same prefix —
    # we deliberately don't guess which one
    repos = [_pacman_repo("arch-core-a", "core"), _pacman_repo("arch-core-b", "core")]
    assert match_repo_id("/arch/core/os/x86_64/linux-6.11.2-1-x86_64.pkg.tar.zst", repos) is None


def test_basename_index_pacman_style_filenames():
    packages = {"linux-6.11.2-1": "linux-6.11.2-1-x86_64.pkg.tar.zst"}
    assert basename_index(packages) == {"linux-6.11.2-1-x86_64.pkg.tar.zst": "linux-6.11.2-1"}


def test_basename_index_apt_style_filenames_with_path():
    packages = {"linux-1-2": "pool/main/l/linux/linux_1-2_amd64.deb"}
    assert basename_index(packages) == {"linux_1-2_amd64.deb": "linux-1-2"}


def test_basename_index_skips_empty_filenames():
    packages = {"broken-1": "", "ok-1": "ok-1.apk"}
    assert basename_index(packages) == {"ok-1.apk": "ok-1"}


def test_match_all_package_keys_finds_match_in_any_repo():
    by_basename = {
        "arch-core": {},
        "ubuntu-noble-main": {"bash_5.2-1_amd64.deb": "bash-5.2-1"},
    }
    assert match_all_package_keys(
        "/ubuntu/pool/main/b/bash/bash_5.2-1_amd64.deb", by_basename
    ) == [("ubuntu-noble-main", "bash-5.2-1")]


def test_match_all_package_keys_returns_all_repos_sharing_the_same_pool_file():
    """Regression for the feature itself: warmed_packages used to depend on
    match_repo_id (by URL prefix), which deliberately returns None with
    multiple candidates — apt repositories sharing upstream/component but
    with different suites (noble/noble-updates/noble-backports) physically
    share the same pool/, so none of them was ever marked warmed from real
    client requests."""
    by_basename = {
        "ubuntu-noble-main": {"bash_5.2-1_amd64.deb": "bash-5.2-1"},
        "ubuntu-noble-updates-main": {"bash_5.2-1_amd64.deb": "bash-5.2-1"},
        "ubuntu-noble-backports-main": {},
    }
    matches = match_all_package_keys("/ubuntu/pool/main/b/bash/bash_5.2-1_amd64.deb", by_basename)
    assert set(matches) == {
        ("ubuntu-noble-main", "bash-5.2-1"),
        ("ubuntu-noble-updates-main", "bash-5.2-1"),
    }


def test_match_all_package_keys_returns_empty_list_when_unknown():
    assert match_all_package_keys("/debian/pool/main/x/xyz/xyz_1_amd64.deb", {}) == []


def test_refresh_package_indexes_builds_everything_on_first_call(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.record_snapshot(
        RepoSnapshot(repo_id="arch-core", packages={"linux-1": "linux-1-x86_64.pkg.tar.zst"})
    )
    repo = _pacman_repo("arch-core", "core")

    packages_by_repo: dict = {}
    by_basename: dict = {}
    last_changed_at: dict = {}

    refresh_package_indexes([repo], store, packages_by_repo, by_basename, last_changed_at)

    assert packages_by_repo["arch-core"] == {"linux-1": "linux-1-x86_64.pkg.tar.zst"}
    assert by_basename["arch-core"] == {"linux-1-x86_64.pkg.tar.zst": "linux-1"}
    assert "arch-core" in last_changed_at


def test_refresh_package_indexes_skips_unchanged_repo_on_second_call(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.record_snapshot(
        RepoSnapshot(repo_id="arch-core", packages={"linux-1": "linux-1-x86_64.pkg.tar.zst"})
    )
    repo = _pacman_repo("arch-core", "core")

    packages_by_repo: dict = {}
    by_basename: dict = {}
    last_changed_at: dict = {}
    refresh_package_indexes([repo], store, packages_by_repo, by_basename, last_changed_at)

    # nothing changed in the repository — the second call must not touch
    # sqlite for packages at all
    with patch.object(store, "get_packages") as mock_get_packages:
        refresh_package_indexes([repo], store, packages_by_repo, by_basename, last_changed_at)
        mock_get_packages.assert_not_called()


def test_refresh_package_indexes_rebuilds_when_changed_at_moves(tmp_path):
    """changed_at has second-level precision (see state._utcnow) — instead
    of relying on real delay between two record_snapshot() calls (flaky if
    both land in the same second), we plant a deliberately stale value into
    last_changed_at directly. This tests the same decision branch ("current
    changed_at differs from last time — refresh"), without depending on
    clock precision."""
    store = StateStore(tmp_path / "state.sqlite3")
    store.record_snapshot(
        RepoSnapshot(
            repo_id="arch-core",
            packages={
                "linux-1": "linux-1-x86_64.pkg.tar.zst",
                "vim-1": "vim-1-x86_64.pkg.tar.zst",
            },
        )
    )
    repo = _pacman_repo("arch-core", "core")

    packages_by_repo: dict = {}
    by_basename: dict = {}
    last_changed_at = {"arch-core": "2000-01-01T00:00:00+00:00"}

    refresh_package_indexes([repo], store, packages_by_repo, by_basename, last_changed_at)

    assert packages_by_repo["arch-core"] == {
        "linux-1": "linux-1-x86_64.pkg.tar.zst",
        "vim-1": "vim-1-x86_64.pkg.tar.zst",
    }
    assert by_basename["arch-core"] == {
        "linux-1-x86_64.pkg.tar.zst": "linux-1",
        "vim-1-x86_64.pkg.tar.zst": "vim-1",
    }
    # last_changed_at was refreshed to the current real value
    assert last_changed_at["arch-core"] == store.get_status("arch-core")["changed_at"]


def test_refresh_package_indexes_refreshes_repo_added_after_first_call(tmp_path):
    """A repository added after the first call (e.g. through the dashboard)
    must be picked up immediately, not wait for its own "change"."""
    store = StateStore(tmp_path / "state.sqlite3")
    store.record_snapshot(
        RepoSnapshot(repo_id="arch-core", packages={"linux-1": "linux-1-x86_64.pkg.tar.zst"})
    )
    core = _pacman_repo("arch-core", "core")
    extra = _pacman_repo("arch-extra", "extra")

    packages_by_repo: dict = {}
    by_basename: dict = {}
    last_changed_at: dict = {}
    refresh_package_indexes([core], store, packages_by_repo, by_basename, last_changed_at)

    store.record_snapshot(
        RepoSnapshot(repo_id="arch-extra", packages={"vim-1": "vim-1-x86_64.pkg.tar.zst"})
    )
    refresh_package_indexes([core, extra], store, packages_by_repo, by_basename, last_changed_at)

    assert packages_by_repo["arch-extra"] == {"vim-1": "vim-1-x86_64.pkg.tar.zst"}


def test_run_listener_records_real_client_download_as_warmed(tmp_path):
    """Regression for the feature itself: warmed_packages used to be fed
    ONLY by repowatch's own warming — real client downloads were invisible.
    Now a successful (200) real request for a known package must also land
    in warmed_packages."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
state_db: {tmp_path / "state.sqlite3"}
cache_base_url: http://127.0.0.1:8080
syslog_listener:
  enabled: true
  bind: 127.0.0.1
  port: 0
repos:
  - id: arch-core
    type: pacman
    upstream: https://example.org/core/os/x86_64
    arch: x86_64
    repo_name: core
"""
    )
    from repowatch.config import load_config

    config = load_config(config_path)
    store = StateStore(config.state_db)
    store.record_snapshot(
        RepoSnapshot(
            repo_id="arch-core",
            packages={"linux-6.11.2-1": "linux-6.11.2-1-x86_64.pkg.tar.zst"},
            names={"linux-6.11.2-1": "linux"},
        )
    )

    # port 0 asks the OS to pick a free one — we learn it via a separate
    # socket, then pass it into run_listener through a replaced Config
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    from dataclasses import replace

    config = replace(config, syslog_listener=replace(config.syslog_listener, port=port))

    t = threading.Thread(target=run_listener, args=(str(config_path), config, store), daemon=True)
    t.start()
    time.sleep(0.2)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    msg = b"<134>Sep  6 12:00:00 myhost repowatch: 203.0.113.7 GET /arch/core/os/x86_64/linux-6.11.2-1-x86_64.pkg.tar.zst 200 HIT"
    sock.sendto(msg, ("127.0.0.1", port))
    time.sleep(0.3)

    warmed = store.get_warmed_packages("arch-core")
    assert len(warmed) == 1
    assert warmed[0]["package_key"] == "linux-6.11.2-1"
    assert warmed[0]["status"] == "ok"


def test_run_listener_ignores_repowatch_own_prefetch_traffic(tmp_path):
    """docs_dev/ROADMAP.md — repowatch's own index checks/prefetch (marked
    via nginx.py's $repowatch_is_prefetch map) must not show up in "Recent
    client requests" (request_events), and must not produce a redundant
    warmed_packages write either (warm_cache already records that directly)."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
state_db: {tmp_path / "state.sqlite3"}
cache_base_url: http://127.0.0.1:8080
syslog_listener:
  enabled: true
  bind: 127.0.0.1
  port: 0
repos:
  - id: arch-core
    type: pacman
    upstream: https://example.org/core/os/x86_64
    arch: x86_64
    repo_name: core
"""
    )
    from repowatch.config import load_config

    config = load_config(config_path)
    store = StateStore(config.state_db)
    store.record_snapshot(
        RepoSnapshot(
            repo_id="arch-core",
            packages={"linux-6.11.2-1": "linux-6.11.2-1-x86_64.pkg.tar.zst"},
            names={"linux-6.11.2-1": "linux"},
        )
    )

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    from dataclasses import replace

    config = replace(config, syslog_listener=replace(config.syslog_listener, port=port))

    t = threading.Thread(target=run_listener, args=(str(config_path), config, store), daemon=True)
    t.start()
    time.sleep(0.2)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Own prefetch traffic — trailing "1" marks it, same as nginx's map would.
    msg = b"<134>Sep  6 12:00:00 myhost repowatch: 127.0.0.1 GET /arch/core/os/x86_64/linux-6.11.2-1-x86_64.pkg.tar.zst 200 MISS 1"
    sock.sendto(msg, ("127.0.0.1", port))
    time.sleep(0.3)

    assert store.get_warmed_packages("arch-core") == []
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM request_events").fetchone()[0] == 0


def test_run_listener_records_warmed_for_all_sibling_repos_sharing_pool(tmp_path):
    """Regression for the bug itself: ubuntu-noble-main and
    ubuntu-noble-updates-main share one upstream/component (meaning the
    same pool/ on upstream) — match_repo_id would return None (multiple
    candidates by prefix), and because of that neither used to be marked
    warmed from real client requests, even though the file was definitely
    served. Now BOTH must be marked."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
state_db: {tmp_path / "state.sqlite3"}
cache_base_url: http://127.0.0.1:8080
syslog_listener:
  enabled: true
  bind: 127.0.0.1
  port: 0
repos:
  - id: ubuntu-noble-main
    type: apt
    upstream: http://archive.ubuntu.com/ubuntu
    distribution: noble
    component: main
    arch: amd64
  - id: ubuntu-noble-updates-main
    type: apt
    upstream: http://archive.ubuntu.com/ubuntu
    distribution: noble-updates
    component: main
    arch: amd64
"""
    )
    from repowatch.config import load_config

    config = load_config(config_path)
    store = StateStore(config.state_db)
    for repo_id in ("ubuntu-noble-main", "ubuntu-noble-updates-main"):
        store.record_snapshot(
            RepoSnapshot(
                repo_id=repo_id,
                packages={"bash-5.2-1": "pool/main/b/bash/bash_5.2-1_amd64.deb"},
                names={"bash-5.2-1": "bash"},
            )
        )

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    from dataclasses import replace

    config = replace(config, syslog_listener=replace(config.syslog_listener, port=port))

    t = threading.Thread(target=run_listener, args=(str(config_path), config, store), daemon=True)
    t.start()
    time.sleep(0.2)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    msg = b"<134>Sep  6 12:00:00 myhost repowatch: 203.0.113.7 GET /ubuntu/pool/main/b/bash/bash_5.2-1_amd64.deb 200 MISS"
    sock.sendto(msg, ("127.0.0.1", port))
    time.sleep(0.3)

    for repo_id in ("ubuntu-noble-main", "ubuntu-noble-updates-main"):
        warmed = store.get_warmed_packages(repo_id)
        assert len(warmed) == 1
        assert warmed[0]["package_key"] == "bash-5.2-1"
        assert warmed[0]["status"] == "ok"


def test_run_listener_ignores_non_200_status(tmp_path):
    """404/5xx does not mean "warmed" — nothing should be recorded."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
state_db: {tmp_path / "state.sqlite3"}
cache_base_url: http://127.0.0.1:8080
syslog_listener:
  enabled: true
  bind: 127.0.0.1
  port: 0
repos:
  - id: arch-core
    type: pacman
    upstream: https://example.org/core/os/x86_64
    arch: x86_64
    repo_name: core
"""
    )
    from dataclasses import replace

    from repowatch.config import load_config

    config = load_config(config_path)
    store = StateStore(config.state_db)
    store.record_snapshot(
        RepoSnapshot(
            repo_id="arch-core",
            packages={"linux-6.11.2-1": "linux-6.11.2-1-x86_64.pkg.tar.zst"},
            names={"linux-6.11.2-1": "linux"},
        )
    )

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    config = replace(config, syslog_listener=replace(config.syslog_listener, port=port))

    t = threading.Thread(target=run_listener, args=(str(config_path), config, store), daemon=True)
    t.start()
    time.sleep(0.2)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    msg = b"<134>Sep  6 12:00:00 myhost repowatch: 203.0.113.7 GET /arch/core/os/x86_64/linux-6.11.2-1-x86_64.pkg.tar.zst 404 MISS"
    sock.sendto(msg, ("127.0.0.1", port))
    time.sleep(0.3)

    assert store.get_warmed_packages("arch-core") == []
