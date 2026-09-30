"""On-demand package presence reports from nginx's physical cache inventory."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx

from repowatch.cache import probe
from repowatch.config.models import Config
from repowatch.nginx.render import resolve_dedup_pairs
from repowatch.routing import CacheKeyBuilder, package_path
from repowatch.runtime.context import ServiceState
from repowatch.reporting.storage_usage import storage_usage
from repowatch.web.access import load_request_config


def _dedup_keys(config: Config, store: ServiceState, builder: CacheKeyBuilder) -> dict[str, str]:
    """Resolve exactly the renderer's accepted rewrites, including chains and conflicts."""
    if not config.nginx.enable_dedup:
        return {}
    rows = store.cache.find_duplicate_files()
    rewrites = {source.lower(): target for source, target in resolve_dedup_pairs(config, rows)}
    repos = {repo.id: repo for repo in config.repos}
    bound = {repo.id: builder.for_repo(repo) for repo in config.repos}
    targets = {package_path(repos[canonical], filename): bound[canonical](filename)
               for _, canonical, filename in rows if canonical in repos}
    keys = {}
    for source, target in rewrites.items():
        seen = {source}
        while target.lower() in rewrites and target.lower() not in seen:
            seen.add(target.lower())
            target = rewrites[target.lower()]
        # An empty key denotes an unresolved cycle, never a cache hit.
        keys[source] = '' if target.lower() in seen else targets.get(target, '')
    return keys


def summarize(config: Config, catalogs: dict, inventory: probe.Inventory,
              builder: CacheKeyBuilder, dedup_keys: dict[str, str]) -> list[dict]:
    """Count catalog entries, conservatively separating absent and unknown packages."""
    present = {entry.key for entry in inventory.entries if entry.key and not entry.error}
    incomplete = bool(inventory.failed_leaves or any(not e.key or e.error for e in inventory.entries))
    # A pending replacement invalidates evidence for aliases and deduplicated
    # dependants too, not only for the package owning the queue entry.
    uncertain_keys = set()
    for repo in config.repos:
        catalog = catalogs[repo.id]
        key_for = builder.for_repo(repo)
        outdated = catalog['source_identity'] != repo.catalog_identity()
        for package, filename in catalog['packages']:
            if outdated or package in catalog['pending']:
                uncertain_keys.add(key_for(filename))
                uncertain_keys.add(dedup_keys.get(package_path(repo, filename).lower(), key_for(filename)))
    items = []
    for repo in config.repos:
        catalog = catalogs[repo.id]
        packages = catalog['packages']
        row = dict(repo_id=repo.id, total_packages=len(packages), cached_packages=0,
                   missing_packages=0, unknown_packages=0, percent=None, state='unknown',
                   last_check=catalog['last_check'], reason=None)
        items.append(row)
        if not catalog['last_check']:
            row.update(total_packages=None, reason='no_snapshot')
            continue
        if catalog['source_identity'] != repo.catalog_identity():
            row.update(unknown_packages=len(packages), reason='source_changed')
            continue
        key_for = builder.for_repo(repo)
        for package, filename in packages:
            if package in catalog['pending']:
                row['unknown_packages'] += 1
                continue
            if repo.type == 'nix':
                artifacts = catalog['artifacts'].get(package, set())
                # Artifact history preserves old encodings for purge. A missing
                # historical file cannot prove the current closure is missing.
                known = filename in artifacts and any(not f.endswith('.narinfo') for f in artifacts)
                cached = known and all(key_for(f) in present for f in artifacts)
                row['cached_packages' if cached else 'unknown_packages'] += 1
                continue
            key = dedup_keys.get(package_path(repo, filename).lower(), key_for(filename))
            if key in uncertain_keys:
                row['unknown_packages'] += 1
            elif key in present:
                row['cached_packages'] += 1
            elif incomplete or not key:
                row['unknown_packages'] += 1
            else:
                row['missing_packages'] += 1
        total = row['total_packages']
        if row['unknown_packages']:
            row['reason'] = 'incomplete_evidence'
        elif total == 0:
            row['state'] = 'empty'
        else:
            row['percent'] = round(100 * row['cached_packages'] / total, 2)
            row['state'] = 'complete' if row['cached_packages'] == total else 'partial'
    return items


async def _inventory(base_url: str) -> probe.Inventory:
    """Check reachability before starting the bounded full-directory scan."""
    async with httpx.AsyncClient() as client:
        await probe.scan_leaf(client, base_url, '0/00')
    return await probe.full_inventory(base_url)


def completeness_payload(config_path: str | Path, store: ServiceState,
                         *, current: Config | None = None, include_usage: bool = False) -> tuple[int, dict]:
    """Run an explicit administrator scan; reject overlap or changing inputs."""
    config, error = load_request_config(config_path, current)
    if error is not None:
        return error
    if not config.nginx.enable_cache_probe:
        return 409, {'error': 'Enable nginx.enable_cache_probe to measure physical cache completeness.'}
    if not store.completeness_lock.acquire(blocking=False):
        return 409, {'error': 'A cache completeness scan is already running.'}
    try:
        started = datetime.now(timezone.utc).isoformat()
        revisions = store.repositories.get_snapshot_revisions()
        catalogs = store.queries.completeness_catalogs(
            [repo.id for repo in config.repos], include_warmed=include_usage)
        builder = CacheKeyBuilder(config)
        dedup_keys = _dedup_keys(config, store, builder)
        try:
            inventory = asyncio.run(_inventory(config.cache_base_url)) if config.repos or include_usage else probe.Inventory([])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            return 502, {'error': f'Cache inventory unavailable: {exc}'}
        latest, error = load_request_config(config_path)
        if error is not None or latest != config or revisions != store.repositories.get_snapshot_revisions():
            return 409, {'error': 'Configuration or repository snapshots changed during the scan; run it again.'}
        payload = dict(
            source='cache_inventory', started_at=started,
            finished_at=datetime.now(timezone.utc).isoformat(),
            failed_leaves=inventory.failed_leaves,
            unreadable_entries=sum(1 for e in inventory.entries if not e.key or e.error),
            items=summarize(config, catalogs, inventory, builder, dedup_keys),
        )
        if include_usage:
            payload['storage_usage'] = storage_usage(config, catalogs, inventory, builder, dedup_keys)
        return 200, payload
    finally:
        store.completeness_lock.release()
