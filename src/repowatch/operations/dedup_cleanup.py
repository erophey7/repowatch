"""Remove obsolete physical copies while retaining current canonical cache keys."""

from __future__ import annotations

import asyncio
from bisect import insort
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import time

import httpx

from repowatch.cache import probe
from repowatch.cache.dedup import accepted_pairs, dedup_keys
from repowatch.config.load import load_config
from repowatch.config.models import Config
from repowatch.errors import ConfigError
from repowatch.nginx.render import dedup_generation
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
    cursor: str = ""
    priority_cursor: str = ""
    scanned: int = 0


def enabled(config: Config) -> bool:
    """Require all three explicit nginx features used by safe cleanup."""
    nginx = config.nginx
    return nginx.enabled and nginx.enable_dedup and nginx.enable_purge and nginx.enable_cache_probe


class _Window:
    """Keep only the next bounded set of distinct keys, wrapping after a cursor."""

    def __init__(self, limit: int | None, after: str):
        """Set the maximum retained keys and the lexical wrap-around cursor."""
        self.limit, self.after = limit, after
        self.ranks: list[tuple[bool, str]] = []
        self.rows: dict[str, dict] = {}

    def add(self, key: str, row: dict) -> None:
        """Retain the earliest distinct keys after the cursor within the limit."""
        if self.limit == 0 or key in self.rows:
            return
        if self.limit is None:
            self.rows[key] = row
            return
        rank = (key <= self.after, key)
        if self.limit is not None and len(self.rows) >= self.limit:
            if rank >= self.ranks[-1]:
                return
            _, removed = self.ranks.pop()
            del self.rows[removed]
        insort(self.ranks, rank)
        self.rows[key] = row

    def cursor(self) -> str:
        """Return the last selected key, including a window that wraps around."""
        return self.ranks[-1][1] if self.ranks else self.after


def _evidence(config: Config, store: ServiceState, builder: CacheKeyBuilder, redirects: dict):
    """Stream physical routes and blockers, including owners outside a selected batch."""
    repos = {repo.id: repo for repo in config.repos}
    bound = {repo.id: builder.for_repo(repo) for repo in config.repos}
    identities = {repo.id: repo.catalog_identity() for repo in config.repos}
    for repo_id, package, filename, state, pending, warmed, artifact in store.queries.cleanup_rows(sorted(repos)):
        repo, key_for = repos[repo_id], bound[repo_id]
        if artifact:
            if repo.type == 'nix':
                yield repo_id, filename, set(), key_for(filename), False, False, False
            continue
        direct = key_for(filename)
        target = redirects.get(package_path(repo, filename).lower(), direct)
        keys = {direct, key_for(filename, dedup=not config.nginx.enable_dedup)}
        blocked = not state[0] or state[1] != identities[repo_id] or pending or not target
        yield repo_id, filename, keys, target, blocked, repo.type != 'nix', warmed


def make_plan(config: Config, store: ServiceState, *, limit: int | None = None,
              after: str = '', priority_after: str = '', selected: set[str] | None = None,
              excluded: set[str] | None = None) -> CleanupPlan:
    """Select a bounded window, then validate it against every streamed owner.

    Selected keys are only a filter, never evidence of safe deletion. No network
    request runs while a database read snapshot is open.
    """
    if limit is not None and limit < 1:
        raise ValueError('cleanup limit must be positive')
    revisions = store.repositories.get_snapshot_revisions()
    rows = store.cache.find_duplicate_files()
    pairs = accepted_pairs(config, store.cache, rows)
    builder = CacheKeyBuilder(config)
    redirects = dedup_keys(config, rows, builder, pairs=pairs)
    generation = dedup_generation(config, pairs)
    del rows, pairs
    fair = _Window(limit, after)
    priority = _Window(0 if limit is None else limit - max(1, limit // 2), priority_after)
    scanned = 0
    for repo_id, filename, keys, target, blocked, eligible, warmed in _evidence(config, store, builder, redirects):
        scanned += 1
        if blocked or not eligible:
            continue
        for key in keys - {target}:
            if ((selected is not None and key not in selected) or (excluded and key in excluded)
                    or not re.fullmatch(r'[A-Za-z0-9_./:+~-]+', key)):
                continue
            row = dict(key=key, repo_id=repo_id, filename=filename, canonical_key=target)
            fair.add(key, row)
            if warmed:
                priority.add(key, row)
    possible = dict(priority.rows)
    fair_cursor = after
    for _, key in fair.ranks if limit is not None else ((False, k) for k in fair.rows):
        if limit is not None and len(possible) >= limit:
            break
        possible.setdefault(key, fair.rows[key])
        fair_cursor = key
    watched = set(possible) | {row['canonical_key'] for row in possible.values()}
    unsafe, active, conflicts, attributed = set(), set(), set(), set()
    for repo_id, filename, keys, target, blocked, eligible, warmed in _evidence(config, store, builder, redirects):
        if target in possible:
            active.add(target)  # Still actively routed, including Nix closure artifacts.
        if blocked:
            unsafe.update((keys | {target}) & watched)
        if eligible and not blocked:
            for key in keys & possible.keys():
                if key != target and key not in attributed:
                    # Priority may select a later owner. Keep the same stable
                    # display attribution as a complete lexicographic scan.
                    possible[key].update(repo_id=repo_id, filename=filename)
                    attributed.add(key)
                if key != target and possible[key]['canonical_key'] != target:
                    conflicts.add(key)  # Multiple canonical owners must never be guessed.
    invalid = unsafe | active | conflicts
    candidates = {key: possible[key] for key in sorted(possible)
                  if key not in invalid and possible[key]['canonical_key'] not in unsafe}
    if revisions != store.repositories.get_snapshot_revisions():
        raise CleanupChanged('Repository catalogs changed; retry the cleanup.')
    return CleanupPlan(config, revisions, generation, candidates, fair_cursor, priority.cursor(), scanned)


def check_current(plan: CleanupPlan, store: ServiceState, config_path: str | Path | None) -> None:
    """Refuse stale evidence before any deletion, including YAML edits."""
    try:
        current = load_config(config_path) if config_path is not None else plan.config
    except ConfigError as exc:
        raise CleanupChanged('Configuration is invalid; cleanup stopped.') from exc
    if (current != plan.config or plan.revisions != store.repositories.get_snapshot_revisions()):
        raise CleanupChanged('Configuration or repository catalogs changed; retry the cleanup.')


async def inspect_pair(client: httpx.AsyncClient, plan: CleanupPlan, row: dict) -> tuple[bool, int, str]:
    """Require both files and the expected applied nginx generation; never GET packages."""
    source = await probe.probe(client, plan.config.cache_base_url, row['key'])
    canonical = await probe.probe(client, plan.config.cache_base_url, row['canonical_key'])
    if source.generation != plan.generation or canonical.generation != plan.generation:
        raise CleanupChanged('nginx has not applied this dedup generation; retry after reconciliation.')
    reason = 'source_missing' if not source.exists else 'canonical_missing' if not canonical.exists else 'ready'
    return source.exists and canonical.exists, source.size or 0, reason


async def remove_copies(plan: CleanupPlan, store: ServiceState, keys: list[str],
                        *, config_path: str | Path | None = None) -> dict:
    """Recheck each candidate and purge only its obsolete key, never warm bookkeeping."""
    results = {}
    freed = 0
    absent = []
    counts = dict(examined=0, source_missing=0, canonical_missing=0, eligible=0, purged=0)

    def finish(stopped=None):
        """Preserve partial outcomes, probe counters and bounded absence evidence."""
        return dict(results=results, observed_removed_bytes=freed, stopped=stopped,
                    counts=counts, absent_keys=absent)

    started = time.monotonic()
    async with httpx.AsyncClient() as client:
        for key in keys:
            if time.monotonic() - started >= 30:
                return finish('time budget reached')
            row = plan.candidates.get(key)
            if row is None:
                results[key] = 'skipped'
                continue
            try:
                check_current(plan, store, config_path)
                ready, size, reason = await inspect_pair(client, plan, row)
                counts['examined'] += 1
                if reason == 'source_missing':
                    absent.append(key)
                if reason != 'ready':
                    counts[reason] += 1
                check_current(plan, store, config_path)
                if not ready:
                    results[key] = 'skipped'
                    continue
                counts['eligible'] += 1
                outcome = await probe.purge_raw(client, plan.config.cache_base_url, key,
                                                generation=plan.generation)
                results[key] = outcome
                if outcome == 'purged':
                    freed += size
                    counts['purged'] += 1
                if outcome in ('purged', 'not_cached'):
                    absent.append(key)
                if outcome == 'error (HTTP 409)':
                    return finish('nginx generation changed')
            except CleanupChanged as exc:
                return finish(str(exc))
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                detail = f'{type(exc).__name__}: {exc}'.rstrip(': ')
                results[key] = f'error ({detail})'
    return finish()


async def automatic_cleanup(config: Config, store: ServiceState,
                            *, config_path: str | Path | None = None) -> None:
    """Bound an hourly pass and rotate through keys without a full cache inventory."""
    if not enabled(config) or not store.dedup_cleanup_lock.acquire(blocking=False):
        return
    try:
        evidence = (config, store.repositories.get_snapshot_revisions())
        if evidence != store.dedup_cleanup_evidence:
            store.dedup_cleanup_recent.clear()
            store.dedup_cleanup_pending = None
            # Keep lexical progress: frequently changing catalogs must not
            # starve keys at the end of the scan.
            store.dedup_cleanup_evidence = evidence
        now = time.monotonic()
        recent = store.dedup_cleanup_recent
        for key in list(recent):
            if recent[key] <= now:
                del recent[key]
        pending = store.dedup_cleanup_pending
        if pending is None:
            plan = await asyncio.to_thread(
                make_plan, config, store, limit=AUTOMATIC_BATCH,
                after=store.dedup_cleanup_cursor, priority_after=store.dedup_cleanup_priority_cursor,
                excluded=set(recent))
            next_cursor, next_priority = plan.cursor, plan.priority_cursor
        else:
            next_cursor, next_priority, keys = pending
            plan = await asyncio.to_thread(make_plan, config, store, selected=set(keys))
        result = await remove_copies(plan, store, list(plan.candidates), config_path=config_path)
        remaining = [key for key in plan.candidates if key not in result['results']]
        if remaining and result['stopped']:
            store.dedup_cleanup_pending = (next_cursor, next_priority, remaining)
        else:
            store.dedup_cleanup_pending = None
            store.dedup_cleanup_cursor = next_cursor
            store.dedup_cleanup_priority_cursor = next_priority
        for key in result['absent_keys']:
            recent.pop(key, None)
            recent[key] = time.monotonic() + 6 * 3600
        while len(recent) > 4096:
            del recent[next(iter(recent))]
        logger.info('dedup cleanup: %d purged, %d checked, %s observed bytes removed; '
                    '%d source missing, %d canonical missing, %d eligible, %d catalog rows scanned%s',
                    result['counts']['purged'], result['counts']['examined'], result['observed_removed_bytes'],
                    result['counts']['source_missing'], result['counts']['canonical_missing'],
                    result['counts']['eligible'], plan.scanned,
                    f"; stopped: {result['stopped']}" if result['stopped'] else '')
    except (CleanupChanged, httpx.HTTPError) as exc:
        logger.info('dedup cleanup deferred: %s', exc)
    finally:
        store.dedup_cleanup_lock.release()
