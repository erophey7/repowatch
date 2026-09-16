"""Self-update: check GitHub Releases for a newer repowatch and install it.

Deliberately separate from scripts/upgrade-system.py (the offline,
backup+install/activate/smoke+rollback system upgrade run by an operator from
a reviewed release checkout) — this is the network-fetching half: check a
configured GitHub repo's latest (non-draft, non-prerelease) release, verify
the downloaded wheel's SHA256 against a checksum file published alongside it
in the same release, and `pip install` the verified wheel into the current
environment. It does not touch config.yaml, state_db, or nginx — only the
installed Python package.

No new runtime dependency: GitHub's REST API is plain HTTPS JSON, fetched
with the already-required httpx (async, per the project's convention — see
CLAUDE.md); the checksum itself is stdlib hashlib, the same primitive
parsers/dnf.py and parsers/apt_rpm.py already use for index integrity.

This protects against a corrupted download or a network-level tamper — NOT
against a compromised GitHub account/release (the checksum file travels in
the exact same release as the wheel it describes, so whoever could swap one
could swap both). That tradeoff was made explicitly, not overlooked: an
alternative GPG-signed release was considered and rejected as a heavier
mechanism than this project needs right now. There is no default keyring or
"warn and install anyway" fallback either way — a release without a matching
checksum asset, or a checksum mismatch, refuses the update outright."""

from __future__ import annotations

import asyncio
import hashlib
import httpx
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as installed_version
from pathlib import Path

_GITHUB_API = "https://api.github.com"
_CHUNK_SIZE = 65536
_SHA256_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")


class SelfUpdateError(Exception):
    """A self-update precondition failed, or the release could not be
    trusted — the caller must not install anything in either case."""


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str


@dataclass(frozen=True)
class Release:
    tag: str
    version: str  # tag with a leading "v" stripped, if present
    wheel: ReleaseAsset
    checksum: ReleaseAsset


def current_version() -> str:
    try:
        return installed_version("repowatch")
    except PackageNotFoundError as exc:
        raise SelfUpdateError(
            "repowatch has no installed package metadata — self-update needs a "
            "pip-installed repowatch (e.g. `make install`), not a source checkout"
        ) from exc


def _parse_version(value: str) -> tuple[int, ...] | None:
    parts = re.findall(r"\d+", value)
    return tuple(int(p) for p in parts) if parts else None


def _is_newer(candidate: str, current: str) -> bool:
    """True if candidate > current. Falls back to plain inequality when
    either string doesn't parse as a dotted version — this only decides
    whether to bother reinstalling, not whether the release is trustworthy
    (that's the checksum check's job, unconditionally)."""
    a, b = _parse_version(candidate), _parse_version(current)
    if a is None or b is None:
        return candidate != current
    return a > b


async def fetch_latest_release(repo: str, client: httpx.AsyncClient) -> Release:
    """repo is "owner/name". GitHub's "latest" endpoint already excludes
    draft and prerelease releases, so this never picks up a prerelease by
    accident.

    Raises SelfUpdateError if there's no (suitable) release, or it doesn't
    carry exactly one .whl asset with a matching <name>.sha256 checksum
    asset — this project only ever builds one universal (py3-none-any)
    wheel per release, so "exactly one" is a real invariant, not an
    arbitrary restriction.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise SelfUpdateError(f"invalid --repo {repo!r}, expected \"owner/name\"")
    try:
        resp = await client.get(
            f"{_GITHUB_API}/repos/{repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json"}, timeout=30,
        )
    except httpx.HTTPError as exc:
        raise SelfUpdateError(f"failed to reach GitHub: {exc}") from exc
    if resp.status_code == 404:
        raise SelfUpdateError(f"no releases found for {repo} (nonexistent, private, or draft-only repo)")
    resp.raise_for_status()
    data = resp.json()
    tag = data.get("tag_name")
    if not tag:
        raise SelfUpdateError(f"GitHub release for {repo} has no tag_name")

    assets = {a["name"]: ReleaseAsset(a["name"], a["browser_download_url"]) for a in data.get("assets", [])}
    wheels = [a for name, a in assets.items() if name.endswith(".whl")]
    if len(wheels) != 1:
        raise SelfUpdateError(f"release {tag} must have exactly one .whl asset, found {len(wheels)}")
    wheel = wheels[0]

    checksum_name = wheel.name + ".sha256"
    if checksum_name not in assets:
        raise SelfUpdateError(
            f"release {tag}'s wheel has no {checksum_name} asset — "
            f"refusing to trust an unverifiable download"
        )
    return Release(tag=tag, version=tag.lstrip("v"), wheel=wheel, checksum=assets[checksum_name])


async def _download(client: httpx.AsyncClient, url: str, dest: Path) -> None:
    try:
        async with client.stream("GET", url, timeout=60, follow_redirects=True) as resp:
            resp.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(_CHUNK_SIZE):
                    f.write(chunk)
    except httpx.HTTPError as exc:
        raise SelfUpdateError(f"failed to download {url}: {exc}") from exc


def _extract_sha256(checksum_text: str, wheel_name: str) -> str:
    """Accepts a bare hex digest, or the common `sha256sum` output format
    ("<hex>  <filename>", possibly with several other files/lines mixed
    in) — picks the line naming this wheel if there's more than one hash
    in the file, otherwise the first (only) one found."""
    for line in checksum_text.splitlines():
        if wheel_name in line:
            match = _SHA256_RE.search(line)
            if match:
                return match.group(0).lower()
    match = _SHA256_RE.search(checksum_text)
    if match:
        return match.group(0).lower()
    raise SelfUpdateError(f"no SHA256 hex digest found in the checksum file for {wheel_name}")


async def download_and_verify(
    release: Release, client: httpx.AsyncClient, tmp_dir: Path,
) -> Path:
    """Downloads the wheel and its checksum file, verifies the wheel's
    SHA256 against it, and returns the wheel's local path. Raises
    SelfUpdateError, never returns a path that wasn't verified."""
    wheel_path = tmp_dir / release.wheel.name
    checksum_path = tmp_dir / release.checksum.name
    await _download(client, release.wheel.url, wheel_path)
    await _download(client, release.checksum.url, checksum_path)

    expected = _extract_sha256(checksum_path.read_text(encoding="utf-8", errors="replace"), release.wheel.name)
    actual = await asyncio.to_thread(lambda: hashlib.sha256(wheel_path.read_bytes()).hexdigest())
    if actual != expected:
        raise SelfUpdateError(
            f"checksum mismatch for {release.wheel.name}: expected {expected}, got {actual} — "
            f"refusing to install a corrupted/tampered download"
        )
    return wheel_path


def install_wheel(wheel_path: Path) -> None:
    """Installs into whatever environment `repowatch` itself is currently
    running from (sys.executable) — inside a venv, that's already the venv's
    own python, no extra layout lookup needed."""
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", str(wheel_path)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SelfUpdateError(f"pip install failed:\n{exc.stderr}") from exc


async def run_self_update(repo: str, *, check_only: bool = False, force: bool = False) -> str:
    current = current_version()
    async with httpx.AsyncClient() as client:
        release = await fetch_latest_release(repo, client)
        if not force and not _is_newer(release.version, current):
            return f"already up to date: installed {current}, latest release is {release.tag}"
        if check_only:
            return f"update available (not installed, --check): {current} -> {release.tag}"
        with tempfile.TemporaryDirectory() as tmp:
            wheel_path = await download_and_verify(release, client, Path(tmp))
            await asyncio.to_thread(install_wheel, wheel_path)
    return (
        f"updated: {current} -> {release.tag}. Restart repowatch's process(es) "
        f"(systemd: `systemctl restart repowatch`; supervise: restart the process) "
        f"to run the new version."
    )
