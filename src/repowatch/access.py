"""Administrative sessions, host tokens and trusted proxy metadata (stdlib only)."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import secrets
import time
from typing import Mapping
from urllib.parse import urlsplit

from repowatch.config import StatusServerConfig
from repowatch.state import StateStore

SESSION_SECONDS = 12 * 60 * 60
COOKIE_NAME = 'repowatch_session'


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def csrf_for(secret: str) -> str:
    return hmac.new(secret.encode(), b'repowatch csrf', hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class AdminSession:
    password_fingerprint: str
    csrf: str
    expires_at: float


class AccessStore:
    def __init__(self, store: StateStore):
        self.store = store

    def create_session(self, password_hash: str, secure: bool) -> tuple[str, AdminSession]:
        now = time.time()
        secret = secrets.token_urlsafe(32)
        fingerprint = digest(password_hash)
        with self.store._connect() as conn:
            conn.execute('DELETE FROM admin_sessions WHERE expires_at <= ? OR password_fingerprint != ?', (now, fingerprint))
            conn.execute('INSERT INTO admin_sessions VALUES (?, ?, ?, ?)',
                         (digest(secret), fingerprint, now + SESSION_SECONDS, int(secure)))
        return secret, AdminSession(fingerprint, csrf_for(secret), now + SESSION_SECONDS)

    def session(self, secret: str, password_hash: str | None, secure: bool) -> AdminSession | None:
        if not password_hash or not secret or len(secret) > 128:
            return None
        with self.store._connect() as conn:
            row = conn.execute('SELECT password_fingerprint, expires_at, secure FROM admin_sessions WHERE secret_hash = ?',
                               (digest(secret),)).fetchone()
        if not row or row[1] <= time.time() or (row[2] and not secure):
            return None
        if not hmac.compare_digest(row[0], digest(password_hash)):
            return None
        return AdminSession(row[0], csrf_for(secret), row[1])

    def logout(self, secret: str) -> None:
        with self.store._connect() as conn:
            conn.execute('DELETE FROM admin_sessions WHERE secret_hash = ?', (digest(secret),))

    def create_token(self, name: str, expires_at: float | None = None, repo_ids: list[str] | None = None) -> dict:
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise ValueError('name must be a nonempty string of at most 128 characters')
        if expires_at is not None:
            if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) or not time.time() < expires_at < 253402300799:
                raise ValueError('expires_at must be a future Unix timestamp or null')
        if repo_ids is not None:
            if not isinstance(repo_ids, list) or len(repo_ids) > 10000 or any(not isinstance(r, str) or not r or len(r) > 256 for r in repo_ids):
                raise ValueError('repo_ids must be null (all) or a list of repository ids')
            repo_ids = sorted(set(repo_ids))
        token_id, secret = secrets.token_hex(16), 'rw_' + secrets.token_urlsafe(32)
        now = time.time()
        with self.store._connect() as conn:
            conn.execute('INSERT INTO host_tokens (id, name, secret_hash, created_at, expires_at, repo_ids) VALUES (?, ?, ?, ?, ?, ?)',
                         (token_id, name.strip(), digest(secret), now, expires_at, json.dumps(repo_ids) if repo_ids is not None else None))
        return {'id': token_id, 'name': name.strip(), 'token': secret, 'created_at': now, 'expires_at': expires_at, 'repo_ids': repo_ids}

    def tokens(self) -> list[dict]:
        columns = ('id', 'name', 'created_at', 'expires_at', 'last_used_at', 'revoked_at', 'repo_ids')
        with self.store._connect() as conn:
            rows = conn.execute('SELECT ' + ','.join(columns) + ' FROM host_tokens ORDER BY created_at DESC').fetchall()
        return [{**dict(zip(columns, row)), 'repo_ids': json.loads(row[-1]) if row[-1] is not None else None} for row in rows]

    def revoke_token(self, token_id: str) -> bool:
        with self.store._connect() as conn:
            return conn.execute('UPDATE host_tokens SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?',
                                (time.time(), token_id)).rowcount > 0

    def token_access(self, secret: str) -> dict | None:
        if not secret.startswith('rw_') or len(secret) > 128:
            return None
        now = time.time()
        with self.store._connect() as conn:
            row = conn.execute('SELECT id, expires_at, last_used_at, revoked_at, repo_ids FROM host_tokens WHERE secret_hash=?',
                               (digest(secret),)).fetchone()
        if not row or row[3] is not None or (row[1] is not None and row[1] <= now):
            return None
        # Reads stay reads during polling. Last use is approximate to one minute;
        # revocation is checked in SQLite every time, never cached in a process.
        if row[2] is None or row[2] < now - 60:
            with self.store._connect() as conn:
                conn.execute('UPDATE host_tokens SET last_used_at=? WHERE id=? AND revoked_at IS NULL '
                             'AND (last_used_at IS NULL OR last_used_at < ?)', (now, row[0], now - 60))
        return {'repo_ids': json.loads(row[4]) if row[4] is not None else None}


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
