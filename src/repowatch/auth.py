"""Password hashing and verification for the dashboard's write endpoints.

PBKDF2-HMAC-SHA256 via stdlib hashlib — no bcrypt/argon2/passlib in the
dependencies. The hash format is self-describing (algorithm$iterations$salt$hash)
so the iteration count can be raised in the future without breaking already
stored hashes of old passwords.
"""

from __future__ import annotations

import hashlib
import hmac
import os

_ALGORITHM = "pbkdf2_sha256"
# OWASP Password Storage Cheat Sheet's current PBKDF2-HMAC-SHA256
# recommendation (checked 2026-09-14 against the live page — an earlier
# value here, 260_000, was a stale reference to a prior revision of that
# same guidance, not lowered for performance) — this password guards write
# access to the dashboard. The hash format embeds its own iteration count
# (see module docstring), so raising this default doesn't invalidate
# admin_password_hash values already in a config.yaml — they keep
# verifying at whatever count they were created with; only a fresh
# `repowatch hash-password` picks up the new default.
_DEFAULT_ITERATIONS = 600_000


def hash_password(password: str, iterations: int = _DEFAULT_ITERATIONS) -> str:
    """Called from `repowatch hash-password` (see cli.py) — the operator
    pastes the result into config.yaml as admin_password_hash."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """True if password matches stored_hash. Does not raise on a malformed
    or foreign hash format — just returns False, which the caller already
    treats as "unauthorized"."""
    try:
        algorithm, iterations_str, salt_hex, digest_hex = stored_hash.split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False

    if not 1 <= iterations <= 10_000_000 or not salt or len(expected) != 32:
        return False
    try:
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    except UnicodeError:
        return False
    return hmac.compare_digest(actual, expected)
