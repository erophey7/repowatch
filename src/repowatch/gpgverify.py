"""GPG signature verification for indexes, via gpgv.

Deliberately an external process, not a Python library (no python-gnupg/pgpy
in the dependencies) — gpgv is already present almost everywhere alongside
gnupg/apt, and apt itself uses it to verify its own indexes, so requiring
gpgv on the host isn't a new requirement.

Supports apt (InRelease, clearsigned) and pacman (<repo>.db.tar.gz.sig,
detached). apk uses a different, non-GPG signature scheme (see
RepoConfig.__post_init__ in config.py) — out of scope here.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


class SignatureError(Exception):
    """The signature is missing, invalid, expired, or the required public
    key isn't in the keyring. The caller must not trust the data."""


def verify_detached(data: bytes, signature: bytes, keyring_path: str) -> None:
    """Verify a detached signature (pacman: <repo>.db.tar.gz + .sig)."""
    with tempfile.TemporaryDirectory() as tmp:
        data_path = Path(tmp) / "data"
        sig_path = Path(tmp) / "data.sig"
        data_path.write_bytes(data)
        sig_path.write_bytes(signature)
        _run_gpgv(keyring_path, sig_path, data_path)


def verify_clearsigned(data: bytes, keyring_path: str) -> bytes:
    """Verify a clearsigned file (apt InRelease — the signature is embedded
    directly in the file, no separate .sig). Returns the verified message
    body (without the PGP envelope) — gpgv doesn't hand that back itself, so
    we extract it manually, but only AFTER gpgv has verified it successfully,
    never before."""
    with tempfile.TemporaryDirectory() as tmp:
        data_path = Path(tmp) / "InRelease"
        data_path.write_bytes(data)
        _run_gpgv(keyring_path, data_path, None)
    return _extract_clearsigned_body(data)


def _run_gpgv(keyring_path: str, sig_path: Path, data_path: Path | None) -> None:
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
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise SignatureError(
            "gpgv not found — install gnupg on the host running repowatch"
        ) from exc

    status_lines = result.stdout.splitlines()
    if any(line.startswith("[GNUPG:] BADSIG") for line in status_lines):
        raise SignatureError(f"gpgv: BADSIG — data does not match the signature: {result.stderr.strip()}")
    if not any(line.startswith("[GNUPG:] VALIDSIG") for line in status_lines):
        raise SignatureError(
            f"gpgv: no signature was confirmed by a key in the keyring "
            f"(exit code {result.returncode}): {result.stderr.strip()}"
        )


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
