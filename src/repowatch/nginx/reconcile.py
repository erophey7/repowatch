"""Cheap inputs and output integrity checks for repeated privileged reconciliation."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3

from repowatch.config.models import Config
from repowatch.storage.database import Database
from repowatch.storage.repositories import RepositoriesStore


def renderer_digest() -> str:
    """Invalidate across installed code/template changes, without version bumps."""
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted([*root.rglob('*.py'), root / 'nginx/probe.js']):
        digest.update(str(path.relative_to(root)).encode() + b'\0')
        digest.update(path.read_bytes())
    return digest.hexdigest()


def input_digest(config: Config, policy: dict) -> str | None:
    """Ignore request traffic and successful checks with unchanged catalogs.

    All repository revisions matter: even orphan catalogs participate in the
    duplicate query's canonical-owner selection. Old schemas cannot use the fast
    path. Database replacement invalidates saved revisions through file identity.
    """
    catalog = None
    if config.nginx.enable_dedup and config.state_db.is_file():
        try:
            info = config.state_db.stat()
            revisions = RepositoriesStore(Database(config.state_db, read_only=True)).get_snapshot_revisions()
            catalog = (info.st_dev, info.st_ino, revisions)
        except (OSError, sqlite3.DatabaseError):
            return None
    data = (asdict(config), policy, renderer_digest(), catalog)
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def output_digest(paths: list[Path]) -> str | None:
    """Hash every managed output; timestamps alone do not establish integrity."""
    digest = hashlib.sha256()
    try:
        for path in paths:
            file_digest = hashlib.sha256()
            with path.open('rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    file_digest.update(chunk)
            digest.update(file_digest.digest())
    except OSError:
        return None
    return digest.hexdigest()


def rendered_digest(contents: list[str]) -> str:
    """Same digest as output_digest, preserving individual file boundaries."""
    return hashlib.sha256(b''.join(hashlib.sha256(text.encode()).digest()
                                  for text in contents)).hexdigest()


def matches(status: Path, inputs: str | None, paths: list[Path]) -> bool:
    """Accept only successful cached inputs whose managed outputs remain intact."""
    if inputs is None:
        return False
    try:
        saved = json.loads(status.read_text())
    except (OSError, ValueError):
        return False
    return (isinstance(saved, dict) and saved.get('ok') is True
            and saved.get('inputs') == inputs and isinstance(saved.get('active'), str)
            and saved['active'] == output_digest(paths))
