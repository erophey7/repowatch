import asyncio
import io
import tarfile
from pathlib import Path

import pytest

from repowatch.config import RepoConfig
from repowatch.gpgverify import SignatureError
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

    bash = next(p for p in packages if p.name == "bash")
    assert bash.version == "5.2.026-1"
    assert bash.filename == "bash-5.2.026-1-x86_64.pkg.tar.zst"


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
