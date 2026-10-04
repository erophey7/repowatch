"""Administrator preview and bounded removal of redundant physical dedup copies."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from repowatch.cache import probe
from repowatch.config.models import Config
from repowatch.operations.dedup_cleanup import CleanupChanged, check_current, enabled, make_plan, remove_copies
from repowatch.runtime.context import ServiceState
from repowatch.storage.access import AdminSession
from repowatch.web.access import load_admin_config, load_request_config


async def _preview(config: Config, store: ServiceState, config_path: str | Path) -> dict:
    """Use one inventory to discover actual obsolete copies with present targets."""
    revisions = store.repositories.get_snapshot_revisions()
    async with httpx.AsyncClient() as client:
        version = await probe.probe(client, config.cache_base_url, 'repowatch-generation-check')
    inventory = await probe.full_inventory(config.cache_base_url)
    if inventory.failed_leaves or any(e.error or not e.key for e in inventory.entries):
        raise CleanupChanged('Cache inventory is incomplete; retry before selecting copies to remove.')
    present = {e.key: e for e in inventory.entries}
    plan = make_plan(config, store, selected=set(present))
    if version.generation != plan.generation or revisions != plan.revisions:
        raise CleanupChanged('Catalog or nginx generation changed; retry the scan.')
    rows = []
    for key, row in plan.candidates.items():
        if key in present and row['canonical_key'] in present:
            rows.append({**row, 'bytes': present[key].size})
    check_current(plan, store, config_path)
    return dict(generation=plan.generation, candidates=rows[:1000], total_candidates=len(rows),
                total_bytes=sum(r['bytes'] or 0 for r in rows), truncated=len(rows) > 1000)


def dedup_cleanup_payload(config_path: str | Path, store: ServiceState, *,
                          current: Config | None = None, admin_session: AdminSession | None = None,
                          body: dict | None = None) -> tuple[int, dict]:
    """Preview on GET; rederive and revalidate each selected obsolete key on POST."""
    config, error = (load_admin_config(config_path, admin_session) if body is not None
                     else load_request_config(config_path, current))
    if error is not None:
        return error
    if not enabled(config):
        return 409, {'error': 'Enable nginx dedup, purge and cache probing, then apply nginx configuration.'}
    if body is not None:
        if not isinstance(body, dict):
            return 400, {'error': 'Expected a JSON object.'}
        keys = body.get('keys')
        if (not isinstance(keys, list) or not 1 <= len(keys) <= 256
                or any(not isinstance(k, str) or len(k) > 4096 for k in keys)
                or not isinstance(body.get('generation'), str)):
            return 400, {'error': 'Expected generation and 1..256 cache keys from the preview.'}
    if not store.dedup_cleanup_lock.acquire(blocking=False):
        return 409, {'error': 'A dedup cleanup or preview is already running.'}
    try:
        if body is None:
            return 200, asyncio.run(_preview(config, store, config_path))
        plan = make_plan(config, store, selected=set(keys))
        if plan.generation != body['generation']:
            raise CleanupChanged('Dedup routing changed since preview; scan again.')
        return 200, asyncio.run(remove_copies(plan, store, list(dict.fromkeys(keys)), config_path=config_path))
    except CleanupChanged as exc:
        return 409, {'error': str(exc)}
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        return 502, {'error': f'Cache inspection failed: {exc}'}
    finally:
        store.dedup_cleanup_lock.release()
