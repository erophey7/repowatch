"""Load YAML configuration and validate the resulting routing configuration."""

from __future__ import annotations

import yaml
from pathlib import Path
from repowatch.config.models import RepoConfig, StatusServerConfig, SyslogListenerConfig, NginxConfig, Config
from repowatch.errors import ConfigError
from typing import Any

# Optional acceleration already supplied by PyYAML; retain its safe Python fallback.
_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        raw: dict[str, Any] = yaml.load(path.read_text(encoding="utf-8"), Loader=_SAFE_LOADER) or {}
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read configuration: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"failed to parse YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"top level of config.yaml must be a mapping, got {type(raw).__name__}")

    try:
        repos_raw = raw.get("repos", [])
        if not isinstance(repos_raw, list):
            raise ConfigError("repos must be a list; use repos: [] for an empty installation")

        repos = [RepoConfig(**r) for r in repos_raw]

        ids = [r.id for r in repos]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ConfigError(f"duplicate repo id: {dupes}")

        status_raw = raw.get("status_server", {})
        status_server = StatusServerConfig(**status_raw)

        syslog_raw = raw.get("syslog_listener", {})
        syslog_listener = SyslogListenerConfig(**syslog_raw)

        config = Config(
            state_db=Path(raw["state_db"]),
            check_interval=raw.get("check_interval", 300),
            cache_base_url=str(raw["cache_base_url"]).rstrip("/"),
            status_server=status_server,
            repos=repos,
            event_retention_days=raw.get("event_retention_days", 90),
            request_retention_days=raw.get("request_retention_days", 7),
            warmed_retention_days=raw.get("warmed_retention_days", 180),
            event_max_rows_per_repo=raw.get("event_max_rows_per_repo"),
            request_max_rows=raw.get("request_max_rows"),
            admin_password_hash=raw.get("admin_password_hash") or None,
            syslog_listener=syslog_listener,
            nginx=NginxConfig(**raw.get("nginx", {})),
            prefetch_concurrency=raw.get("prefetch_concurrency", 8),
            check_concurrency=raw.get("check_concurrency", 8),
            public_cache_url=(str(raw["public_cache_url"]).rstrip("/") if raw.get("public_cache_url") else None),
            prefetch_bandwidth_limit=raw.get('prefetch_bandwidth_limit'),
            prefetch_bandwidth_timezone=raw.get('prefetch_bandwidth_timezone', 'UTC'),
            prefetch_bandwidth_schedule=raw.get('prefetch_bandwidth_schedule', []),
            notify_webhook_url=(str(raw["notify_webhook_url"]) if raw.get("notify_webhook_url") else None),
            notify_after_failures=raw.get("notify_after_failures", 3),
            notify_events=raw.get("notify_events", ["repository.failing", "repository.recovered"]),
            key_expiry_warning_days=raw.get("key_expiry_warning_days", 30),
        )
        if config.nginx.enabled or any(repo.url_template is not None for repo in config.repos):
            from repowatch.nginx.render import render
            render(config)
        return config
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ConfigError(f"invalid configuration structure: {exc}") from exc
