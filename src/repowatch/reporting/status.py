"""Build repository status, freshness, and health representations."""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from repowatch.config.load import load_config
from repowatch.config.models import Config
from repowatch.errors import ConfigError
from repowatch.routing import repo_prefix
from repowatch.runtime.context import ServiceState

# How many times a repository's effective_check_interval it may exceed
# before /healthz considers it "suspiciously stale". check_repo() does not
# update last_check on failure (see operations.check.check_repo), so a steadily
# growing age specifically means "checks aren't actually succeeding", not a
# single upstream hiccup for one cycle.
_HEALTHZ_STALE_MULTIPLIER = 3


logger = logging.getLogger(__name__)


def repos_list_payload(config_path: str | Path, store: ServiceState, *, current: Config | None = None) -> tuple[int, list | dict]:
    """List of repositories from the current config.yaml, merged with status
    from sqlite. Factored out of Handler into a plain function so it's
    testable via a direct call, without spinning up a real HTTP server."""
    try:
        current = current if current is not None else load_config(config_path)
    except ConfigError:
        logger.exception("failed to reload config.yaml for API request")
        return 500, {"error": "config.yaml is currently invalid"}

    statuses = store.repositories.get_repo_summaries(include_warmed=True)
    pending_replacements = store.cache.get_pending_replacement_counts()
    result = []
    for repo in current.repos:
        status = statuses.get(repo.id, {})
        # package_count lives in repo_state (see ServiceState.repositories.record_snapshot) —
        # no need to parse the whole packages_json just for a count. Rows
        # written before this column existed have it NULL — only then do we
        # fall back to counting the old way (a one-off cost, self-heals on
        # the next snapshot).
        package_count = status.get("package_count")
        if package_count is None:
            package_count = len(store.repositories.get_packages(repo.id) or {})
        warmed_count = status.get("warmed_count", 0)
        try:
            browse_url = f"{current.browse_base_url}{repo_prefix(repo)}/"
        except ValueError:
            browse_url = None
        key_expires_at = status.get("key_expires_at")
        result.append(
            {
                "id": repo.id,
                "type": repo.type,
                "upstream": repo.upstream,
                "prefetch": repo.prefetch,
                "package_count": package_count,
                "warmed_count": warmed_count,
                "pending_replacements": pending_replacements.get(repo.id, 0),
                "browse_url": browse_url,
                "check_interval": current.effective_check_interval(repo),
                "last_check": status.get("last_check"),
                "changed_at": status.get("changed_at"),
                # Trust state (docs_dev/ROADMAP.md item 20) — the soonest
                # expiring key in this repo's keyring, from the last check
                # cycle (see verification.gpg.soonest_key_expiry). None for apk
                # repos, repos without verify_signature, or when unknown
                # (e.g. the full `gpg` binary isn't installed). Computed
                # once per check_interval by the watcher, not on this
                # request — a live `gpg` call on every dashboard poll would
                # be wasteful, see repowatch_repo_key_expires_at in metrics.
                "key_expires_at": key_expires_at,
                "key_expiring_soon": (
                    key_expires_at is not None
                    and datetime.fromisoformat(key_expires_at) - datetime.now(timezone.utc)
                    < timedelta(days=current.key_expiry_warning_days)
                ),
                # Raw RepoConfig fields as-is (unlike check_interval above,
                # which is already merged with the global default) — used
                # to pre-fill the dashboard edit form, so "inherits the
                # global value" doesn't turn into an explicit override on
                # the next save.
                "config": asdict(repo),
            }
        )
    return 200, result


def _repo_staleness(
    config: Config, store: ServiceState, statuses: dict[str, dict] | None = None,
) -> list[dict]:
    """List of repositories whose last check is older than
    interval * _HEALTHZ_STALE_MULTIPLIER — shared by healthz_payload and
    metrics_payload so the "suspiciously stale" threshold isn't duplicated.
    A repository that has never been checked at all (just added / service
    just started) is not considered stale — that's an expected state, not
    a failure."""
    now = datetime.now(timezone.utc)
    stale = []
    if statuses is None:
        statuses = store.repositories.get_repo_summaries()
    for repo in config.repos:
        status = statuses.get(repo.id)
        if not status or not status.get("last_check"):
            continue
        last_check = datetime.fromisoformat(status["last_check"])
        interval = config.effective_check_interval(repo)
        age_seconds = (now - last_check).total_seconds()
        if age_seconds > interval * _HEALTHZ_STALE_MULTIPLIER:
            stale.append(
                {
                    "repo_id": repo.id,
                    "last_check": status["last_check"],
                    "age_seconds": int(age_seconds),
                    "check_interval": interval,
                }
            )
    return stale


def status_payload(config_path: str | Path, store: ServiceState,
                   repo_id: str | None = None, *, current: Config | None = None) -> tuple[int, dict]:
    """Small polling signal; freshness is not a guarantee of cache readiness."""
    try:
        current = current if current is not None else load_config(config_path)
    except ConfigError:
        return 503, {"error": "status unavailable: invalid configuration"}
    if repo_id is not None and current.repo_by_id(repo_id) is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}
    summaries = store.repositories.get_repo_summaries()
    pending_replacements = store.cache.get_pending_replacement_counts()
    stale_ids = {item["repo_id"] for item in _repo_staleness(current, store, summaries)}
    result = {}
    for repo in current.repos:
        if repo_id is not None and repo.id != repo_id:
            continue
        summary = summaries.get(repo.id, {})
        last_check = summary.get("last_check")
        result[repo.id] = {
            "last_check": last_check,
            "changed_at": summary.get("changed_at"),
            "stale": not last_check or repo.id in stale_ids,
            "pending_replacements": pending_replacements.get(repo.id, 0),
        }
    return 200, result if repo_id is None else result[repo_id]


def healthz_payload(config_path: str | Path, store: ServiceState, *, current: Config | None = None) -> tuple[int, dict]:
    """200 if every repository has been checked recently relative to its own
    check_interval; otherwise 503 with the list of stale ones.

    A broken config.yaml is not by itself considered an unhealthy state —
    run_forever() keeps running on the last valid config in that case (see
    operations/check.py), so here we simply can't check freshness and silently
    answer ok.
    """
    try:
        current = current if current is not None else load_config(config_path)
    except ConfigError:
        return 200, {"ok": True, "warning": "config.yaml is currently invalid, check skipped"}

    stale = _repo_staleness(current, store)
    if stale:
        return 503, {"ok": False, "stale_repos": stale}
    return 200, {"ok": True}
