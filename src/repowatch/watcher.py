"""Orchestration: for each repository — download the index, diff it against
the previous snapshot, save the state, and warm the cache with new packages
if needed.

This is the one place that knows about all the components at once
(config, parsers, state, prefetch) — the components themselves don't know
about each other.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from repowatch.config import Config, ConfigError, RepoConfig, load_config
from repowatch.gpgverify import SignatureError
from repowatch.notifications import record_failure_and_maybe_notify, record_success_and_maybe_notify
from repowatch.parsers import PARSERS
from repowatch.parsers.base import IndexHeadResult
from repowatch.prefetch import purge_removed, warm_cache
from repowatch.state import StateStore

logger = logging.getLogger(__name__)

# How often the scheduler "ticks" — each tick checks which repositories are
# already due (by their own effective_check_interval), rather than running
# all of them at once on a single global check_interval (see check_all).
_SCHEDULER_TICK_SECONDS = 10
# Cleanup of repo_events/request_events — a timer independent of the
# scheduler tick: once an hour is enough, no need to run DELETE every 10s.
_PRUNE_INTERVAL_SECONDS = 3600


async def check_repo(config: Config, repo: RepoConfig, store: StateStore) -> None:
    parser_cls = PARSERS[repo.type]
    parser = parser_cls(repo)

    prev_etag, prev_last_modified = store.get_index_meta(repo.id)

    # One httpx client for this repository's whole check (HEAD + index GET
    # + a possible InRelease GET for apt) — reuses a connection instead of
    # opening a new one per request.
    async with httpx.AsyncClient() as client:
        try:
            head = await parser.check_index_changed(client, prev_etag, prev_last_modified)
        except Exception:
            # HEAD is only an optimization; if it fails on its own, that
            # shouldn't block the real check — just download the full index.
            logger.warning(
                "%s: index HEAD check failed, downloading in full", repo.id, exc_info=True
            )
            head = IndexHeadResult(unchanged=False, etag=None, last_modified=None)

        # Signed APK indexes are reverified each cycle: an unchanged HTTP
        # validator says nothing about changed local trust keys/backend. This
        # also covers enabling verification over an existing unsigned snapshot.
        if head.unchanged and not (repo.type == 'apk' and repo.verify_signature):
            store.touch_last_check(repo.id)
            logger.debug(
                "%s: index unchanged (ETag/Last-Modified), download skipped", repo.id
            )
            return

        try:
            snapshot = await parser.fetch(client)
        except SignatureError as exc:
            # A separate branch (not the generic except Exception below) —
            # this is currently the only index-check error we can notify
            # about (see notifications.py); other failures stay in the logs
            # only.
            logger.warning("%s: index signature/integrity check failed: %s", repo.id, exc)
            await record_failure_and_maybe_notify(config, store, repo.id, "gpg", str(exc))
            return
        except Exception:
            logger.exception("failed to fetch index for %s, skipping this cycle", repo.id)
            return

    # No condition on repo.verify_signature here: AptParser now
    # cross-checks the SHA256 of Packages.gz against InRelease (by-hash, see
    # parsers/apt.py), and DnfParser checks primary via repomd.xml's
    # checksum. Both can raise SignatureError even when
    # verify_signature=False — if the reset were gated on
    # verify_signature=True only, the "gpg" streak for such a repository
    # would never clear after a failure (a regression found and reproduced
    # by hand: failure_state stayed untouched after a successful cycle).
    # reset_failure is a cheap SELECT with no write for repositories that
    # never had a streak.
    await record_success_and_maybe_notify(config, store, repo.id, "gpg")

    diff = store.record_snapshot(
        snapshot, index_etag=head.etag, index_last_modified=head.last_modified
    )

    if not diff.changed:
        logger.debug("%s: no changes (%d packages)", repo.id, len(snapshot.packages))
        return

    logger.info(
        "%s: changes — %d new, %d removed",
        repo.id,
        len(diff.new_packages),
        len(diff.removed_packages),
    )

    if diff.new_packages:
        new_packages = {key: snapshot.packages[key] for key in diff.new_packages}
        await warm_cache(config, repo, store, new_packages)

    if diff.removed_packages:
        # Active proxy_cache eviction (docs_dev/ROADMAP.md item 24) — a
        # no-op unless nginx.enable_purge is set (see purge_removed), so
        # this doesn't change behavior for any config that hasn't opted in.
        await purge_removed(config, repo, diff.removed_filenames)


def _is_due(config: Config, repo: RepoConfig, store: StateStore) -> bool:
    """Per-repository timers: repo.check_interval overrides the global
    config.check_interval (see Config.effective_check_interval)."""
    status = store.get_status(repo.id)
    if not status or not status.get("last_check"):
        return True  # never checked — definitely due

    last_check = datetime.fromisoformat(status["last_check"])
    interval = config.effective_check_interval(repo)
    return (datetime.now(timezone.utc) - last_check).total_seconds() >= interval


async def check_all(config: Config, store: StateStore) -> None:
    """Checks every repository that's due (see _is_due) concurrently — not
    one at a time as before: a slow/hung upstream for one repo no longer
    delays checking the rest in the same tick. Parallelism is bounded by
    config.check_concurrency (asyncio.Semaphore) — not "all at once", so a
    large number of repositories doesn't overwhelm the process.

    check_repo() swallows its own exceptions (see its try/except) — gather()
    doesn't need return_exceptions=True; one failing repository doesn't
    cancel or fail the check of the rest, same as the old sequential loop.
    """
    semaphore = asyncio.Semaphore(config.check_concurrency)

    async def _run(repo: RepoConfig) -> None:
        async with semaphore:
            await check_repo(config, repo, store)

    due_repos = [repo for repo in config.repos if _is_due(config, repo, store)]
    if due_repos:
        await asyncio.gather(*(_run(repo) for repo in due_repos))


def prune_all(config: Config, store: StateStore) -> None:
    pruned = store.prune_events(config.event_retention_days)
    if pruned:
        logger.debug("cleaned up %d stale repo_events rows", pruned)

    pruned_requests = store.prune_requests(config.request_retention_days)
    if pruned_requests:
        logger.debug("cleaned up %d stale request_events rows", pruned_requests)

    pruned_warmed = store.prune_warmed_packages(config.warmed_retention_days)
    if pruned_warmed:
        logger.debug("cleaned up %d stale warmed_packages rows", pruned_warmed)

    # Size-based — on top of the time-based cleanup above, only if the
    # operator set a limit.
    if config.event_max_rows_per_repo is not None:
        pruned_by_size = store.prune_events_by_size(config.event_max_rows_per_repo)
        if pruned_by_size:
            logger.debug(
                "cleaned up %d repo_events rows over the %d-row-per-repo limit",
                pruned_by_size, config.event_max_rows_per_repo,
            )

    if config.request_max_rows is not None:
        pruned_requests_by_size = store.prune_requests_by_size(config.request_max_rows)
        if pruned_requests_by_size:
            logger.debug(
                "cleaned up %d request_events rows over the global %d-row limit",
                pruned_requests_by_size, config.request_max_rows,
            )


async def run_forever(
    config_path: str, store: StateStore, initial_config: Config | None = None,
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

    `stop` is optional and unused by both real callers (cli.py's `run` and
    `supervise` commands loop forever by design) — it exists so tests can
    stop this loop after a bounded number of iterations instead of relying
    on an exception to unwind out of `while True`, the same pattern already
    used by supervisor.py's backup/nginx loops."""
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
            prune_all(config, store)
            last_prune = time.monotonic()

        elapsed = time.monotonic() - started
        sleep_for = max(0.0, _SCHEDULER_TICK_SECONDS - elapsed)
        logger.debug("cycle took %.1fs, sleeping %.1fs", elapsed, sleep_for)
        await asyncio.sleep(sleep_for)
