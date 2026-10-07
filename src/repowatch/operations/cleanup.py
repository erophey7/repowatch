"""Coordinate retention and cache deletion with persistent bookkeeping."""

from __future__ import annotations

import asyncio
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


async def purge_removed_from_index(
    config: Config, store: ServiceState, items: list[dict]
) -> dict[str, dict[str, str]]:
    """Purge candidates already known to be removed from the current index
    snapshot — e.g. from ServiceState.cache.find_all_stale_warmed(). Each
    item needs only "repo_id" and "package_key" (an optional "filename",
    as find_all_stale_warmed() includes, is ignored — the real filename
    and revision are always re-derived from warmed_packages itself just
    before purging, the same "never trust a stale/caller-supplied
    filename" reasoning as web.packages.purge_selected_payload). Shared by
    the automatic per-cycle sweep in prune_all and the Storage tab's
    manual cross-repo "zombie packages" purge.

    Unlike _purge_and_unwarm_stale, this does NOT re-check
    warmed_retention_days: "no longer listed in the current index" is
    itself sufficient justification, known purely from record_snapshot's
    own diff — waiting out the same 180-day "not recently touched" window
    a still-current package gets would defeat the whole point (see
    prune_all and find_all_stale_warmed's own docstrings for the concrete
    firefox example that motivated this, 2026-09-23).

    A repo_id not currently in config.repos is skipped — a fully removed
    repository has no RepoConfig to compute its cache key from, and is
    find_orphaned_repos/purge_orphaned_repos' job, not this one. Returns
    {repo_id: {package_key: outcome}} for every repo actually purged, so
    a manual caller can report results; the automatic caller only logs
    the aggregate error count."""
    by_repo: dict[str, set[str]] = {}
    for item in items:
        by_repo.setdefault(item["repo_id"], set()).add(item["package_key"])

    all_results: dict[str, dict[str, str]] = {}
    for repo_id, candidates in by_repo.items():
        repo = config.repo_by_id(repo_id)
        if repo is None:
            continue
        records = store.cache.get_warmed_records(repo_id, list(candidates))
        filenames = {key: record[0] for key, record in records.items()}
        if not filenames:
            continue
        results = await purge_items(config, repo, filenames, store)
        resolved = resolved_purge_keys(results)
        store.cache.remove_purged_records(repo_id, records, resolved)
        all_results[repo_id] = results
    return all_results


async def prune_all(config: Config, store: ServiceState) -> None:
    pruned = store.repositories.prune_events(config.event_retention_days)
    if pruned:
        logger.debug("cleaned up %d stale repo_events rows", pruned)

    # Each removed request row also updates the statistics rollups, so a backlog
    # takes long enough to stall the event loop; run it in a worker thread
    # (every store call opens its own connection).
    pruned_requests = await asyncio.to_thread(store.requests.prune_requests, config.request_retention_days)
    if pruned_requests:
        logger.debug("cleaned up %d stale request_events rows", pruned_requests)

    if config.nginx.enable_purge:
        # Independent of syslog_listener.enabled below: "no longer in the
        # current index" doesn't need client-request visibility to be a
        # reliable signal, unlike the generic time-based retention that
        # follows — see purge_removed_from_index's own docstring.
        removed = store.cache.find_all_stale_warmed()
        if removed:
            results = await purge_removed_from_index(config, store, removed)
            errored = sum(1 for repo_results in results.values()
                          for outcome in repo_results.values() if outcome.startswith("error"))
            if errored:
                logger.warning(
                    "%d package(s) removed from the index failed to purge, will retry next cycle", errored)

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
        pruned_requests_by_size = await asyncio.to_thread(
            store.requests.prune_requests_by_size, config.request_max_rows)
        if pruned_requests_by_size:
            logger.debug(
                "cleaned up %d request_events rows over the global %d-row limit",
                pruned_requests_by_size, config.request_max_rows,
            )


def find_orphaned_repos(config: Config, store: ServiceState) -> dict[str, dict[str, int]]:
    """Repo ids with SQL bookkeeping (repo_state/repo_packages/repo_events/
    pending_replacements/warmed_packages/prefetch_bans/nix_artifacts/
    nix_trust/failure_state) that no longer appear in config.repos —
    typically a repository removed from config.yaml, or left over from a
    renamed id (RepoConfig.id is immutable: a rename is a delete+add,
    not an update).

    request_events/host_tokens are deliberately excluded: the former has
    its own separate, already-implemented retention
    (event_retention_days/request_max_rows) and a nullable repo_id shared
    across repos with overlapping URL prefixes, not a per-repo ownership
    concept this cleanup can reason about; the latter is a host-auth
    concern (token scoping), unrelated to repository state.

    This only reports/deletes SQL rows, never physical cache files — a
    removed repository has no current RepoConfig to recompute its nginx
    cache key format from, so there is nothing here that could locate its
    files on disk. Finding those (true "zombie" cache entries) is the
    separate, not-yet-built scan-based detector already tracked in
    docs_dev/ROADMAP.md (item 8/33) — this function is a different,
    narrower kind of orphan (bookkeeping, not bytes)."""
    active = {repo.id for repo in config.repos}
    orphaned = (
        store.repositories.get_repo_ids_with_data()
        | store.cache.get_repo_ids_with_data()
        | store.notifications.get_repo_ids_with_data()
    ) - active
    counts: dict[str, dict[str, int]] = {}
    for repo_id in sorted(orphaned):
        rows = {**store.repositories.count_repo_rows(repo_id), **store.cache.count_repo_rows(repo_id)}
        failure_rows = store.notifications.count_repo_rows(repo_id)
        if failure_rows:
            rows['failure_state'] = failure_rows
        counts[repo_id] = rows
    return counts


def purge_orphaned_repos(config: Config, store: ServiceState, repo_ids: list[str]) -> dict[str, dict[str, int]]:
    """Delete SQL bookkeeping for the given repo_ids, re-checking each is
    still absent from config.repos right before deleting (the same
    defensive re-check pattern as _purge_and_unwarm_stale/purge_items —
    the operator could re-add a repository with the same id between the
    scan the dashboard showed and this call). Ids still in config.repos
    are silently skipped, not an error: the caller (web layer) already
    only sends what find_orphaned_repos reported, so this only matters for
    a genuine race."""
    active = {repo.id for repo in config.repos}
    results: dict[str, dict[str, int]] = {}
    for repo_id in repo_ids:
        if repo_id in active:
            continue
        rows = {**store.repositories.delete_repo_rows(repo_id), **store.cache.delete_repo_rows(repo_id)}
        failure_rows = store.notifications.delete_repo_rows(repo_id)
        if failure_rows:
            rows['failure_state'] = failure_rows
        if rows:
            results[repo_id] = rows
    return results


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
            from repowatch.routing import CacheKeyBuilder, package_path
            from repowatch.cache.dedup import accepted_pairs, dedup_keys
            rows = store.cache.find_duplicate_files()
            redirects = dedup_keys(config, rows, CacheKeyBuilder(config),
                pairs=accepted_pairs(config, store.cache, rows))
            canonical_keys = {filename: redirects[uri] for filename in items.values()
                              if (uri := package_path(repo, filename).lower()) in redirects
                              and redirects[uri]}
        if canonical_keys:
            return await cache_probe.purge_selected_raw(
                config, repo, items, canonical_keys=canonical_keys)
        return await cache_probe.purge_selected_raw(config, repo, items)
    return await purge_selected(config, repo, items)


def resolved_purge_keys(results: dict[str, str]) -> list[str]:
    """Keys safe to untrack: absent/deleted files or Nix ownership retained elsewhere."""
    return [key for key, outcome in results.items()
            if outcome in ("purged", "not_cached", "retained_shared")]
