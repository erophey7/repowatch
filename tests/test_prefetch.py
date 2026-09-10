import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx

from repowatch.config import Config, RepoConfig, StatusServerConfig
from repowatch.prefetch import BandwidthLimiter, _repo_url_prefix, warm_cache
from repowatch.state import StateStore


def _config(prefetch_concurrency: int = 8, prefetch_bandwidth_limit: float | None = None) -> Config:
    return Config(
        state_db="/tmp/unused.sqlite3",
        check_interval=300,
        cache_base_url="http://127.0.0.1:8080",
        status_server=StatusServerConfig(),
        prefetch_concurrency=prefetch_concurrency,
        prefetch_bandwidth_limit=prefetch_bandwidth_limit,
    )


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.sqlite3")


def test_pacman_prefix_matches_upstream_layout():
    repo = RepoConfig(
        id="arch-core",
        type="pacman",
        upstream="https://geo.mirror.pkgbuild.com/core/os/x86_64",
        arch="x86_64",
        repo_name="core",
    )
    assert _repo_url_prefix(repo) == "/arch/core/os/x86_64"


def test_apk_prefix_includes_version_and_component_from_upstream():
    """Regression: this used to hardcode /alpine/{arch}, dropping v3.20/main —
    apk warming always 404'd (see the session history/spawn_task about this bug)."""
    repo = RepoConfig(
        id="alpine-main",
        type="apk",
        upstream="https://dl-cdn.alpinelinux.org/alpine/v3.20/main",
        arch="x86_64",
    )
    assert _repo_url_prefix(repo) == "/alpine/v3.20/main/x86_64"


def test_apk_prefix_strips_trailing_slash_from_upstream():
    repo = RepoConfig(
        id="alpine-community",
        type="apk",
        upstream="https://dl-cdn.alpinelinux.org/alpine/v3.20/community/",
        arch="aarch64",
    )
    assert _repo_url_prefix(repo) == "/alpine/v3.20/community/aarch64"


def test_apt_prefix_derives_top_segment_from_upstream_not_hardcoded_debian():
    """Regression: this used to hardcode "/debian" for ANY apt repository
    (including, e.g., Ubuntu) — warming/browse_url for a non-Debian apt repo
    would point at a nonexistent /debian/... path in nginx."""
    debian_repo = RepoConfig(
        id="debian-bookworm", type="apt", upstream="http://deb.debian.org/debian",
        distribution="bookworm", component="main", arch="amd64",
    )
    ubuntu_repo = RepoConfig(
        id="ubuntu-noble", type="apt", upstream="http://archive.ubuntu.com/ubuntu",
        distribution="noble", component="main", arch="amd64",
    )
    assert _repo_url_prefix(debian_repo) == "/debian/pool/main"
    assert _repo_url_prefix(ubuntu_repo) == "/ubuntu/pool/main"


def test_apt_prefix_includes_component_to_distinguish_sibling_repos():
    """main/restricted/universe/multiverse — different components of the same
    distribution (same upstream) physically live under different
    pool/<component>/, so they must not collapse into one prefix (see
    syslog_listener.match_repo_id)."""
    main_repo = RepoConfig(
        id="ubuntu-noble-main", type="apt", upstream="http://archive.ubuntu.com/ubuntu",
        distribution="noble", component="main", arch="amd64",
    )
    universe_repo = RepoConfig(
        id="ubuntu-noble-universe", type="apt", upstream="http://archive.ubuntu.com/ubuntu",
        distribution="noble", component="universe", arch="amd64",
    )
    assert _repo_url_prefix(main_repo) != _repo_url_prefix(universe_repo)


def test_warm_cache_skips_when_prefetch_disabled(tmp_path):
    repo = RepoConfig(
        id="debian-test", type="apt", upstream="https://example.org", arch="amd64",
        distribution="bookworm", component="main", prefetch=False,
    )
    with patch("repowatch.prefetch._warm_one") as mock_warm:
        asyncio.run(warm_cache(
            _config(), repo, _store(tmp_path), {"bash-1": "pool/main/b/bash/bash_1_amd64.deb"}
        ))
    mock_warm.assert_not_called()


def test_warm_cache_force_bypasses_prefetch_flag(tmp_path):
    """Manual warming through the dashboard (force=True) must work even for
    repositories with prefetch: false (e.g. apt — off by default there due
    to volume, but a targeted manual warm is still needed)."""
    repo = RepoConfig(
        id="debian-test", type="apt", upstream="https://example.org", arch="amd64",
        distribution="bookworm", component="main", prefetch=False,
    )
    with patch("repowatch.prefetch._warm_one", return_value=(True, 200)) as mock_warm:
        asyncio.run(warm_cache(
            _config(), repo, _store(tmp_path),
            {"bash-1": "pool/main/b/bash/bash_1_amd64.deb"}, force=True,
        ))
    mock_warm.assert_called_once()


def test_warm_cache_builds_correct_urls_for_apk(tmp_path):
    repo = RepoConfig(
        id="alpine-main", type="apk",
        upstream="https://dl-cdn.alpinelinux.org/alpine/v3.20/main", arch="x86_64",
    )
    with patch("repowatch.prefetch._warm_one", return_value=(True, 200)) as mock_warm:
        asyncio.run(warm_cache(
            _config(), repo, _store(tmp_path),
            {"musl-1.2.5-r0": "musl-1.2.5-r0.apk", "zlib-1.3.1-r0": "zlib-1.3.1-r0.apk"},
        ))

    # _warm_one(client, url, limiter) — url is now the second positional argument
    called_urls = {c.args[1] for c in mock_warm.call_args_list}
    assert called_urls == {
        "http://127.0.0.1:8080/alpine/v3.20/main/x86_64/musl-1.2.5-r0.apk",
        "http://127.0.0.1:8080/alpine/v3.20/main/x86_64/zlib-1.3.1-r0.apk",
    }


def test_warm_cache_builds_correct_urls_for_apt_non_debian_upstream(tmp_path):
    """Regression for _apt_top_segment: the warm URL used to hardcode
    "/debian/" for ANY apt repo, even Ubuntu."""
    repo = RepoConfig(
        id="ubuntu-noble-main", type="apt",
        upstream="http://archive.ubuntu.com/ubuntu",
        distribution="noble", component="main", arch="amd64",
    )
    with patch("repowatch.prefetch._warm_one", return_value=(True, 200)) as mock_warm:
        asyncio.run(warm_cache(
            _config(), repo, _store(tmp_path),
            {"bash-1": "pool/main/b/bash/bash_1_amd64.deb"},
        ))

    called_urls = {c.args[1] for c in mock_warm.call_args_list}
    assert called_urls == {"http://127.0.0.1:8080/ubuntu/pool/main/b/bash/bash_1_amd64.deb"}


def test_warm_cache_records_results_in_store(tmp_path):
    repo = RepoConfig(
        id="alpine-main", type="apk",
        upstream="https://dl-cdn.alpinelinux.org/alpine/v3.20/main", arch="x86_64",
    )
    store = _store(tmp_path)

    with patch(
        "repowatch.prefetch._warm_one",
        side_effect=[(True, 200), (False, 404)],
    ):
        asyncio.run(warm_cache(
            _config(), repo, store,
            {"musl-1.2.5-r0": "musl-1.2.5-r0.apk", "broken-1.0-r0": "broken-1.0-r0.apk"},
        ))

    warmed = {row["package_key"]: row for row in store.get_warmed_packages("alpine-main")}
    assert set(warmed) == {"musl-1.2.5-r0", "broken-1.0-r0"}
    statuses = {row["status"] for row in warmed.values()}
    assert statuses == {"ok", "failed"}


def test_warm_cache_bumps_prefetch_failure_series_when_any_package_fails(tmp_path):
    """See notifications.py — "repeated warm failures" is counted per
    warm_cache run, not per package: at least one failure in a run bumps
    the streak."""
    repo = RepoConfig(
        id="alpine-main", type="apk",
        upstream="https://dl-cdn.alpinelinux.org/alpine/v3.20/main", arch="x86_64",
    )
    store = _store(tmp_path)

    with patch("repowatch.prefetch._warm_one", side_effect=[(True, 200), (False, 404)]):
        asyncio.run(warm_cache(
            _config(), repo, store,
            {"ok-1.0-r0": "ok-1.0-r0.apk", "broken-1.0-r0": "broken-1.0-r0.apk"},
        ))

    # bump_failure on an already-existing streak returns count 2 — meaning
    # the run above already recorded this streak's first failure
    assert store.bump_failure("alpine-main", "prefetch", "peek") == (2, False)


def test_warm_cache_resets_prefetch_failure_series_on_fully_successful_run(tmp_path):
    repo = RepoConfig(
        id="alpine-main", type="apk",
        upstream="https://dl-cdn.alpinelinux.org/alpine/v3.20/main", arch="x86_64",
    )
    store = _store(tmp_path)
    store.bump_failure("alpine-main", "prefetch", "previous failed run")

    with patch("repowatch.prefetch._warm_one", return_value=(True, 200)):
        asyncio.run(warm_cache(_config(), repo, store, {"ok-1.0-r0": "ok-1.0-r0.apk"}))

    # the streak is fully reset — the next failure starts the count at 1 again
    assert store.bump_failure("alpine-main", "prefetch", "peek") == (1, False)


def test_warm_cache_runs_concurrently_not_sequentially(tmp_path):
    """Checks that warm_cache actually overlaps task execution
    (asyncio.gather + Semaphore), rather than waiting for each in turn."""
    active = 0
    max_active = 0

    async def fake_warm_one(client, url, limiter):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)  # keep the task "busy" so others get a chance to start
        active -= 1
        return True, 200

    repo = RepoConfig(
        id="alpine-main", type="apk",
        upstream="https://dl-cdn.alpinelinux.org/alpine/v3.20/main", arch="x86_64",
    )
    packages = {f"pkg{i}-1.0-r0": f"pkg{i}-1.0-r0.apk" for i in range(10)}

    with patch("repowatch.prefetch._warm_one", side_effect=fake_warm_one):
        asyncio.run(warm_cache(_config(prefetch_concurrency=4), repo, _store(tmp_path), packages))

    assert max_active > 1


def test_warm_cache_empty_packages_is_noop(tmp_path):
    repo = RepoConfig(
        id="alpine-main", type="apk",
        upstream="https://dl-cdn.alpinelinux.org/alpine/v3.20/main", arch="x86_64",
    )
    with patch("repowatch.prefetch._warm_one") as mock_warm:
        asyncio.run(warm_cache(_config(), repo, _store(tmp_path), {}))
    mock_warm.assert_not_called()


def test_warm_cache_skips_banned_packages_by_name(tmp_path):
    from repowatch.state import RepoSnapshot

    repo = RepoConfig(
        id="alpine-main", type="apk",
        upstream="https://dl-cdn.alpinelinux.org/alpine/v3.20/main", arch="x86_64",
    )
    store = _store(tmp_path)
    store.record_snapshot(
        RepoSnapshot(
            repo_id="alpine-main",
            packages={
                "musl-1.2.5-r0": "musl-1.2.5-r0.apk",
                "linux-headers-6.11-r0": "linux-headers-6.11-r0.apk",
            },
            names={"musl-1.2.5-r0": "musl", "linux-headers-6.11-r0": "linux-headers"},
        )
    )
    store.ban_package("alpine-main", "linux-headers")

    with patch("repowatch.prefetch._warm_one", return_value=(True, 200)) as mock_warm:
        asyncio.run(warm_cache(
            _config(), repo, store,
            {"musl-1.2.5-r0": "musl-1.2.5-r0.apk", "linux-headers-6.11-r0": "linux-headers-6.11-r0.apk"},
        ))

    called_urls = {c.args[1] for c in mock_warm.call_args_list}
    assert called_urls == {"http://127.0.0.1:8080/alpine/v3.20/main/x86_64/musl-1.2.5-r0.apk"}


def test_warm_cache_ban_applies_even_with_force(tmp_path):
    """Manual warming (force=True) must not bypass a ban — to warm a banned
    package on purpose, unban it explicitly first."""
    from repowatch.state import RepoSnapshot

    repo = RepoConfig(
        id="debian-test", type="apt", upstream="https://example.org", arch="amd64",
        distribution="bookworm", component="main", prefetch=False,
    )
    store = _store(tmp_path)
    store.record_snapshot(
        RepoSnapshot(
            repo_id="debian-test",
            packages={"linux-1-2": "pool/main/l/linux/linux_1-2_amd64.deb"},
            names={"linux-1-2": "linux"},
        )
    )
    store.ban_package("debian-test", "linux")

    with patch("repowatch.prefetch._warm_one") as mock_warm:
        asyncio.run(warm_cache(
            _config(), repo, store,
            {"linux-1-2": "pool/main/l/linux/linux_1-2_amd64.deb"}, force=True,
        ))
    mock_warm.assert_not_called()


def test_bandwidth_limiter_unlimited_does_not_wait():
    async def _run():
        limiter = BandwidthLimiter(None)
        started = time.monotonic()
        for _ in range(20):
            await limiter.consume(1_000_000)
        assert time.monotonic() - started < 0.1

    asyncio.run(_run())


def test_bandwidth_limiter_paces_calls_to_configured_rate():
    async def _run():
        limiter = BandwidthLimiter(bytes_per_sec=100.0)  # 5 bytes -> 50ms "cost"
        started = time.monotonic()
        for _ in range(5):
            await limiter.consume(5)
        elapsed = time.monotonic() - started
        # 5 calls of 5 bytes at 100 bytes/sec -> at least ~4 * 0.05s = 0.2s
        # (the first one is free)
        assert elapsed >= 0.18

    asyncio.run(_run())


def test_bandwidth_limiter_shared_across_concurrent_tasks():
    """consume() from different concurrent tasks must be paced TOGETHER, not
    independently per task — otherwise the limit could be bypassed just by
    raising prefetch_concurrency."""

    async def _run():
        limiter = BandwidthLimiter(bytes_per_sec=100.0)
        started = time.monotonic()
        await asyncio.gather(*(limiter.consume(5) for _ in range(5)))
        elapsed = time.monotonic() - started
        assert elapsed >= 0.18

    asyncio.run(_run())


def test_bandwidth_limiter_zero_byte_chunk_is_noop():
    async def _run():
        limiter = BandwidthLimiter(bytes_per_sec=100.0)
        started = time.monotonic()
        await limiter.consume(0)
        assert time.monotonic() - started < 0.05

    asyncio.run(_run())


def test_warm_cache_respects_prefetch_bandwidth_limit(tmp_path):
    """_warm_one really streams and calls limiter.consume() for each chunk
    (see test_warm_one_streams_body_in_chunks) — here we mock _warm_one
    entirely and simulate byte consumption by hand, to check specifically
    that warm_cache builds and passes a SINGLE SHARED limiter for the whole
    pool (see test_bandwidth_limiter_shared_across_concurrent_tasks above —
    same principle)."""
    repo = RepoConfig(
        id="alpine-main", type="apk",
        upstream="https://dl-cdn.alpinelinux.org/alpine/v3.20/main", arch="x86_64",
    )
    packages = {f"pkg{i}-1.0-r0": f"pkg{i}-1.0-r0.apk" for i in range(4)}

    async def fake_warm_one(client, url, limiter):
        await limiter.consume(5)
        return True, 200

    started = time.monotonic()
    with patch("repowatch.prefetch._warm_one", side_effect=fake_warm_one):
        asyncio.run(warm_cache(
            _config(prefetch_concurrency=4, prefetch_bandwidth_limit=20.0),
            repo, _store(tmp_path), packages,
        ))
    elapsed = time.monotonic() - started
    # 4 packages of 5 bytes at 20 bytes/sec, even with concurrency=4 (it
    # would finish in a fraction of a second unlimited) — should take at
    # least ~3 * 0.25s in total
    assert elapsed >= 0.6


class _FakeStreamResponse:
    """Mocks just as much of httpx.Response as _warm_one needs:
    .status_code, raise_for_status(), aiter_bytes(chunk_size) — an async
    generator yielding chunks from a pre-set buffer (like a real stream —
    up to chunk_size bytes at a time, less at the end)."""

    def __init__(self, body: bytes, status_code: int = 200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://example.org")
            raise httpx.HTTPStatusError("error", request=request, response=self)

    async def aiter_bytes(self, chunk_size: int):
        pos = 0
        while pos < len(self._body):
            yield self._body[pos : pos + chunk_size]
            pos += chunk_size


class _FakeStreamCM:
    def __init__(self, response: _FakeStreamResponse):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeStreamClient:
    """Mocks just as much of httpx.AsyncClient as _warm_one needs:
    only client.stream(...)."""

    def __init__(self, response: _FakeStreamResponse | None = None, raises: Exception | None = None):
        self._response = response
        self._raises = raises

    def stream(self, method, url, headers=None, timeout=None):
        if self._raises is not None:
            raise self._raises
        return _FakeStreamCM(self._response)


def _fake_limiter() -> MagicMock:
    limiter = MagicMock()
    limiter.consume = AsyncMock()
    return limiter


def test_warm_one_streams_body_in_chunks():
    from repowatch.prefetch import _CHUNK_SIZE, _warm_one

    body = b"x" * (_CHUNK_SIZE + 37)  # one full chunk + a remainder
    fake_limiter = _fake_limiter()
    client = _FakeStreamClient(_FakeStreamResponse(body))

    ok, status = asyncio.run(_warm_one(client, "http://example.org/pkg.apk", fake_limiter))

    assert ok is True
    assert status == 200
    assert fake_limiter.consume.call_args_list == [call(_CHUNK_SIZE), call(37)]


def test_warm_one_reports_http_status_error():
    """httpx doesn't raise on a non-2xx status by itself — raise_for_status()
    inside _warm_one does it explicitly, BEFORE streaming the body (so we
    don't spend the bandwidth budget on an error page's body)."""
    from repowatch.prefetch import _warm_one

    fake_limiter = _fake_limiter()
    client = _FakeStreamClient(_FakeStreamResponse(b"not found", status_code=404))

    ok, status = asyncio.run(_warm_one(client, "http://example.org/pkg.apk", fake_limiter))

    assert ok is False
    assert status == 404
    fake_limiter.consume.assert_not_called()


def test_warm_one_reports_request_error():
    """A connection-level error (not an HTTP status) — httpx.RequestError,
    the counterpart of the old urllib.error.URLError."""
    from repowatch.prefetch import _warm_one

    fake_limiter = _fake_limiter()
    client = _FakeStreamClient(raises=httpx.ConnectError("connection refused"))

    ok, status = asyncio.run(_warm_one(client, "http://example.org/pkg.apk", fake_limiter))

    assert ok is False
    assert status is None
    fake_limiter.consume.assert_not_called()


def test_security_and_ppa_keep_distinct_cache_paths_and_matching():
    from repowatch.prefetch import _build_warm_url
    from repowatch.syslog_listener import match_repo_id

    repos = [
        RepoConfig(id=name, type="apt", upstream=upstream, distribution="noble",
                   component="main", arch="amd64")
        for name, upstream in (
            ("ubuntu", "http://archive.ubuntu.com/ubuntu/"),
            ("security", "https://security.ubuntu.com/ubuntu/"),
            ("ppa", "https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu/"),
        )
    ]
    filename = "pool/main/p/python3.13/python3.13_1_amd64.deb"
    for repo, prefix in zip(repos, ("/ubuntu", "/ubuntu-security", "/ppa-deadsnakes-ppa")):
        assert _repo_url_prefix(repo) == prefix + "/pool/main"
        assert _build_warm_url(_config(), repo, filename) == "http://127.0.0.1:8080" + prefix + "/" + filename
        assert match_repo_id(prefix + "/" + filename, repos) == repo.id


def test_dnf_prefix_uses_repo_id_and_preserves_relative_package_path():
    from repowatch.prefetch import _build_warm_url
    from repowatch.syslog_listener import match_repo_id
    repo = RepoConfig(id='rocky-9-baseos-x86_64', type='dnf',
                      upstream='https://dl.rockylinux.org/pub/rocky/9/BaseOS/x86_64/os', arch='x86_64')
    path = '/rpm/rocky-9-baseos-x86_64/Packages/b/bash-1.x86_64.rpm'
    assert _repo_url_prefix(repo) == '/rpm/rocky-9-baseos-x86_64'
    assert _build_warm_url(_config(), repo, 'Packages/b/bash-1.x86_64.rpm') == _config().cache_base_url + path
    assert match_repo_id(path, [repo]) == repo.id


# --- active cache purge on package removal (docs_dev/ROADMAP.md item 24) ---

def _purge_config(**overrides):
    from repowatch.config import NginxConfig
    return Config(
        state_db="/tmp/unused.sqlite3", check_interval=300,
        cache_base_url="http://127.0.0.1:8080", status_server=StatusServerConfig(),
        nginx=NginxConfig(enable_purge=True),
        **overrides,
    )


def test_purge_url_mirrors_build_warm_url_under_a_purge_prefix():
    from repowatch.prefetch import _build_warm_url, _purge_url
    repo = RepoConfig(id='r', type='pacman', upstream='https://mirror.test/core/os/x86_64',
                       arch='x86_64', repo_name='core')
    config = _purge_config()
    warm = _build_warm_url(config, repo, 'acl-1-1-x86_64.pkg.tar.zst')
    purge = _purge_url(config, repo, 'acl-1-1-x86_64.pkg.tar.zst')
    assert purge == warm.replace(config.cache_base_url, config.cache_base_url + '/purge', 1)
    assert purge == 'http://127.0.0.1:8080/purge/arch/core/os/x86_64/acl-1-1-x86_64.pkg.tar.zst'


def test_purge_removed_is_a_noop_when_disabled():
    from repowatch.prefetch import purge_removed
    repo = RepoConfig(id='r', type='pacman', upstream='https://mirror.test/core/os/x86_64',
                       arch='x86_64', repo_name='core')
    config = Config(state_db="/tmp/unused.sqlite3", check_interval=300,
                    cache_base_url="http://127.0.0.1:8080", status_server=StatusServerConfig())
    assert config.nginx.enable_purge is False

    def boom(**kwargs):
        raise AssertionError("must not even construct an httpx.AsyncClient when disabled")

    with patch("repowatch.prefetch.httpx.AsyncClient", boom):
        asyncio.run(purge_removed(config, repo, {"acl-1-1": "acl-1-1-x86_64.pkg.tar.zst"}))


def test_purge_removed_is_a_noop_for_an_empty_batch():
    from repowatch.prefetch import purge_removed
    repo = RepoConfig(id='r', type='pacman', upstream='https://mirror.test/core/os/x86_64',
                       arch='x86_64', repo_name='core')
    # enable_purge=True but nothing removed — must not even try to build a client/URL.
    asyncio.run(purge_removed(_purge_config(), repo, {}))


def test_purge_removed_requests_the_purge_url_for_each_removed_file():
    from repowatch.prefetch import purge_removed
    repo = RepoConfig(id='r', type='pacman', upstream='https://mirror.test/core/os/x86_64',
                       arch='x86_64', repo_name='core')
    requested = []
    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200)

    real_async_client = httpx.AsyncClient
    async def run():
        with patch("repowatch.prefetch.httpx.AsyncClient",
                    lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            await purge_removed(_purge_config(), repo, {
                    "acl-1-1": "acl-1-1-x86_64.pkg.tar.zst",
                    "zlib-1-1": "zlib-1-1-x86_64.pkg.tar.zst",
                })
    asyncio.run(run())
    assert sorted(requested) == sorted([
        "http://127.0.0.1:8080/purge/arch/core/os/x86_64/acl-1-1-x86_64.pkg.tar.zst",
        "http://127.0.0.1:8080/purge/arch/core/os/x86_64/zlib-1-1-x86_64.pkg.tar.zst",
    ])


def test_purge_removed_sends_the_repowatch_user_agent():
    """Real production bug (2026-09-10): purge_removed's own GET requests
    didn't set User-Agent at all, so nginx's $repowatch_is_prefetch map
    (keyed on this exact header, see nginx.py) classified them as real
    client traffic — every automatic purge polluted "Recent client
    requests" with its own /purge/... URL. _warm_one already set this
    correctly; purge_removed/purge_selected did not."""
    from repowatch.parsers.base import USER_AGENT
    from repowatch.prefetch import purge_removed
    repo = RepoConfig(id='r', type='pacman', upstream='https://mirror.test/core/os/x86_64',
                       arch='x86_64', repo_name='core')
    seen_user_agents = []
    def handler(request):
        seen_user_agents.append(request.headers.get("user-agent"))
        return httpx.Response(200)

    real_async_client = httpx.AsyncClient
    async def run():
        with patch("repowatch.prefetch.httpx.AsyncClient",
                    lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            await purge_removed(_purge_config(), repo, {"acl-1-1": "acl-1-1-x86_64.pkg.tar.zst"})
    asyncio.run(run())
    assert seen_user_agents == [USER_AGENT]


def test_purge_removed_one_failure_does_not_abort_the_rest():
    from repowatch.prefetch import purge_removed
    repo = RepoConfig(id='r', type='pacman', upstream='https://mirror.test/core/os/x86_64',
                       arch='x86_64', repo_name='core')
    requested = []
    def handler(request):
        requested.append(str(request.url))
        if "acl" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200)

    real_async_client = httpx.AsyncClient
    async def run():
        with patch("repowatch.prefetch.httpx.AsyncClient",
                    lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            await purge_removed(_purge_config(), repo, {
                    "acl-1-1": "acl-1-1-x86_64.pkg.tar.zst",
                    "zlib-1-1": "zlib-1-1-x86_64.pkg.tar.zst",
                })
    asyncio.run(run())
    assert len(requested) == 2  # both attempted despite the first failing


def test_purge_removed_survives_a_connection_error():
    from repowatch.prefetch import purge_removed
    repo = RepoConfig(id='r', type='pacman', upstream='https://mirror.test/core/os/x86_64',
                       arch='x86_64', repo_name='core')
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    real_async_client = httpx.AsyncClient
    async def run():
        with patch("repowatch.prefetch.httpx.AsyncClient",
                    lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            await purge_removed(_purge_config(), repo, {"acl-1-1": "acl-1-1-x86_64.pkg.tar.zst"})
    asyncio.run(run())  # must not raise


def test_purge_selected_is_a_noop_for_an_empty_batch():
    from repowatch.prefetch import purge_selected
    repo = RepoConfig(id='r', type='pacman', upstream='https://mirror.test/core/os/x86_64',
                       arch='x86_64', repo_name='core')

    def boom(**kwargs):
        raise AssertionError("must not even construct an httpx.AsyncClient for an empty batch")

    with patch("repowatch.prefetch.httpx.AsyncClient", boom):
        result = asyncio.run(purge_selected(_purge_config(), repo, {}))
    assert result == {}


def test_purge_selected_does_not_check_enable_purge_itself():
    """Unlike purge_removed, purge_selected trusts the caller (the API
    payload function) to have already refused the request when
    enable_purge is off — it always attempts the HTTP call it's given."""
    from repowatch.prefetch import purge_selected
    repo = RepoConfig(id='r', type='pacman', upstream='https://mirror.test/core/os/x86_64',
                       arch='x86_64', repo_name='core')
    config = Config(state_db="/tmp/unused.sqlite3", check_interval=300,
                    cache_base_url="http://127.0.0.1:8080", status_server=StatusServerConfig())
    assert config.nginx.enable_purge is False

    real_async_client = httpx.AsyncClient
    def handler(request):
        return httpx.Response(200)
    async def run():
        with patch("repowatch.prefetch.httpx.AsyncClient",
                    lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            return await purge_selected(config, repo, {"acl-1-1": "acl-1-1-x86_64.pkg.tar.zst"})
    assert asyncio.run(run()) == {"acl-1-1": "purged"}


def test_purge_selected_reports_purged_not_cached_and_error_per_item():
    from repowatch.prefetch import purge_selected
    repo = RepoConfig(id='r', type='pacman', upstream='https://mirror.test/core/os/x86_64',
                       arch='x86_64', repo_name='core')

    def handler(request):
        if "purged-me" in str(request.url):
            return httpx.Response(200)
        if "already-gone" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(500)

    real_async_client = httpx.AsyncClient
    async def run():
        with patch("repowatch.prefetch.httpx.AsyncClient",
                    lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            return await purge_selected(_purge_config(), repo, {
                "a": "purged-me-1-x86_64.pkg.tar.zst",
                "b": "already-gone-1-x86_64.pkg.tar.zst",
                "c": "server-error-1-x86_64.pkg.tar.zst",
            })
    results = asyncio.run(run())
    assert results["a"] == "purged"
    assert results["b"] == "not_cached"
    assert results["c"] == "error (HTTP 500)"


def test_purge_selected_sends_the_repowatch_user_agent():
    """Same real bug as purge_removed (see
    test_purge_removed_sends_the_repowatch_user_agent) — the manual
    "Purge selected" button's own requests must also be marked, or every
    click pollutes "Recent client requests" with /purge/... calls."""
    from repowatch.parsers.base import USER_AGENT
    from repowatch.prefetch import purge_selected
    repo = RepoConfig(id='r', type='pacman', upstream='https://mirror.test/core/os/x86_64',
                       arch='x86_64', repo_name='core')
    seen_user_agents = []
    def handler(request):
        seen_user_agents.append(request.headers.get("user-agent"))
        return httpx.Response(200)

    real_async_client = httpx.AsyncClient
    async def run():
        with patch("repowatch.prefetch.httpx.AsyncClient",
                    lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            return await purge_selected(_purge_config(), repo, {"acl-1-1": "acl-1-1-x86_64.pkg.tar.zst"})
    asyncio.run(run())
    assert seen_user_agents == [USER_AGENT]


def test_purge_selected_reports_error_for_a_connection_failure():
    from repowatch.prefetch import purge_selected
    repo = RepoConfig(id='r', type='pacman', upstream='https://mirror.test/core/os/x86_64',
                       arch='x86_64', repo_name='core')
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    real_async_client = httpx.AsyncClient
    async def run():
        with patch("repowatch.prefetch.httpx.AsyncClient",
                    lambda **kw: real_async_client(transport=httpx.MockTransport(handler))):
            return await purge_selected(_purge_config(), repo, {"acl-1-1": "acl-1-1-x86_64.pkg.tar.zst"})
    results = asyncio.run(run())
    assert results["acl-1-1"].startswith("error (")
