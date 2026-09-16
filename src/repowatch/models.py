"""Repository index and change models, independent of storage and transports."""

from __future__ import annotations

from dataclasses import dataclass, field

@dataclass
class RepoSnapshot:
    """Result of parsing one repository's index at the current moment."""

    repo_id: str
    # key — a unique package identifier, usually "name-version"
    packages: dict[str, str] = field(default_factory=dict)
    # the same key -> the "bare" package name (without version) — needed
    # separately because key = f"{name}-{version}" can't be split back
    # unambiguously (both name and version may contain hyphens). Used for
    # bans by package name.
    names: dict[str, str] = field(default_factory=dict)
    # the same key -> SHA256 hex digest, only for keys where PackageRef.content_hash
    # was known from the index (docs_dev/ROADMAP.md item 29) — missing keys
    # simply aren't candidates for dedup, not an error.
    content_hashes: dict[str, str] = field(default_factory=dict)


@dataclass
class DiffResult:
    repo_id: str
    changed: bool
    new_packages: list[str]
    removed_packages: list[str]
    # {package_key: filename} for exactly the packages in removed_packages —
    # captured from the previous snapshot before its rows are deleted below.
    # Needed by operations.check.check_repo/cache.purge.purge_removed (docs_dev/ROADMAP.md
    # item 24) to know which file to purge from nginx's cache — the key
    # alone isn't a filename.
    removed_filenames: dict[str, str]
    modified_packages: list[str] = field(default_factory=list)
    modified_filenames: dict[str, str] = field(default_factory=dict)


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
