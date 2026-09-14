import asyncio
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from repowatch import cache_probe
from repowatch.config import Config, NginxConfig, RepoConfig, StatusServerConfig
from repowatch.parsers.base import USER_AGENT


def _config(repos, **nginx_overrides):
    return Config(
        Path('/tmp/unused.sqlite'), 300, 'http://127.0.0.1:8080', StatusServerConfig(),
        repos=repos, nginx=NginxConfig(enabled=True, **nginx_overrides),
    )


def test_leaf_dirs_has_4096_entries_matching_the_levels_1_2_scheme():
    """Must match nginx.py's hardcoded `proxy_cache_path ... levels=1:2` —
    16 possible 1-char first levels x 256 possible 2-char second levels."""
    assert len(cache_probe.LEAF_DIRS) == 4096
    assert len(set(cache_probe.LEAF_DIRS)) == 4096
    for leaf in cache_probe.LEAF_DIRS:
        first, _, second = leaf.partition('/')
        assert len(first) == 1 and len(second) == 2
        assert all(c in '0123456789abcdef' for c in first + second)


def test_probe_reports_exists_with_size_and_mtime():
    def handler(request):
        assert request.url.params['key'] == 'httpdeb.debian.org/debian/pool/main/a/a.deb'
        return httpx.Response(200, json={'exists': True, 'size': 123, 'mtime': '2026-01-01T00:00:00.000Z'})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await cache_probe.probe(client, 'http://127.0.0.1:8080', 'httpdeb.debian.org/debian/pool/main/a/a.deb')

    result = asyncio.run(run())
    assert result == cache_probe.ProbeResult(exists=True, size=123, mtime='2026-01-01T00:00:00.000Z')


def test_probe_reports_not_exists():
    def handler(request):
        return httpx.Response(200, json={'exists': False})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await cache_probe.probe(client, 'http://127.0.0.1:8080', 'some-key')

    assert asyncio.run(run()) == cache_probe.ProbeResult(exists=False)


def test_probe_sends_the_repowatch_user_agent_and_hits_cache_probe_path():
    seen = {}
    def handler(request):
        seen['path'] = request.url.path
        seen['user_agent'] = request.headers.get('user-agent')
        return httpx.Response(200, json={'exists': False})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await cache_probe.probe(client, 'http://127.0.0.1:8080', 'some-key')

    asyncio.run(run())
    assert seen['path'] == '/cache-probe'
    assert seen['user_agent'] == USER_AGENT


def test_probe_raises_on_http_error_status():
    def handler(request):
        return httpx.Response(500)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await cache_probe.probe(client, 'http://127.0.0.1:8080', 'some-key')

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())


def test_scan_leaf_parses_entries_including_the_leaf_it_was_asked_for():
    def handler(request):
        assert request.url.params['dir'] == 'c/29'
        return httpx.Response(200, json=[
            {'file': 'abc', 'key': 'httpexample.com/x', 'size': 10, 'mtime': 't1'},
            {'file': 'def', 'error': 'ENOENT'},
        ])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await cache_probe.scan_leaf(client, 'http://127.0.0.1:8080', 'c/29')

    entries = asyncio.run(run())
    assert entries == [
        cache_probe.ScanEntry(leaf='c/29', file='abc', key='httpexample.com/x', size=10, mtime='t1'),
        cache_probe.ScanEntry(leaf='c/29', file='def', key=None, error='ENOENT'),
    ]


def test_scan_leaf_empty_directory_returns_empty_list():
    def handler(request):
        return httpx.Response(200, json=[])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await cache_probe.scan_leaf(client, 'http://127.0.0.1:8080', '9/99')

    assert asyncio.run(run()) == []


def test_full_inventory_queries_every_leaf_directory_and_flattens_results():
    seen_dirs = []
    def handler(request):
        leaf = request.url.params['dir']
        seen_dirs.append(leaf)
        if leaf == 'c/29':
            return httpx.Response(200, json=[{'file': 'f1', 'key': 'k1', 'size': 1, 'mtime': 't'}])
        return httpx.Response(200, json=[])

    real_async_client = httpx.AsyncClient
    async def run():
        with patch('repowatch.cache_probe.httpx.AsyncClient',
                   lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            return await cache_probe.full_inventory('http://127.0.0.1:8080', concurrency=16)

    result = asyncio.run(run())
    assert sorted(seen_dirs) == sorted(cache_probe.LEAF_DIRS)
    assert result.entries == [cache_probe.ScanEntry(leaf='c/29', file='f1', key='k1', size=1, mtime='t')]
    assert result.failed_leaves == 0


def test_full_inventory_respects_the_concurrency_limit(monkeypatch):
    # A small fake leaf namespace — the real one (4096 entries) would make
    # this timing-based test unnecessarily slow without testing anything
    # more; full_inventory()'s own traversal of the real LEAF_DIRS is
    # already covered by test_full_inventory_queries_every_leaf_directory_....
    monkeypatch.setattr(cache_probe, 'LEAF_DIRS', [f'0/{i:02x}' for i in range(40)])
    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def async_handler(request):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        async with lock:
            in_flight -= 1
        return httpx.Response(200, json=[])

    real_async_client = httpx.AsyncClient
    async def run():
        with patch('repowatch.cache_probe.httpx.AsyncClient',
                   lambda **kw: real_async_client(transport=httpx.MockTransport(async_handler))):
            await cache_probe.full_inventory('http://127.0.0.1:8080', concurrency=4)

    asyncio.run(run())
    assert max_in_flight <= 4


def test_full_inventory_one_leaf_failure_does_not_abort_the_rest():
    def handler(request):
        if request.url.params['dir'] == '0/00':
            raise httpx.ConnectError('refused', request=request)
        return httpx.Response(200, json=[{'file': 'f', 'key': 'k'}])

    real_async_client = httpx.AsyncClient
    async def run():
        with patch('repowatch.cache_probe.httpx.AsyncClient',
                   lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            return await cache_probe.full_inventory('http://127.0.0.1:8080', concurrency=32)

    result = asyncio.run(run())  # must not raise
    assert len(result.entries) == 4095  # every leaf except the one that failed
    # Real bug found during a documentation-accuracy pass (2026-09-14): a
    # failed leaf used to just vanish with no way for a caller to know the
    # scan was incomplete — must be visible, not just silently dropped.
    assert result.failed_leaves == 1


def test_cache_dir_size_sums_bytes_and_flags_unreadable_keys(monkeypatch):
    monkeypatch.setattr(cache_probe, 'LEAF_DIRS', ['0/00', '0/01'])

    def handler(request):
        if request.url.params.get('dir') == '0/00':
            return httpx.Response(200, json=[
                {'file': 'a', 'key': 'ka', 'size': 100},
                {'file': 'b', 'key': None, 'error': 'bad header'},
            ])
        return httpx.Response(200, json=[{'file': 'c', 'key': 'kc', 'size': 50}])

    real_async_client = httpx.AsyncClient
    async def run():
        with patch('repowatch.cache_probe.httpx.AsyncClient',
                   lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            return await cache_probe.cache_dir_size('http://127.0.0.1:8080', concurrency=4)

    result = asyncio.run(run())
    # The missing stat is a byte-total gap, not only an unreadable key.
    assert result == {'size_bytes': 150, 'file_count': 3, 'unreadable_keys': 1, 'unreadable_sizes': 1}


def test_cache_dir_size_flags_incomplete_leaves_instead_of_silently_undercounting(monkeypatch):
    """Real gap found during a documentation-accuracy pass (2026-09-14): the
    docstring claimed "always the true size, no inaccessible_directories
    possible" — but a single leaf that fails mid-scan (independent of the
    initial connectivity check, which only catches TOTAL unreachability)
    used to just disappear with no trace in the result. Must be visible,
    the same principle as os.walk()'s inaccessible_directories."""
    monkeypatch.setattr(cache_probe, 'LEAF_DIRS', ['0/00', '0/01', '0/02'])

    def handler(request):
        if request.url.params.get('dir') == '0/00':
            return httpx.Response(200, json=[])  # connectivity pre-check
        if request.url.params.get('dir') == '0/01':
            raise httpx.ConnectError('refused', request=request)
        return httpx.Response(200, json=[{'file': 'a', 'key': 'ka', 'size': 100}])

    real_async_client = httpx.AsyncClient
    async def run():
        with patch('repowatch.cache_probe.httpx.AsyncClient',
                   lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            return await cache_probe.cache_dir_size('http://127.0.0.1:8080', concurrency=4)

    result = asyncio.run(run())
    assert result == {'size_bytes': 100, 'file_count': 1, 'incomplete_leaves': 1}


def test_cache_dir_size_propagates_a_connection_failure_instead_of_reporting_zero():
    """The initial connectivity check must fail loudly — otherwise a
    completely unreachable /cache-scan would make every one of the 4096
    leaf requests fail identically and get silently swallowed inside
    full_inventory(), reporting size_bytes=0 indistinguishable from a
    genuinely empty cache."""
    def handler(request):
        raise httpx.ConnectError('refused', request=request)

    real_async_client = httpx.AsyncClient
    async def run():
        with patch('repowatch.cache_probe.httpx.AsyncClient',
                   lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            return await cache_probe.cache_dir_size('http://127.0.0.1:8080')

    with pytest.raises(httpx.ConnectError):
        asyncio.run(run())


def test_purge_raw_purged():
    def handler(request):
        assert request.url.path == '/purge-raw'
        assert request.url.params['key'] == 'some-key'
        return httpx.Response(200)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await cache_probe.purge_raw(client, 'http://127.0.0.1:8080', 'some-key')

    assert asyncio.run(run()) == 'purged'


def test_purge_raw_sends_the_key_with_literal_slashes_not_percent_encoded():
    """Real bug found on production (2026-09-13): httpx's dict-based
    `params={"key": key}` percent-encodes "/" as "%2F". probe()/scan_leaf()
    are fine with that because njs's r.args DOES url-decode query
    parameters — but /purge-raw's `proxy_cache_purge repo_cache $arg_key;`
    reads nginx's native, NON-decoding $arg_key variable directly (no njs
    involved at all for that location). A %2F-encoded key hashes to a
    completely different MD5 than the real on-disk key, so every purge
    silently reported 404/not_cached without deleting anything — confirmed
    live against a real still-cached file (probe() before/after showed no
    change). Asserting on request.url.params (which auto-decodes) would
    hide this exact bug — must check the raw wire query string instead."""
    key = 'httpexample.com/pool/main/a/a.deb'
    seen_raw_query = {}
    def handler(request):
        seen_raw_query['query'] = bytes(request.url.query)
        return httpx.Response(200)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await cache_probe.purge_raw(client, 'http://127.0.0.1:8080', key)

    asyncio.run(run())
    assert seen_raw_query['query'] == f'key={key}'.encode()
    assert b'%2F' not in seen_raw_query['query']


def test_purge_raw_not_cached():
    def handler(request):
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await cache_probe.purge_raw(client, 'http://127.0.0.1:8080', 'some-key')

    assert asyncio.run(run()) == 'not_cached'


def test_purge_raw_reports_other_http_errors():
    def handler(request):
        return httpx.Response(500)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await cache_probe.purge_raw(client, 'http://127.0.0.1:8080', 'some-key')

    assert asyncio.run(run()) == 'error (HTTP 500)'


def test_purge_raw_survives_a_connection_error():
    def handler(request):
        raise httpx.ConnectError('refused', request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await cache_probe.purge_raw(client, 'http://127.0.0.1:8080', 'some-key')

    result = asyncio.run(run())  # must not raise
    assert result.startswith('error (')


# --- purge_selected_raw: dashboard "Purge selected" alternative mechanism
# when nginx.enable_cache_probe is on (2026-09-14) ---

def test_purge_selected_raw_computes_the_key_via_compute_cache_key_and_purges_each():
    repo = RepoConfig('core', 'pacman', 'https://mirror.test/core/os/x86_64', 'x86_64', repo_name='core')
    config = _config([repo], enable_purge=True, enable_cache_probe=True)
    seen_keys = []

    def handler(request):
        key = request.url.raw_path.decode().split('key=', 1)[1]
        seen_keys.append(key)
        if 'bash' in key:
            return httpx.Response(200)
        return httpx.Response(404)

    real_async_client = httpx.AsyncClient
    async def run():
        with patch('repowatch.cache_probe.httpx.AsyncClient',
                   lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            return await cache_probe.purge_selected_raw(config, repo, {
                'bash-1-1': 'bash-1-1-x86_64.pkg.tar.zst',
                'zlib-1-1': 'zlib-1-1-x86_64.pkg.tar.zst',
            })

    results = asyncio.run(run())
    assert results == {'bash-1-1': 'purged', 'zlib-1-1': 'not_cached'}
    # Both distinct keys are checked even when the current copy was purged.
    assert sorted(seen_keys) == sorted([
        'httpmirror.test/arch/core/os/x86_64/bash-1-1-x86_64.pkg.tar.zst',
        'httpmirror.test/core/os/x86_64/bash-1-1-x86_64.pkg.tar.zst',
        'httpmirror.test/arch/core/os/x86_64/zlib-1-1-x86_64.pkg.tar.zst',
        'httpmirror.test/core/os/x86_64/zlib-1-1-x86_64.pkg.tar.zst',
    ])


def test_purge_selected_raw_falls_back_to_the_alternate_dedup_basis_on_a_miss():
    """Real bug found during a documentation-accuracy pass (2026-09-14): the
    first version of this function only ever tried the CURRENT
    nginx.enable_dedup basis — functionally identical to
    prefetch.purge_selected()'s per-route purge, so it did NOT actually
    catch the real production case that motivated building it: a file
    cached before a dedup toggle sits under the OTHER basis. This is the
    regression test for the fix — the mock only accepts the pre-dedup
    (local, "/arch/core/...") key; the primary current-basis (dedup on,
    "/core/...") attempt must miss and correctly fall through to it."""
    repo = RepoConfig('core', 'pacman', 'https://mirror.test/core/os/x86_64', 'x86_64', repo_name='core')
    config = _config([repo], enable_purge=True, enable_cache_probe=True, enable_dedup=True)
    seen_keys = []

    def handler(request):
        key = request.url.raw_path.decode().split('key=', 1)[1]
        seen_keys.append(key)
        if key == 'httpmirror.test/arch/core/os/x86_64/bash-1-1-x86_64.pkg.tar.zst':
            return httpx.Response(200)
        return httpx.Response(404)

    real_async_client = httpx.AsyncClient
    async def run():
        with patch('repowatch.cache_probe.httpx.AsyncClient',
                   lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            return await cache_probe.purge_selected_raw(config, repo, {'bash-1-1': 'bash-1-1-x86_64.pkg.tar.zst'})

    results = asyncio.run(run())
    assert results == {'bash-1-1': 'purged'}
    assert seen_keys == [
        'httpmirror.test/core/os/x86_64/bash-1-1-x86_64.pkg.tar.zst',       # current (dedup-on) basis, misses
        'httpmirror.test/arch/core/os/x86_64/bash-1-1-x86_64.pkg.tar.zst',  # alternate basis, hits
    ]


def test_purge_selected_raw_does_not_retry_when_current_and_alt_keys_are_identical():
    """local == remote for this apt fixture's default upstream — toggling
    enable_dedup produces the SAME key, so a genuine miss must stay
    "not_cached" after exactly one request, not a wasted identical retry."""
    repo = RepoConfig('debian', 'apt', 'http://deb.debian.org/debian', 'amd64',
                       distribution='bookworm', component='main')
    config = _config([repo], enable_purge=True, enable_cache_probe=True)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    real_async_client = httpx.AsyncClient
    async def run():
        with patch('repowatch.cache_probe.httpx.AsyncClient',
                   lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            return await cache_probe.purge_selected_raw(config, repo, {'a-1': 'pool/main/a/a.deb'})

    results = asyncio.run(run())
    assert results == {'a-1': 'not_cached'}
    assert len(calls) == 1


def test_purge_selected_raw_empty_items_is_a_noop():
    repo = RepoConfig('core', 'pacman', 'https://mirror.test/core/os/x86_64', 'x86_64', repo_name='core')
    config = _config([repo], enable_purge=True, enable_cache_probe=True)

    def boom(request):
        raise AssertionError('must not make any HTTP call for an empty batch')

    real_async_client = httpx.AsyncClient
    async def run():
        with patch('repowatch.cache_probe.httpx.AsyncClient',
                   lambda **kw: real_async_client(transport=httpx.MockTransport(boom))):
            return await cache_probe.purge_selected_raw(config, repo, {})

    assert asyncio.run(run()) == {}


@pytest.mark.parametrize("first,second,expected", [
    (200, 200, "purged"),
    (200, 404, "purged"),
    (404, 200, "purged"),
    (404, 404, "not_cached"),
    (404, 503, "error (HTTP 503)"),
    (200, 503, "error (HTTP 503)"),
    (503, 200, "error (HTTP 503)"),
])
def test_purge_both_copies_and_preserve_any_failure(first, second, expected):
    repo = RepoConfig('core', 'pacman', 'https://mirror.test/core/os/x86_64', 'x86_64', repo_name='core')
    config = _config([repo], enable_purge=True, enable_cache_probe=True, enable_dedup=True)
    codes = iter([first, second])
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(next(codes))
    real_client = httpx.AsyncClient
    with patch('repowatch.cache_probe.httpx.AsyncClient',
               lambda **kw: real_client(transport=httpx.MockTransport(handler))):
        result = asyncio.run(cache_probe.purge_selected_raw(config, repo, {'foo': 'foo.pkg.tar.zst'}))
    assert result == {'foo': expected}
    assert len(seen) == len(set(seen)) == 2


def test_scan_reports_missing_sizes_separately_from_missing_keys(monkeypatch):
    monkeypatch.setattr(cache_probe, 'LEAF_DIRS', ['0/00'])
    def handler(request):
        return httpx.Response(200, json=[
            {'file': 'read-error', 'size': 100, 'error': 'EIO'},
            {'file': 'stat-error', 'error': 'EACCES'},
        ])
    real_client = httpx.AsyncClient
    with patch('repowatch.cache_probe.httpx.AsyncClient',
               lambda **kw: real_client(transport=httpx.MockTransport(handler))):
        result = asyncio.run(cache_probe.cache_dir_size('http://localhost:8080'))
    assert result == {'size_bytes': 100, 'file_count': 2, 'unreadable_keys': 2, 'unreadable_sizes': 1}
