"""Check repository indexes, persist changes, and initiate cache updates."""

from __future__ import annotations

import asyncio
import httpx
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from repowatch.cache.purge import purge_removed
from repowatch.config.models import Config, RepoConfig
from repowatch.errors import SignatureError
from repowatch.notifications import emit, record_failure_and_maybe_notify, record_success_and_maybe_notify
from repowatch.operations.replacements import refresh_replacements
from repowatch.operations.slots import RepoSlots
from repowatch.operations.warm import warm_cache
from repowatch.parsers import PARSERS
from repowatch.parsers.base import IndexHeadResult
from repowatch.runtime.context import ServiceState
from repowatch.verification.gpg import KeyExpiry, signing_key_expiry

logger = logging.getLogger(__name__)


async def _check_key_expiry(config: Config, repo: RepoConfig, store: ServiceState,
                            signers: list[tuple[str, str]] | None = None) -> None:
    """Diagnose actual signers after verification; unknown cannot signal recovery."""
    expiry = KeyExpiry()
    if repo.verify_signature and repo.type not in ("apk", "nix") and repo.keyring_path and signers:
        expiry = await asyncio.to_thread(signing_key_expiry, repo.keyring_path, signers)
    store.repositories.record_key_expiry(repo.id, expiry.expires_at, known=expiry.known)
    if not expiry.known:
        return
    remaining_days = ((datetime.fromisoformat(expiry.expires_at) - datetime.now(timezone.utc)).days
                      if expiry.expires_at else None)
    if remaining_days is not None and remaining_days < config.key_expiry_warning_days:
        await record_failure_and_maybe_notify(
            config, store, repo.id, "key_expiry",
            f"verified signing keys expire {expiry.expires_at} ({remaining_days} day(s) left)",
        )
    else:
        await record_success_and_maybe_notify(config, store, repo.id, "key_expiry")


@dataclass(frozen=True)
class CacheWork:
    """Cache operations decided by an index check and run in a later phase."""

    warm: dict[str, str]
    removed_filenames: dict[str, str]


async def check_repo(config: Config, repo: RepoConfig, store: ServiceState,
                     *, slots: RepoSlots | None = None) -> None:
    """Check one repository, then update its cache.

    With `slots` the two phases hold a slot only while they run, so a long
    warm does not hold up index checks (see RepoSlots). Without it the phases
    run unbounded, as direct callers and tests expect.
    """
    if slots is None:
        work = await check_index(config, repo, store)
        if work is not None:
            await apply_cache_work(config, repo, store, work)
        return
    async with slots.check():
        work = await check_index(config, repo, store)
    if work is not None:
        async with slots.warm():
            await apply_cache_work(config, repo, store, work)


async def check_index(config: Config, repo: RepoConfig, store: ServiceState) -> CacheWork | None:
    """Fetch and record the index; return the cache work it calls for, if any."""
    await _check_key_expiry(config, repo, store)

    parser_cls = PARSERS[repo.type]
    parser = parser_cls(repo)

    prev_etag, prev_last_modified = store.repositories.get_index_meta(repo.id, repo.catalog_identity())

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
        if (head.unchanged and not repo.verify_signature
                and (prev_etag is not None or prev_last_modified is not None)
                and store.repositories.has_snapshot(repo.id)):
            store.repositories.touch_last_check(repo.id)
            logger.debug(
                "%s: index unchanged (ETag/Last-Modified), download skipped", repo.id
            )
            return CacheWork({}, {}) if store.cache.get_pending_replacements(repo.id) else None

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

    await _check_key_expiry(config, repo, store, snapshot.signers)

    # No condition on repo.verify_signature here: AptParser now
    # cross-checks the SHA256 of the selected Packages index against InRelease (by-hash, see
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

    diff = store.repositories.record_snapshot(
        snapshot, index_etag=head.etag, index_last_modified=head.last_modified,
        source_identity=repo.catalog_identity()
    )

    if diff.changed:
        await emit(config, 'repository.changed', repo.id, {
            'added': len(diff.new_packages), 'removed': len(diff.removed_packages),
            'modified': len(diff.modified_packages), 'packages': len(snapshot.packages)})

    interested = store.cache.interested_keys(repo.id, repo.catalog_identity()) if repo.prefetch else set()

    if repo.type == 'nix':
        store.cache.update_nix_trust(repo.id, repo.verify_signature, repo.nix_public_keys, source=repo.upstream)
        # Retry demanded roots with missing binaries or failed artifacts even
        # on an unchanged catalog; never subscribe unrequested roots here.
        warmed = {item['package_key']: item['status'] for item in store.cache.get_warmed_packages(repo.id)}
        retry = {key: filename for key, filename in snapshot.packages.items() if key in interested and warmed.get(key) != 'ok'}
        logger.info('%s: Nix catalog checked (%d outputs, changed=%s)', repo.id, len(snapshot.packages), diff.changed)
        return CacheWork(retry, diff.removed_filenames)

    if not diff.changed:
        logger.debug("%s: no changes (%d packages)", repo.id, len(snapshot.packages))
        return CacheWork({}, {}) if store.cache.get_pending_replacements(repo.id) else None

    logger.info(
        "%s: changes — %d new, %d removed, %d modified",
        repo.id,
        len(diff.new_packages),
        len(diff.removed_packages),
        len(diff.modified_packages),
    )

    return CacheWork(
        {key: snapshot.packages[key] for key in diff.new_packages if key in interested},
        diff.removed_filenames,
    )


async def apply_cache_work(config: Config, repo: RepoConfig, store: ServiceState,
                           work: CacheWork) -> None:
    """Retry replacements, warm new packages and purge removed ones."""
    await refresh_replacements(config, repo, store)
    if repo.type == 'nix':
        await warm_cache(config, repo, store, work.warm)
        if work.removed_filenames and config.nginx.enable_purge:
            from repowatch.cache.nix import purge
            await purge(config, repo, store, work.removed_filenames)
        return

    if work.warm:
        await warm_cache(config, repo, store, work.warm)

    if work.removed_filenames:
        # Active proxy_cache eviction — a
        # no-op unless nginx.enable_purge is set (see purge_removed), so
        # this doesn't change behavior for any config that hasn't opted in.
        await purge_removed(config, repo, work.removed_filenames)
