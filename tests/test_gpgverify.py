"""Tests for gpgverify.py. Some tests actually generate an ephemeral GPG key
(an isolated GNUPGHOME under tmp_path — never touches the user's own keys)
and sign data with it, to exercise real crypto verification through gpgv,
not just mocks. Skipped if gpg/gpgv aren't installed."""

import os
import shutil
import subprocess

import pytest

from datetime import datetime, timedelta, timezone

from repowatch.errors import SignatureError
from repowatch.verification.gpg import _extract_clearsigned_body
from repowatch.verification.gpg import find_sha256_in_release
from repowatch.verification.gpg import KeyExpiry, signing_key_expiry, _signing_expiry
from repowatch.verification.gpg import verify_clearsigned
from repowatch.verification.gpg import verify_detached

pytestmark = pytest.mark.skipif(
    not (shutil.which("gpg") and shutil.which("gpgv")),
    reason="gpg/gpgv are not installed in this environment",
)


@pytest.fixture
def gpg_env(tmp_path):
    gnupghome = tmp_path / "gnupg"
    gnupghome.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(gnupghome)}

    batch = (
        "%no-protection\n"
        "Key-Type: RSA\n"
        "Key-Length: 2048\n"
        "Name-Real: repowatch test signer\n"
        "Name-Email: test@example.org\n"
        "Expire-Date: 0\n"
        "%commit\n"
    )
    subprocess.run(
        ["gpg", "--batch", "--gen-key"],
        input=batch, text=True, env=env, check=True, capture_output=True,
    )

    keyring_path = tmp_path / "keyring.gpg"
    subprocess.run(
        ["gpg", "--export", "-o", str(keyring_path)],
        env=env, check=True, capture_output=True,
    )
    return env, str(keyring_path)


def _sign_detached(env, data: bytes, out_path) -> bytes:
    subprocess.run(
        ["gpg", "--batch", "--yes", "--detach-sign", "--output", str(out_path), "-"],
        input=data, env=env, check=True, capture_output=True,
    )
    return out_path.read_bytes()


def _sign_clearsigned(env, data: bytes, out_path) -> bytes:
    subprocess.run(
        ["gpg", "--batch", "--yes", "--clearsign", "--output", str(out_path), "-"],
        input=data, env=env, check=True, capture_output=True,
    )
    return out_path.read_bytes()


def test_verify_detached_accepts_valid_signature(gpg_env, tmp_path):
    env, keyring_path = gpg_env
    data = b"hello, this is a fake pacman db\n"
    signature = _sign_detached(env, data, tmp_path / "data.sig")

    verify_detached(data, signature, keyring_path)  # must not raise


def test_verify_detached_rejects_tampered_data(gpg_env, tmp_path):
    env, keyring_path = gpg_env
    data = b"original content"
    signature = _sign_detached(env, data, tmp_path / "data.sig")

    with pytest.raises(SignatureError):
        verify_detached(b"tampered content", signature, keyring_path)


def test_verify_detached_rejects_unknown_signer(gpg_env, tmp_path):
    env, _keyring_path = gpg_env
    data = b"some data"
    signature = _sign_detached(env, data, tmp_path / "data.sig")

    empty_keyring = tmp_path / "empty-keyring.gpg"
    empty_keyring.write_bytes(b"")

    with pytest.raises(SignatureError):
        verify_detached(data, signature, str(empty_keyring))


def test_verify_clearsigned_returns_verified_body(gpg_env, tmp_path):
    env, keyring_path = gpg_env
    body = "SHA256:\n abc123 100 main/binary-amd64/Packages.gz\n"
    signed = _sign_clearsigned(env, body.encode(), tmp_path / "InRelease")

    verified_body = verify_clearsigned(signed, keyring_path)

    assert b"abc123 100 main/binary-amd64/Packages.gz" in verified_body


def test_verify_clearsigned_rejects_tampered_body(gpg_env, tmp_path):
    env, keyring_path = gpg_env
    signed = _sign_clearsigned(env, b"SHA256:\n real 1 x\n", tmp_path / "InRelease")
    tampered = signed.replace(b"real 1 x", b"fake 1 x")

    with pytest.raises(SignatureError):
        verify_clearsigned(tampered, keyring_path)


def test_verify_clearsigned_accepts_when_one_of_several_signers_is_known(tmp_path):
    """Regression found on a live host: a real Debian InRelease is signed by
    SEVERAL keys at once (gradual rotation) — gpgv returns a nonzero exit
    code if even one key isn't in the keyring, even when the other(s) are
    valid. Verification must not require ALL signatures to match, same as
    apt itself — otherwise upstream key rotation breaks verify_signature
    for no good reason."""
    gnupghome = tmp_path / "gnupg"
    gnupghome.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(gnupghome)}

    def _gen_key(name: str) -> None:
        batch = (
            "%no-protection\nKey-Type: RSA\nKey-Length: 2048\n"
            f"Name-Real: {name}\nName-Email: {name}@example.org\nExpire-Date: 0\n%commit\n"
        )
        subprocess.run(
            ["gpg", "--batch", "--gen-key"],
            input=batch, text=True, env=env, check=True, capture_output=True,
        )

    _gen_key("signer-one")
    _gen_key("signer-two")

    # the keyring contains ONLY signer-one — signer-two is deliberately unknown
    keyring_path = tmp_path / "keyring.gpg"
    subprocess.run(
        ["gpg", "--export", "-o", str(keyring_path), "signer-one@example.org"],
        env=env, check=True, capture_output=True,
    )

    body = "SHA256:\n abc123 1 main/binary-amd64/Packages.gz\n"
    out_path = tmp_path / "InRelease"
    subprocess.run(
        [
            "gpg", "--batch", "--yes",
            "-u", "signer-one@example.org", "-u", "signer-two@example.org",
            "--clearsign", "--output", str(out_path), "-",
        ],
        input=body.encode(), env=env, check=True, capture_output=True,
    )

    verified_body = verify_clearsigned(out_path.read_bytes(), str(keyring_path))
    assert b"abc123 1 main/binary-amd64/Packages.gz" in verified_body


def test_find_sha256_in_release_finds_matching_path():
    body = (
        b"Origin: Test\nSuite: bookworm\n"
        b"SHA256:\n"
        b" aaa111 1000 main/binary-amd64/Packages.gz\n"
        b" bbb222 2000 main/binary-amd64/Packages.xz\n"
    )
    assert find_sha256_in_release(body, "main/binary-amd64/Packages.gz") == "aaa111"
    assert find_sha256_in_release(body, "main/binary-amd64/Packages.xz") == "bbb222"


def test_find_sha256_in_release_raises_when_not_found():
    body = b"SHA256:\n aaa111 1000 main/binary-amd64/Packages.gz\n"
    with pytest.raises(SignatureError):
        find_sha256_in_release(body, "main/binary-arm64/Packages.gz")


def test_extract_clearsigned_body_unescapes_dash_lines():
    raw = (
        "-----BEGIN PGP SIGNED MESSAGE-----\n"
        "Hash: SHA256\n"
        "\n"
        "line one\n"
        "- -----not a real marker-----\n"
        "line three\n"
        "-----BEGIN PGP SIGNATURE-----\n"
        "fakebase64\n"
        "-----END PGP SIGNATURE-----\n"
    ).encode()
    assert _extract_clearsigned_body(raw) == b"line one\n-----not a real marker-----\nline three\n"


def test_extract_clearsigned_body_rejects_non_clearsigned_input():
    with pytest.raises(SignatureError):
        _extract_clearsigned_body(b"just some random bytes, not pgp at all")


def _gen_expiring_key(env, name: str, expire_date: str) -> None:
    batch = (
        "%no-protection\nKey-Type: RSA\nKey-Length: 2048\n"
        f"Name-Real: {name}\nName-Email: {name}@example.org\n"
        f"Expire-Date: {expire_date}\n%commit\n"
    )
    subprocess.run(
        ["gpg", "--batch", "--gen-key"],
        input=batch, text=True, env=env, check=True, capture_output=True,
    )


def test_actual_non_expiring_signer_is_known(gpg_env, tmp_path):
    env, keyring = gpg_env
    data = b"signed index"
    signature = _sign_detached(env, data, tmp_path / "index.sig")
    signers = []
    verify_detached(data, signature, keyring, signers=signers)
    assert len(signers) == 1
    assert len(signers[0][0]) == 40
    assert signing_key_expiry(keyring, signers) == KeyExpiry(True)


def test_unrelated_expiring_key_does_not_warn(gpg_env, tmp_path):
    env, keyring = gpg_env
    _gen_expiring_key(env, "unrelated", "10")
    subprocess.run(["gpg", "--batch", "--yes", "--export", "-o", keyring],
                   env=env, check=True, capture_output=True)
    signed = tmp_path / "signed"
    subprocess.run(["gpg", "--batch", "--yes", "--local-user", "test@example.org",
                    "--clearsign", "--output", str(signed)],
                   input=b"index", env=env, check=True, capture_output=True)
    signers = []
    assert verify_clearsigned(signed.read_bytes(), keyring, signers=signers) == b"index\n"
    assert signing_key_expiry(keyring, signers) == KeyExpiry(True)


def test_key_expiry_unknown_for_empty_keyring(tmp_path):
    keyring = tmp_path / "empty"
    keyring.write_bytes(b"")
    assert signing_key_expiry(str(keyring), [("a", "a")]) == KeyExpiry()


def test_key_expiry_unknown_without_gpg(monkeypatch):
    monkeypatch.setattr("repowatch.verification.gpg.shutil.which", lambda name: None)
    assert signing_key_expiry("missing", [("a", "a")]) == KeyExpiry()


def _key_record(kind, fingerprint, expiry="", validity="-"):
    return f"{kind}:{validity}:2048:1:id:0:{expiry}:::\nfpr:::::::::{fingerprint}:\n"


@pytest.mark.parametrize("primary,sub,expected", [
    ("2000000000", "1900000000", "1900000000"),
    ("1800000000", "1900000000", "1800000000"),
    ("", "1900000000", "1900000000"),
    ("2000000000", "", "2000000000"),
    ("", "", None),
])
def test_signing_subkey_and_primary_both_limit_lifetime(primary, sub, expected):
    listing = _key_record("pub", "p", primary) + _key_record("sub", "s", sub)
    result = _signing_expiry(listing, [("s", "p")])
    assert result == KeyExpiry(True, datetime.fromtimestamp(int(expected), timezone.utc).isoformat(timespec="seconds") if expected else None)


def test_multiple_verified_paths_use_longest_lifetime():
    listing = _key_record("pub", "a", "1800000000") + _key_record("pub", "b", "2000000000")
    result = _signing_expiry(listing, [("a", "a"), ("b", "b")])
    assert result.expires_at == datetime.fromtimestamp(2000000000, timezone.utc).isoformat(timespec="seconds")
    assert _signing_expiry(listing + _key_record("pub", "c"), [("a", "a"), ("c", "c")]) == KeyExpiry(True)


@pytest.mark.parametrize("listing,signers", [
    ("", [("a", "a")]),
    (_key_record("pub", "a", "bad"), [("a", "a")]),
    (_key_record("pub", "a", validity="r"), [("a", "a")]),
    (_key_record("pub", "a") + _key_record("sub", "s"), [("s", "other")]),
    (_key_record("pub", "a", "2000000000"), [("a", "a"), ("missing", "missing")]),
])
def test_incomplete_signer_metadata_remains_unknown(listing, signers):
    assert _signing_expiry(listing, signers) == KeyExpiry()


def test_real_signing_subkey_expiry_and_keyring_replacement(gpg_env, tmp_path):
    env, keyring = gpg_env
    listing = subprocess.run(['gpg', '--with-colons', '--list-keys'], env=env,
                             check=True, capture_output=True, text=True).stdout
    primary = next(line.split(':')[9] for line in listing.splitlines() if line.startswith('fpr:'))
    subprocess.run(['gpg', '--batch', '--pinentry-mode', 'loopback', '--passphrase', '',
                    '--quick-add-key', primary, 'rsa2048', 'sign', '10d'],
                   env=env, check=True, capture_output=True)
    subprocess.run(['gpg', '--batch', '--yes', '--export', '-o', keyring],
                   env=env, check=True, capture_output=True)
    data = b'index signed with a subkey'
    signature = _sign_detached(env, data, tmp_path / 'subkey.sig')
    signers = []
    verify_detached(data, signature, keyring, signers=signers)
    assert len(signers) == 1 and signers[0][0] != primary and signers[0][1] == primary
    expiry = signing_key_expiry(keyring, signers)
    assert expiry.known
    remaining = datetime.fromisoformat(expiry.expires_at) - datetime.now(timezone.utc)
    assert timedelta(days=8) < remaining < timedelta(days=12)
    # No global fingerprint/expiry cache may survive a changed keyring.
    from pathlib import Path
    Path(keyring).write_bytes(b'')
    assert signing_key_expiry(keyring, signers) == KeyExpiry()


def test_expired_unrelated_key_does_not_limit_actual_signer():
    listing = _key_record('pub', 'old', '1000000000') + _key_record('pub', 'current', '2000000000')
    expiry = _signing_expiry(listing, [('current', 'current')])
    assert expiry == KeyExpiry(True, datetime.fromtimestamp(2000000000, timezone.utc).isoformat(timespec='seconds'))
