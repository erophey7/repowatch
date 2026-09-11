"""Notifications for repeated warm/GPG-verification failures.

The only channel is a generic JSON webhook via httpx (POST), not email/SMTP:
we don't want to drag SMTP server config/credentials into this otherwise
simple tool (see CLAUDE.md — minimum dependencies — httpx is already there
for network I/O, adding an smtplib layer for a second channel would be
excessive). The "text" field in the payload is a human-readable string that
Slack/Mattermost/Discord incoming webhooks display directly with no extra
setup on their side; the other fields are for anyone parsing the JSON
themselves.

A notification is sent not on every failure, but once when the
consecutive_failures threshold (config.notify_after_failures) is reached —
otherwise a persistently broken upstream would produce a message on every
check cycle. And once on recovery (the first success after a failure
notification) — so the operator doesn't have to guess whether it fixed
itself.

Never raises outward — a failure to send the notification itself (webhook
unreachable, DNS, etc.) must not take down check_repo/warm_cache."""

from __future__ import annotations

import logging

import httpx

from repowatch.config import Config
from repowatch.state import StateStore

logger = logging.getLogger(__name__)


async def record_failure_and_maybe_notify(
    config: Config, store: StateStore, repo_id: str, kind: str, message: str
) -> None:
    """Record another failure in the (repo_id, kind) streak and, if the
    threshold was just reached (and this streak hasn't been notified yet),
    send a webhook. kind: "prefetch" | "gpg" | "key_expiry" (the last one —
    docs_dev/ROADMAP.md item 20 — behaves slightly differently in spirit
    from the other two: it's not a transient failure but "still within the
    expiry warning window", reassessed once per check_repo cycle by
    watcher._check_key_expiry; the mechanism itself is identical)."""
    count, already_notified = store.bump_failure(repo_id, kind, message)

    if not config.notify_webhook_url or already_notified or count < config.notify_after_failures:
        return

    sent = await _send(
        config.notify_webhook_url,
        {
            "text": f"repowatch: {repo_id} — {kind} has failed {count} time(s) in a row: {message}",
            "repo_id": repo_id,
            "kind": kind,
            "status": "failing",
            "consecutive_failures": count,
            "message": message,
        },
    )
    if sent:
        store.mark_failure_notified(repo_id, kind)


async def record_success_and_maybe_notify(
    config: Config, store: StateStore, repo_id: str, kind: str
) -> None:
    """Reset the (repo_id, kind) failure streak after a success; if a
    failure notification was already sent for this streak, send a separate
    recovery notification."""
    was_notified = store.reset_failure(repo_id, kind)
    if not was_notified or not config.notify_webhook_url:
        return

    await _send(
        config.notify_webhook_url,
        {
            "text": f"repowatch: {repo_id} — {kind} is working again",
            "repo_id": repo_id,
            "kind": kind,
            "status": "recovered",
        },
    )


async def _send(url: str, payload: dict) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10)
            resp.raise_for_status()
        return True
    except httpx.HTTPError:
        logger.warning("failed to send webhook notification to %s", url, exc_info=True)
        return False
