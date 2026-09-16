import sqlite3
import pytest
from repowatch.runtime.context import ServiceState
from repowatch.models import RepoSnapshot


# Unicode fixtures exercise accented case folding and CJK substring matching.
@pytest.mark.parametrize('q', ['lib', 'LIB', 'a_b', 'a%b', '"ab', 'a-b', '\xe9t\xe9', '\xc9T\xc9', '\u4e2d\u6587\u5305', 'x', 'ab', 'OR', 'missing', 'a\x00b'])
def test_search_matches_scan_across_pages(tmp_path, q):
    store = ServiceState(tmp_path / 'state')
    names = ['libtest', 'a_b', 'a%b', '"abc', 'a-bc', '\xe9t\xe9', '\xc9T\xc9', '\u4e2d\u6587\u5305', 'xab', 'OR', 'a\x00b']
    for repo in ('a', 'b'):
        store.repositories.record_snapshot(RepoSnapshot(repo, {f'{n}-{i}': 'file' for n in names for i in range(3)},
                                           {f'{n}-{i}': n for n in names for i in range(3)}))
    def pages():
        result, cursor = [], None
        while True:
            page = store.queries.get_page('packages', 'a', q=q, limit=2, cursor=cursor)
            result.extend(page['items']); cursor = page['next_cursor']
            if not cursor: return result
    indexed = pages()
    store.database.search_index = False
    assert indexed == pages()


def test_search_migration_update_delete_and_reopen(tmp_path):
    path = tmp_path / 'old'
    store = ServiceState(path)
    with store.database.connect() as conn:
        for trigger in ('insert', 'update', 'delete'):
            conn.execute('DROP TRIGGER package_search_' + trigger)
        conn.execute('DROP TABLE package_search')
        conn.execute("INSERT INTO repo_packages (repo_id, package_key, package_name, filename) VALUES ('a','old-1','old','file')")
    store = ServiceState(path)
    assert store.queries.get_page('packages', 'a', q='old')['items']
    with store.database.connect() as conn:
        conn.execute("UPDATE repo_packages SET package_name='newname' WHERE repo_id='a'")
    assert store.queries.get_page('packages', 'a', q='newname')['items']
    store.repositories.record_snapshot(RepoSnapshot('a', {'other-1': 'file'}, {'other-1': 'other'}))
    assert not store.queries.get_page('packages', 'a', q='old')['items']
    assert ServiceState(path).queries.get_page('packages', 'a', q='other')['items']
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        conn.execute("INSERT INTO package_search(package_search, rank) VALUES ('integrity-check', 1)")


def test_common_term_uses_same_cursor_results(tmp_path):
    store=ServiceState(tmp_path/'common')
    store.repositories.record_snapshot(RepoSnapshot('a', {f'common-{i:04}':'file' for i in range(1100)}))
    first=store.queries.get_page('packages','a',q='common',limit=200)
    second=store.queries.get_page('packages','a',q='common',limit=200,cursor=first['next_cursor'])
    store.database.search_index=False
    assert second==store.queries.get_page('packages','a',q='common',limit=200,cursor=first['next_cursor'])


def test_no_fts_build_keeps_scan_available():
    from repowatch.storage.schema import _search_index
    class WithoutFts:
        def execute(self, sql):
            if sql.startswith('CREATE VIRTUAL'):
                raise sqlite3.OperationalError('no such module: fts5')
            return self
        def fetchone(self): return None
    assert _search_index(WithoutFts()) is False
