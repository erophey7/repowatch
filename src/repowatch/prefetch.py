"""Cache warming: once the watcher finds new packages, we GET them through
the local nginx (cache_base_url) so the file lands in proxy_cache BEFORE a
client actually requests it.

Important: we hit our own nginx, not upstream directly — otherwise the
warmed file wouldn't land in the same cache real clients use.
"""

from __future__ import annotations

import asyncio
import logging
import hashlib
import urllib.parse

import httpx

from repowatch.config import Config, RepoConfig
from repowatch.bandwidth import BandwidthLimiter
from repowatch.notifications import record_failure_and_maybe_notify, record_success_and_maybe_notify
from repowatch.parsers.base import USER_AGENT
from repowatch.state import StateStore

logger = logging.getLogger(__name__)

# Chunk size for streaming the response body in _warm_one — small enough for
# accurate byte-level pacing, large enough not to flood syscalls.
_CHUNK_SIZE = 64 * 1024



def _apt_top_segment(repo: RepoConfig) -> str:
    """Local apt prefix — nginx.py's render() imports this same function to
    build the matching location block, so there's a single source of truth.

    Usually the same as the upstream path. Security uses the same /ubuntu
    but a different host: the path alone would collapse two different
    sources. PPA gets its own prefix; each PPA needs its own nginx location
    (see the deadsnakes/ppa example) — we don't open up an arbitrary
    upstream in nginx.
    """
    from repowatch.url_templates import expand
    custom = expand(repo)
    if custom is not None:
        return custom
    upstream = urllib.parse.urlparse(repo.upstream)
    path = upstream.path.rstrip("/")
    if upstream.hostname == "security.ubuntu.com" and path == "/ubuntu":
        return "/ubuntu-security"
    parts = path.strip("/").split("/")
    if upstream.hostname == "ppa.launchpadcontent.net" and len(parts) == 3 and parts[2] == "ubuntu":
        return f"/ppa-{parts[0]}-{parts[1]}"
    return path


def _apt_rpm_top_segment(repo: RepoConfig) -> str:
    from repowatch.url_templates import expand
    return expand(repo) or f"/apt-rpm/{urllib.parse.quote(repo.id, safe='')}"


def _repo_url_prefix(repo: RepoConfig) -> str:
    """Relative repository path to append to cache_base_url.

    nginx.py's render() imports this same function to build the matching
    location block — a single source of truth, not two configs to keep
    in sync by hand.
    """
    from repowatch.url_templates import expand
    custom = expand(repo)
    if custom is not None and repo.type not in ("apt", "apt-rpm"):
        return custom
    if repo.type == "pacman":
        return f"/arch/{repo.repo_name}/os/{repo.arch}"
    if repo.type == "apt-rpm":
        return f"{_apt_rpm_top_segment(repo)}/RPMS.{repo.component}"
    if repo.type == "dnf":
        # RPM-MD upstream paths have no common structure. An explicit
        # namespace by repo_id avoids mixing hosts/architectures that
        # happen to share a path.
        return f"/rpm/{urllib.parse.quote(repo.id, safe='')}"
    if repo.type in ('gentoo', 'slackware'):
        return f"/{repo.type}/{urllib.parse.quote(repo.id, safe='')}"
    if repo.type == "xbps":
        # Same reasoning as dnf above — Void mirrors/components (nonfree,
        # multilib, multilib/nonfree, debug) have no common upstream
        # structure either, and packages sit flat next to <arch>-repodata,
        # so a plain repo_id namespace is enough (no component suffix
        # needed here, unlike apt-rpm's RPMS.<component> — a Void
        # component is just a different upstream directory).
        return f"/xbps/{urllib.parse.quote(repo.id, safe='')}"
    if repo.type == "apt":
        # apt packages live under pool/<component>/..., where component is
        # the first segment after pool/ (that's upstream's actual
        # structure, not our own convention) — we include it in the prefix
        # so different components of the same distribution
        # (main/restricted/universe/multiverse) don't collapse into the
        # same prefix in match_repo_id (see syslog_listener.py). This
        # doesn't remove ambiguity entirely — several suites of the same
        # component (noble/noble-updates/noble-backports) physically share
        # the same pool/, and there's nothing to be done about that (see
        # CLAUDE.md/ROADMAP).
        return f"{_apt_top_segment(repo)}/pool/{repo.component}"
    if repo.type == "nix":
        return f"/nix/{repo.id}"
    if repo.type == "apk":
        # upstream already includes version+component (e.g.
        # ".../alpine/v3.20/main") — this path segment is also needed in
        # the local warm URL, otherwise you'd get .../alpine/{arch}/...
        # without v3.20/main, and upstream has no such path (this used to
        # cause 404s while warming apk).
        upstream_path = urllib.parse.urlparse(repo.upstream).path.rstrip("/")
        return f"{upstream_path}/{repo.arch}"
    raise ValueError(f"unknown repository type: {repo.type}")


def _local_path(repo: RepoConfig, filename: str) -> str:
    """Local path (no scheme/host) this file is reachable at through
    cache_base_url — factored out so the warm URL and the purge URL (see
    _build_warm_url/_purge_url) share exactly one source of truth for the
    apt/apt-rpm special-casing, instead of two copies that could drift."""
    if repo.type == "apt-rpm":
        return f"{_apt_rpm_top_segment(repo)}/{filename}"
    if repo.type == "apt":
        # The apt index's Filename already includes a path like
        # pool/main/x/xz-utils/... (by itself, without the top segment —
        # that's added here via _apt_top_segment)
        return f"{_apt_top_segment(repo)}/{filename}"
    return f"{_repo_url_prefix(repo)}/{filename}"


def _build_warm_url(config: Config, repo: RepoConfig, filename: str) -> str:
    return f"{config.cache_base_url}{_local_path(repo, filename)}"


def _purge_url(config: Config, repo: RepoConfig, filename: str) -> str:
    """Local URL that, when GET-requested, purges this package's
    proxy_cache entry — see nginx.py's purge location, only emitted when
    nginx.enable_purge is set (docs_dev/ROADMAP.md item 24). Same local
    path as _build_warm_url, under a /purge prefix nginx.py matches with a
    dedicated, loopback-only location per repository."""
    return f"{config.cache_base_url}/purge{_local_path(repo, filename)}"


async def warm_cache(
    config: Config,
    repo: RepoConfig,
    store: StateStore,
    new_packages: dict[str, str],
    force: bool = False,
    *, expected_hashes: dict[str, str] | None = None,
) -> dict[str, bool]:
    """Hit the local cache for each new package — in parallel
    (asyncio.Semaphore(config.prefetch_concurrency)), since on the first
    full warm of repositories like arch-extra/debian the count of new
    packages runs into the thousands, and sequential GETs one at a time
    would take hours per cycle.

    The outcome of each attempt is written to StateStore.warmed_packages
    (see api.py: the dashboard shows exactly this, not the full list of
    upstream packages — it used to show the latter, which didn't answer
    "what's actually warmed").

    Failures on individual files must not take down the whole warm run —
    we log and move on (see _warm_one).

    new_packages — {package_key: filename}, usually built from
    DiffResult.new_packages + RepoSnapshot.packages (see watcher.py), but
    can also be an arbitrary subset — for manual warming through the
    dashboard (see api.py.warm_packages_payload), hence force=True skips
    the repo.prefetch check: a manual operator action shouldn't be blocked
    by the repository's automatic policy.

    Warm-up speed is paced by the shared application bandwidth budget and
    an additional per-repository ceiling. Manual and automatic operations
    share this budget across threads and event loops — separate from
    prefetch_concurrency, which only bounds parallelism, not total
    bandwidth (you might want many concurrent connections without
    exceeding N bytes/sec in aggregate).

    Whitelist/blacklist patterns and exact bans are
    filtered out right here — a single point that behaves the same for
    automatic warming and force=True (manual warming does not bypass bans:
    if you really need to warm a banned package, unban it first).

    Returns attempted package keys mapped to success. expected_hashes, when
    supplied for replacements, verifies the downloaded bytes before success
    is recorded. Skipped packages are absent from the result.

    If this run had at least one failure, it bumps the "repeated warm
    failure" streak used for webhook notifications (see notifications.py);
    a run with zero failures resets the streak.
    """
    if repo.type == 'nix':
        from repowatch.nix_cache import warm
        return await warm(config, repo, store, new_packages, force)
    if not force and not repo.prefetch:
        logger.debug("prefetch disabled for %s, skipping", repo.id)
        return {}

    from repowatch.warming_policy import WarmingPolicy
    policy = WarmingPolicy(repo, store)

    tasks: list[tuple[str, str, str]] = []
    for key, filename in new_packages.items():
        if not filename:
            logger.warning("%s: empty filename for package %s, cannot warm", repo.id, key)
            continue
        if not policy.allows(key):
            logger.debug("%s: package %s is excluded by warming policy, skipping", repo.id, key)
            continue
        tasks.append((key, filename, _build_warm_url(config, repo, filename)))

    if not tasks:
        return {}

    limiter = store.bandwidth.limiter(config, repo)
    semaphore = asyncio.Semaphore(config.prefetch_concurrency)
    failed_keys: list[str] = []
    outcomes: dict[str, bool] = {}

    async def _run(client: httpx.AsyncClient, task: tuple[str, str, str]) -> None:
        key, filename, url = task
        async with semaphore:
            digest = (expected_hashes or {}).get(key)
            if digest:
                ok, http_status = await _warm_one(client, url, limiter, expected_sha256=digest)
            else:
                ok, http_status = await _warm_one(client, url, limiter)
        store.record_warmed_package(repo.id, key, filename, ok, http_status, source="prefetch")
        outcomes[key] = ok
        if not ok:
            # A single event loop, no real thread parallelism — append()
            # from different coroutines is safe here without a lock.
            failed_keys.append(key)

    async with httpx.AsyncClient() as client:
        # Exceptions are swallowed inside _warm_one and never propagate out —
        # gather() doesn't need return_exceptions=True
        await asyncio.gather(*(_run(client, task) for task in tasks))

    # "Repeated warm failures" (see notifications.py) is counted per
    # warm_cache RUN, not per package: at least one failure in this run
    # bumps the streak, a run with zero failures resets it. Not perfect (a
    # single 404 from upstream churn also counts), but repeated runs in a
    # row with zero failures quickly clear out random noise — a
    # consistently broken upstream/cache produces a consistently growing
    # streak, which is the actual signal.
    if failed_keys:
        await record_failure_and_maybe_notify(
            config, store, repo.id, "prefetch",
            f"{len(failed_keys)}/{len(tasks)} packages failed to warm in this run "
            f"(e.g.: {failed_keys[0]})",
        )
    else:
        await record_success_and_maybe_notify(config, store, repo.id, "prefetch")

    return outcomes


async def purge_removed(
    config: Config, repo: RepoConfig, removed: dict[str, str],
) -> None:
    """Actively evicts each removed package's proxy_cache entry the moment
    repowatch's diff confirms it's gone from the upstream index, instead of
    waiting for nginx's own inactive=180d to eventually notice the file was
    never requested again (see docs_dev/ROADMAP.md item 24 — a definitively
    dead file, since package managers address files by exact version, not
    just "cold").

    A no-op unless nginx.enable_purge is set — without it there's no purge
    location to send the request to (see nginx.py), and behavior is exactly
    as before: inactive=/max_size= remain the only eviction. Also a no-op
    for repositories not served by the generator at all — repowatch doesn't
    know whether a hand-written nginx config has anything at /purge/... —
    this is a `enable_purge`-gated pairing with nginx.py's own generator,
    not a general-purpose HTTP call.

    removed — {package_key: filename}, the exact shape of
    DiffResult.removed_filenames.

    No bandwidth pacing (unlike warm_cache/_warm_one) — a purge request
    carries no file body worth throttling. Concurrency and per-item error
    handling follow the same shape as warm_cache: one failed purge logs and
    moves on, it must not abort the rest of the batch.
    """
    if not config.nginx.enable_purge or not removed:
        return
    semaphore = asyncio.Semaphore(config.prefetch_concurrency)

    async def _run(client: httpx.AsyncClient, filename: str) -> None:
        url = _purge_url(config, repo, filename)
        async with semaphore:
            try:
                resp = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
                if resp.status_code >= 400:
                    logger.warning("%s: purge failed (HTTP %s) for %s", repo.id, resp.status_code, filename)
            except httpx.RequestError as exc:
                logger.warning("%s: purge failed (%s) for %s", repo.id, exc, filename)

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(_run(client, filename) for filename in removed.values()))


async def purge_selected(config: Config, repo: RepoConfig, items: dict[str, str]) -> dict[str, str]:
    """Manual "purge selected" (dashboard, docs_dev/ROADMAP.md) — same HTTP
    call as purge_removed, but reports a per-item OUTCOME instead of firing
    and forgetting: this is a user-initiated action, someone is waiting to
    see what actually happened, not a side effect of a background check
    cycle. The purge attempt itself is also the ONLY reliable way to learn
    whether nginx still actually has a given key cached — ngx_cache_purge's
    own response code already tells us (200 existed and was deleted, 404
    nothing was there) at zero extra cost, so there is no separate
    non-destructive "check" step here (see docs_dev/ROADMAP.md for why: a
    live HEAD/GET probe against the same proxy_cache key used by real
    traffic has a real correctness risk — proxy_cache_key doesn't include
    the request method, so caching a HEAD response under a key a real GET
    also uses could later serve that bodyless response to an actual client).

    Unlike purge_removed, does NOT check config.nginx.enable_purge itself —
    the caller (api.purge_selected_payload) already refuses the request
    before this is ever reached, with a clear error explaining why, since a
    user clicking a button deserves a real answer, not a silent no-op.

    items — {package_key: filename}, already re-derived from warmed_packages
    server-side by the caller (never trust client-supplied filenames for a
    purge target).

    Returns {package_key: "purged" | "not_cached" | "error (...)"}.
    """
    if not items:
        return {}
    semaphore = asyncio.Semaphore(config.prefetch_concurrency)
    results: dict[str, str] = {}

    async def _run(client: httpx.AsyncClient, key: str, filename: str) -> None:
        url = _purge_url(config, repo, filename)
        async with semaphore:
            try:
                resp = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
                if resp.status_code == 200:
                    results[key] = "purged"
                elif resp.status_code == 404:
                    results[key] = "not_cached"
                else:
                    results[key] = f"error (HTTP {resp.status_code})"
            except httpx.RequestError as exc:
                results[key] = f"error ({exc})"

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(_run(client, key, filename) for key, filename in items.items()))
    return results


async def _warm_one(
    client: httpx.AsyncClient, url: str, limiter: BandwidthLimiter,
    *, expected_sha256: str | None = None, expected_size: int | None = None,
) -> tuple[bool, int | None]:
    """The body is read in chunks (not loaded into memory whole) — each
    chunk "spends" its size in the limiter, so the limit is actually paced
    by bytes that went over the network, not by request count.

    Unlike urllib, httpx doesn't raise on a non-2xx status by itself —
    raise_for_status() runs right after headers arrive (before streaming
    the body), so the bandwidth budget isn't spent on an error page's body."""
    try:
        async with client.stream(
            "GET", url, headers={"User-Agent": USER_AGENT}, timeout=60
        ) as resp:
            resp.raise_for_status()
            digest = hashlib.sha256() if expected_sha256 else None
            received = 0
            async for chunk in resp.aiter_bytes(_CHUNK_SIZE):
                received += len(chunk)
                await limiter.consume(len(chunk))
                if expected_size is not None and received > expected_size:
                    logger.warning("warm size exceeds metadata: %s", url)
                    return False, resp.status_code
                if digest is not None:
                    digest.update(chunk)
            if expected_size is not None and received != expected_size:
                logger.warning("warm size mismatch: %s", url)
                return False, resp.status_code
            if digest is not None and digest.hexdigest() != expected_sha256.lower():
                logger.warning("warm hash mismatch: %s", url)
                return False, resp.status_code
            logger.info("warmed: %s (%s)", url, resp.status_code)
            return True, resp.status_code
    except httpx.HTTPStatusError as exc:
        logger.warning("warm failed (HTTP %s): %s", exc.response.status_code, url)
        return False, exc.response.status_code
    except httpx.RequestError as exc:
        logger.warning("warm failed (%s): %s", exc, url)
        return False, None
