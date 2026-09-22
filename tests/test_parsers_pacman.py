import asyncio
import io
import tarfile
from pathlib import Path

import pytest

from repowatch.config.models import RepoConfig
from repowatch.errors import SignatureError
from repowatch.parsers.pacman import PacmanParser

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pacman"


def _repo(**overrides) -> RepoConfig:
    return RepoConfig(
        id="arch-test",
        type="pacman",
        upstream="https://example.org/core/os/x86_64",
        arch="x86_64",
        repo_name="core",
        **overrides,
    )


def _make_fake_db_tar_gz(fixtures_dir: Path) -> bytes:
    """Build core.db.tar.gz from tests/fixtures/pacman/<pkg>-<ver>/desc, the
    same way a real repository lays desc files out into subfolders."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for pkg_dir in sorted(fixtures_dir.iterdir()):
            desc_path = pkg_dir / "desc"
            data = desc_path.read_bytes()
            info = tarfile.TarInfo(name=f"{pkg_dir.name}/desc")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_pacman_parser_parses_fixture(monkeypatch):
    parser = PacmanParser(_repo())
    fake_bytes = _make_fake_db_tar_gz(FIXTURES_DIR)

    async def fake_http_get(client, url):
        return fake_bytes

    monkeypatch.setattr(parser, "_http_get", fake_http_get)

    packages = asyncio.run(parser.fetch_packages(client=None))
    names = {p.name for p in packages}

    assert names == {"zlib", "bash"}

    zlib = next(p for p in packages if p.name == "zlib")
    assert zlib.version == "1.3-1"
    assert zlib.filename == "zlib-1.3-1-x86_64.pkg.tar.zst"
    # Cross-repo dedup: %SHA256SUM%, same shape as apt's SHA256:.
    assert zlib.content_hash == "b" * 63 + "2"

    bash = next(p for p in packages if p.name == "bash")
    assert bash.version == "5.2.026-1"
    assert bash.filename == "bash-5.2.026-1-x86_64.pkg.tar.zst"
    assert bash.content_hash == "a" * 63 + "1"


def test_pacman_index_url_matches_repo_layout():
    parser = PacmanParser(_repo())
    assert parser.index_url() == "https://example.org/core/os/x86_64/core.db.tar.gz"


def test_pacman_parser_verifies_signature_when_enabled(monkeypatch):
    repo = _repo(verify_signature=True, keyring_path="/fake/keyring.gpg")
    parser = PacmanParser(repo)
    fake_bytes = _make_fake_db_tar_gz(FIXTURES_DIR)

    requested_urls = []

    async def fake_http_get(client, url):
        requested_urls.append(url)
        return b"fake-signature-bytes" if url.endswith(".sig") else fake_bytes

    monkeypatch.setattr(parser, "_http_get", fake_http_get)

    verify_calls = []
    # verify_detached stays synchronous — called via asyncio.to_thread
    monkeypatch.setattr(
        "repowatch.parsers.pacman.verify_detached",
        lambda data, sig, keyring: verify_calls.append((data, sig, keyring)),
    )

    packages = asyncio.run(parser.fetch_packages(client=None))

    assert {p.name for p in packages} == {"zlib", "bash"}
    assert any(u.endswith(".sig") for u in requested_urls)
    assert len(verify_calls) == 1
    assert verify_calls[0][2] == "/fake/keyring.gpg"


def test_pacman_parser_propagates_signature_verification_failure(monkeypatch):
    repo = _repo(verify_signature=True, keyring_path="/fake/keyring.gpg")
    parser = PacmanParser(repo)
    fake_bytes = _make_fake_db_tar_gz(FIXTURES_DIR)

    async def fake_http_get(client, url):
        return b"sig" if url.endswith(".sig") else fake_bytes

    monkeypatch.setattr(parser, "_http_get", fake_http_get)

    def raise_error(data, sig, keyring):
        raise SignatureError("boom")

    monkeypatch.setattr("repowatch.parsers.pacman.verify_detached", raise_error)

    with pytest.raises(SignatureError):
        asyncio.run(parser.fetch_packages(client=None))


# --- direct tar reader vs. tarfile ----------------------------------------------------

import gzip
import random

from repowatch.parsers import pacman as pacman_module

HASH = "ab" * 32


def _desc(name, version, filename=None, sha=None, extra=""):
    blocks = [f"%NAME%\n{name}\n", f"%VERSION%\n{version}\n"]
    if filename is not None:
        blocks.append(f"%FILENAME%\n{filename}\n")
    if sha is not None:
        blocks.append(f"%SHA256SUM%\n{sha}\n")
    return ("\n".join(blocks) + "\n" + extra).encode()


def _tar(members, fmt=tarfile.PAX_FORMAT, end=True):
    """members: (name, data, type) tuples; data None for directories."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=fmt) as tar:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.mtime = 1700000000
            if kind == tarfile.REGTYPE:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = data
                tar.addfile(info)
            else:
                tar.addfile(info)
    raw = buffer.getvalue()
    if not end:
        raw = raw.rstrip(b"\0")
        raw += b"\0" * (-len(raw) % 512)
    return raw


def _outcome(function, raw):
    try:
        return ("ok", function(raw))
    except Exception as exc:  # the type is part of the contract, the message is not
        return ("error", type(exc).__name__)


def _same_as_tarfile(raw_tar, compress=True):
    raw = gzip.compress(raw_tar) if compress else raw_tar
    fast = _outcome(pacman_module._parse_db_tar_gz, raw)
    assert fast == _outcome(pacman_module._parse_db_tar_gz_reference, raw)
    return fast


def _direct(raw_tar):
    """True if the direct reader accepted the archive (did not decline to tarfile)."""
    try:
        list(pacman_module._desc_members(gzip.compress(raw_tar)))
    except pacman_module._Unusual:
        return False
    return True


def _standard(count=5):
    members = []
    for i in range(count):
        base = f"pkg{i}-1.{i}-1"
        members.append((base, None, tarfile.DIRTYPE))
        members.append((f"{base}/desc", _desc(f"pkg{i}", f"1.{i}-1", f"{base}-x86_64.pkg.tar.zst", HASH), tarfile.REGTYPE))
        members.append((f"{base}/depends", b"%DEPENDS%\nglibc\n", tarfile.REGTYPE))
    return members


@pytest.mark.parametrize("fmt", [tarfile.USTAR_FORMAT, tarfile.GNU_FORMAT, tarfile.PAX_FORMAT])
def test_direct_reader_matches_tarfile_for_each_tar_format(fmt):
    raw = _tar(_standard(), fmt)
    outcome = _same_as_tarfile(raw)
    assert outcome[0] == "ok" and [p.name for p in outcome[1]] == [f"pkg{i}" for i in range(5)]
    assert _direct(raw)


def test_direct_reader_used_for_real_looking_fixture():
    raw = _tar(_standard(30))
    assert _direct(raw)
    assert len(_same_as_tarfile(raw)[1]) == 30


@pytest.mark.parametrize("members", [
    [],
    [("only-a-dir", None, tarfile.DIRTYPE)],
    [("a/desc", b"", tarfile.REGTYPE)],  # empty desc
    [("a/desc", _desc("a", ""), tarfile.REGTYPE)],  # no version
    [("a/desc", b"%NAME%\na\n", tarfile.REGTYPE)],
    [("a/desc", _desc("a", "1", "a-1.pkg", "not-a-hash"), tarfile.REGTYPE)],
    [("a/desc", b"\xff\xfe%NAME%\nb\xc3\n\n%VERSION%\n1\n", tarfile.REGTYPE)],  # invalid UTF-8
    [("x/desc", _desc("a", "1"), tarfile.REGTYPE), ("y/desc", _desc("a", "1"), tarfile.REGTYPE)],  # duplicates
    [("a/desc", _desc("a", "1"), tarfile.REGTYPE), ("a/desc", _desc("b", "2"), tarfile.REGTYPE)],  # same member name twice
    [("desc", _desc("a", "1"), tarfile.REGTYPE)],  # not "<dir>/desc"
    [("a/descx", _desc("a", "1"), tarfile.REGTYPE), ("a/Desc", _desc("a", "1"), tarfile.REGTYPE)],
    [("a/desc/", None, tarfile.DIRTYPE)],  # directory named like a desc
    [("a/desc", _desc("a", "1", extra="%DESC%\nline1\nline2\n\n%ARCH%\nany\n"), tarfile.REGTYPE)],
    [("deep/" * 30 + "desc", _desc("long", "1"), tarfile.REGTYPE)],  # long path: ustar prefix or pax
    [("d" * 120 + "/desc", _desc("long", "1"), tarfile.REGTYPE)],  # name longer than 100 bytes
    [("a/desc", b"x" * 600 + b"\n" + _desc("a", "1"), tarfile.REGTYPE)],  # spans several blocks
    [("a/desc", _desc("a", "1"), tarfile.REGTYPE)] * 3,
])
@pytest.mark.parametrize("fmt", [tarfile.USTAR_FORMAT, tarfile.GNU_FORMAT, tarfile.PAX_FORMAT])
def test_direct_reader_matches_tarfile_on_edge_members(members, fmt):
    try:
        raw = _tar(members, fmt)
    except ValueError:  # this tar format cannot store the name
        return
    _same_as_tarfile(raw)


@pytest.mark.parametrize("build", [
    lambda: _tar([("a/desc", _desc("a", "1"), tarfile.SYMTYPE)][:0] + [("a/desc", "elsewhere", tarfile.SYMTYPE)]),
    lambda: _tar([("real/desc", _desc("a", "1"), tarfile.REGTYPE), ("a/desc", "real/desc", tarfile.LNKTYPE)]),
    lambda: _tar(_standard(), tarfile.GNU_FORMAT)[:-1024 - 512],  # cut inside the end marker's block padding
    lambda: _tar(_standard(), end=False),
    lambda: _tar(_standard())[: 512 * 5 + 100],  # truncated in the middle of a header's data
    lambda: _tar(_standard())[: 512 * 3 + 17],
    lambda: _tar(_standard()) + b"trailing garbage" * 40,
    lambda: _tar(_standard()) + b"\0" * 4096,
    lambda: b"\0" * 1024,
    lambda: b"",
    lambda: b"not a tar archive at all" * 100,
])
def test_direct_reader_declines_or_agrees_on_unusual_archives(build):
    _same_as_tarfile(build())


def test_direct_reader_declines_what_tarfile_alone_understands():
    assert not _direct(_tar([("a/desc", _desc("a", "1"), tarfile.REGTYPE), ("a/desc", "x", tarfile.SYMTYPE)]))
    assert not _direct(_tar([("d" * 150 + "/desc", _desc("a", "1"), tarfile.REGTYPE)], tarfile.PAX_FORMAT))
    assert not _direct(_tar([("d" * 150 + "/desc", _desc("a", "1"), tarfile.REGTYPE)], tarfile.GNU_FORMAT))
    assert not _direct(_tar(_standard(), end=False))
    raw = bytearray(_tar(_standard()))
    raw[100:107] = b"9999999"  # a mode field that is not octal
    assert not _direct(bytes(raw))
    raw = bytearray(_tar(_standard()))
    raw[148:155] = b"0000001"  # wrong checksum
    assert not _direct(bytes(raw))
    raw = bytearray(_tar(_standard()))
    raw[124] = 0x80  # base-256 size
    assert not _direct(bytes(raw))


def test_ustar_prefix_names_are_read_directly():
    name = "/".join(["segment"] * 14) + "/desc"  # > 100 bytes, fits ustar's prefix + name
    assert 100 < len(name) < 255
    raw = _tar([(name, _desc("prefixed", "1"), tarfile.REGTYPE)], tarfile.USTAR_FORMAT)
    assert _direct(raw)
    assert [p.name for p in _same_as_tarfile(raw)[1]] == ["prefixed"]


@pytest.mark.parametrize("chunk", [1, 7, 100, 511, 512, 513, 1500, 4096])
def test_small_read_chunks_do_not_change_results(monkeypatch, chunk):
    monkeypatch.setattr(pacman_module, "_CHUNK", chunk)
    members = _standard(8) + [("big/desc", _desc("big", "1", extra="%DESC%\n" + "y" * 3000 + "\n"), tarfile.REGTYPE)]
    raw = _tar(members)
    assert _direct(raw)
    assert len(_same_as_tarfile(raw)[1]) == 9


def test_blocks_reader_short_reads_only_at_end():
    blocks = pacman_module._Blocks(io.BytesIO(b"abcdefghij"))
    assert blocks.take(4) == b"abcd"
    blocks.skip(2)
    assert blocks.take(10) == b"ghij"
    assert blocks.take(1) == b""
    with pytest.raises(pacman_module._Unusual):
        blocks.skip(1)


def test_corrupt_gzip_falls_back_and_raises_like_before():
    raw = bytearray(gzip.compress(_tar(_standard(20))))
    raw[len(raw) // 2] ^= 0xFF
    assert _outcome(pacman_module._parse_db_tar_gz, bytes(raw)) == _outcome(pacman_module._parse_db_tar_gz_reference, bytes(raw))
    assert _outcome(pacman_module._parse_db_tar_gz, b"plain text")[0] == "error"


def _seal(raw, offset):
    """Recompute a header's checksum so corruption elsewhere in it is not caught by the checksum."""
    head = raw[offset:offset + 512]
    total = sum(head[:148]) + 32 * 8 + sum(head[156:])
    raw[offset + 148:offset + 156] = b"%06o\0 " % total


@pytest.mark.parametrize("field", ["mode", "uid", "gid", "mtime", "devmajor", "devminor"])
@pytest.mark.parametrize("garbage", [b"zzzzzzz\0", b"12 34\0\0\0", b"\0\0\0\x0012", b"-000001\0"])
@pytest.mark.parametrize("which_header", [0, 3, 8])
def test_garbage_in_ignored_numeric_fields_is_not_accepted(field, garbage, which_header):
    """tarfile parses these fields in every header and rejects garbage (the first header
    with an error, later ones by silently ending the archive); the direct reader must not
    accept what tarfile rejects, even when the checksum has been made to match."""
    start, end = {"mode": (100, 108), "uid": (108, 116), "gid": (116, 124), "mtime": (136, 148),
                  "devmajor": (329, 337), "devminor": (337, 345)}[field]
    raw = bytearray(_tar(_standard(6)))
    offsets = [o for o in range(0, len(raw) - 512, 512) if raw[o:o + 100].strip(b"\0")]
    offset = offsets[min(which_header, len(offsets) - 1)]
    raw[offset + start:offset + end] = garbage.ljust(end - start, b"\0")[: end - start]
    _seal(raw, offset)
    _same_as_tarfile(bytes(raw))


def test_mutated_headers_never_change_the_outcome():
    """Flip bytes of a valid archive, mostly inside header blocks; the direct reader must
    either decline (tarfile decides) or return exactly what tarfile returns."""
    rng = random.Random(20260919)
    clean = _tar(_standard(6))
    headers = [offset for offset in range(0, len(clean) - 512, 512) if clean[offset:offset + 100].strip(b"\0")]
    direct = 0
    for _ in range(1500):
        raw = bytearray(clean)
        for _ in range(rng.choice([1, 1, 2, 3])):
            if rng.random() < 0.85:
                position = rng.choice(headers) + rng.randrange(512)
            else:
                position = rng.randrange(len(raw))
            raw[position] = rng.choice([0, 0xFF, 0x80, ord("0"), ord("7"), ord("8"), ord(" "), rng.randrange(256)])
        if rng.random() < 0.75:
            for offset in headers:
                if rng.random() < 0.8:
                    _seal(raw, offset)
        _same_as_tarfile(bytes(raw))
        direct += _direct(bytes(raw))
    assert direct > 50  # the fuzz must not be all declines, or it proves nothing


def _old_parse_desc(desc_text):
    fields = {}
    lines = desc_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("%") and line.endswith("%"):
            key = line.strip("%")
            i += 1
            value_lines = []
            while i < len(lines) and lines[i] != "":
                value_lines.append(lines[i])
                i += 1
            fields[key] = "\n".join(value_lines)
        i += 1
    return fields


def test_parse_desc_matches_the_index_based_original():
    rng = random.Random(7)
    pieces = ["%NAME%", "%VERSION%", "%", "%%", "%A", "B%", "value", "x y", "", "", "\n", "\r\n", "\r", "\x0b",
              "\x85", " ", "%FILENAME%", " ", "é", "%NAME%\nother"]
    for _ in range(3000):
        text = rng.choice(["\n", "\n", "\r\n", ""]).join(rng.choice(pieces) for _ in range(rng.randrange(0, 14)))
        assert pacman_module._parse_desc(text) == _old_parse_desc(text), repr(text)
