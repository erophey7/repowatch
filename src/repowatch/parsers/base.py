"""Common interface for repository index parsers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from repowatch.config import RepoConfig
from repowatch.state import RepoSnapshot

USER_AGENT = "repowatch/0.1 (+https://github.com/you/repowatch)"


@dataclass
class IndexHeadResult:
    """Result of a cheap HEAD-based index check, without downloading the body.

    unchanged=True means there's no need to download and parse the index
    this cycle — the content matches what we already saw last time.
    """

    unchanged: bool
    etag: str | None
    last_modified: str | None


@dataclass
class PackageRef:
    """One package from the index — everything needed to (a) build the
    snapshot key and (b) optionally build the file URL for cache warming."""

    name: str
    version: str
    # relative path of the package file from the repository's upstream URL, if known
    filename: str | None = None
    # SHA256 hex digest of the package file, when the index publishes one
    # (docs_dev/ROADMAP.md item 29 — cross-repository dedup). Deliberately
    # left None for formats where a same-algorithm whole-file hash isn't
    # available without downloading the file itself: apk's APKINDEX `C:`
    # field is a different digest (SHA1, base64, "Q1" prefix) that would
    # never match a SHA256 value from apt/pacman/dnf even for a genuinely
    # identical file, and apt-rpm's pkglist RPM headers (parsers/apt_rpm.py)
    # don't currently carry a verified whole-file digest tag — guessing
    # either would risk a false-positive match (serving one package's bytes
    # under a different one's name), so both stay unfilled rather than wrong.
    content_hash: str | None = None

    @property
    def key(self) -> str:
        return f"{self.name}-{self.version}"


class IndexParser(ABC):
    """Implementations: AptParser, PacmanParser, ApkParser, DnfParser (see
    parsers/*.py).

    All network methods take an already-created httpx.AsyncClient as an
    explicit parameter (they never create or hold their own — the caller
    owns the client and its lifecycle, see watcher.check_repo), in keeping
    with the project's general "no global state" principle.
    """

    def __init__(self, repo: RepoConfig, timeout: int = 30):
        self.repo = repo
        self.timeout = timeout

    @abstractmethod
    def index_url(self) -> str:
        """URL of the index file — used both for the HEAD check and for downloading."""

    @abstractmethod
    async def fetch_packages(self, client: httpx.AsyncClient) -> list[PackageRef]:
        """Download and parse the index, return the list of packages.

        Must not silently swallow exceptions — let it raise, the watcher
        decides how to log and retry. CPU-heavy parsing of already-downloaded
        bytes (gzip/tarfile) should go through asyncio.to_thread — otherwise
        concurrent checking of other repositories (see watcher.check_all)
        would stall for the whole duration of parsing.
        """

    async def fetch(self, client: httpx.AsyncClient) -> RepoSnapshot:
        packages = await self.fetch_packages(client)
        return RepoSnapshot(
            repo_id=self.repo.id,
            packages={p.key: p.filename or "" for p in packages},
            names={p.key: p.name for p in packages},
            content_hashes={p.key: p.content_hash for p in packages if p.content_hash},
        )

    async def check_index_changed(
        self,
        client: httpx.AsyncClient,
        prev_etag: str | None,
        prev_last_modified: str | None,
    ) -> IndexHeadResult:
        """A HEAD request to the index instead of a full download.

        Saves bandwidth: the full index (Packages.gz / *.db.tar.gz /
        APKINDEX.tar.gz) can be megabytes, while HEAD fetches no body at
        all. Conditional headers (If-None-Match/If-Modified-Since) get a
        304 from servers that support them; for the rest we compare
        ETag/Last-Modified from the response by hand — in case the server
        replied 200 but the headers are unchanged.

        Unlike urllib, httpx doesn't raise on a non-2xx status by itself —
        304 is an expected, normal status here (not an error), any other
        non-2xx requires an explicit raise_for_status().
        """
        url = self.index_url()
        headers = {"User-Agent": USER_AGENT}
        if prev_etag:
            headers["If-None-Match"] = prev_etag
        if prev_last_modified:
            headers["If-Modified-Since"] = prev_last_modified

        resp = await client.head(url, headers=headers, timeout=self.timeout)

        if resp.status_code == 304:
            return IndexHeadResult(unchanged=True, etag=prev_etag, last_modified=prev_last_modified)

        resp.raise_for_status()

        etag = resp.headers.get("ETag")
        last_modified = resp.headers.get("Last-Modified")

        unchanged = bool(
            (prev_etag and etag and prev_etag == etag)
            or (
                not etag
                and prev_last_modified
                and last_modified
                and prev_last_modified == last_modified
            )
        )
        return IndexHeadResult(unchanged=unchanged, etag=etag, last_modified=last_modified)

    async def _http_get(self, client: httpx.AsyncClient, url: str) -> bytes:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.content
