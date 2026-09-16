"""Coordinate retention and cache deletion with persistent bookkeeping."""

from __future__ import annotations

import logging
import repowatch.cache.probe as cache_probe
from repowatch.cache.purge import purge_selected
from repowatch.config.models import Config, RepoConfig
from repowatch.runtime.context import ServiceState

logger = logging.getLogger(__name__)


async def _purge_and_unwarm_stale(
    config: Config, store: ServiceState, stale: list[tuple[str, str, str]]
) -> None:
    """Purge expired entries through the shared backend and conditionally untrack them.

    Failures remain eligible for the next retention pass. Deleted repositories
    have no current route; their physical files require an inventory scan.
    """
    by_repo: dict[str, dict[str, str]] = {}
    for repo_id, key, filename in stale:
        by_repo.setdefault(repo_id, {})[key] = filename

    for repo_id, items in by_repo.items():
        records = store.cache.get_warmed_records(
            repo_id, list(items), retention_days=config.warmed_retention_days)
        items = {key: record[0] for key, record in records.items()}
        if not items:
            continue
        repo = config.repo_by_id(repo_id)
        if repo is None:
            store.cache.remove_purged_records(repo_id, records, list(items))
            continue
        results = await purge_items(config, repo, items, store)
        resolved = resolved_purge_keys(results)
        store.cache.remove_purged_records(repo_id, records, resolved)
        errored = len(items) - len(resolved)
        if errored:
            logger.warning(
                "%s: %d stale warmed package(s) failed to purge, will retry next cycle",
                repo_id, errored,
            )


async def prune_all(config: Config, store: ServiceState) -> None:
    pruned = store.repositories.prune_events(config.event_retention_days)
    if pruned:
        logger.debug("cleaned up %d stale repo_events rows", pruned)

    pruned_requests = store.requests.prune_requests(config.request_retention_days)
    if pruned_requests:
        logger.debug("cleaned up %d stale request_events rows", pruned_requests)

    if not config.syslog_listener.enabled:
        # Without real client-request visibility, warmed_at only ever
        # advances at first warm (see ServiceState.cache.prune_warmed_packages'
        # own docstring) — ageing rows out here would silently drop
        # bookkeeping (and, worse, purge real cache entries below) for
        # packages real clients might still be actively using, purely
        # because repowatch has no way to know either way. Skip the whole
        # warmed_packages retention step entirely rather than guess.
        logger.debug("syslog_listener disabled — skipping warmed_packages expiry (no request visibility)")
    elif config.nginx.enable_purge:
        stale = store.cache.get_stale_warmed_packages(config.warmed_retention_days)
        if stale:
            await _purge_and_unwarm_stale(config, store, stale)
    else:
        pruned_warmed = store.cache.prune_warmed_packages(config.warmed_retention_days)
        if pruned_warmed:
            logger.debug("cleaned up %d stale warmed_packages rows", pruned_warmed)

    # Size-based — on top of the time-based cleanup above, only if the
    # operator set a limit.
    if config.event_max_rows_per_repo is not None:
        pruned_by_size = store.repositories.prune_events_by_size(config.event_max_rows_per_repo)
        if pruned_by_size:
            logger.debug(
                "cleaned up %d repo_events rows over the %d-row-per-repo limit",
                pruned_by_size, config.event_max_rows_per_repo,
            )

    if config.request_max_rows is not None:
        pruned_requests_by_size = store.requests.prune_requests_by_size(config.request_max_rows)
        if pruned_requests_by_size:
            logger.debug(
                "cleaned up %d request_events rows over the global %d-row limit",
                pruned_requests_by_size, config.request_max_rows,
            )


async def purge_items(config: Config, repo: RepoConfig, items: dict[str, str], store: ServiceState) -> dict[str, str]:
    """Picks the purge mechanism for the dashboard's two purge-capable
    handlers (purge_selected_payload, remove_warmed_package_payload): the
    route-independent /purge-raw location (cache.probe.purge_selected_raw,
    keyed via routing.compute_cache_key()) when nginx.enable_cache_probe is
    on, else the standard per-repo /purge<prefix> location
    (cache.purge.purge_selected). Both paths use the same result contract ("purged"/"not_cached"/"error (...)" per key), so callers don't
    need to know which path ran.

    cache_probe's version isn't just an alternate transport for the same
    outcome — see its own docstring: it catches a real class of cache entry
    the per-repo path structurally cannot (a file cached under a since-
    changed nginx.enable_dedup basis)."""
    if repo.type == 'nix':
        from repowatch.cache.nix import purge
        return await purge(config, repo, store, items)
    if config.nginx.enable_cache_probe:
        canonical_keys = {}
        if config.nginx.enable_dedup:
            from repowatch.routing import compute_cache_key
            selected = set(items.values())
            for duplicate_id, canonical_id, filename in store.cache.find_duplicate_files():
                if duplicate_id != repo.id or filename not in selected:
                    continue
                canonical = config.repo_by_id(canonical_id)
                if canonical is not None:
                    canonical_keys[filename] = compute_cache_key(config, canonical, filename)
        if canonical_keys:
            return await cache_probe.purge_selected_raw(
                config, repo, items, canonical_keys=canonical_keys)
        return await cache_probe.purge_selected_raw(config, repo, items)
    return await purge_selected(config, repo, items)


def resolved_purge_keys(results: dict[str, str]) -> list[str]:
    """Keys safe to untrack: absent/deleted files or Nix ownership retained elsewhere."""
    return [key for key, outcome in results.items()
            if outcome in ("purged", "not_cached", "retained_shared")]
