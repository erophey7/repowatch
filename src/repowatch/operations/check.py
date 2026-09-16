"""Check repository indexes, persist changes, and initiate cache updates."""

from __future__ import annotations

import asyncio
import httpx
import logging
from datetime import datetime, timezone
from repowatch.cache.purge import purge_removed
from repowatch.config.models import Config, RepoConfig
from repowatch.errors import SignatureError
from repowatch.notifications import emit, record_failure_and_maybe_notify, record_success_and_maybe_notify
from repowatch.operations.replacements import refresh_replacements
from repowatch.operations.warm import warm_cache
from repowatch.parsers import PARSERS
from repowatch.parsers.base import IndexHeadResult
from repowatch.runtime.context import ServiceState
from repowatch.verification.gpg import soonest_key_expiry

logger = logging.getLogger(__name__)


async def _check_key_expiry(config: Config, repo: RepoConfig, store: ServiceState) -> None:
    """Trust state (docs_dev/ROADMAP.md item 20) — runs unconditionally at
    the very start of every check cycle, independent of whether the index
    itself turns out to be unchanged: a quiet repository that rarely
    changes must still get its signing key's expiry reassessed on
    schedule, not only when there happens to be a new snapshot to record.

    apk is excluded — its embedded RSA keys (see verification/apk.py) have no
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
    store.repositories.record_key_expiry(repo.id, expires_at)

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


async def check_repo(config: Config, repo: RepoConfig, store: ServiceState) -> None:
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

    diff = store.repositories.record_snapshot(
        snapshot, index_etag=head.etag, index_last_modified=head.last_modified,
        source_identity=repo.catalog_identity()
    )

    if diff.changed:
        await emit(config, 'repository.changed', repo.id, {
            'added': len(diff.new_packages), 'removed': len(diff.removed_packages),
            'modified': len(diff.modified_packages), 'packages': len(snapshot.packages)})

    await refresh_replacements(config, repo, store)

    if repo.type == 'nix':
        store.cache.update_nix_trust(repo.id, repo.verify_signature, repo.nix_public_keys, source=repo.upstream)
        # A missing binary or failed artifact needs a retry even if the source
        # catalog has not changed. Successful roots retain normal warm policy.
        warmed = {item['package_key']: item['status'] for item in store.cache.get_warmed_packages(repo.id)}
        retry = {key: filename for key, filename in snapshot.packages.items() if warmed.get(key) != 'ok'}
        await warm_cache(config, repo, store, retry)
        if diff.removed_packages and config.nginx.enable_purge:
            from repowatch.cache.nix import purge
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
