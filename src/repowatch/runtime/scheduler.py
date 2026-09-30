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
from repowatch.operations.slots import RepoSlots
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


def _due_repositories(config: Config, store: ServiceState,
                      repos: list[RepoConfig]) -> tuple[list[RepoConfig], list[str]]:
    """Isolate invalid per-repository timestamps while keeping database failures fatal."""
    summaries = store.repositories.get_repo_summaries()
    due, failed = [], []
    for repo in repos:
        try:
            if _is_due(config, repo, store, summaries=summaries):
                due.append(repo)
        except sqlite3.Error:
            raise
        except Exception:
            logger.exception("failed to determine check interval for %s", repo.id)
            failed.append(repo.id)
    return due, failed


async def _check_one(config: Config, repo: RepoConfig, store: ServiceState,
                     slots: RepoSlots) -> bool:
    """Run one repository operation; False if it failed.

    SQLite failures are fatal because all repositories share that database.
    """
    try:
        await check_repo(config, repo, store, slots=slots)
        return True
    except sqlite3.Error:
        raise
    except Exception:
        logger.exception("repository operation failed for %s", repo.id)
        return False


async def check_all(config: Config, store: ServiceState) -> list[str]:
    """Checks every repository that's due (see _is_due) concurrently, once.

    Used by `check-once`; the daemon uses RepoDispatcher instead. Parallelism
    is bounded by config.check_concurrency through RepoSlots, so a large
    number of repositories doesn't overwhelm the process.

    Per-repository operation failures are logged and returned as repository ids.
    Cancellation and fatal failures cancel and drain all in-flight checks.
    """
    slots = RepoSlots(config.check_concurrency)
    due, failed = _due_repositories(config, store, config.repos)

    tasks = [asyncio.create_task(_check_one(config, repo, store, slots)) for repo in due]
    try:
        results = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return failed + [repo.id for repo, ok in zip(due, results) if not ok]


class RepoDispatcher:
    """One task per repository, started when it is due and never awaited by
    the scheduler tick.

    Cache work cannot consume the reserved index-check capacity. Checks may
    queue behind other checks, but configuration reloads and retention do not
    wait for repository tasks. New repositories are queued on the next tick. A repository never has two tasks at once.
    Removing a repository cancels its task; editing one lets the running
    task finish with the settings it started with, and the next task uses the
    new ones (cancelling would lose the unwarmed remainder of a one-shot diff).
    """

    def __init__(self, store: ServiceState) -> None:
        """Own the live tasks and their shared admission limits."""
        self._store = store
        self._tasks: dict[str, asyncio.Task[bool]] = {}
        self._slots: RepoSlots | None = None

    def reconcile(self, config: Config) -> None:
        """Reap finished tasks, cancel removed repositories, start due ones.

        Raises the SQLite error of a finished task, if any.
        """
        self._reap()
        if self._slots is None:
            self._slots = RepoSlots(config.check_concurrency)
        else:
            self._slots.resize(config.check_concurrency)
        wanted = {repo.id for repo in config.repos}
        for repo_id in [rid for rid in self._tasks if rid not in wanted]:
            task = self._tasks[repo_id]
            if not task.cancelling():
                task.cancel()
        due, _ = _due_repositories(
            config, self._store, [repo for repo in config.repos if repo.id not in self._tasks])
        for repo in due:
            self._tasks[repo.id] = asyncio.create_task(
                _check_one(config, repo, self._store, self._slots))

    def _reap(self) -> None:
        """Observe completed tasks and propagate fatal database failures."""
        for repo_id, task in list(self._tasks.items()):
            if not task.done():
                continue
            del self._tasks[repo_id]
            if not task.cancelled():
                task.result()  # only fatal errors escape _check_one

    async def close(self) -> None:
        """Cancel and drain tasks without interrupting cancellation cleanup twice."""
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


def _reap_prune(task: asyncio.Task[None] | None) -> asyncio.Task[None] | None:
    """Forget a finished prune task; a failed prune is fatal as before."""
    if task is not None and task.done():
        if not task.cancelled():
            task.result()
        return None
    return task


async def run_forever(
    config_path: str, store: ServiceState, initial_config: Config | None = None,
    *, stop: asyncio.Event | None = None,
) -> None:
    """Re-reads config.yaml on every tick — that's how repositories added
    through the dashboard get picked up without a process restart. If the file
    happens to be invalid at read time (e.g. someone is editing it by hand
    mid-write), the last known valid config is used instead — an error here
    shouldn't take down the daemon.

    Ticks every _SCHEDULER_TICK_SECONDS (not once per config.check_interval)
    — RepoDispatcher itself decides which repositories are already due by
    their own interval, so the global check_interval here is just the
    default for repositories without their own override. The tick never waits
    for repository work, and retention runs as its own task on its own timer.

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
    dispatcher = RepoDispatcher(store)
    prune_task: asyncio.Task[None] | None = None
    last_prune = 0.0
    try:
        while stop is None or not stop.is_set():
            try:
                config = load_config(config_path)
            except ConfigError:
                logger.warning(
                    "failed to reload %s, keeping the previous valid config",
                    config_path,
                    exc_info=True,
                )

            dispatcher.reconcile(config)

            prune_task = _reap_prune(prune_task)
            if prune_task is None and time.monotonic() - last_prune >= _PRUNE_INTERVAL_SECONDS:
                prune_task = asyncio.create_task(prune_all(config, store))
                last_prune = time.monotonic()

            await asyncio.sleep(_SCHEDULER_TICK_SECONDS)
    finally:
        if prune_task is not None:
            prune_task.cancel()
            await asyncio.gather(prune_task, return_exceptions=True)
        await dispatcher.close()
