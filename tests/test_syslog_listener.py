import socket
import threading
import time

from unittest.mock import patch

from repowatch.config.models import RepoConfig
from repowatch.models import RepoSnapshot
from repowatch.runtime.context import ServiceState
from repowatch.runtime.syslog import package_path_index
from repowatch.runtime.syslog import match_all_package_keys
from repowatch.runtime.syslog import match_repo_id
from repowatch.runtime.syslog import parse_syslog_line
from repowatch.runtime.syslog import refresh_package_indexes
from repowatch.runtime.syslog import run_listener


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


def test_package_path_index_pacman_style_filenames():
    packages = {"linux-6.11.2-1": "linux-6.11.2-1-x86_64.pkg.tar.zst"}
    assert package_path_index(_pacman_repo("r", "core"), packages) == {"/arch/core/os/x86_64/linux-6.11.2-1-x86_64.pkg.tar.zst": ["linux-6.11.2-1"]}


def test_package_path_index_apt_style_filenames_with_path():
    packages = {"linux-1-2": "pool/main/l/linux/linux_1-2_amd64.deb"}
    assert package_path_index(RepoConfig(id="r", type="apt", upstream="https://example.org/ubuntu", arch="amd64", distribution="noble", component="main"), packages) == {"/ubuntu/pool/main/l/linux/linux_1-2_amd64.deb": ["linux-1-2"]}


def test_package_path_index_skips_empty_filenames():
    packages = {"broken-1": "", "ok-1": "ok-1.apk"}
    assert package_path_index(_pacman_repo("r", "core"), packages) == {"/arch/core/os/x86_64/ok-1.apk": ["ok-1"]}


def test_match_all_package_keys_finds_match_in_any_repo():
    by_path = {
        "arch-core": {},
        "ubuntu-noble-main": {"/ubuntu/pool/main/b/bash/bash_5.2-1_amd64.deb": ["bash-5.2-1"]},
    }
    assert match_all_package_keys(
        "/ubuntu/pool/main/b/bash/bash_5.2-1_amd64.deb", by_path
    ) == [("ubuntu-noble-main", "bash-5.2-1")]


def test_match_all_package_keys_returns_all_repos_sharing_the_same_pool_file():
    """Regression for the feature itself: warmed_packages used to depend on
    match_repo_id (by URL prefix), which deliberately returns None with
    multiple candidates — apt repositories sharing upstream/component but
    with different suites (noble/noble-updates/noble-backports) physically
    share the same pool/, so none of them was ever marked warmed from real
    client requests."""
    by_path = {
        "ubuntu-noble-main": {"/ubuntu/pool/main/b/bash/bash_5.2-1_amd64.deb": ["bash-5.2-1"]},
        "ubuntu-noble-updates-main": {"/ubuntu/pool/main/b/bash/bash_5.2-1_amd64.deb": ["bash-5.2-1"]},
        "ubuntu-noble-backports-main": {},
    }
    matches = match_all_package_keys("/ubuntu/pool/main/b/bash/bash_5.2-1_amd64.deb", by_path)
    assert set(matches) == {
        ("ubuntu-noble-main", "bash-5.2-1"),
        ("ubuntu-noble-updates-main", "bash-5.2-1"),
    }


def test_match_all_package_keys_returns_empty_list_when_unknown():
    assert match_all_package_keys("/debian/pool/main/x/xyz/xyz_1_amd64.deb", {}) == []


def test_refresh_package_indexes_builds_everything_on_first_call(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    store.repositories.record_snapshot(
        RepoSnapshot(repo_id="arch-core", packages={"linux-1": "linux-1-x86_64.pkg.tar.zst"})
    )
    repo = _pacman_repo("arch-core", "core")

    packages_by_repo: dict = {}
    by_path: dict = {}
    last_revision: dict = {}

    refresh_package_indexes([repo], store, packages_by_repo, by_path, last_revision)

    assert packages_by_repo["arch-core"] == {"linux-1": "linux-1-x86_64.pkg.tar.zst"}
    assert by_path["arch-core"] == {"/arch/core/os/x86_64/linux-1-x86_64.pkg.tar.zst": ["linux-1"]}
    assert "arch-core" in last_revision


def test_refresh_package_indexes_skips_unchanged_repo_on_second_call(tmp_path):
    store = ServiceState(tmp_path / "state.sqlite3")
    store.repositories.record_snapshot(
        RepoSnapshot(repo_id="arch-core", packages={"linux-1": "linux-1-x86_64.pkg.tar.zst"})
    )
    repo = _pacman_repo("arch-core", "core")

    packages_by_repo: dict = {}
    by_path: dict = {}
    last_revision: dict = {}
    refresh_package_indexes([repo], store, packages_by_repo, by_path, last_revision)

    # nothing changed in the repository — the second call must not touch
    # sqlite for packages at all
    with patch.object(store.repositories, "get_packages") as mock_get_packages:
        refresh_package_indexes([repo], store, packages_by_repo, by_path, last_revision)
        mock_get_packages.assert_not_called()


def test_refresh_package_indexes_rebuilds_when_changed_at_moves(tmp_path):
    """changed_at has second-level precision (see state._utcnow) — instead
    of relying on real delay between two record_snapshot() calls (flaky if
    both land in the same second), we plant a deliberately stale value into
    last_revision directly. This tests the same decision branch ("current
    changed_at differs from last time — refresh"), without depending on
    clock precision."""
    store = ServiceState(tmp_path / "state.sqlite3")
    store.repositories.record_snapshot(
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
    by_path: dict = {}
    last_revision = {"arch-core": "2000-01-01T00:00:00+00:00"}

    refresh_package_indexes([repo], store, packages_by_repo, by_path, last_revision)

    assert packages_by_repo["arch-core"] == {
        "linux-1": "linux-1-x86_64.pkg.tar.zst",
        "vim-1": "vim-1-x86_64.pkg.tar.zst",
    }
    assert by_path["arch-core"] == {
        "/arch/core/os/x86_64/linux-1-x86_64.pkg.tar.zst": ["linux-1"],
        "/arch/core/os/x86_64/vim-1-x86_64.pkg.tar.zst": ["vim-1"],
    }
    # last_revision was refreshed to the current real value
    assert last_revision["arch-core"] == (store.repositories.get_snapshot_revisions()["arch-core"], repo.catalog_identity())


def test_refresh_package_indexes_refreshes_repo_added_after_first_call(tmp_path):
    """A repository added after the first call (e.g. through the dashboard)
    must be picked up immediately, not wait for its own "change"."""
    store = ServiceState(tmp_path / "state.sqlite3")
    store.repositories.record_snapshot(
        RepoSnapshot(repo_id="arch-core", packages={"linux-1": "linux-1-x86_64.pkg.tar.zst"})
    )
    core = _pacman_repo("arch-core", "core")
    extra = _pacman_repo("arch-extra", "extra")

    packages_by_repo: dict = {}
    by_path: dict = {}
    last_revision: dict = {}
    refresh_package_indexes([core], store, packages_by_repo, by_path, last_revision)

    store.repositories.record_snapshot(
        RepoSnapshot(repo_id="arch-extra", packages={"vim-1": "vim-1-x86_64.pkg.tar.zst"})
    )
    refresh_package_indexes([core, extra], store, packages_by_repo, by_path, last_revision)

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
    from repowatch.config.load import load_config

    config = load_config(config_path)
    store = ServiceState(config.state_db)
    store.repositories.record_snapshot(
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

    warmed = store.cache.get_warmed_packages("arch-core")
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
    from repowatch.config.load import load_config

    config = load_config(config_path)
    store = ServiceState(config.state_db)
    store.repositories.record_snapshot(
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

    assert store.cache.get_warmed_packages("arch-core") == []
    with store.database.connect() as conn:
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
    from repowatch.config.load import load_config

    config = load_config(config_path)
    store = ServiceState(config.state_db)
    for repo_id in ("ubuntu-noble-main", "ubuntu-noble-updates-main"):
        store.repositories.record_snapshot(
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
        warmed = store.cache.get_warmed_packages(repo_id)
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

    from repowatch.config.load import load_config

    config = load_config(config_path)
    store = ServiceState(config.state_db)
    store.repositories.record_snapshot(
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

    assert store.cache.get_warmed_packages("arch-core") == []


def test_full_path_matching_preserves_collisions_without_cross_repo_matches():
    core = _pacman_repo('core', 'core')
    extra = _pacman_repo('extra', 'extra')
    packages = {'a': 'one/file.pkg', 'b': 'two/file.pkg', 'alias': 'one/file.pkg'}
    indexes = {r.id: package_path_index(r, packages) for r in (core, extra)}
    assert match_all_package_keys('/arch/core/os/x86_64/one/file.pkg?download=1', indexes) == [('core', 'a'), ('core', 'alias')]
    assert match_all_package_keys('/unrelated/one/file.pkg', indexes) == []


def test_refresh_detects_same_second_snapshot_and_discards_deleted_repo(tmp_path, monkeypatch):
    monkeypatch.setattr('repowatch.storage.repositories._utcnow', lambda: '2026-09-16T00:00:00+00:00')
    store = ServiceState(tmp_path / 'state.sqlite3')
    repo = _pacman_repo('r', 'core')
    packages, paths, revisions = {}, {}, {}
    store.repositories.record_snapshot(RepoSnapshot('r', {'a': 'a.pkg'}))
    refresh_package_indexes([repo], store, packages, paths, revisions)
    store.repositories.record_snapshot(RepoSnapshot('r', {'b': 'b.pkg'}))
    refresh_package_indexes([repo], store, packages, paths, revisions)
    assert packages['r'] == {'b': 'b.pkg'}
    refresh_package_indexes([], store, packages, paths, revisions)
    assert packages == paths == revisions == {}


def test_path_index_preserves_decoding_queries_and_alias_owners(monkeypatch):
    import repowatch.runtime.syslog as listener
    repo = RepoConfig(id='r', type='apt', upstream='https://example.org/ubuntu',
                      arch='amd64', distribution='noble', component='main')
    original = listener.package_prefix
    calls = []
    def prefix(repo):
        calls.append(repo.id)
        return original(repo)
    monkeypatch.setattr(listener, 'package_prefix', prefix)
    assert listener.package_path_index(repo, {
        'a': 'pool/main/a%20b.deb?hash=one',
        'alias': 'pool/main/a%20b.deb?hash=two',
        'empty': '',
    }) == {'/ubuntu/pool/main/a b.deb': ['a', 'alias']}
    assert calls == ['r']


def test_prepared_repo_matcher_keeps_ambiguity_and_config_snapshot():
    from repowatch.runtime.syslog import RepoMatcher
    repos = [_pacman_repo('a', 'core')]
    first = RepoMatcher(repos)
    repos.append(_pacman_repo('b', 'core'))
    assert first.match('/arch/core/os/x86_64/file.pkg') == 'a'
    assert RepoMatcher(repos).match('/arch/core/os/x86_64/file.pkg') is None
    assert first.match('/arch/core/os/x86_64-other/file.pkg') is None
    assert first.match('/arch/core/os/x86_64') == 'a'


def test_path_index_shortcut_matches_url_oracle():
    from urllib.parse import unquote, urlsplit
    from repowatch.routing import package_prefix
    filenames = ['plain.pkg', 'dir/file.pkg', 'a%2Fb', 'a%252Fb', 'a%FF',
                 'a?query#fragment', 'a#fragment', 'a\tb', 'a\rb', 'a\nb',
                 'a\x00b', 'a\x1fb', 'café/包.pkg', '../a', '//host/path',
                 '\u2028path', ' space ', 'a\\b', 'a%zz']
    repos = [_pacman_repo('p', 'core'), RepoConfig(id='a', type='apt',
        upstream='https://example.org', arch='amd64', distribution='stable', component='main')]
    packages = {str(i): name for i, name in enumerate(filenames)}
    packages['alias'] = filenames[0]
    for repo in repos:
        expected = {}
        for key, filename in packages.items():
            path = unquote(urlsplit(package_prefix(repo) + '/' + filename).path)
            expected.setdefault(path, []).append(key)
        assert package_path_index(repo, packages) == expected


def test_listener_rebuilds_repo_matcher_on_config_reload(tmp_path, monkeypatch):
    from dataclasses import replace
    from repowatch.config.models import Config, StatusServerConfig
    import repowatch.runtime.syslog as listener
    store = ServiceState(tmp_path / 'state.sqlite3')
    config = Config(state_db=store.database.db_path, check_interval=300,
                    cache_base_url='http://127.0.0.1:8080', status_server=StatusServerConfig(),
                    repos=[_pacman_repo('old', 'core')])
    updated = replace(config, repos=[_pacman_repo('new', 'extra')])
    monkeypatch.setattr(listener, 'load_config', lambda _: updated)
    ticks = iter([0, 1, 61, 61, 62])
    monkeypatch.setattr(listener.time, 'monotonic', lambda: next(ticks))
    stop = threading.Event()
    paths = iter(['/arch/core/os/x86_64/a', '/arch/extra/os/x86_64/b', '/arch/core/os/x86_64/c'])

    class Packets:
        def recvfrom(self, size):
            path = next(paths)
            if path.endswith('/c'):
                stop.set()
            return f'repowatch: 192.0.2.1 GET {path} 200 HIT'.encode(), ('127.0.0.1', 1)

    run_listener('unused.yaml', config, store, sock=Packets(), stop=stop)
    with store.database.connect() as conn:
        rows = conn.execute('SELECT repo_id FROM request_events ORDER BY id').fetchall()
    assert rows == [('old',), ('new',), (None,)]


def test_path_index_retains_url_parser_errors_for_authorities():
    import pytest
    repo = RepoConfig(id='r', type='apt', upstream='https://example.org',
                      arch='amd64', distribution='stable', component='main')
    with pytest.raises(ValueError):
        package_path_index(repo, {'broken': '/[invalid-authority'})
