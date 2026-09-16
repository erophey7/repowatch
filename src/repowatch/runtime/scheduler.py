"""Schedule repository checks and periodic retention work."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone
from repowatch.config.load import load_config
from repowatch.config.models import Config, RepoConfig
from repowatch.errors import ConfigError
from repowatch.operations.check import check_repo
from repowatch.operations.cleanup import prune_all
from repowatch.runtime.context import ServiceState

logger = logging.getLogger(__name__)


# How often the scheduler "ticks" — each tick checks which repositories are
# already due (by their own effective_check_interval), rather than running
# all of them at once on a single global check_interval (see check_all).
_SCHEDULER_TICK_SECONDS = 10

# Cleanup of repo_events/request_events — a timer independent of the
# scheduler tick: once an hour is enough, no need to run DELETE every 10s.
_PRUNE_INTERVAL_SECONDS = 3600


def _is_due(config: Config, repo: RepoConfig, store: ServiceState,
            *, summaries: dict[str, dict] | None = None) -> bool:
    """Per-repository timers: repo.check_interval overrides the global
    config.check_interval (see Config.effective_check_interval)."""
    status = (summaries if summaries is not None else store.repositories.get_repo_summaries()).get(repo.id)
    if not status or not status.get("last_check"):
        return True  # never checked — definitely due

    last_check = datetime.fromisoformat(status["last_check"])
    interval = config.effective_check_interval(repo)
    return (datetime.now(timezone.utc) - last_check).total_seconds() >= interval


async def check_all(config: Config, store: ServiceState) -> list[str]:
    """Checks every repository that's due (see _is_due) concurrently — not
    one at a time as before: a slow/hung upstream for one repo no longer
    delays checking the rest in the same tick. Parallelism is bounded by
    config.check_concurrency (asyncio.Semaphore) — not "all at once", so a
    large number of repositories doesn't overwhelm the process.

    Per-repository operation failures are logged and returned as repository ids.
    SQLite failures are fatal because all repositories share that database.
    Cancellation and fatal failures cancel and drain all in-flight checks.
    """
    semaphore = asyncio.Semaphore(config.check_concurrency)
    failed = []
    summaries = store.repositories.get_repo_summaries()

    async def _run(repo: RepoConfig) -> None:
        async with semaphore:
            try:
                if _is_due(config, repo, store, summaries=summaries):
                    await check_repo(config, repo, store)
            except sqlite3.Error:
                raise
            except Exception:
                logger.exception("repository operation failed for %s", repo.id)
                failed.append(repo.id)

    tasks = [asyncio.create_task(_run(repo)) for repo in config.repos]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return failed



async def run_forever(
    config_path: str, store: ServiceState, initial_config: Config | None = None,
    *, stop: asyncio.Event | None = None,
) -> None:
    """Re-reads config.yaml at the start of every cycle — that's how
    repositories added through the dashboard get picked up without a
    process restart. If the file happens to be invalid at read time (e.g.
    someone is editing it by hand mid-write), the last known valid config is
    used instead — an error here shouldn't take down the daemon.

    Ticks every _SCHEDULER_TICK_SECONDS (not once per config.check_interval)
    — check_all() itself decides which repositories are already due by
    their own interval, so the global check_interval here is just the
    default for repositories without their own override.

    `stop` is optional and unused by both real callers (cli/main.py's `run` and
    `supervise` commands loop forever by design) — it exists so tests can
    stop this loop after a bounded number of iterations instead of relying
    on an exception to unwind out of `while True`, the same pattern already
    used by runtime/service.py's backup/nginx loops."""
    store.bandwidth.bind(config_path)
    config = initial_config or load_config(config_path)
    logger.info(
        "repowatch started: %d repositories, default interval %ds (overridable per repo)",
        len(config.repos),
        config.check_interval,
    )
    last_prune = 0.0
    while stop is None or not stop.is_set():
        started = time.monotonic()
        try:
            config = load_config(config_path)
        except ConfigError:
            logger.warning(
                "failed to reload %s, keeping the previous valid config",
                config_path,
                exc_info=True,
            )

        await check_all(config, store)

        if time.monotonic() - last_prune >= _PRUNE_INTERVAL_SECONDS:
            await prune_all(config, store)
            last_prune = time.monotonic()

        elapsed = time.monotonic() - started
        sleep_for = max(0.0, _SCHEDULER_TICK_SECONDS - elapsed)
        logger.debug("cycle took %.1fs, sleeping %.1fs", elapsed, sleep_for)
        await asyncio.sleep(sleep_for)
