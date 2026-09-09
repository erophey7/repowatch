"""Tests for gpgverify.py. Some tests actually generate an ephemeral GPG key
(an isolated GNUPGHOME under tmp_path — never touches the user's own keys)
and sign data with it, to exercise real crypto verification through gpgv,
not just mocks. Skipped if gpg/gpgv aren't installed."""

import os
import shutil
import subprocess

import pytest

from repowatch.gpgverify import (
    SignatureError,
    _extract_clearsigned_body,
    find_sha256_in_release,
    verify_clearsigned,
    verify_detached,
)

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
