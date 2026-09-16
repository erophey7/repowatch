"""Render the Prometheus metrics representation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from repowatch.config.load import load_config
from repowatch.config.models import Config
from repowatch.errors import ConfigError
from repowatch.reporting.status import _HEALTHZ_STALE_MULTIPLIER, _repo_staleness
from repowatch.runtime.context import ServiceState

def _escape_label_value(value: str) -> str:
    """Escape a label value per the Prometheus text exposition format
    (https://prometheus.io/docs/instrumenting/exposition_formats/)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def metrics_payload(config_path: str | Path, store: ServiceState, *, current: Config | None = None) -> tuple[int, str]:
    """Prometheus text format — for Grafana/alerting integration, separate
    from status.json/the dashboard (those are for humans and host agents,
    this one is for monitoring systems).

    Every metric is a gauge, none is a counter: values here can DECREASE
    (retention, process restart), and a Prometheus counter must be
    monotonically increasing — so, for example, the size of request_events
    is deliberately not exposed here (it would be a mistyped counter)."""
    try:
        current = current if current is not None else load_config(config_path)
    except ConfigError:
        return 500, "# config.yaml is currently invalid, metrics unavailable\n"

    summaries = store.repositories.get_repo_summaries(include_warmed=True)
    statuses = {repo.id: summaries.get(repo.id, {}) for repo in current.repos}
    lines: list[str] = []

    lines.append(
        "# HELP repowatch_repo_package_count Number of packages known in this repo's last snapshot."
    )
    lines.append("# TYPE repowatch_repo_package_count gauge")
    for repo in current.repos:
        package_count = statuses[repo.id].get("package_count")
        if package_count is None:
            package_count = len(store.repositories.get_packages(repo.id) or {})
        label = _escape_label_value(repo.id)
        lines.append(f'repowatch_repo_package_count{{repo_id="{label}"}} {package_count}')

    lines.append(
        "# HELP repowatch_repo_warmed_count Number of packages repowatch has attempted to warm for this repo."
    )
    lines.append("# TYPE repowatch_repo_warmed_count gauge")
    for repo in current.repos:
        label = _escape_label_value(repo.id)
        lines.append(f'repowatch_repo_warmed_count{{repo_id="{label}"}} {statuses[repo.id].get("warmed_count", 0)}')

    lines.append(
        "# HELP repowatch_repo_last_check_timestamp_seconds Unix timestamp of the last check for this repo."
    )
    lines.append("# TYPE repowatch_repo_last_check_timestamp_seconds gauge")
    for repo in current.repos:
        last_check = statuses[repo.id].get("last_check")
        if last_check:
            ts = datetime.fromisoformat(last_check).timestamp()
            label = _escape_label_value(repo.id)
            lines.append(f'repowatch_repo_last_check_timestamp_seconds{{repo_id="{label}"}} {ts}')

    lines.append(
        "# HELP repowatch_repo_changed_at_timestamp_seconds "
        "Unix timestamp of the last detected package change for this repo."
    )
    lines.append("# TYPE repowatch_repo_changed_at_timestamp_seconds gauge")
    for repo in current.repos:
        changed_at = statuses[repo.id].get("changed_at")
        if changed_at:
            ts = datetime.fromisoformat(changed_at).timestamp()
            label = _escape_label_value(repo.id)
            lines.append(f'repowatch_repo_changed_at_timestamp_seconds{{repo_id="{label}"}} {ts}')

    lines.append(
        "# HELP repowatch_repo_key_expires_at_timestamp_seconds Unix timestamp of the soonest "
        "expiring GPG key in this repo's keyring (docs_dev/ROADMAP.md item 20) — absent for apk "
        "repos, repos without verify_signature, or when unknown."
    )
    lines.append("# TYPE repowatch_repo_key_expires_at_timestamp_seconds gauge")
    for repo in current.repos:
        key_expires_at = statuses[repo.id].get("key_expires_at")
        if key_expires_at:
            ts = datetime.fromisoformat(key_expires_at).timestamp()
            label = _escape_label_value(repo.id)
            lines.append(f'repowatch_repo_key_expires_at_timestamp_seconds{{repo_id="{label}"}} {ts}')

    lines.append(
        "# HELP repowatch_repo_key_expiring_soon Whether the soonest expiring key is within "
        "key_expiry_warning_days (1) or not (0) — absent (not 0) when there is no known expiry "
        "to compare, same reasoning as repowatch_repo_stale."
    )
    lines.append("# TYPE repowatch_repo_key_expiring_soon gauge")
    for repo in current.repos:
        key_expires_at = statuses[repo.id].get("key_expires_at")
        if key_expires_at:
            soon = datetime.fromisoformat(key_expires_at) - datetime.now(timezone.utc) < timedelta(
                days=current.key_expiry_warning_days)
            label = _escape_label_value(repo.id)
            lines.append(f'repowatch_repo_key_expiring_soon{{repo_id="{label}"}} {1 if soon else 0}')

    stale = _repo_staleness(current, store, statuses)
    stale_ids = {item["repo_id"] for item in stale}
    lines.append(
        "# HELP repowatch_repo_stale Whether this repo's last check is older than "
        f"{_HEALTHZ_STALE_MULTIPLIER}x its check_interval (1) or not (0) — a repo never "
        "checked at all is not stale, see /healthz."
    )
    lines.append("# TYPE repowatch_repo_stale gauge")
    for repo in current.repos:
        label = _escape_label_value(repo.id)
        lines.append(f'repowatch_repo_stale{{repo_id="{label}"}} {1 if repo.id in stale_ids else 0}')

    lines.append(
        "# HELP repowatch_healthy Whether repowatch considers itself healthy (1) or not (0) — see /healthz."
    )
    lines.append("# TYPE repowatch_healthy gauge")
    lines.append(f"repowatch_healthy {0 if stale else 1}")

    failure_counts = store.notifications.get_failure_counts()
    lines.append(
        "# HELP repowatch_repo_consecutive_failures Current consecutive-failure streak for this "
        "repo and kind (gpg: signature/checksum verification, prefetch: at least one warm failure "
        "in a check cycle) — 0 once the streak is reset by a success, see notifications.py."
    )
    lines.append("# TYPE repowatch_repo_consecutive_failures gauge")
    for repo in current.repos:
        label = _escape_label_value(repo.id)
        for kind in ("gpg", "prefetch"):
            count = failure_counts.get((repo.id, kind), 0)
            lines.append(f'repowatch_repo_consecutive_failures{{repo_id="{label}",kind="{kind}"}} {count}')

    ban_counts = store.cache.get_ban_counts()
    lines.append(
        "# HELP repowatch_repo_banned_packages Number of package names currently excluded "
        "from auto-warm for this repo (prefetch_bans)."
    )
    lines.append("# TYPE repowatch_repo_banned_packages gauge")
    for repo in current.repos:
        label = _escape_label_value(repo.id)
        lines.append(f'repowatch_repo_banned_packages{{repo_id="{label}"}} {ban_counts.get(repo.id, 0)}')

    type_counts: dict[str, int] = {}
    for repo in current.repos:
        type_counts[repo.type] = type_counts.get(repo.type, 0) + 1
    lines.append(
        "# HELP repowatch_repos_by_type Number of configured repositories of this type."
    )
    lines.append("# TYPE repowatch_repos_by_type gauge")
    for repo_type, count in sorted(type_counts.items()):
        label = _escape_label_value(repo_type)
        lines.append(f'repowatch_repos_by_type{{type="{label}"}} {count}')

    lines.append(
        "# HELP repowatch_state_db_bytes Size in bytes of the state_db SQLite file."
    )
    lines.append("# TYPE repowatch_state_db_bytes gauge")
    lines.append(f"repowatch_state_db_bytes {store.database.get_storage_stats()['state_db_bytes']}")

    if current.syslog_listener.enabled:
        # Only emitted when the syslog listener is on — otherwise request_events
        # stays empty and these gauges would be a misleading, permanent 0 rather
        # than "not tracked". repo_id is bounded by the number of configured
        # repos (already used as a label above); the unmatched (repo_id IS NULL)
        # bucket is skipped here, same as everywhere else, to avoid a label
        # that isn't one of the fixed repo_ids.
        hit_stats = store.requests.get_request_hit_stats()
        lines.append(
            "# HELP repowatch_repo_requests Number of client requests recorded for this repo "
            "within the current request_events retention window (see request_retention_days/"
            "request_max_rows) — a gauge, not a running total."
        )
        lines.append("# TYPE repowatch_repo_requests gauge")
        for repo in current.repos:
            label = _escape_label_value(repo.id)
            total = hit_stats.get(repo.id, {}).get("total", 0)
            lines.append(f'repowatch_repo_requests{{repo_id="{label}"}} {total}')

        lines.append(
            "# HELP repowatch_repo_requests_cache_hit Of repowatch_repo_requests, how many nginx "
            "reported as a cache HIT (cache_status)."
        )
        lines.append("# TYPE repowatch_repo_requests_cache_hit gauge")
        for repo in current.repos:
            label = _escape_label_value(repo.id)
            hits = hit_stats.get(repo.id, {}).get("hits", 0)
            lines.append(f'repowatch_repo_requests_cache_hit{{repo_id="{label}"}} {hits}')

    return 200, "\n".join(lines) + "\n"
