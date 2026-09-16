"""Opt-in repository events and bounded, idempotency-aware webhook delivery.

Existing failure streak bookkeeping remains in SQLite. Delivery retries are
in memory, not a durable outbox. See docs/webhooks.md for guarantees."""

from __future__ import annotations

import asyncio
import httpx
import logging
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from repowatch.config.models import Config
from repowatch.runtime.context import ServiceState

logger = logging.getLogger(__name__)


async def record_failure_and_maybe_notify(
    config: Config, store: ServiceState, repo_id: str, kind: str, message: str
) -> None:
    """Record another failure in the (repo_id, kind) streak and, if the
    threshold was just reached (and this streak hasn't been notified yet),
    send a webhook. kind: "prefetch" | "gpg" | "key_expiry" (the last one —
    docs_dev/ROADMAP.md item 20 — behaves slightly differently in spirit
    from the other two: it's not a transient failure but "still within the
    expiry warning window", reassessed once per check_repo cycle by
    operations.check._check_key_expiry; the mechanism itself is identical)."""
    count, already_notified = store.notifications.bump_failure(repo_id, kind, message)

    if (not enabled(config, "repository.failing") or already_notified
            or count < config.notify_after_failures):
        return

    sent = await emit(
        config, "repository.failing", repo_id,
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
        store.notifications.mark_failure_notified(repo_id, kind)


async def record_success_and_maybe_notify(
    config: Config, store: ServiceState, repo_id: str, kind: str
) -> None:
    """Reset the (repo_id, kind) failure streak after a success; if a
    failure notification was already sent for this streak, send a separate
    recovery notification."""
    was_notified = store.notifications.reset_failure(repo_id, kind)
    if not was_notified or not enabled(config, "repository.recovered"):
        return

    await emit(
        config, "repository.recovered", repo_id,
        {
            "text": f"repowatch: {repo_id} — {kind} is working again",
            "repo_id": repo_id,
            "kind": kind,
            "status": "recovered",
        },
    )


def enabled(config: Config, event: str) -> bool:
    return bool(config.notify_webhook_url and event in config.notify_events)


async def emit(config: Config, event: str, repo_id: str, data: dict) -> bool:
    """Create one immutable event envelope; retries keep its ID and timestamp."""
    if not enabled(config, event):
        return False
    payload = {**data, "event": event, "event_id": str(uuid.uuid4()),
               "schema_version": 1, "occurred_at": datetime.now(timezone.utc).isoformat(),
               "repo_id": repo_id}
    payload.setdefault("text", f"repowatch: {repo_id} — {event}")
    return await _send(config.notify_webhook_url, payload)


def _retry_delay(value: str | None, attempt: int) -> float:
    """Honor Retry-After seconds or HTTP dates; caller declines long delays."""
    delay = float(2 ** attempt)
    if value:
        try:
            seconds = int(value) if value.isascii() and value.isdigit() else (
                parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            delay = max(delay, seconds)
        except (ValueError, TypeError, OverflowError):
            pass
    return delay


async def _send(url: str, payload: dict) -> bool:
    # Never log URLs, response bodies or exception text: webhook URLs commonly
    # contain bearer credentials. Cancellation must still propagate normally.
    async with httpx.AsyncClient(follow_redirects=False) as client:
        for attempt in range(3):
            retry_after = None
            try:
                async with asyncio.timeout(10):
                    response = await client.post(
                        url, json=payload, timeout=10,
                        headers={"Idempotency-Key": payload["event_id"]})
                if 200 <= response.status_code < 300:
                    return True
                if response.status_code != 429 and not 500 <= response.status_code < 600:
                    logger.warning("webhook rejected event %s (HTTP %s)",
                                   payload["event_id"], response.status_code)
                    return False
                retry_after = response.headers.get("Retry-After")
            except (httpx.HTTPError, httpx.InvalidURL, TimeoutError):
                pass
            if attempt == 2:
                break
            delay = _retry_delay(retry_after, attempt)
            if delay > 30:
                break
            await asyncio.sleep(delay)
    logger.warning("webhook delivery exhausted for event %s", payload["event_id"])
    return False
