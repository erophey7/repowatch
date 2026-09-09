"""Cache warming: once the watcher finds new packages, we GET them through
the local nginx (cache_base_url) so the file lands in proxy_cache BEFORE a
client actually requests it.

Important: we hit our own nginx, not upstream directly — otherwise the
warmed file wouldn't land in the same cache real clients use.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse

import httpx

from repowatch.config import Config, RepoConfig
from repowatch.notifications import record_failure_and_maybe_notify, record_success_and_maybe_notify
from repowatch.parsers.base import USER_AGENT
from repowatch.state import StateStore

logger = logging.getLogger(__name__)

# Chunk size for streaming the response body in _warm_one — small enough for
# accurate byte-level pacing, large enough not to flood syscalls.
_CHUNK_SIZE = 64 * 1024


class BandwidthLimiter:
    """Paces the TOTAL warm-up bandwidth (bytes/sec), not request rate (not
    a token bucket that accumulates "credit" — a hard ceiling, no bursts
    after idling). Shared across all concurrent warm tasks — bounds the
    aggregate rate independently of prefetch_concurrency (which only bounds
    parallelism, not bandwidth).

    Bytes, not requests: the size of warmed files varies by orders of
    magnitude (a couple hundred bytes for a pacman .desc vs. hundreds of
    megabytes for a debian .deb) — a requests/sec limit wouldn't protect the
    actual bandwidth to upstream/the local nginx (see this field's history —
    it used to be RateLimiter.acquire(), which counted requests)."""

    def __init__(self, bytes_per_sec: float | None):
        self._bytes_per_sec = bytes_per_sec
        self._lock = asyncio.Lock()
        self._next_allowed = time.monotonic()

    async def consume(self, n_bytes: int) -> None:
        if not self._bytes_per_sec or n_bytes <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_allowed = max(now, self._next_allowed) + (n_bytes / self._bytes_per_sec)


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
    if repo.type == "apk":
        # upstream already includes version+component (e.g.
        # ".../alpine/v3.20/main") — this path segment is also needed in
        # the local warm URL, otherwise you'd get .../alpine/{arch}/...
        # without v3.20/main, and upstream has no such path (this used to
        # cause 404s while warming apk).
        upstream_path = urllib.parse.urlparse(repo.upstream).path.rstrip("/")
        return f"{upstream_path}/{repo.arch}"
    raise ValueError(f"unknown repository type: {repo.type}")


def _build_warm_url(config: Config, repo: RepoConfig, filename: str) -> str:
    if repo.type == "apt-rpm":
        return f"{config.cache_base_url}{_apt_rpm_top_segment(repo)}/{filename}"
    if repo.type == "apt":
        # The apt index's Filename already includes a path like
        # pool/main/x/xz-utils/... (by itself, without the top segment —
        # that's added here via _apt_top_segment)
        return f"{config.cache_base_url}{_apt_top_segment(repo)}/{filename}"
    return f"{config.cache_base_url}{_repo_url_prefix(repo)}/{filename}"


async def warm_cache(
    config: Config,
    repo: RepoConfig,
    store: StateStore,
    new_packages: dict[str, str],
    force: bool = False,
) -> None:
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

    Warm-up speed is bounded by
    config.effective_prefetch_bandwidth_limit(repo) (bytes/sec, a per-repo
    override of the global prefetch_bandwidth_limit) — separate from
    prefetch_concurrency, which only bounds parallelism, not total
    bandwidth (you might want many concurrent connections without
    exceeding N bytes/sec in aggregate).

    Banned packages (StateStore.prefetch_bans, by name, see api.py) are
    filtered out right here — a single point that behaves the same for
    automatic warming and force=True (manual warming does not bypass bans:
    if you really need to warm a banned package, unban it first).

    If this run had at least one failure, it bumps the "repeated warm
    failure" streak used for webhook notifications (see notifications.py);
    a run with zero failures resets the streak.
    """
    if not force and not repo.prefetch:
        logger.debug("prefetch disabled for %s, skipping", repo.id)
        return

    banned = set(store.get_banned_packages(repo.id))
    names = store.get_names(repo.id) if banned else {}

    tasks: list[tuple[str, str, str]] = []
    for key, filename in new_packages.items():
        if not filename:
            logger.warning("%s: empty filename for package %s, cannot warm", repo.id, key)
            continue
        if names.get(key) in banned:
            logger.debug("%s: package %s is banned from warming, skipping", repo.id, key)
            continue
        tasks.append((key, filename, _build_warm_url(config, repo, filename)))

    if not tasks:
        return

    limiter = BandwidthLimiter(config.effective_prefetch_bandwidth_limit(repo))
    semaphore = asyncio.Semaphore(config.prefetch_concurrency)
    failed_keys: list[str] = []

    async def _run(client: httpx.AsyncClient, task: tuple[str, str, str]) -> None:
        key, filename, url = task
        async with semaphore:
            ok, http_status = await _warm_one(client, url, limiter)
        store.record_warmed_package(repo.id, key, filename, ok, http_status)
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


async def _warm_one(
    client: httpx.AsyncClient, url: str, limiter: BandwidthLimiter
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
            async for chunk in resp.aiter_bytes(_CHUNK_SIZE):
                await limiter.consume(len(chunk))
            logger.info("warmed: %s (%s)", url, resp.status_code)
            return True, resp.status_code
    except httpx.HTTPStatusError as exc:
        logger.warning("warm failed (HTTP %s): %s", exc.response.status_code, url)
        return False, exc.response.status_code
    except httpx.RequestError as exc:
        logger.warning("warm failed (%s): %s", exc, url)
        return False, None
