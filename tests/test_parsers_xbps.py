import asyncio
import io
import plistlib
import shutil
import subprocess
import tarfile

import httpx
import pytest

from repowatch.errors import ConfigError
from repowatch.config.models import RepoConfig
from repowatch.parsers import PARSERS, XbpsParser
from repowatch.parsers.xbps import _parse_repodata

pytestmark = pytest.mark.skipif(
    shutil.which("zstd") is None, reason="system 'zstd' binary is not installed"
)


def repo(**overrides):
    return RepoConfig(**{"id": "void-current", "type": "xbps", "upstream": "https://example.org/current",
                         "arch": "x86_64", "prefetch": False, **overrides})


def _tar_with_index_plist(name: str, payload: bytes) -> bytes:
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    # Compressed the same way the parser decompresses it: the real system
    # 'zstd' binary, not compression.zstd — see parsers/xbps.py's docstring
    # for why this parser deliberately avoids that Python 3.14+ stdlib
    # module (repodata is Zstandard-compressed unconditionally, unlike
    # dnf's optional support, so gating it on a stdlib module still absent
    # from this project's normal Python 3.11+ floor was the wrong trade).
    return subprocess.run(["zstd", "-c", "-q"], input=tar_buf.getvalue(),
                          capture_output=True, check=True).stdout


def _build_repodata(entries: dict) -> bytes:
    """entries: {pkgname: {pkgver, architecture, filename-sha256?, filename-size?, ...}} —
    same shape verified by hand against the real
    repo-default.voidlinux.org/current/x86_64-repodata."""
    return _tar_with_index_plist("index.plist", plistlib.dumps(entries, fmt=plistlib.FMT_XML))


REPODATA = _build_repodata({
    "0ad": {"pkgver": "0ad-0.27.1_6", "architecture": "x86_64",
            "filename-sha256": "a" * 64, "filename-size": 4303556},
    "bash": {"pkgver": "bash-5.2.021_1", "architecture": "x86_64",
             "filename-sha256": "b" * 64, "filename-size": 1234},
})


def fetch(parser, repodata=REPODATA):
    requests = []
    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, content=repodata)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await parser.fetch(client)
    return asyncio.run(run()), requests


def test_full_fetch_strips_pkgname_prefix_and_builds_filename():
    assert PARSERS["xbps"] is XbpsParser
    snapshot, requested = fetch(XbpsParser(repo()))
    assert snapshot.packages == {
        "0ad-0.27.1_6": "0ad-0.27.1_6.x86_64.xbps",
        "bash-5.2.021_1": "bash-5.2.021_1.x86_64.xbps",
    }
    assert snapshot.names["0ad-0.27.1_6"] == "0ad"
    assert snapshot.content_hashes["0ad-0.27.1_6"] == "a" * 64
    assert requested == ["/current/x86_64-repodata"]


def test_index_url_uses_arch_suffix():
    assert XbpsParser(repo(arch="i686")).index_url() == "https://example.org/current/i686-repodata"
    assert XbpsParser(repo(upstream="https://example.org/current/nonfree")).index_url() == \
        "https://example.org/current/nonfree/x86_64-repodata"


def test_head_uses_repodata_url_and_passes_conditional_headers():
    parser = XbpsParser(repo())
    def handler(request):
        assert request.method == "HEAD"
        assert str(request.url) == "https://example.org/current/x86_64-repodata"
        assert request.headers["If-None-Match"] == "v1"
        return httpx.Response(304)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await parser.check_index_changed(client, "v1", None)
            assert result.unchanged
    asyncio.run(run())


def test_content_hash_only_set_for_valid_sha256():
    repodata = _build_repodata({
        "nohash": {"pkgver": "nohash-1_1", "architecture": "x86_64"},
        "badhash": {"pkgver": "badhash-1_1", "architecture": "x86_64", "filename-sha256": "not-hex"},
    })
    snapshot, _ = fetch(XbpsParser(repo()), repodata)
    assert "nohash-1_1" not in snapshot.content_hashes
    assert "badhash-1_1" not in snapshot.content_hashes


def test_pkgver_not_matching_pkgname_is_rejected():
    repodata = _build_repodata({"foo": {"pkgver": "totally-different-1_1", "architecture": "x86_64"}})
    with pytest.raises(ValueError, match="pkgver"):
        fetch(XbpsParser(repo()), repodata)


def test_missing_architecture_is_rejected():
    repodata = _build_repodata({"foo": {"pkgver": "foo-1_1"}})
    with pytest.raises(ValueError, match="architecture"):
        fetch(XbpsParser(repo()), repodata)


def test_index_plist_root_must_be_a_dict():
    repodata = _tar_with_index_plist("index.plist", plistlib.dumps(["not", "a", "dict"], fmt=plistlib.FMT_XML))
    with pytest.raises(ValueError, match="dict"):
        _parse_repodata(repodata)


def test_missing_index_plist_member_is_rejected():
    repodata = _tar_with_index_plist("index-meta.plist", b"")
    with pytest.raises(ValueError, match="index.plist"):
        _parse_repodata(repodata)


def test_empty_repository_is_valid():
    repodata = _build_repodata({})
    snapshot, _ = fetch(XbpsParser(repo()), repodata)
    assert snapshot.packages == {}


def test_zstd_binary_missing_has_actionable_error(monkeypatch):
    monkeypatch.setattr("repowatch.parsers.xbps.shutil.which", lambda name: None)
    with pytest.raises(ValueError, match="zstd"):
        _parse_repodata(b"anything")


def test_zstd_decompression_failure_is_reported():
    with pytest.raises(ValueError, match="zstd"):
        _parse_repodata(b"not actually zstd-compressed data")


def test_verify_signature_rejected_at_config_time():
    with pytest.raises(ConfigError, match="verify_signature"):
        repo(verify_signature=True)
