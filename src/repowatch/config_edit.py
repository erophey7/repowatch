"""Serialize CLI/dashboard config edits and replace YAML without exposing secrets."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from functools import wraps
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Callable, Iterator

import yaml

from repowatch.config import ConfigError, load_config


@contextmanager
def config_lock(path: str | Path) -> Iterator[None]:
    path = Path(path)
    # Lock the containing directory: root CLI must not leave a root-owned
    # lock file that later prevents the service user from editing the YAML.
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def locked_config(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def wrapper(config_path: str | Path, *args: Any, **kwargs: Any) -> Any:
        with config_lock(config_path):
            return function(config_path, *args, **kwargs)
    return wrapper


def atomic_config(path: str | Path, content: str) -> None:
    path = Path(path)
    if path.is_symlink():
        raise ConfigError('config.yaml must be a regular file, not a symlink')
    original = path.stat()
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(original.st_mode) & 0o660)
            if (original.st_uid, original.st_gid) != (os.geteuid(), os.getegid()):
                os.fchown(stream.fileno(), original.st_uid, original.st_gid)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        load_config(name)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@locked_config
def set_password_hash(config_path: str | Path, password_hash: str) -> None:
    path = Path(config_path)
    load_config(path)
    text = path.read_text(encoding='utf-8')
    node = yaml.compose(text)
    if not isinstance(node, yaml.MappingNode):
        raise ConfigError('config.yaml must be a mapping')
    matches = [(key, value) for key, value in node.value if key.value == 'admin_password_hash']
    if len(matches) > 1:
        raise ConfigError('admin_password_hash is specified more than once')
    if matches:
        key, value = matches[0]
        segment = text[value.start_mark.index:value.end_mark.index]
        if value.start_mark.index < key.end_mark.index or segment.startswith(('&', '*', '!')):
            # YAML aliases may point at another field's source location; never
            # rewrite that shared scalar as though it belonged only to this key.
            raw = yaml.safe_load(text)
            raw['admin_password_hash'] = password_hash
            text = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
        else:
            suffix = '\n' if segment.endswith('\n') else ''
            text = text[:value.start_mark.index] + ' ' + json.dumps(password_hash) + suffix + text[value.end_mark.index:]
    elif node.flow_style:
        # Flow mappings have no safe line to append a block mapping entry to.
        raw = yaml.safe_load(text)
        raw['admin_password_hash'] = password_hash
        text = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    else:
        # Insert before an optional YAML document end marker.
        index = node.end_mark.index
        text = text[:index].rstrip('\n') + '\nadmin_password_hash: ' + json.dumps(password_hash) + '\n' + text[index:]
    atomic_config(path, text)
