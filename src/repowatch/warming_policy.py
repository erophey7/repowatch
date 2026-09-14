"""Package-name selection shared by automatic, manual and replacement warming."""
from __future__ import annotations

import fnmatch
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from repowatch.config import RepoConfig
    from repowatch.state import StateStore


def validate_patterns(name: str, patterns: object) -> None:
    if not isinstance(patterns, list) or len(patterns) > 128:
        raise ValueError(f'{name} must be a list of at most 128 glob patterns')
    if any(not isinstance(p, str) or not p or len(p) > 256 or p != p.strip()
           or any(ord(c) < 32 or ord(c) == 127 for c in p) for p in patterns):
        raise ValueError(f'{name} patterns must be nonempty strings up to 256 characters without surrounding whitespace or control characters')


class WarmingPolicy:
    """Snapshot of one operation's rules; blacklist and exact bans win.

    Compile patterns once per operation, never once per chunk or package.
    Match case-sensitively against full catalog names, not filenames or keys.
    Nix callers apply this to roots and retain each allowed root's closure.
    """

    def __init__(self, repo: RepoConfig, store: StateStore):
        self.whitelist = tuple(re.compile(fnmatch.translate(p)) for p in repo.prefetch_whitelist)
        self.blacklist = tuple(re.compile(fnmatch.translate(p)) for p in repo.prefetch_blacklist)
        self.banned = set(store.get_banned_packages(repo.id))
        self.filtered = bool(self.whitelist or self.blacklist or self.banned)
        self.names = store.get_names(repo.id) if self.filtered else {}

    def allows(self, package_key: str) -> bool:
        if not self.filtered:
            return True
        name = self.names.get(package_key)
        # Do not guess a name by stripping versions: formats have different
        # identity rules. Missing catalog metadata cannot bypass active filters.
        if not name or name in self.banned:
            return False
        return (not any(p.match(name) for p in self.blacklist)
                and (not self.whitelist or any(p.match(name) for p in self.whitelist)))
