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
from repowatch.gpgverify import SignatureError, soonest_key_expiry
from repowatch.notifications import emit, record_failure_and_maybe_notify, record_success_and_maybe_notify
from repowatch.parsers import PARSERS
from repowatch.parsers.base import IndexHeadResult
from repowatch.prefetch import purge_removed, purge_selected, warm_cache
from repowatch import cache_probe
from repowatch.state import StateStore

logger = logging.getLogger(__name__)

# How often the scheduler "ticks" — each tick checks which repositories are
# already due (by their own effective_check_interval), rather than running
# all of them at once on a single global check_interval (see check_all).
_SCHEDULER_TICK_SECONDS = 10
# Cleanup of repo_events/request_events — a timer independent of the
# scheduler tick: once an hour is enough, no need to run DELETE every 10s.
_PRUNE_INTERVAL_SECONDS = 3600


async def _check_key_expiry(config: Config, repo: RepoConfig, store: StateStore) -> None:
    """Trust state (docs_dev/ROADMAP.md item 20) — runs unconditionally at
    the very start of every check cycle, independent of whether the index
    itself turns out to be unchanged: a quiet repository that rarely
    changes must still get its signing key's expiry reassessed on
    schedule, not only when there happens to be a new snapshot to record.

    apk is excluded — its embedded RSA keys (see apkverify.py) have no
    expiry concept at all, unlike apt/pacman/dnf/apt-rpm's GPG keys.
    Notification reuses the same (repo_id, kind) consecutive-streak
    mechanism as repeated warm/gpg-verification failures (kind=
    "key_expiry") — "still within the warning window" behaves exactly like
    "still failing" for that purpose: notify once when first crossed, once
    more on recovery (renewed past the threshold), silent in between.
    """
    if repo.verify_signature and repo.type not in ("apk", "nix") and repo.keyring_path:
        expires_at = await asyncio.to_thread(soonest_key_expiry, repo.keyring_path)
    else:
        expires_at = None
    store.record_key_expiry(repo.id, expires_at)

    if expires_at is None:
        # Unknown expiry is not evidence of recovery. Preserve the streak
        # until a known expiry outside the warning window is observed.
        return
    remaining_days = (datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)).days
    if remaining_days < config.key_expiry_warning_days:
        await record_failure_and_maybe_notify(
            config, store, repo.id, "key_expiry",
            f"soonest key in keyring expires {expires_at} ({remaining_days} day(s) left)",
        )
    else:
        await record_success_and_maybe_notify(config, store, repo.id, "key_expiry")


async def check_repo(config: Config, repo: RepoConfig, store: StateStore) -> None:
    await _check_key_expiry(config, repo, store)

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

        # Signed indexes are reverified each cycle: an unchanged HTTP
        # validator says nothing about changed local trust keys/backend. This
        # also covers enabling verification over an existing unsigned snapshot.
        if head.unchanged and not repo.verify_signature:
            store.touch_last_check(repo.id)
            await refresh_replacements(config, repo, store)
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
    if repo.type != "nix":
        await record_success_and_maybe_notify(config, store, repo.id, "gpg")

    diff = store.record_snapshot(
        snapshot, index_etag=head.etag, index_last_modified=head.last_modified
    )

    if diff.changed:
        await emit(config, 'repository.changed', repo.id, {
            'added': len(diff.new_packages), 'removed': len(diff.removed_packages),
            'modified': len(diff.modified_packages), 'packages': len(snapshot.packages)})

    await refresh_replacements(config, repo, store)

    if repo.type == 'nix':
        store.update_nix_trust(repo.id, repo.verify_signature, repo.nix_public_keys)
        # A missing binary or failed artifact needs a retry even if the source
        # catalog has not changed. Successful roots retain normal warm policy.
        warmed = {item['package_key']: item['status'] for item in store.get_warmed_packages(repo.id)}
        retry = {key: filename for key, filename in snapshot.packages.items() if warmed.get(key) != 'ok'}
        await warm_cache(config, repo, store, retry)
        if diff.removed_packages and config.nginx.enable_purge:
            from repowatch.nix_cache import purge
            await purge(config, repo, store, diff.removed_filenames)
        logger.info('%s: Nix catalog checked (%d outputs, changed=%s)', repo.id, len(snapshot.packages), diff.changed)
        return

    if not diff.changed:
        logger.debug("%s: no changes (%d packages)", repo.id, len(snapshot.packages))
        return

    logger.info(
        "%s: changes — %d new, %d removed, %d modified",
        repo.id,
        len(diff.new_packages),
        len(diff.removed_packages),
        len(diff.modified_packages),
    )

    if diff.new_packages:
        new_packages = {key: snapshot.packages[key] for key in diff.new_packages}
        await warm_cache(config, repo, store, new_packages)

    if diff.removed_packages:
        # Active proxy_cache eviction (docs_dev/ROADMAP.md item 24) — a
        # no-op unless nginx.enable_purge is set (see purge_removed), so
        # this doesn't change behavior for any config that hasn't opted in.
        await purge_removed(config, repo, diff.removed_filenames)


async def refresh_replacements(config: Config, repo: RepoConfig, store: StateStore) -> None:
    """Retry replacements even on unchanged indexes; never warm over an unpurged HIT."""
    pending = store.get_pending_replacements(repo.id)
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
        store.finish_replacement(repo.id, key, item['revision'], error)


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


async def _purge_and_unwarm_stale(
    config: Config, store: StateStore, stale: list[tuple[str, str, str]]
) -> None:
    """Auto-unwarm's purge step (docs_dev/ROADMAP.md item 33) — same
    principle the user asked for on api.remove_warmed_package_payload
    (2026-09-13): a warmed_packages row ageing out because nothing's asked
    for it in warmed_retention_days shouldn't just lose its bookkeeping,
    it should lose the real cache entry too — otherwise the file becomes
    an orphan invisible to future stale-scans (the exact problem item 33
    is about), just via the automatic path instead of a manual click.

    Grouped by repo_id since prefetch.purge_selected() is a per-repo call.
    Same conservative rule as remove_warmed_package_payload: the
    bookkeeping row is only dropped for keys nginx confirms are gone
    ("purged"/"not_cached") — an errored key keeps its (still-stale)
    warmed_at, so the next hourly prune_all cycle naturally retries it,
    same as a stuck purge would for any other mechanism here.

    A repo_id no longer in config.repos (removed since the row was
    written) has no RepoConfig to purge against at all — nothing more this
    function can do for those, so their bookkeeping just drops. Finding
    THAT class of orphan is cache_probe's full inventory scan's job (see
    docs_dev/NGINX.md), not this one.
    """
    by_repo: dict[str, dict[str, str]] = {}
    for repo_id, key, filename in stale:
        by_repo.setdefault(repo_id, {})[key] = filename

    for repo_id, items in by_repo.items():
        repo = config.repo_by_id(repo_id)
        if repo is None:
            store.remove_warmed_packages(repo_id, list(items))
            continue
        if repo.type == 'nix':
            from repowatch.nix_cache import purge
            results = await purge(config, repo, store, items)
        else:
            results = await purge_selected(config, repo, items)
        resolved = [key for key, outcome in results.items() if outcome in ("purged", "not_cached", "retained_shared")]
        if resolved:
            store.remove_warmed_packages(repo_id, resolved)
        errored = len(items) - len(resolved)
        if errored:
            logger.warning(
                "%s: %d stale warmed package(s) failed to purge, will retry next cycle",
                repo_id, errored,
            )


async def prune_all(config: Config, store: StateStore) -> None:
    pruned = store.prune_events(config.event_retention_days)
    if pruned:
        logger.debug("cleaned up %d stale repo_events rows", pruned)

    pruned_requests = store.prune_requests(config.request_retention_days)
    if pruned_requests:
        logger.debug("cleaned up %d stale request_events rows", pruned_requests)

    if not config.syslog_listener.enabled:
        # Without real client-request visibility, warmed_at only ever
        # advances at first warm (see StateStore.prune_warmed_packages'
        # own docstring) — ageing rows out here would silently drop
        # bookkeeping (and, worse, purge real cache entries below) for
        # packages real clients might still be actively using, purely
        # because repowatch has no way to know either way. Skip the whole
        # warmed_packages retention step entirely rather than guess.
        logger.debug("syslog_listener disabled — skipping warmed_packages expiry (no request visibility)")
    elif config.nginx.enable_purge:
        stale = store.get_stale_warmed_packages(config.warmed_retention_days)
        if stale:
            await _purge_and_unwarm_stale(config, store, stale)
    else:
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
