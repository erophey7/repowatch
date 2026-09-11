"""Loading and validating config.yaml."""

from __future__ import annotations

import ipaddress
import re

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Invalid configuration."""


@dataclass(frozen=True)
class RepoConfig:
    id: str
    type: str  # "pacman" | "apt" | "apk" | "dnf" | "apt-rpm" | "xbps"
    upstream: str
    arch: str
    prefetch: bool = True
    # fields specific to particular repo types
    repo_name: str | None = None       # pacman: core/extra/community
    distribution: str | None = None    # apt: bookworm/jammy/...
    component: str | None = None       # apt: main/contrib/non-free
    # GPG verification of the index before parsing (see gpgverify.py) —
    # implemented for apt (InRelease, clearsigned), pacman
    # (<repo>.db.tar.gz.sig, detached), and dnf (repomd.xml.asc, detached).
    # apk uses a different, non-GPG signature scheme via apkverify.py.
    verify_signature: bool = False
    apk_signature_backend: str = 'openssl'
    apk_keys_dir: str | None = None
    keyring_path: str | None = None    # exported keyring (not .asc!), see README
    # per-repo overrides of global Config settings — None means "use the
    # global value", see Config.effective_check_interval/
    # effective_prefetch_bandwidth_limit
    check_interval: int | None = None
    prefetch_bandwidth_limit: float | None = None  # bytes/sec, not requests/sec
    # Free-text label for manual grouping in the dashboard (see
    # static/dashboard.html) — not automatic by type/distribution, the
    # operator decides (e.g. "arch"/"ubuntu-noble"/"staging"). Affects
    # nothing but display — no validation of the set of values, duplicates
    # and any string are allowed.
    group: str | None = None
    url_template: str | None = None
    url_variables: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from repowatch.url_templates import expand
        expand(self)
        if self.type not in {"pacman", "apt", "apk", "dnf", "apt-rpm", "xbps"}:
            raise ConfigError(f"{self.id}: unknown type={self.type!r}")
        if self.type == "pacman" and not self.repo_name:
            raise ConfigError(f"{self.id}: repo_name is required for pacman")
        if self.type == "apt" and not (self.distribution and self.component):
            raise ConfigError(f"{self.id}: distribution and component are required for apt")
        if self.type == "apt-rpm":
            if not isinstance(self.component, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", self.component):
                raise ConfigError(f"{self.id}: apt-rpm requires a safe component (e.g. classic)")
            if not isinstance(self.arch, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", self.arch):
                raise ConfigError(f"{self.id}: apt-rpm requires arch")
        if self.apk_signature_backend not in ('openssl', 'apk-tools'):
            raise ConfigError(f'{self.id}: apk_signature_backend must be openssl or apk-tools')
        if self.apk_keys_dir is not None and (not isinstance(self.apk_keys_dir, str) or not self.apk_keys_dir.strip()):
            raise ConfigError(f'{self.id}: apk_keys_dir must be a nonempty directory path')
        if self.verify_signature:
            if self.type == 'apk':
                if not self.apk_keys_dir:
                    raise ConfigError(f'{self.id}: verify_signature=true requires apk_keys_dir')
            elif self.type == 'xbps':
                # Unlike apt/pacman/dnf/apt-rpm, XBPS repos don't publish a
                # signed index at all (no <arch>-repodata.sig(2) — verified
                # by hand against repo-default.voidlinux.org). Trust instead
                # comes from a per-PACKAGE RSA signature sidecar
                # (<file>.xbps.sig2), checked by the real xbps client at
                # install time — a different shape of verification (at
                # warm-time, per package) than anything else here, not
                # implemented yet (see docs_dev/ROADMAP.md item 21). Refusing
                # outright avoids a false sense of security, same reasoning
                # as apk's original GPG rejection before apkverify.py existed.
                raise ConfigError(
                    f'{self.id}: verify_signature is not yet supported for xbps '
                    f'(no signed index is published upstream; see docs_dev/ROADMAP.md item 21)'
                )
            elif not self.keyring_path:
                raise ConfigError(f'{self.id}: verify_signature=true requires keyring_path')



@dataclass(frozen=True)
class StatusServerConfig:
    bind: str = "0.0.0.0"
    port: int = 8085
    tls_cert_path: str | None = None
    tls_key_path: str | None = None
    guest_read_only: bool = False
    token_repo_restrictions: bool = False
    allow_insecure_http: bool = False
    trusted_proxies: list[str] = field(default_factory=list)
    metrics_allowed_networks: list[str] = field(default_factory=lambda: ["127.0.0.1/32", "::1/128"])

    def __post_init__(self) -> None:
        if type(self.token_repo_restrictions) is not bool:
            raise ConfigError("status_server.token_repo_restrictions: must be a bool")
        if type(self.guest_read_only) is not bool:
            raise ConfigError("status_server.guest_read_only: must be a bool")
        if type(self.allow_insecure_http) is not bool:
            raise ConfigError("status_server.allow_insecure_http: must be a bool")
        for name in ("trusted_proxies", "metrics_allowed_networks"):
            networks = getattr(self, name)
            if not isinstance(networks, list) or any(not isinstance(n, str) for n in networks):
                raise ConfigError(f"status_server.{name}: must be a list of IP/CIDR")
            try:
                for network in networks:
                    ipaddress.ip_network(network, strict=False)
            except ValueError as exc:
                raise ConfigError(f"status_server.{name}: invalid IP/CIDR") from exc
        paths = (self.tls_cert_path, self.tls_key_path)
        if all(path is None for path in paths):
            return
        if not all(isinstance(path, str) and path.strip() for path in paths):
            raise ConfigError("status_server: tls_cert_path and tls_key_path are required together (nonempty strings)")


@dataclass(frozen=True)
class SyslogListenerConfig:
    """Listener for nginx's syslog access_log — the source of "what was
    requested" for the dashboard. Disabled by default (opt-in): we don't
    open an extra network port for operators who haven't configured this
    in nginx."""

    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 1514


@dataclass(frozen=True)
class NginxConfig:
    enabled: bool = False
    listen: str = "8080"
    server_name: str = "repo-cache.local"
    resolvers: list[str] = field(default_factory=lambda: ["127.0.0.53"])
    cache_max_size: str = "100g"
    cache_key_version: str = ""
    index_ttl: int = 300
    package_ttl: int = 15552000
    # Purely informational/read-only from the operator's point of view — the
    # actual cache directory nginx uses comes from the root-owned
    # policy.json written by `make activate` (see install.py), not from
    # here. NOT in api.SAFE_CONFIG_FIELDS/dashboard-editable, same reasoning
    # as admin_password_hash: writable service YAML choosing where
    # root-owned nginx writes cache files would be a privilege boundary
    # problem. When set, nginx.apply() cross-checks it against policy.json's
    # cache_dir and refuses to apply on a mismatch (see nginx.py) — so this
    # field can't silently drift from what's actually enforced; it exists so
    # the path isn't hidden entirely inside a file the service user can't
    # read. To actually change the cache directory, use
    # `CACHE_DIR=... make install && sudo make activate`/`nginx-apply`.
    cache_dir: str | None = None
    # Active cache eviction on package removal (see docs_dev/ROADMAP.md item
    # 24) via the third-party ngx_cache_purge nginx module — NOT bundled
    # with stock nginx, and NOT the nginx-plus proxy_cache_purge API. Off by
    # default: without it, behavior is exactly as before (inactive=/
    # max_size= are the only eviction, see nginx.py) — enabling this doesn't
    # change existing configs' rendered output at all besides adding the
    # purge locations. The operator must separately add
    # `load_module ".../ngx_http_cache_purge_module.so";` to their own main
    # nginx.conf (a main-context directive — the generated file here lives
    # inside http{}/sites-enabled, which can't emit it); nginx -t during
    # nginx-apply catches a missing module with a clear error and rolls
    # back, same as any other bad generated config.
    enable_purge: bool = False
    # docs_dev/ROADMAP.md item 29 — reroute a package request to another
    # repository's already-cached copy when the index reports the SAME
    # filename+SHA256 in more than one repo (real dedup, saves both origin
    # bandwidth and cache disk — see nginx.render_dedup()). Off by default,
    # no effect on existing configs' rendered output when disabled. Only
    # apt/pacman/dnf packages currently carry a comparable SHA256 (see
    # PackageRef.content_hash) — apk/apt-rpm files never participate.
    enable_dedup: bool = False

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ConfigError("nginx.enabled: must be a bool")
        if type(self.enable_purge) is not bool:
            raise ConfigError("nginx.enable_purge: must be a bool")
        if type(self.enable_dedup) is not bool:
            raise ConfigError("nginx.enable_dedup: must be a bool")
        if not isinstance(self.listen, str) or not re.fullmatch(r"(?:[0-9.]+:)?[0-9]{1,5}", self.listen):
            raise ConfigError("nginx.listen: a port or IPv4:port string")
        host, _, port = self.listen.rpartition(":")
        try:
            if host:
                ipaddress.IPv4Address(host)
            if not 1 <= int(port) <= 65535:
                raise ValueError()
            if not isinstance(self.resolvers, list) or not self.resolvers:
                raise ValueError()
            for address in self.resolvers:
                if not isinstance(address, str):
                    raise ValueError()
                ipaddress.IPv4Address(address)
        except (ValueError, TypeError) as exc:
            raise ConfigError("nginx: invalid port/resolvers IPv4") from exc
        if not isinstance(self.server_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", self.server_name):
            raise ConfigError("nginx.server_name: a single name without nginx syntax")
        if not isinstance(self.cache_max_size, str) or not re.fullmatch(r"[1-9][0-9]{0,6}[mg]", self.cache_max_size):
            raise ConfigError("nginx.cache_max_size: a size like 100g")
        if not isinstance(self.cache_key_version, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,64}", self.cache_key_version):
            raise ConfigError("nginx.cache_key_version: up to 64 letters/digits/hyphens")
        for ttl in (self.index_ttl, self.package_ttl):
            if type(ttl) is not int or not 1 <= ttl <= 31536000:
                raise ConfigError("nginx: TTL in seconds, 1-31536000")
        if self.cache_dir is not None:
            if (not isinstance(self.cache_dir, str)
                    or not re.fullmatch(r"/[A-Za-z0-9_./+-]+", self.cache_dir)
                    or ".." in self.cache_dir.split("/")):
                raise ConfigError("nginx.cache_dir: an absolute filesystem path with no traversal")


@dataclass(frozen=True)
class Config:
    state_db: Path
    check_interval: int
    cache_base_url: str
    status_server: StatusServerConfig
    repos: list[RepoConfig] = field(default_factory=list)
    # how many days to keep the change log (repo_events) before cleanup
    event_retention_days: int = 90
    # how many days to keep the log of real client requests (request_events)
    request_retention_days: int = 7
    # how many days to keep warmed_packages rows that haven't been updated —
    # much longer than event_retention_days: this isn't an event log but the
    # "last known warm state", and a package can legitimately go unwarmed
    # (and not update warmed_at) for months if its version doesn't change
    warmed_retention_days: int = 180
    # Size-based auto-cleanup on top of the time-based one above, for cases
    # where volume/traffic is high enough that day-based retention alone
    # doesn't prevent unbounded growth. None means no limit (time-based
    # only). event_max_rows_per_repo is per-repo (repo_events);
    # request_max_rows is global (request_events contains rows with
    # repo_id IS NULL, so "per repo" doesn't cleanly apply there).
    # warmed_packages deliberately has no size limit — its ceiling is
    # already naturally bounded by the number of packages ever seen, not an
    # open-ended event log.
    event_max_rows_per_repo: int | None = None
    request_max_rows: int | None = None
    # password hash (see auth.py, generated by `repowatch hash-password`)
    # for administrator login. Until set, the dashboard and administrative
    # API are closed; initial setup is via repowatch set-password. The
    # plaintext password is never stored anywhere, only a PBKDF2 hash — if
    # config.yaml leaks, the password itself is not exposed.
    admin_password_hash: str | None = None
    syslog_listener: SyslogListenerConfig = field(default_factory=SyslogListenerConfig)
    nginx: NginxConfig = field(default_factory=NginxConfig)
    # how many files to warm in parallel per warm_cache() run — on the
    # first full warm of a large repo (thousands of new packages),
    # warming one at a time sequentially would take hours
    prefetch_concurrency: int = 8
    # how many repositories to check concurrently per scheduler tick
    # (watcher.check_all, asyncio.Semaphore) — repositories used to be
    # checked strictly one at a time, so a slow/hung upstream for one repo
    # delayed checking all the others in that tick
    check_concurrency: int = 8
    # address of the nginx cache that a human actually browses to (for
    # "view files" links in the dashboard). Separate from cache_base_url,
    # because that one is usually 127.0.0.1 — the address repowatch itself
    # uses for warming, useless as a clickable link in the operator's
    # browser. If unset, cache_base_url is used as-is (fine for simple
    # single-host setups without the loopback quirk).
    public_cache_url: str | None = None
    # Webhook for notifications about repeated warm/GPG-verification
    # failures (see notifications.py) — not email/SMTP: we don't want to
    # drag SMTP server config/credentials into this otherwise simple tool;
    # a generic JSON webhook covers Slack/Discord/Mattermost incoming
    # webhooks and a custom HTTP receiver equally well. None (default)
    # means notifications are off. NOT included in api.SAFE_CONFIG_FIELDS
    # and NOT returned by safe_config_payload — this is effectively a
    # secret (the incoming webhook URL is itself a bearer token), not
    # returned even to the administrator, same reasoning as
    # admin_password_hash.
    notify_webhook_url: str | None = None
    # after how many CONSECUTIVE failures to send a notification (see
    # state.bump_failure/notifications.record_failure_and_maybe_notify) —
    # exactly once when the threshold is reached, not on every subsequent
    # failure.
    notify_after_failures: int = 3
    # global limit on total warm-up bandwidth (bytes/sec through
    # warm_cache, not requests/sec — the size of warmed files varies by
    # orders of magnitude, from a few hundred bytes for a pacman .desc to
    # hundreds of megabytes for a debian .deb, so a requests/sec limit
    # wouldn't protect the actual bandwidth to upstream/the local nginx).
    # None means no limit. Overridable per-repo, see
    # RepoConfig.prefetch_bandwidth_limit /
    # effective_prefetch_bandwidth_limit.
    prefetch_bandwidth_limit: float | None = None
    # Below how many days left until the soonest-expiring key in a repo's
    # keyring counts as "expiring soon" — surfaced in /api/repos and
    # /metrics, and drives a webhook notification the same way repeated
    # warm/GPG failures do (see watcher.check_repo, notifications.py, kind
    # "key_expiry"). Only meaningful for repositories with
    # verify_signature=true and a GPG-based type (apt/pacman/dnf/apt-rpm) —
    # apk's embedded RSA keys have no expiry concept at all.
    key_expiry_warning_days: int = 30

    def repo_by_id(self, repo_id: str) -> RepoConfig | None:
        return next((r for r in self.repos if r.id == repo_id), None)

    @property
    def browse_base_url(self) -> str:
        """What to show a human as a clickable link — see public_cache_url."""
        return self.public_cache_url or self.cache_base_url

    def effective_check_interval(self, repo: RepoConfig) -> int:
        """Per-repository check timers — RepoConfig.check_interval overrides
        the global value when set."""
        return repo.check_interval if repo.check_interval is not None else self.check_interval

    def effective_prefetch_bandwidth_limit(self, repo: RepoConfig) -> float | None:
        """Bytes/sec, not requests/sec — see prefetch_bandwidth_limit above."""
        if repo.prefetch_bandwidth_limit is not None:
            return repo.prefetch_bandwidth_limit
        return self.prefetch_bandwidth_limit


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    try:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"failed to parse YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"top level of config.yaml must be a mapping, got {type(raw).__name__}")

    try:
        repos_raw = raw.get("repos", [])
        if not repos_raw:
            raise ConfigError("configuration has no repositories (repos: [])")

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
            check_interval=int(raw.get("check_interval", 300)),
            cache_base_url=str(raw["cache_base_url"]).rstrip("/"),
            status_server=status_server,
            repos=repos,
            event_retention_days=int(raw.get("event_retention_days", 90)),
            request_retention_days=int(raw.get("request_retention_days", 7)),
            warmed_retention_days=int(raw.get("warmed_retention_days", 180)),
            event_max_rows_per_repo=(
                int(raw["event_max_rows_per_repo"]) if raw.get("event_max_rows_per_repo") else None
            ),
            request_max_rows=(
                int(raw["request_max_rows"]) if raw.get("request_max_rows") else None
            ),
            admin_password_hash=raw.get("admin_password_hash") or None,
            syslog_listener=syslog_listener,
            nginx=NginxConfig(**raw.get("nginx", {})),
            prefetch_concurrency=int(raw.get("prefetch_concurrency", 8)),
            check_concurrency=int(raw.get("check_concurrency", 8)),
            public_cache_url=(str(raw["public_cache_url"]).rstrip("/") if raw.get("public_cache_url") else None),
            prefetch_bandwidth_limit=(
                float(raw["prefetch_bandwidth_limit"]) if raw.get("prefetch_bandwidth_limit") else None
            ),
            notify_webhook_url=(str(raw["notify_webhook_url"]) if raw.get("notify_webhook_url") else None),
            notify_after_failures=int(raw.get("notify_after_failures", 3)),
            key_expiry_warning_days=int(raw.get("key_expiry_warning_days", 30)),
        )
        if config.nginx.enabled or any(repo.url_template is not None for repo in config.repos):
            from repowatch.nginx import render
            render(config)
        return config
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"invalid configuration structure: {exc}") from exc
