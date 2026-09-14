"""XBPS (Void Linux): <arch>-repodata -> packages.

<arch>-repodata is a Zstandard-compressed POSIX tar archive containing
index.plist (an Apple-style XML property list, stdlib plistlib handles it
directly) — a dict keyed by package NAME, each value a dict with at least
pkgver ("<name>-<version>"), architecture, and (usually) filename-sha256/
filename-size. Verified by hand against the real
repo-default.voidlinux.org/current/x86_64-repodata (14785 packages,
2026-09-12): every pkgver was confirmed to start with "<name>-", and the
package file itself is fetchable at exactly "<pkgver>.<architecture>.xbps"
relative to the repo root — there is no separate "filename" key to read.

No architecture filtering here (unlike dnf's noarch handling): Void splits
architectures into separate repodata files (x86_64-repodata,
i686-repodata, ...) and separate directory components (nonfree, multilib,
multilib/nonfree, debug) rather than mixing them into one index the way
RPM-MD does — one RepoConfig (one upstream, one arch) already gets exactly
the packages it should serve, same assumption apk/pacman/apt already make
for their own single-arch-per-index formats.

Decompression shells out to the system `zstd` binary — the same "external
tool required for a specific feature" pattern already used for
gpgv/openssl/apk-tools elsewhere in this project, not a new Python
dependency. Deliberately NOT stdlib `compression.zstd` (Python 3.14+):
unlike dnf, where Zstandard only matters for some repositories, XBPS
repodata is Zstandard-compressed unconditionally — gating that on a stdlib
module still absent from the oldest Python this project otherwise supports
(3.11) would have forced the whole project's floor up just for one repo
type. A system binary keeps xbps on the same Python 3.11+ baseline as
everything else here.

No index-level signature verification: RepoConfig.__post_init__ rejects
verify_signature=true for type=xbps outright (see the comment there) — the
repodata itself isn't signed upstream at all, unlike everywhere else in
this project.
"""

from __future__ import annotations

import asyncio
import io
import plistlib
import re
import shutil
import subprocess

from repowatch.processes import run
import tarfile

import httpx

from repowatch.parsers.base import IndexParser, PackageRef


def _decompress(raw: bytes) -> bytes:
    binary = shutil.which("zstd")
    if binary is None:
        raise ValueError(
            "xbps: repodata is Zstandard-compressed — install the system 'zstd' package"
        )
    try:
        result = run([binary, "-d", "-c", "-q"], input=raw, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"xbps: failed to run zstd: {exc}") from exc
    if result.returncode != 0:
        raise ValueError(f"xbps: zstd decompression failed: {result.stderr.decode(errors='replace')[-500:]}")
    return result.stdout


def _parse_repodata(raw: bytes) -> list[PackageRef]:
    decompressed = _decompress(raw)
    with tarfile.open(fileobj=io.BytesIO(decompressed), mode="r:") as tar:
        member = next((m for m in tar.getmembers() if m.name == "index.plist"), None)
        if member is None or not member.isfile():
            raise ValueError("xbps: index.plist not found inside repodata")
        extracted = tar.extractfile(member)
        if extracted is None:
            raise ValueError("xbps: index.plist has no content")
        data = plistlib.load(extracted)

    if not isinstance(data, dict):
        raise ValueError("xbps: index.plist root is not a dict")

    packages: list[PackageRef] = []
    for pkgname, info in data.items():
        if not isinstance(pkgname, str) or not pkgname or not isinstance(info, dict):
            raise ValueError("xbps: malformed index.plist entry")
        pkgver = info.get("pkgver")
        if not isinstance(pkgver, str) or not pkgver.startswith(pkgname + "-"):
            raise ValueError(f"xbps: pkgver does not match package name: {pkgname!r}")
        version = pkgver[len(pkgname) + 1:]
        if not version:
            raise ValueError(f"xbps: empty version for {pkgname!r}")
        architecture = info.get("architecture")
        if not isinstance(architecture, str) or not architecture:
            raise ValueError(f"xbps: missing architecture for {pkgname!r}")
        filename = f"{pkgver}.{architecture}.xbps"

        # docs_dev/ROADMAP.md item 29 (cross-repo dedup) — filename-sha256
        # is a real whole-file SHA256, the same algorithm apt/pacman/dnf
        # already publish, so it's safe to participate in cross-format
        # matches (unlike apk's different-algorithm "C:" field).
        content_hash = info.get("filename-sha256")
        if not (isinstance(content_hash, str) and re.fullmatch(r"[0-9a-fA-F]{64}", content_hash)):
            content_hash = None

        packages.append(PackageRef(name=pkgname, version=version, filename=filename, content_hash=content_hash))
    return packages


class XbpsParser(IndexParser):
    def index_url(self) -> str:
        return f"{self.repo.upstream.rstrip('/')}/{self.repo.arch}-repodata"

    async def fetch_packages(self, client: httpx.AsyncClient) -> list[PackageRef]:
        raw = await self._http_get(client, self.index_url())
        return await asyncio.to_thread(_parse_repodata, raw)
