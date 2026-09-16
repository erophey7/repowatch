"""Invalidate and refresh packages whose content changed at the same path."""

from __future__ import annotations

import logging
import repowatch.cache.probe as cache_probe
from repowatch.cache.purge import purge_selected
from repowatch.config.models import Config, RepoConfig
from repowatch.operations.warm import warm_cache
from repowatch.runtime.context import ServiceState

logger = logging.getLogger(__name__)


async def refresh_replacements(config: Config, repo: RepoConfig, store: ServiceState) -> None:
    """Retry replacements even on unchanged indexes; never warm over an unpurged HIT."""
    pending = store.cache.get_pending_replacements(repo.id)
    if not pending:
        return
    from repowatch.warming_policy import WarmingPolicy
    policy = WarmingPolicy(repo, store)
    for item in pending:
        key = item['package_key']
        error = None
        if not config.nginx.enable_purge:
            error = 'replacement requires nginx.enable_purge; cache refresh is pending'
        else:
            for target_id, filename in item['targets']:
                if target_id != repo.id and not config.nginx.enable_dedup:
                    continue
                if not filename:
                    continue
                target = config.repo_by_id(target_id)
                if target is None:
                    error = 'replacement purge target is unavailable'
                    break
                purge = cache_probe.purge_selected_raw if config.nginx.enable_cache_probe else purge_selected
                results = await purge(config, target, {key: filename})
                if results[key] not in ('purged', 'not_cached'):
                    error = results[key]
                    break
        if error is None and item['filename'] and repo.prefetch and policy.allows(key):
            warmed = await warm_cache(
                config, repo, store, {key: item['filename']},
                expected_hashes={key: item['content_hash']} if item['content_hash'] else None)
            if not warmed or not warmed.get(key):
                error = 'replacement warm failed; retry on next check'
        if error:
            logger.warning('%s: %s: %s', repo.id, key, error)
        store.cache.finish_replacement(repo.id, key, item['revision'], error)
