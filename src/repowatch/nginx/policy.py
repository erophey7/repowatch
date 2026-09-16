"""Validate the ownership of the privileged nginx installation policy."""

from __future__ import annotations

from pathlib import Path
from repowatch.errors import ConfigError

def _check_policy_owner(policy_file: Path) -> None:
    for path in (policy_file, *policy_file.parents):
        info = path.lstat()
        if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise ConfigError('nginx policy and parent directories must be root-owned and not writable by others')
