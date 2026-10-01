"""Remove obsolete physical copies while retaining current canonical cache keys."""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import time

import httpx

from repowatch.cache import probe
from repowatch.cache.dedup import dedup_keys
from repowatch.config.load import load_config
from repowatch.config.models import Config
from repowatch.errors import ConfigError
from repowatch.nginx.render import dedup_generation, resolve_dedup_pairs
from repowatch.routing import CacheKeyBuilder, package_path
from repowatch.runtime.context import ServiceState

logger = logging.getLogger(__name__)
AUTOMATIC_BATCH = 256


class CleanupChanged(ValueError):
    """Cleanup evidence or the applied nginx generation is no longer current."""


@dataclass
class CleanupPlan:
    """Server-derived keys, guarded by configuration and catalog generations."""

    config: Config
    revisions: dict[str, int]
    generation: str
    candidates: dict[str, dict]


def enabled(config: Config) -> bool:
    """Require all three explicit nginx features used by safe cleanup."""
    nginx = config.nginx
    return nginx.enabled and nginx.enable_dedup and nginx.enable_purge and nginx.enable_cache_probe


def make_plan(config: Config, store: ServiceState) -> CleanupPlan:
    """Select unreferenced direct keys from current catalog dedup identities."""
    revisions = store.repositories.get_snapshot_revisions()
    catalogs = store.queries.completeness_catalogs([r.id for r in config.repos])
    rows = store.cache.find_duplicate_files()
    builder = CacheKeyBuilder(config)
    redirects = dedup_keys(config, rows, builder)
    generation = dedup_generation(config, resolve_dedup_pairs(config, rows))
    active, blocked = set(), set()
    possible = {}
    for repo in sorted(config.repos, key=lambda r: r.id):
        catalog = catalogs[repo.id]
        key_for = builder.for_repo(repo)
        outdated = not catalog['last_check'] or catalog['source_identity'] != repo.catalog_identity()
        for package, filename in catalog['packages']:
            direct = key_for(filename)
            target = redirects.get(package_path(repo, filename).lower(), direct)
            keys = {direct, key_for(filename, dedup=not config.nginx.enable_dedup)}
            active.add(target)
            if repo.type == 'nix':
                active.update(key_for(f) for f in catalog['artifacts'].get(package, ()))
            if outdated or package in catalog['pending'] or not target:
                blocked.update(keys | {target})
                continue
            if repo.type == 'nix':
                continue
            for key in keys - {target}:
                # Native nginx $arg_key is literal, not URL-decoded. Do not send
                # keys that can split/change the query or HTTP URL representation.
                if re.fullmatch(r'[A-Za-z0-9_./:+~-]+', key):
                    row = possible.setdefault(key, dict(key=key, repo_id=repo.id,
                                                        filename=filename, targets=set()))
                    row['targets'].add(target)
    candidates = {}
    for key, row in sorted(possible.items()):
        targets = row.pop('targets')
        if key in active or key in blocked or len(targets) != 1:
            continue
        target = next(iter(targets))
        if target in blocked:
            continue
        candidates[key] = {**row, 'canonical_key': target}
    if revisions != store.repositories.get_snapshot_revisions():
        raise CleanupChanged('Repository catalogs changed; retry the cleanup.')
    return CleanupPlan(config, revisions, generation, candidates)


def check_current(plan: CleanupPlan, store: ServiceState, config_path: str | Path | None) -> None:
    """Refuse stale evidence before any deletion, including YAML edits."""
    try:
        current = load_config(config_path) if config_path is not None else plan.config
    except ConfigError as exc:
        raise CleanupChanged('Configuration is invalid; cleanup stopped.') from exc
    if (current != plan.config or plan.revisions != store.repositories.get_snapshot_revisions()):
        raise CleanupChanged('Configuration or repository catalogs changed; retry the cleanup.')


async def inspect_pair(client: httpx.AsyncClient, plan: CleanupPlan, row: dict) -> tuple[bool, int]:
    """Require both files and the expected applied nginx generation; never GET packages."""
    source = await probe.probe(client, plan.config.cache_base_url, row['key'])
    canonical = await probe.probe(client, plan.config.cache_base_url, row['canonical_key'])
    if source.generation != plan.generation or canonical.generation != plan.generation:
        raise CleanupChanged('nginx has not applied this dedup generation; retry after reconciliation.')
    return source.exists and canonical.exists, source.size or 0


async def remove_copies(plan: CleanupPlan, store: ServiceState, keys: list[str],
                        *, config_path: str | Path | None = None) -> dict:
    """Recheck each candidate and purge only its obsolete key, never warm bookkeeping."""
    results = {}
    freed = 0
    started = time.monotonic()
    async with httpx.AsyncClient() as client:
        for key in keys:
            if time.monotonic() - started >= 30:
                return dict(results=results, observed_removed_bytes=freed, stopped='time budget reached')
            row = plan.candidates.get(key)
            if row is None:
                results[key] = 'skipped'
                continue
            try:
                check_current(plan, store, config_path)
                ready, size = await inspect_pair(client, plan, row)
                check_current(plan, store, config_path)
                if not ready:
                    results[key] = 'skipped'
                    continue
                outcome = await probe.purge_raw(client, plan.config.cache_base_url, key,
                                                generation=plan.generation)
                results[key] = outcome
                if outcome == 'purged':
                    freed += size
                if outcome == 'error (HTTP 409)':
                    return dict(results=results, observed_removed_bytes=freed,
                                stopped='nginx generation changed')
            except CleanupChanged as exc:
                return dict(results=results, observed_removed_bytes=freed, stopped=str(exc))
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                results[key] = f'error ({exc})'
    return dict(results=results, observed_removed_bytes=freed, stopped=None)


async def automatic_cleanup(config: Config, store: ServiceState,
                            *, config_path: str | Path | None = None) -> None:
    """Bound an hourly pass and rotate through keys without a full cache inventory."""
    if not enabled(config) or not store.dedup_cleanup_lock.acquire(blocking=False):
        return
    try:
        plan = await asyncio.to_thread(make_plan, config, store)
        keys = list(plan.candidates)
        if not keys:
            store.dedup_cleanup_cursor = ''
            return
        offset = bisect_right(keys, store.dedup_cleanup_cursor)
        batch = (keys[offset:] + keys[:offset])[:AUTOMATIC_BATCH]
        result = await remove_copies(plan, store, batch, config_path=config_path)
        if result['results']:
            store.dedup_cleanup_cursor = next(reversed(result['results']))
        logger.info('dedup cleanup: %d purged, %d checked, %s observed bytes removed%s',
                    sum(v == 'purged' for v in result['results'].values()),
                    len(result['results']), result['observed_removed_bytes'],
                    f"; stopped: {result['stopped']}" if result['stopped'] else '')
    except (CleanupChanged, httpx.HTTPError) as exc:
        logger.info('dedup cleanup deferred: %s', exc)
    finally:
        store.dedup_cleanup_lock.release()
