"""Expose and edit the explicit allowlist of web-configurable settings."""

from __future__ import annotations

import json
import logging
import yaml
from pathlib import Path
from repowatch.config.models import Config
from repowatch.config.edit import atomic_config, locked_config
from repowatch.config.load import load_config
from repowatch.errors import ConfigError
from repowatch.storage.access import AdminSession
from repowatch.web.access import load_admin_config, load_request_config
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _cast_int(value: Any) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str):
        return int(value)
    raise ValueError('an integer is required')


def _cast_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _cast_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return _cast_int(value)


def _cast_url(value: Any) -> str:
    return str(value).rstrip("/")


def _cast_optional_url(value: Any) -> str | None:
    if not value:
        return None
    return str(value).rstrip("/")


# Global Config fields that can be edited through the dashboard: not secret
# (unlike admin_password_hash) and take effect without a process restart,
# since config.yaml is re-read every cycle (see runtime.scheduler.run_forever).
# Deliberately NOT included here: state_db/status_server/syslog_listener —
# they are only read once at startup, so an edit through the dashboard would
# silently do nothing until a restart and would be misleading;
# admin_password_hash — a secret, changed only via `repowatch hash-password`;
# notify_webhook_url — ALSO a secret (the incoming webhook URL is itself a
# bearer token), and this list is what safe_config_payload returns WITHOUT a
# password — the value would leak to any dashboard visitor. Edited only by
# hand in config.yaml.
SAFE_CONFIG_FIELDS: dict[str, Callable[[Any], Any]] = {
    "check_interval": _cast_int,
    "event_retention_days": _cast_int,
    "request_retention_days": _cast_int,
    "warmed_retention_days": _cast_int,
    "event_max_rows_per_repo": _cast_optional_int,
    "request_max_rows": _cast_optional_int,
    "prefetch_concurrency": _cast_int,
    "check_concurrency": _cast_int,
    "prefetch_bandwidth_limit": _cast_optional_float,
    "prefetch_bandwidth_timezone": str,
    "prefetch_bandwidth_schedule": lambda value: json.loads(value) if isinstance(value, str) else value,
    "cache_base_url": _cast_url,
    "public_cache_url": _cast_optional_url,
    "notify_after_failures": _cast_int,
    "key_expiry_warning_days": _cast_int,
}


def safe_config_payload(config_path: str | Path, *, current: Config | None = None) -> tuple[int, dict]:
    """Current values of the safe global fields — used to pre-fill the
    settings form in the dashboard. Requires no password: none of these
    fields are secret (see SAFE_CONFIG_FIELDS)."""
    current, error = load_request_config(config_path, current)
    if error is not None:
        return error

    return 200, {field: getattr(current, field) for field in SAFE_CONFIG_FIELDS}


@locked_config
def update_safe_config_payload(
    config_path: str | Path, admin_session: AdminSession | None, body: dict
) -> tuple[int, dict]:
    """Change one or more safe global fields (a partial body — only the keys
    actually being changed). The written file is validated by a full reload
    (load_config) BEFORE it replaces the original — if anything is wrong,
    the original is left untouched."""
    current, error = load_admin_config(config_path, admin_session)
    if error is not None:
        return error

    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}

    unknown = set(body) - set(SAFE_CONFIG_FIELDS)
    if unknown:
        return 400, {"error": f"not editable through the dashboard: {sorted(unknown)}"}

    casted: dict[str, Any] = {}
    for key, raw_value in body.items():
        try:
            casted[key] = SAFE_CONFIG_FIELDS[key](raw_value)
        except (TypeError, ValueError) as exc:
            return 400, {"error": f"{key}: invalid value ({exc})"}

    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw.update(casted)

    try:
        atomic_config(path, yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    except ConfigError as exc:
        return 400, {"error": f"result fails validation: {exc}"}
    try:
        reloaded = load_config(path)
    except ConfigError as exc:
        return 500, {"error": f"configuration could not be reloaded: {exc}"}

    logger.info("global settings changed via API: %s", sorted(casted))
    return 200, {field: getattr(reloaded, field) for field in SAFE_CONFIG_FIELDS}
