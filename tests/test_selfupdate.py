import asyncio
import hashlib
import subprocess
from pathlib import Path

import httpx
import pytest

from repowatch.selfupdate import (
    Release,
    ReleaseAsset,
    SelfUpdateError,
    _extract_sha256,
    _is_newer,
    _parse_version,
    current_version,
    download_and_verify,
    fetch_latest_release,
    install_wheel,
    run_self_update,
)


def _release_json(*, tag="v1.2.0", assets=None):
    if assets is None:
        assets = [
            {"name": "repowatch-1.2.0-py3-none-any.whl", "browser_download_url": "https://dl.test/wheel"},
            {"name": "repowatch-1.2.0-py3-none-any.whl.sha256", "browser_download_url": "https://dl.test/sum"},
        ]
    return {"tag_name": tag, "assets": assets}


def _fetch(handler, repo="owner/name"):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_latest_release(repo, client)
    return asyncio.run(run())


# --- version parsing ---

def test_parse_version_extracts_digit_groups():
    assert _parse_version("1.2.0") == (1, 2, 0)
    assert _parse_version("v1.2.0") == (1, 2, 0)
    assert _parse_version("not-a-version") is None


def test_is_newer_compares_numerically_not_lexically():
    assert _is_newer("1.10.0", "1.9.0")
    assert not _is_newer("1.9.0", "1.10.0")
    assert not _is_newer("1.0.0", "1.0.0")


def test_is_newer_falls_back_to_inequality_for_unparseable_versions():
    assert _is_newer("banana", "1.0.0")
    assert not _is_newer("same", "same")


# --- fetch_latest_release ---

def test_fetch_latest_release_returns_wheel_and_checksum():
    release = _fetch(lambda request: httpx.Response(200, json=_release_json()))
    assert release.tag == "v1.2.0"
    assert release.version == "1.2.0"
    assert release.wheel.name == "repowatch-1.2.0-py3-none-any.whl"
    assert release.checksum.name == "repowatch-1.2.0-py3-none-any.whl.sha256"


def test_fetch_latest_release_rejects_invalid_repo_without_a_network_call():
    called = []
    def handler(request):
        called.append(request)
        return httpx.Response(200, json=_release_json())
    with pytest.raises(SelfUpdateError, match="invalid --repo"):
        _fetch(handler, repo="not-a-valid-repo-string")
    assert called == []


def test_fetch_latest_release_404_raises():
    with pytest.raises(SelfUpdateError, match="no releases found"):
        _fetch(lambda request: httpx.Response(404, json={}))


def test_fetch_latest_release_missing_tag_raises():
    with pytest.raises(SelfUpdateError, match="tag_name"):
        _fetch(lambda request: httpx.Response(200, json={"assets": []}))


def test_fetch_latest_release_requires_exactly_one_wheel():
    with pytest.raises(SelfUpdateError, match="exactly one .whl"):
        _fetch(lambda request: httpx.Response(200, json=_release_json(assets=[])))

    two_wheels = [
        {"name": "a-py3-none-any.whl", "browser_download_url": "https://dl.test/a"},
        {"name": "b-py3-none-any.whl", "browser_download_url": "https://dl.test/b"},
    ]
    with pytest.raises(SelfUpdateError, match="exactly one .whl"):
        _fetch(lambda request: httpx.Response(200, json=_release_json(assets=two_wheels)))


def test_fetch_latest_release_requires_matching_checksum_asset():
    wheel_only = [{"name": "repowatch-1.2.0-py3-none-any.whl", "browser_download_url": "https://dl.test/wheel"}]
    with pytest.raises(SelfUpdateError, match="\\.sha256"):
        _fetch(lambda request: httpx.Response(200, json=_release_json(assets=wheel_only)))


# --- _extract_sha256 ---

def test_extract_sha256_accepts_bare_digest():
    digest = "a" * 64
    assert _extract_sha256(digest + "\n", "repowatch.whl") == digest


def test_extract_sha256_accepts_sha256sum_style_line_and_picks_matching_file():
    text = f"{'b' * 64}  other-package.whl\n{'c' * 64}  repowatch.whl\n"
    assert _extract_sha256(text, "repowatch.whl") == "c" * 64


def test_extract_sha256_raises_when_nothing_found():
    with pytest.raises(SelfUpdateError, match="no SHA256"):
        _extract_sha256("not a hash", "repowatch.whl")


# --- download_and_verify ---

def _run_download_and_verify(release, handler):
    async def run(tmp_path):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await download_and_verify(release, client, tmp_path)
    return run


def test_download_and_verify_accepts_matching_checksum(tmp_path):
    content = b"fake wheel bytes"
    digest = hashlib.sha256(content).hexdigest()
    release = Release(
        tag="v1.0.0", version="1.0.0",
        wheel=ReleaseAsset("pkg.whl", "https://dl.test/pkg.whl"),
        checksum=ReleaseAsset("pkg.whl.sha256", "https://dl.test/pkg.whl.sha256"),
    )
    def handler(request):
        if request.url.path.endswith("pkg.whl"):
            return httpx.Response(200, content=content)
        return httpx.Response(200, content=f"{digest}  pkg.whl\n".encode())
    wheel_path = asyncio.run(_run_download_and_verify(release, handler)(tmp_path))
    assert wheel_path.read_bytes() == content


def test_download_and_verify_rejects_mismatched_checksum(tmp_path):
    release = Release(
        tag="v1.0.0", version="1.0.0",
        wheel=ReleaseAsset("pkg.whl", "https://dl.test/pkg.whl"),
        checksum=ReleaseAsset("pkg.whl.sha256", "https://dl.test/pkg.whl.sha256"),
    )
    def handler(request):
        if request.url.path.endswith("pkg.whl"):
            return httpx.Response(200, content=b"real content")
        return httpx.Response(200, content=b"0" * 64)
    with pytest.raises(SelfUpdateError, match="checksum mismatch"):
        asyncio.run(_run_download_and_verify(release, handler)(tmp_path))


def test_download_and_verify_wraps_http_errors(tmp_path):
    release = Release(
        tag="v1.0.0", version="1.0.0",
        wheel=ReleaseAsset("pkg.whl", "https://dl.test/pkg.whl"),
        checksum=ReleaseAsset("pkg.whl.sha256", "https://dl.test/pkg.whl.sha256"),
    )
    with pytest.raises(SelfUpdateError, match="failed to download"):
        asyncio.run(_run_download_and_verify(release, lambda request: httpx.Response(503))(tmp_path))


# --- install_wheel ---

def test_install_wheel_invokes_pip_with_current_interpreter(monkeypatch, tmp_path):
    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr("repowatch.selfupdate.subprocess.run", fake_run)
    wheel = tmp_path / "pkg.whl"
    wheel.write_bytes(b"x")
    install_wheel(wheel)
    assert calls[0][0] == __import__("sys").executable
    assert "--force-reinstall" in calls[0]
    assert str(wheel) in calls[0]


def test_install_wheel_raises_on_pip_failure(monkeypatch, tmp_path):
    def fake_run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, output="", stderr="boom")
    monkeypatch.setattr("repowatch.selfupdate.subprocess.run", fake_run)
    with pytest.raises(SelfUpdateError, match="pip install failed"):
        install_wheel(tmp_path / "pkg.whl")


# --- current_version ---

def test_current_version_raises_without_package_metadata(monkeypatch):
    from importlib.metadata import PackageNotFoundError
    def raise_not_found(name):
        raise PackageNotFoundError(name)
    monkeypatch.setattr("repowatch.selfupdate.installed_version", raise_not_found)
    with pytest.raises(SelfUpdateError, match="no installed package metadata"):
        current_version()


# --- run_self_update (end to end, install_wheel mocked out) ---

def _mock_release_transport(content=b"wheel bytes"):
    digest = hashlib.sha256(content).hexdigest()
    def handler(request):
        path = request.url.path
        if path.endswith("/releases/latest"):
            return httpx.Response(200, json=_release_json(assets=[
                {"name": "pkg.whl", "browser_download_url": "https://dl.test/pkg.whl"},
                {"name": "pkg.whl.sha256", "browser_download_url": "https://dl.test/pkg.whl.sha256"},
            ]))
        if path.endswith("pkg.whl"):
            return httpx.Response(200, content=content)
        return httpx.Response(200, content=f"{digest}  pkg.whl\n".encode())
    return handler


def _patch_async_client(monkeypatch, handler):
    """Replaces httpx.AsyncClient() with one wired to a MockTransport.
    Captures the REAL class before patching — the replacement lambda must
    not resolve httpx.AsyncClient again at call time, or it would recurse
    into itself."""
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "repowatch.selfupdate.httpx.AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler)),
    )


def test_run_self_update_already_up_to_date(monkeypatch):
    monkeypatch.setattr("repowatch.selfupdate.installed_version", lambda name: "1.2.0")
    _patch_async_client(monkeypatch, _mock_release_transport())
    result = asyncio.run(run_self_update("owner/name"))
    assert "already up to date" in result


def test_run_self_update_check_only_does_not_install(monkeypatch):
    monkeypatch.setattr("repowatch.selfupdate.installed_version", lambda name: "1.0.0")
    _patch_async_client(monkeypatch, _mock_release_transport())
    calls = []
    monkeypatch.setattr("repowatch.selfupdate.install_wheel", lambda path: calls.append(path))
    result = asyncio.run(run_self_update("owner/name", check_only=True))
    assert "update available" in result
    assert calls == []


def test_run_self_update_installs_verified_wheel(monkeypatch):
    monkeypatch.setattr("repowatch.selfupdate.installed_version", lambda name: "1.0.0")
    _patch_async_client(monkeypatch, _mock_release_transport())
    installed = []
    monkeypatch.setattr("repowatch.selfupdate.install_wheel", lambda path: installed.append(Path(path).read_bytes()))
    result = asyncio.run(run_self_update("owner/name"))
    assert "updated: 1.0.0 -> v1.2.0" in result
    assert installed == [b"wheel bytes"]


def test_run_self_update_force_reinstalls_same_version(monkeypatch):
    monkeypatch.setattr("repowatch.selfupdate.installed_version", lambda name: "1.2.0")
    _patch_async_client(monkeypatch, _mock_release_transport())
    installed = []
    monkeypatch.setattr("repowatch.selfupdate.install_wheel", lambda path: installed.append(path))
    result = asyncio.run(run_self_update("owner/name", force=True))
    assert "updated:" in result
    assert len(installed) == 1
