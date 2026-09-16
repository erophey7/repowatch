"""Session and token persistence using a shared Database."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from repowatch.storage.database import Database

SESSION_SECONDS = 12 * 60 * 60


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
    def __init__(self, db: Database):
        self.db = db

    def create_session(self, password_hash: str, secure: bool) -> tuple[str, AdminSession]:
        now = time.time()
        secret = secrets.token_urlsafe(32)
        fingerprint = digest(password_hash)
        with self.db.connect() as conn:
            conn.execute('DELETE FROM admin_sessions WHERE expires_at <= ? OR password_fingerprint != ?', (now, fingerprint))
            conn.execute('INSERT INTO admin_sessions VALUES (?, ?, ?, ?)',
                         (digest(secret), fingerprint, now + SESSION_SECONDS, int(secure)))
        return secret, AdminSession(fingerprint, csrf_for(secret), now + SESSION_SECONDS)

    def session(self, secret: str, password_hash: str | None, secure: bool) -> AdminSession | None:
        if not password_hash or not secret or len(secret) > 128:
            return None
        with self.db.connect() as conn:
            row = conn.execute('SELECT password_fingerprint, expires_at, secure FROM admin_sessions WHERE secret_hash = ?',
                               (digest(secret),)).fetchone()
        if not row or row[1] <= time.time() or (row[2] and not secure):
            return None
        if not hmac.compare_digest(row[0], digest(password_hash)):
            return None
        return AdminSession(row[0], csrf_for(secret), row[1])

    def logout(self, secret: str) -> None:
        with self.db.connect() as conn:
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
        with self.db.connect() as conn:
            conn.execute('INSERT INTO host_tokens (id, name, secret_hash, created_at, expires_at, repo_ids) VALUES (?, ?, ?, ?, ?, ?)',
                         (token_id, name.strip(), digest(secret), now, expires_at, json.dumps(repo_ids) if repo_ids is not None else None))
        return {'id': token_id, 'name': name.strip(), 'token': secret, 'created_at': now, 'expires_at': expires_at, 'repo_ids': repo_ids}

    def tokens(self) -> list[dict]:
        columns = ('id', 'name', 'created_at', 'expires_at', 'last_used_at', 'revoked_at', 'repo_ids')
        with self.db.connect() as conn:
            rows = conn.execute('SELECT ' + ','.join(columns) + ' FROM host_tokens ORDER BY created_at DESC').fetchall()
        return [{**dict(zip(columns, row)), 'repo_ids': json.loads(row[-1]) if row[-1] is not None else None} for row in rows]

    def revoke_token(self, token_id: str) -> bool:
        with self.db.connect() as conn:
            return conn.execute('UPDATE host_tokens SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?',
                                (time.time(), token_id)).rowcount > 0

    def token_access(self, secret: str) -> dict | None:
        if not secret.startswith('rw_') or len(secret) > 128:
            return None
        now = time.time()
        with self.db.connect() as conn:
            row = conn.execute('SELECT id, expires_at, last_used_at, revoked_at, repo_ids FROM host_tokens WHERE secret_hash=?',
                               (digest(secret),)).fetchone()
        if not row or row[3] is not None or (row[1] is not None and row[1] <= now):
            return None
        # Reads stay reads during polling. Last use is approximate to one minute;
        # revocation is checked in SQLite every time, never cached in a process.
        if row[2] is None or row[2] < now - 60:
            with self.db.connect() as conn:
                conn.execute('UPDATE host_tokens SET last_used_at=? WHERE id=? AND revoked_at IS NULL '
                             'AND (last_used_at IS NULL OR last_used_at < ?)', (now, row[0], now - 60))
        return {'repo_ids': json.loads(row[4]) if row[4] is not None else None}
