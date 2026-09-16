"""Select and warm packages under the configured cache and bandwidth policies."""

from __future__ import annotations

import asyncio
import httpx
import logging
from repowatch.cache.transport import download_package
from repowatch.config.models import Config, RepoConfig
from repowatch.notifications import record_failure_and_maybe_notify, record_success_and_maybe_notify
from repowatch.routing import warm_url
from repowatch.runtime.context import ServiceState

logger = logging.getLogger(__name__)


async def warm_cache(
    config: Config, repo: RepoConfig, store: ServiceState,
    new_packages: dict[str, str], force: bool = False,
    *, expected_hashes: dict[str, str] | None = None,
) -> dict[str, bool]:
    """Warm selected packages, optionally publishing correlated lifecycle events."""
    from repowatch.notifications import emit, enabled
    if (not new_packages or (not force and not repo.prefetch)
            or not any(enabled(config, e) for e in ('warm.started', 'warm.completed'))):
        return await _warm_cache(config, repo, store, new_packages, force,
                                 expected_hashes=expected_hashes)
    from repowatch.warming_policy import WarmingPolicy
    import uuid
    policy = WarmingPolicy(repo, store)
    selected = {key: path for key, path in new_packages.items() if path and policy.allows(key)}
    if not selected:
        return await _warm_cache(config, repo, store, new_packages, force,
                                 expected_hashes=expected_hashes)
    operation = {'operation_id': str(uuid.uuid4()), 'requested': len(new_packages),
                 'eligible': len(selected), 'manual': force}
    await emit(config, 'warm.started', repo.id, operation)
    try:
        outcomes = await _warm_cache(config, repo, store, selected, force,
                                     expected_hashes=expected_hashes)
    except Exception:
        await emit(config, 'warm.completed', repo.id, {**operation, 'status': 'error'})
        raise
    succeeded = sum(outcomes.values())
    failed = len(outcomes) - succeeded
    await emit(config, 'warm.completed', repo.id, {
        **operation, 'status': 'partial' if failed and succeeded else 'failed' if failed else 'completed',
        'succeeded': succeeded, 'failed': failed, 'skipped': len(new_packages) - len(outcomes)})
    return outcomes


async def _warm_cache(
    config: Config,
    repo: RepoConfig,
    store: ServiceState,
    new_packages: dict[str, str],
    force: bool = False,
    *, expected_hashes: dict[str, str] | None = None,
) -> dict[str, bool]:
    """Hit the local cache for each new package — in parallel
    (asyncio.Semaphore(config.prefetch_concurrency)), since on the first
    full warm of repositories like arch-extra/debian the count of new
    packages runs into the thousands, and sequential GETs one at a time
    would take hours per cycle.

    The outcome of each attempt is written to ServiceState.warmed_packages
    (see web/handler.py: the dashboard shows exactly this, not the full list of
    upstream packages — it used to show the latter, which didn't answer
    "what's actually warmed").

    Failures on individual files must not take down the whole warm run —
    we log and move on (see download_package).

    new_packages — {package_key: filename}, usually built from
    DiffResult.new_packages + RepoSnapshot.packages (see operations/check.py), but
    can also be an arbitrary subset — for manual warming through the
    dashboard (see web/handler.py.warm_packages_payload), hence force=True skips
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
        from repowatch.cache.nix import warm
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
        tasks.append((key, filename, warm_url(config, repo, filename)))

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
                ok, http_status = await download_package(client, url, limiter, expected_sha256=digest)
            else:
                ok, http_status = await download_package(client, url, limiter)
        store.cache.record_warmed_package(repo.id, key, filename, ok, http_status, source="prefetch")
        outcomes[key] = ok
        if not ok:
            # A single event loop, no real thread parallelism — append()
            # from different coroutines is safe here without a lock.
            failed_keys.append(key)

    async with httpx.AsyncClient() as client:
        # download_package handles network failures. Bookkeeping failures in
        # _run still propagate through gather to the caller.
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


async def warm_selected(config: Config, repo: RepoConfig, store: ServiceState,
                        requested_keys: list[str]) -> dict:
    """Resolve requested package keys and report manual warm outcomes."""
    known_packages = store.repositories.get_packages_by_keys(repo.id, requested_keys) or {}

    to_warm = {k: known_packages[k] for k in requested_keys if k in known_packages}
    not_found = [k for k in requested_keys if k not in known_packages]

    outcomes: dict[str, bool] = {}
    if to_warm:
        outcomes = await warm_cache(config, repo, store, to_warm, force=True)

    return {
        "warmed": sorted(key for key, ok in outcomes.items() if ok),
        "failed": sorted(key for key, ok in outcomes.items() if not ok),
        "skipped": sorted(set(to_warm) - outcomes.keys()),
        "not_found": not_found,
    }
