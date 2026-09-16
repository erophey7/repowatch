"""Interpret proxy/session context and enforce administrator access."""

from __future__ import annotations

import hmac
import ipaddress
import logging
from pathlib import Path
from repowatch.config.load import load_config
from repowatch.errors import ConfigError
from dataclasses import dataclass
from repowatch.config.models import StatusServerConfig, Config
from repowatch.storage.access import AdminSession, digest
from typing import Mapping
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

COOKIE_NAME = 'repowatch_session'


def ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value)
    return getattr(address, 'ipv4_mapped', None) or address


def in_networks(address: str, networks: list[str]) -> bool:
    try:
        return any(ip(address) in ipaddress.ip_network(network, strict=False) for network in networks)
    except ValueError:
        return False


@dataclass(frozen=True)
class ClientContext:
    client_ip: str | None
    secure: bool
    host: str
    local_direct: bool

    @property
    def credentials_allowed(self) -> bool:
        return self.secure or self.local_direct


def client_context(peer: str, headers: Mapping[str, str], tls: bool,
                   config: StatusServerConfig) -> ClientContext:
    peer = str(ip(peer))
    forwarded = any(headers.get(k) is not None for k in
                    ('X-Forwarded-For', 'X-Forwarded-Proto', 'X-Forwarded-Host'))
    trusted = in_networks(peer, config.trusted_proxies)
    secure, host, client = tls, headers.get('Host', ''), peer
    if trusted:
        raw = headers.get('X-Forwarded-For', '')
        if not raw or len(raw) > 4096:
            client = None  # a trusted proxy's own loopback IP is not the client
        else:
            try:
                chain = [str(ip(part.strip())) for part in raw.split(',')] + [peer]
                if len(chain) > 32:
                    raise ValueError('proxy chain too long')
                while chain and in_networks(chain[-1], config.trusted_proxies):
                    chain.pop()
                client = chain[-1] if chain else None
            except ValueError:
                client = None
        proto = headers.get('X-Forwarded-Proto')
        if proto is not None:
            if proto not in ('http', 'https'):
                raise ValueError('proxy must overwrite X-Forwarded-Proto with http or https')
            secure = proto == 'https'
        host = headers.get('X-Forwarded-Host', host)
    # Also refuse an unconfigured loopback proxy for metrics/HTTP credentials.
    elif forwarded and ip(peer).is_loopback:
        client = None
    if not host or any(c in host for c in '/\\@,\r\n\t #?'):
        raise ValueError('invalid external host')
    parsed = urlsplit('//' + host)
    try:
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('invalid external host')
        parsed.port
    except ValueError as exc:
        raise ValueError('invalid external host') from exc
    return ClientContext(client, secure, host.lower(), ip(peer).is_loopback and not forwarded and not trusted)


def _check_admin_session(current: Config, admin_session: AdminSession | None) -> tuple[int, dict] | None:
    """Recheck the authenticated identity against the current config during writes."""
    if not current.admin_password_hash:
        return 501, {"error": "set an administrator password with repowatch set-password"}
    if not isinstance(admin_session, AdminSession) or not hmac.compare_digest(
            admin_session.password_fingerprint, digest(current.admin_password_hash)):
        return 401, {"error": "administrator session required"}
    return None


def load_request_config(config_path: str | Path, current: Config | None = None) -> tuple[Config | None, tuple[int, dict] | None]:
    """Reload configuration for a request, preserving the API's invalid-YAML response."""
    try:
        return current if current is not None else load_config(config_path), None
    except ConfigError:
        logger.exception("failed to reload config.yaml for API request")
        return None, (500, {"error": "config.yaml is currently invalid"})


def load_admin_config(config_path: str | Path, admin_session: AdminSession | None
                      ) -> tuple[Config | None, tuple[int, dict] | None]:
    """Reload and recheck the session; YAML writers must already hold config_lock."""
    current, error = load_request_config(config_path)
    if error is not None:
        return None, error
    error = _check_admin_session(current, admin_session)
    return (None, error) if error is not None else (current, None)
