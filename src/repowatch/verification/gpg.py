from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from repowatch.errors import SignatureError
from repowatch.processes import run

logger = logging.getLogger(__name__)


def verify_detached(data: bytes, signature: bytes, keyring_path: str, *, signers: list[tuple[str, str]] | None = None) -> None:
    """Verify a detached signature (pacman: <repo>.db.tar.gz + .sig)."""
    with tempfile.TemporaryDirectory() as tmp:
        data_path = Path(tmp) / "data"
        sig_path = Path(tmp) / "data.sig"
        data_path.write_bytes(data)
        sig_path.write_bytes(signature)
        _run_gpgv(keyring_path, sig_path, data_path, signers)


def verify_clearsigned(data: bytes, keyring_path: str, *, signers: list[tuple[str, str]] | None = None) -> bytes:
    """Verify a clearsigned file (apt InRelease — the signature is embedded
    directly in the file, no separate .sig). Returns the verified message
    body (without the PGP envelope) — gpgv doesn't hand that back itself, so
    we extract it manually, but only AFTER gpgv has verified it successfully,
    never before."""
    with tempfile.TemporaryDirectory() as tmp:
        data_path = Path(tmp) / "InRelease"
        data_path.write_bytes(data)
        _run_gpgv(keyring_path, data_path, None, signers)
    return _extract_clearsigned_body(data)


def _run_gpgv(keyring_path: str, sig_path: Path, data_path: Path | None,
              signers: list[tuple[str, str]] | None = None) -> None:
    # --status-fd 1 — a machine-readable status instead of parsing
    # localizable human text. Real indexes (e.g. Debian's InRelease) are
    # often signed by SEVERAL keys at once (gradual key rotation) — gpgv
    # then returns a nonzero exit code even when one key is valid and the
    # other simply isn't in the keyring (NO_PUBKEY, not BADSIG). apt itself
    # behaves the same way: we don't require ALL signatures to match, one
    # VALIDSIG from a trusted key is enough — otherwise upstream key
    # rotation would break verification for no good reason.
    args = ["gpgv", "--status-fd", "1", "--keyring", keyring_path, str(sig_path)]
    if data_path is not None:
        args.append(str(data_path))
    try:
        result = run(args, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise SignatureError(
            "gpgv not found — install gnupg on the host running repowatch"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise SignatureError(f"gpgv: verification process failed: {exc}") from exc

    status_lines = result.stdout.splitlines()
    if any(line.startswith("[GNUPG:] BADSIG") for line in status_lines):
        raise SignatureError(f"gpgv: BADSIG — data does not match the signature: {result.stderr.strip()}")
    if not any(line.startswith("[GNUPG:] VALIDSIG") for line in status_lines):
        raise SignatureError(
            f"gpgv: no signature was confirmed by a key in the keyring "
            f"(exit code {result.returncode}): {result.stderr.strip()}"
        )

    if signers is not None:
        for line in status_lines:
            fields = line.split()
            if len(fields) >= 11 and fields[:2] == ["[GNUPG:]", "VALIDSIG"]:
                # VALIDSIG's expiration field belongs to the signature, not
                # the key. Retain fingerprints and read key metadata separately.
                signers.append((fields[2], fields[11] if len(fields) >= 12 else fields[2]))


def _extract_clearsigned_body(data: bytes) -> bytes:
    """PGP clearsign format:

        -----BEGIN PGP SIGNED MESSAGE-----
        Hash: SHA256

        <body>
        -----BEGIN PGP SIGNATURE-----
        ...
        -----END PGP SIGNATURE-----

    We extract exactly <body>. Lines inside the body starting with "-" are
    escaped by the clearsign envelope as "- <line>" — we undo that escaping.
    """
    text = data.decode("utf-8", errors="replace")
    start_marker = "-----BEGIN PGP SIGNED MESSAGE-----"
    sig_marker = "-----BEGIN PGP SIGNATURE-----"
    try:
        start = text.index(start_marker)
        header_end = text.index("\n\n", start) + 2
        end = text.index(sig_marker, header_end)
    except ValueError as exc:
        raise SignatureError("doesn't look like a clearsigned file (no PGP headers)") from exc

    body = text[header_end:end]
    lines = [line[2:] if line.startswith("- ") else line for line in body.splitlines()]
    return ("\n".join(lines) + "\n").encode("utf-8")


@dataclass(frozen=True)
class KeyExpiry:
    """Unknown metadata differs from a verified path with no expiration."""

    known: bool = False
    expires_at: str | None = None


def signing_key_expiry(keyring_path: str, signers: list[tuple[str, str]]) -> KeyExpiry:
    """Report the usable lifetime of the actual verified signing paths.

    Each path ends when either its primary key or signing subkey expires.
    Multiple confirmed signatures are alternatives, so the longest-lived path
    determines the warning. Unrelated keys never affect the result. This is
    diagnostic only: missing gpg or unreadable metadata cannot reject an index.
    """
    if not signers or shutil.which("gpg") is None:
        return KeyExpiry()
    try:
        with tempfile.TemporaryDirectory(prefix="repowatch-gpg-home-") as home:
            result = run(
                ["gpg", "--batch", "--no-options", "--no-autostart",
                 "--with-colons", "--with-fingerprint", "--with-subkey-fingerprint",
                 "--import-options", "show-only", "--import", keyring_path],
                text=True, timeout=10, env={**os.environ, "GNUPGHOME": home},
            )
    except (OSError, subprocess.SubprocessError):
        logger.warning("could not read signing key expiry for %s", keyring_path, exc_info=True)
        return KeyExpiry()
    if result.returncode:
        logger.warning("gpg key inspection failed for %s: %s", keyring_path, result.stderr.strip())
        return KeyExpiry()
    return _signing_expiry(result.stdout, signers)


def _signing_expiry(listing: str, signers: list[tuple[str, str]]) -> KeyExpiry:
    """Join GnuPG pub/sub/fpr records to VALIDSIG fingerprints."""
    keys: dict[str, tuple[str, datetime | None, bool]] = {}
    primary = ""
    pending = None
    for line in listing.splitlines():
        fields = line.split(":")
        if fields[0] in ("pub", "sub"):
            if fields[0] == "pub":
                primary = ""
            pending = fields
        elif fields[0] == "fpr" and len(fields) > 9 and pending is not None:
            fingerprint = fields[9]
            if pending[0] == "pub":
                primary = fingerprint
            try:
                expiry = datetime.fromtimestamp(int(pending[6]), timezone.utc) if pending[6] else None
                valid = pending[1] not in ("r", "d", "i") and bool(primary)
            except (IndexError, ValueError, OverflowError, OSError):
                expiry, valid = None, False
            keys[fingerprint] = (primary, expiry, valid)
            pending = None
    deadlines = []
    unknown = False
    for fingerprint, primary in set(signers):
        path = (keys.get(fingerprint), keys.get(primary))
        if any(key is None or not key[2] or key[0] != primary for key in path):
            unknown = True
            continue
        dates = [key[1] for key in path if key[1] is not None]
        if not dates:
            return KeyExpiry(True)
        deadlines.append(min(dates))
    if unknown or not deadlines:
        return KeyExpiry()
    return KeyExpiry(True, max(deadlines).isoformat(timespec="seconds"))


def find_sha256_in_release(release_body: bytes, target_path: str) -> str:
    """Extract the SHA256 hash of a specific file (e.g.
    "main/binary-amd64/Packages.gz") from the "SHA256:" section of a
    verified Release/InRelease. The section format is, per line,
    " <hex> <size> <path>"."""
    text = release_body.decode("utf-8", errors="replace")
    in_section = False
    for line in text.splitlines():
        if line.strip() == "SHA256:":
            in_section = True
            continue
        if not in_section:
            continue
        if not line.startswith(" "):
            break  # end of the SHA256 section (next Release file header)
        parts = line.split()
        if len(parts) == 3 and parts[2] == target_path:
            return parts[0]
    raise SignatureError(f"no SHA256 entry found for {target_path!r} in Release")
