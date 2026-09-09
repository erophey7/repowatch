import asyncio
import gzip
import hashlib
from pathlib import Path

import pytest
import httpx

from repowatch.config import RepoConfig
from repowatch.gpgverify import SignatureError
from repowatch.parsers.apt import AptParser

FIXTURE = Path(__file__).parent / "fixtures" / "apt" / "Packages"


def _repo(**overrides) -> RepoConfig:
    return RepoConfig(
        id="debian-test",
        type="apt",
        upstream="https://example.org/debian",
        arch="amd64",
        distribution="bookworm",
        component="main",
        **overrides,
    )


def test_apt_parser_parses_fixture(monkeypatch):
    parser = AptParser(_repo())
    raw_gz = gzip.compress(FIXTURE.read_bytes())

    async def fake_http_get(client, url):
        if url.endswith('InRelease'):
            digest = hashlib.sha256(raw_gz).hexdigest()
            return (f'-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nSHA256:\n {digest} 1 main/binary-amd64/Packages.gz\n-----BEGIN PGP SIGNATURE-----').encode()
        return raw_gz

    monkeypatch.setattr(parser, "_http_get", fake_http_get)

    packages = asyncio.run(parser.fetch_packages(client=None))
    names = {p.name for p in packages}

    assert names == {"zlib1g", "bash"}

    zlib = next(p for p in packages if p.name == "zlib1g")
    assert zlib.version == "1:1.2.13.dfsg-1"
    assert zlib.filename == "pool/main/z/zlib/zlib1g_1.2.13.dfsg-1_amd64.deb"

    bash = next(p for p in packages if p.name == "bash")
    assert bash.version == "5.2.15-2+b2"
    assert bash.filename == "pool/main/b/bash/bash_5.2.15-2+b2_amd64.deb"


def test_apt_index_url_matches_repo_layout():
    parser = AptParser(_repo())
    assert parser.index_url() == (
        "https://example.org/debian/dists/bookworm/main/binary-amd64/Packages.gz"
    )


def test_apt_parser_accepts_when_sha256_matches_verified_release(monkeypatch):
    repo = _repo(verify_signature=True, keyring_path="/fake/keyring.gpg")
    parser = AptParser(repo)
    raw_gz = gzip.compress(FIXTURE.read_bytes())
    correct_sha256 = hashlib.sha256(raw_gz).hexdigest()
    release_body = f"SHA256:\n {correct_sha256} 999 main/binary-amd64/Packages.gz\n".encode()

    async def fake_http_get(client, url):
        return b"fake-inrelease" if url.endswith("InRelease") else raw_gz

    monkeypatch.setattr(parser, "_http_get", fake_http_get)
    # verify_clearsigned stays synchronous — called via
    # asyncio.to_thread (see AptParser._verified_expected_sha256)
    monkeypatch.setattr(
        "repowatch.parsers.apt.verify_clearsigned",
        lambda data, keyring: release_body,
    )

    packages = asyncio.run(parser.fetch_packages(client=None))
    assert {p.name for p in packages} == {"zlib1g", "bash"}


def test_apt_parser_rejects_when_sha256_mismatches(monkeypatch):
    repo = _repo(verify_signature=True, keyring_path="/fake/keyring.gpg")
    parser = AptParser(repo)
    raw_gz = gzip.compress(FIXTURE.read_bytes())
    wrong_release_body = b"SHA256:\n 0000000000000000 999 main/binary-amd64/Packages.gz\n"

    async def fake_http_get(client, url):
        return b"fake-inrelease" if url.endswith("InRelease") else raw_gz

    monkeypatch.setattr(parser, "_http_get", fake_http_get)
    monkeypatch.setattr(
        "repowatch.parsers.apt.verify_clearsigned",
        lambda data, keyring: wrong_release_body,
    )

    with pytest.raises(SignatureError):
        asyncio.run(parser.fetch_packages(client=None))


def test_apt_parser_propagates_signature_verification_failure(monkeypatch):
    repo = _repo(verify_signature=True, keyring_path="/fake/keyring.gpg")
    parser = AptParser(repo)

    async def fake_http_get(client, url):
        return b"whatever"

    monkeypatch.setattr(parser, "_http_get", fake_http_get)

    def raise_error(data, keyring):
        raise SignatureError("InRelease signature verification failed")

    monkeypatch.setattr("repowatch.parsers.apt.verify_clearsigned", raise_error)

    with pytest.raises(SignatureError):
        asyncio.run(parser.fetch_packages(client=None))


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("by_hash", [False, True])
def test_release_first_and_by_hash(monkeypatch, signed, by_hash):
    raw = gzip.compress(FIXTURE.read_bytes())
    digest = hashlib.sha256(raw).hexdigest()
    body = (f"Acquire-By-Hash: {'yes' if by_hash else 'no'}\nSHA256:\n"
            f" {digest} {len(raw)} main/binary-amd64/Packages.gz\n").encode()
    envelope = b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\n" + body + b"-----BEGIN PGP SIGNATURE-----"
    parser = AptParser(_repo(verify_signature=signed, keyring_path="/key" if signed else None))
    monkeypatch.setattr("repowatch.parsers.apt.verify_clearsigned", lambda *_: body)
    calls = []
    async def get(client, url):
        calls.append(url)
        return envelope if url.endswith("InRelease") else raw
    monkeypatch.setattr(parser, "_http_get", get)
    assert len(asyncio.run(parser.fetch_packages(None))) == 2
    assert calls[0].endswith("InRelease")
    assert calls[1].endswith("/by-hash/SHA256/" + digest if by_hash else "/Packages.gz")
    assert len(calls) == 2


@pytest.mark.parametrize("tampered", [False, True])
def test_by_hash_404_fallback_still_checks_same_hash(monkeypatch, tampered):
    raw = gzip.compress(FIXTURE.read_bytes())
    digest = hashlib.sha256(raw).hexdigest()
    body = f"Acquire-By-Hash: yes\nSHA256:\n {digest} 1 main/binary-amd64/Packages.gz\n".encode()
    parser = AptParser(_repo(verify_signature=True, keyring_path="/key"))
    monkeypatch.setattr("repowatch.parsers.apt.verify_clearsigned", lambda *_: body)
    async def get(client, url):
        if "/by-hash/" in url:
            response = httpx.Response(404, request=httpx.Request("GET", url))
            response.raise_for_status()
        if url.endswith("InRelease"):
            return b"signed"
        return b"tampered" if tampered else raw
    monkeypatch.setattr(parser, "_http_get", get)
    if tampered:
        with pytest.raises(SignatureError):
            asyncio.run(parser.fetch_packages(None))
    else:
        assert len(asyncio.run(parser.fetch_packages(None))) == 2


def test_unsigned_legacy_repository_without_release(monkeypatch):
    parser = AptParser(_repo())
    async def get(client, url):
        if url.endswith("Release"):
            httpx.Response(404, request=httpx.Request("GET", url)).raise_for_status()
        return gzip.compress(FIXTURE.read_bytes())
    monkeypatch.setattr(parser, "_http_get", get)
    assert len(asyncio.run(parser.fetch_packages(None))) == 2
