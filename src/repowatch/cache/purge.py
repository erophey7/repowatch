"""Send per-repository cache purge requests and report individual outcomes."""

from __future__ import annotations

import asyncio
import httpx
import logging
from repowatch.config.models import Config, RepoConfig
from repowatch.parsers.base import USER_AGENT
from repowatch.routing import purge_url

logger = logging.getLogger(__name__)


async def purge_removed(
    config: Config, repo: RepoConfig, removed: dict[str, str],
) -> None:
    """Purge removed package paths when enable_purge is on; otherwise do nothing.

    Log HTTP statuses >= 400 and request failures without aborting other items.
    Results are not returned to the watcher. Requests use the same bounded
    transport and User-Agent as manual purge, without bandwidth pacing.
    """
    if not config.nginx.enable_purge or not removed:
        return
    await _purge_batch(config, repo, removed, log_failures=True)


async def purge_selected(config: Config, repo: RepoConfig, items: dict[str, str]) -> dict[str, str]:
    """Return per-key purged/not_cached/error outcomes without logging failures.

    The caller validates enable_purge and derives filenames from stored state.
    Only HTTP 200 means purged and 404 means absent; other responses are errors.
    Do not add an upstream HEAD probe: nginx cache keys omit the request method,
    so a cached bodyless response could affect a later client GET.
    """
    return await _purge_batch(config, repo, items, log_failures=False)


async def _purge_batch(config: Config, repo: RepoConfig, items: dict[str, str], *,
                       log_failures: bool) -> dict[str, str]:
    """Run bounded purge requests; automatic callers also log HTTP/request failures."""
    if not items:
        return {}
    semaphore = asyncio.Semaphore(config.prefetch_concurrency)
    results: dict[str, str] = {}

    async def _run(client: httpx.AsyncClient, key: str, filename: str) -> None:
        url = purge_url(config, repo, filename)
        async with semaphore:
            try:
                resp = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
                if log_failures and resp.status_code >= 400:
                    logger.warning("%s: purge failed (HTTP %s) for %s", repo.id, resp.status_code, filename)
                if resp.status_code == 200:
                    results[key] = "purged"
                elif resp.status_code == 404:
                    results[key] = "not_cached"
                else:
                    results[key] = f"error (HTTP {resp.status_code})"
            except httpx.RequestError as exc:
                if log_failures:
                    logger.warning("%s: purge failed (%s) for %s", repo.id, exc, filename)
                results[key] = f"error ({exc})"

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(_run(client, key, filename) for key, filename in items.items()))
    return results
