import base64
import json
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from urllib.request import build_opener, ProxyHandler
from urllib.error import HTTPError

import pytest

from repowatch.reporting.statistics import paged_payload
from repowatch.web.handler import make_handler
from repowatch.config.models import Config
from repowatch.config.models import StatusServerConfig
from repowatch.models import RepoSnapshot
from repowatch.runtime.context import ServiceState


def test_packages_search_and_cursor_cover_large_snapshot(tmp_path):
    store = ServiceState(tmp_path / "state")
    packages = {f"pkg-{i:06}": f"{i}.deb" for i in range(63000)}
    store.repositories.record_snapshot(RepoSnapshot("r", packages))
    seen, cursor = [], None
    while True:
        page = store.queries.get_page("packages", "r", q="pkg-062", limit=137, cursor=cursor)
        seen.extend(p["package_key"] for p in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == sorted(k for k in packages if "pkg-062" in k)
    assert len(seen) == len(set(seen)) == 1000
    assert store.repositories.get_packages_by_keys("r", ["absent"]) == {}
    assert not store.queries.get_page("packages", "r", q="%")["items"]


def test_warmed_search_matches_package_key_or_filename(tmp_path):
    store = ServiceState(tmp_path / "state")
    store.cache.record_warmed_package("r", "linux-6.11.2-1", "linux-6.11.2-1-x86_64.pkg.tar.zst", True, 200)
    store.cache.record_warmed_package("r", "bash-5.2-1", "bash-5.2-1-x86_64.pkg.tar.zst", True, 200)
    store.cache.record_warmed_package("r", "renamed-1", "totally-different-name.pkg.tar.zst", True, 200)

    by_key = store.queries.get_page("warmed", "r", q="linux")["items"]
    assert [p["package_key"] for p in by_key] == ["linux-6.11.2-1"]

    by_filename = store.queries.get_page("warmed", "r", q="totally-different")["items"]
    assert [p["package_key"] for p in by_filename] == ["renamed-1"]

    assert not store.queries.get_page("warmed", "r", q="nothing-matches-this")["items"]
    assert len(store.queries.get_page("warmed", "r", q="")["items"]) == 3


@pytest.mark.parametrize("kind", ["requests", "warmed"])
def test_tied_timestamps_and_insert_between_pages(tmp_path, kind):
    store = ServiceState(tmp_path / "state")
    for i in range(7):
        store.requests.record_request("r", "ip", "GET", f"/{i}", "200", "HIT")
        store.cache.record_warmed_package("r", str(i), str(i), True, 200)
    with store.database.connect() as conn:
        conn.execute("UPDATE warmed_packages SET warmed_at = '2026-01-01'")
        conn.execute("UPDATE request_events SET ts = '2026-01-01'")
    first = store.queries.get_page(kind, "r", limit=3)
    store.requests.record_request("r", "ip", "GET", "/new", "200", "HIT")
    store.cache.record_warmed_package("r", "new", "new", True, 200)
    seen = first["items"]
    cursor = first["next_cursor"]
    while cursor:
        page = store.queries.get_page(kind, "r", limit=3, cursor=cursor)
        seen.extend(page["items"])
        cursor = page["next_cursor"]
    key = "id" if kind == "requests" else "package_key"
    assert len(seen) == len({p[key] for p in seen}) == 7


@pytest.mark.parametrize("query", ["limit=0", "limit=-1", "limit=201", "limit=no",
                                 "cursor=garbage", "q=" + "a" * 201])
def test_bad_page_arguments_are_400(tmp_path, query):
    store = ServiceState(tmp_path / "state")
    store.repositories.record_snapshot(RepoSnapshot("r", {"a": "a"}))
    assert paged_payload(store, "packages", "r", query)[0] == 400


def test_cursor_is_bound_to_filter_and_resource(tmp_path):
    store = ServiceState(tmp_path / "state")
    store.repositories.record_snapshot(RepoSnapshot("r", {"a": "a", "b": "b"}))
    cursor = store.queries.get_page("packages", "r", limit=1)["next_cursor"]
    for kind, repo, q in [("packages", "other", ""), ("packages", "r", "a"),
                          ("warmed", "r", "")]:
        with pytest.raises(ValueError):
            store.queries.get_page(kind, repo, q=q, cursor=cursor)
    for malformed in [[], None, {"scope": ["packages", "r", ""], "after": "x"}]:
        cursor = base64.urlsafe_b64encode(json.dumps(malformed).encode()).decode()
        assert paged_payload(store, "packages", "r", "cursor=" + cursor)[0] == 400


def legacy_db(path, broken=False):
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE repo_state (
            repo_id TEXT PRIMARY KEY, last_check TEXT, changed_at TEXT,
            packages_json TEXT, names_json TEXT, package_count INTEGER)""")
        conn.execute("INSERT INTO repo_state VALUES (?, ?, ?, ?, ?, ?)",
                     ("r", "last", "changed", '{"new": "new.deb"}', '{"new": "name"}', 1))
        conn.execute("""CREATE TABLE repo_packages (
            repo_id TEXT, package_key TEXT, package_name TEXT, filename TEXT,
            PRIMARY KEY(repo_id, package_key))""")
        conn.execute("INSERT INTO repo_packages VALUES ('r', 'stale', 'old', 'old.deb')")
        if broken:
            conn.execute("INSERT INTO repo_state VALUES ('broken', '', '', 'invalid', NULL, 0)")


def test_migration_replaces_equal_count_stale_table_and_is_idempotent(tmp_path):
    path = tmp_path / "state"
    legacy_db(path)
    store = ServiceState(path)
    assert store.repositories.get_packages("r") == {"new": "new.deb"}
    assert store.repositories.get_names("r") == {"new": "name"}
    assert store.repositories.get_status("r")["changed_at"] == "changed"
    assert store.repositories.get_history("r") == []
    with sqlite3.connect(path) as conn:
        assert not {"packages_json", "names_json"} & {
            row[1] for row in conn.execute("PRAGMA table_info(repo_state)")}
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert ServiceState(path).repositories.get_packages("r") == {"new": "new.deb"}
    diff = store.repositories.record_snapshot(RepoSnapshot("r", {}))
    assert diff.removed_packages == ["new"]
    assert ServiceState(path).repositories.get_packages("r") == {}


def test_failed_migration_rolls_back_all_data(tmp_path):
    path = tmp_path / "state"
    legacy_db(path, broken=True)
    with pytest.raises(ValueError):
        ServiceState(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT package_key FROM repo_packages").fetchall() == [("stale",)]
        assert conn.execute("SELECT packages_json FROM repo_state WHERE repo_id='r'").fetchone()


def test_http_pages_and_validation(tmp_path):
    store = ServiceState(tmp_path / "state")
    store.repositories.record_snapshot(RepoSnapshot("r", {"one": "one", "two": "two"}))
    config = Config(state_db=str(tmp_path / "state"), cache_base_url="http://localhost",
                    check_interval=300, status_server=StatusServerConfig())
    import yaml
    from repowatch.auth import hash_password
    from repowatch.storage.access import AccessStore
    from repowatch.web.access import COOKIE_NAME
    from repowatch.config.load import load_config
    (tmp_path / 'config').write_text(yaml.safe_dump({
        'state_db': str(tmp_path / 'state'), 'cache_base_url': 'http://localhost',
        'admin_password_hash': hash_password('test', iterations=1000),
        'repos': [{'id': 'r', 'type': 'apk', 'upstream': 'https://example.org', 'arch': 'x86_64'}],
    }))
    config = load_config(tmp_path / 'config')
    secret, _ = AccessStore(store.database).create_session(config.admin_password_hash, False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config, store, tmp_path / "config"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = build_opener(ProxyHandler({}))
    opener.addheaders = [("Cookie", f"{COOKIE_NAME}={secret}")]
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with opener.open(base + "/api/repos/r/packages?limit=1", timeout=5) as res:
            data = json.load(res)
            assert len(data["items"]) == 1
            assert data["next_cursor"]
        for endpoint in ["/api/requests", "/api/repos/r/warmed"]:
            with opener.open(base + endpoint, timeout=5) as res:
                assert json.load(res) == {"items": [], "next_cursor": None}
        with pytest.raises(HTTPError) as error:
            opener.open(base + "/api/requests?limit=-1", timeout=5)
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_large_dual_write_migration_preserves_history_bans_and_warmed(tmp_path):
    path = tmp_path / "state"
    store = ServiceState(path)
    packages = {f"pkg-{i:06}": f"pool/{i}.deb" for i in range(63000)}
    store.repositories.record_snapshot(RepoSnapshot("r", packages))
    store.cache.record_warmed_package("r", "pkg-000001", "pool/1.deb", True, 200)
    store.cache.ban_package("r", "pkg")
    before = store.repositories.get_status("r")
    with store.database.connect() as conn:
        conn.execute("ALTER TABLE repo_state ADD COLUMN packages_json TEXT")
        conn.execute("ALTER TABLE repo_state ADD COLUMN names_json TEXT")
        conn.execute("UPDATE repo_state SET packages_json = ?, names_json = '{}'",
                     (json.dumps(packages),))
        conn.execute("UPDATE repo_packages SET filename = 'stale' WHERE package_key = 'pkg-000001'")
    migrated = ServiceState(path)
    assert migrated.repositories.get_packages("r") == packages
    assert migrated.repositories.get_status("r") == before
    assert migrated.cache.get_banned_packages("r") == ["pkg"]
    assert len(migrated.cache.get_warmed_packages("r")) == 1
