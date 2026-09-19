import asyncio
import gzip
import hashlib
from pathlib import Path

import pytest
import httpx

from repowatch.config.models import RepoConfig
from repowatch.errors import SignatureError
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
    # docs_dev/ROADMAP.md item 29 — per-stanza SHA256:, not the by-hash SHA256
    # of the whole Packages.gz checked above via InRelease.
    assert zlib.content_hash == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    bash = next(p for p in packages if p.name == "bash")
    assert bash.version == "5.2.15-2+b2"
    assert bash.content_hash == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
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


# --- differential tests: fast path vs. the splitlines reference ------------------

import random

from repowatch.parsers import apt as apt_module

HASH_A = "a" * 64
HASH_UPPER = "ABCDEF0123456789" * 4


def _same_as_reference(data: bytes):
    fast = apt_module._parse_packages(data)
    assert fast == apt_module._parse_packages_reference(data)
    return fast


def test_fast_path_matches_reference_on_fixture():
    assert len(_same_as_reference(FIXTURE.read_bytes())) == 2


@pytest.mark.parametrize("data", [
    b"",
    b"\n",
    b"no fields at all\n",
    b"Package: a\nVersion: 1\n",  # no trailing blank line
    b"Package: a\nVersion: 1",  # no trailing newline at all
    b"Version: 1\nPackage: a\n",  # field before the first Package
    b"Package: a\n",  # stanza without a version is dropped
    b"Package: a\nVersion: 1\nPackage: b\nVersion: 2\n",  # no blank line between stanzas
    b"Package: a\nVersion: 1\nVersion: 2\nFilename: f1\nFilename: f2\n",  # repeated fields: last wins
    b"Package: a\nPackage: b\nVersion: 1\n",  # repeated Package restarts the stanza
    b"Package:\nVersion: 1\n",  # empty name is falsy
    b"Package: a\nVersion:\n",  # empty version is falsy
    b"Package:a\nVersion:1\nFilename:x\n",  # no space after the colon
    b"Package:   spaced   \nVersion:\t1\t\nFilename:  f  \n",
    b"Package-Type: deb\nPackage: a\nVersion: 1\nSHA256sum: x\n",  # look-alike field names
    b"Package: a\n Package: indented\n Version: 9\nVersion: 1\n Filename: indented\n SHA256: " + HASH_A.encode() + b"\n",
    b"package: a\nVERSION: 1\nPackage: b\nVersion: 2\n",  # field names are case sensitive
    b"Package: a\nDescription: mentions Version: 3 mid-line\nVersion: 1\n",
    b"Package: a\nVersion: 1\nSHA256: " + HASH_A.encode() + b"\n",
    b"Package: a\nVersion: 1\nSHA256: " + HASH_UPPER.encode() + b"\n",
    b"Package: a\nVersion: 1\nSHA256: " + HASH_A.encode() + b"0\n",  # 65 hex digits
    b"Package: a\nVersion: 1\nSHA256: " + HASH_A[:-1].encode() + b"\n",  # 63
    b"Package: a\nVersion: 1\nSHA256: " + b"g" * 64 + b"\n",  # not hex
    b"Package: a\nVersion: 1\nSHA256: " + HASH_A.encode() + b"\nSHA256: nothex\n",  # a later value replaces the hash
    b"Package: a\nVersion: 1\n\n\n\nPackage: b\nVersion: 2\n",
])
def test_fast_path_matches_reference_on_edge_cases(data):
    _same_as_reference(data)


@pytest.mark.parametrize("data", [
    b"Package: a\xff\nVersion: 1\xc2\nFilename: \xe2\x80\n",  # invalid / truncated sequences
    "Package: café\nVersion: 1–2\nFilename: 日本\n".encode(),
    b"Package: a\nVersion: 1\nFilename: \x80\x80\x80\n",
    b"Package: a\xf0\x9f\nVersion: 1\n",  # truncated 4-byte sequence
    b"Package: \xc2\xa0a\xc2\xa0\nVersion: \xe3\x80\x801\xe3\x80\x80\n",  # str.strip removes NBSP / ideographic space
    b"Package: a\nVersion: \x1f1\x1f\n",  # 0x1f is stripped after decoding, not a line break
    b"Package: a\nVersion: 1\nSHA256: \xef\xbb\xbf" + HASH_A.encode() + b"\n",
    b"\xef\xbb\xbfPackage: a\nVersion: 1\n",  # BOM before the first field
    b"Package: a\x00b\nVersion: 1\x00\n",
])
def test_fast_path_matches_reference_on_unusual_bytes(data):
    _same_as_reference(data)


@pytest.mark.parametrize("separator", [
    b"\r", b"\r\n", b"\x0b", b"\x0c", b"\x1c", b"\x1d", b"\x1e",
    "".encode(), " ".encode(), " ".encode(),
])
def test_unusual_line_breaks_use_the_reference_result(separator):
    # str.splitlines() treats these as line ends, so a field can start right after one.
    mid_line = b"Package: a" + separator + b"Version: 1\nPackage: b\nVersion: 2\n"
    trailing = b"Package: a\nVersion: 1" + separator + b"Filename: f\nSHA256: " + HASH_A.encode() + separator
    inside_text = b"Package: a\nVersion: 1\nDescription: x" + separator + b"y\n"
    for data in (mid_line, trailing, inside_text):
        assert apt_module._has_unusual_line_break(data)
        _same_as_reference(data)
    packages = _same_as_reference(mid_line)
    assert [(p.name, p.version) for p in packages] == [("a", "1"), ("b", "2")]


def test_crlf_index_is_parsed_like_the_reference():
    data = b"Package: a\r\nVersion: 1\r\nFilename: f\r\n\r\nPackage: b\r\nVersion: 2\r\n"
    packages = _same_as_reference(data)
    assert [(p.name, p.version, p.filename) for p in packages] == [("a", "1", "f"), ("b", "2", None)]


@pytest.mark.parametrize("data", [
    b"Package: a\nVersion: 1\nDescription: \xe2\x80\x9cquoted\xe2\x80\x9d \xc2\xa9 \xe2\x80\x93 dash\n",
    b"Package: a\nVersion: 1\nDescription: \xe2\x80\xa7 near-miss\n \xc2\x84 also near\n",
])
def test_ordinary_multibyte_text_does_not_trigger_the_fallback(data):
    assert not apt_module._has_unusual_line_break(data)
    _same_as_reference(data)


def test_lead_byte_scan_limit_still_finds_a_break(monkeypatch):
    monkeypatch.setattr(apt_module, "_LEAD_BYTE_SCAN_LIMIT", 3)
    noise = b"Description: " + b"\xe2\x80\x9d" * 20 + b"\n"
    clean = b"Package: a\nVersion: 1\n" + noise
    assert not apt_module._has_unusual_line_break(clean)
    dirty = clean + b"Package: b\nVersion: 2\nDescription: x\xe2\x80\xa8Package: c\nVersion: 3\n"
    assert apt_module._has_unusual_line_break(dirty)
    assert [p.name for p in _same_as_reference(dirty)] == ["a", "b", "c"]


def test_fast_path_matches_reference_on_random_documents():
    rng = random.Random(20260919)
    pieces = [
        b"Package: ", b"Version: ", b"Filename: ", b"SHA256: ", b"Package:", b"Description: ", b" Version: ",
        b"\n", b"\n", b"\n\n", b" ", b"  ", b"\t", b"a", b"b1", b"1:2.3-4", b"pool/x/y.deb", b"-",
        HASH_A.encode(), HASH_UPPER.encode(), b"e" * 63, b"\xff", b"\xc2", b"\xe2\x80", b"\xc2\xa0",
        "é".encode(), "中".encode(), b"\xc2\x85", " ".encode(), b"\x1f", b"\r", b"\x0c",
    ]
    for _ in range(400):
        data = b"".join(rng.choice(pieces) for _ in range(rng.randrange(0, 60)))
        _same_as_reference(data)


def test_parse_packages_gz_decompresses_then_parses():
    raw = FIXTURE.read_bytes()
    assert apt_module._parse_packages_gz(gzip.compress(raw)) == _same_as_reference(raw)


# --- bounded scan slices (no single C call may hold the GIL for the whole document) ---

@pytest.mark.parametrize("slice_size", [1, 2, 3, 7, 16, 64, 1000])
def test_slicing_never_changes_the_result(monkeypatch, slice_size):
    monkeypatch.setattr(apt_module, "_SCAN_SLICE", slice_size)
    rng = random.Random(slice_size)
    pieces = [
        b"Package: ", b"Version: ", b"Filename: ", b"SHA256: ", b"Description: ", b" Version: ",
        b"\n", b"\n", b"\n\n", b" ", b"a", b"b1", b"1:2.3-4", b"pool/x/y.deb",
        HASH_A.encode(), b"\xff", b"\xc2", b"\xe2\x80\x9d", "é".encode(),
    ]
    for _ in range(150):
        data = b"".join(rng.choice(pieces) for _ in range(rng.randrange(0, 50)))
        assert not apt_module._has_unusual_line_break(data)
        _same_as_reference(data)
    assert len(_same_as_reference(FIXTURE.read_bytes())) == 2


@pytest.mark.parametrize("slice_size", [1, 2, 5, 11, 64])
def test_field_lines_match_one_unsliced_scan(monkeypatch, slice_size):
    monkeypatch.setattr(apt_module, "_SCAN_SLICE", slice_size)
    data = FIXTURE.read_bytes() + b"\nPackage: tail\nVersion: 1"  # no final newline
    expected = apt_module._LATER_FIELDS.findall(data)
    assert apt_module._field_lines(data) == expected
    assert apt_module._field_lines(b"") == []
    assert apt_module._field_lines(b"no fields\n") == []


@pytest.mark.parametrize("slice_size", [1, 2, 3, 8])
@pytest.mark.parametrize("needle", [b"x", b"xy", b"xyz"])
def test_find_matches_bytes_find_for_every_position(monkeypatch, slice_size, needle):
    monkeypatch.setattr(apt_module, "_SCAN_SLICE", slice_size)
    for length in range(0, 20):
        for position in range(0, length + 1):
            data = bytearray(b"." * length)
            data[position:position + len(needle)] = needle  # may be clipped at the end
            data = bytes(data)
            for start in (0, 1, position, position + 1, length + 3):
                assert apt_module._find(data, needle, start) == data.find(needle, start)
        assert apt_module._find(b"." * length, needle) == -1


def test_break_detection_finds_separators_across_slice_boundaries(monkeypatch):
    monkeypatch.setattr(apt_module, "_SCAN_SLICE", 4)
    for separator in (b"\r", b"\x0c", "".encode(), " ".encode(), " ".encode()):
        for padding in range(0, 9):
            data = b"Package: a\n" + b"x" * padding + separator + b"y" * padding + b"\n"
            assert apt_module._has_unusual_line_break(data), (separator, padding)
    assert not apt_module._has_unusual_line_break(b"Package: a\n" + b"\xe2\x80\x9d" * 20)
