"""Repository configuration changes within the caller's config_lock transaction.

The caller loads Config and authorizes the request while holding that same lock.
These operations validate edits and publish YAML without HTTP response handling."""

from __future__ import annotations

import logging
import yaml
from pathlib import Path
from repowatch.config.edit import atomic_config
from repowatch.config.models import Config, RepoConfig
from repowatch.errors import ConfigError

logger = logging.getLogger(__name__)

class InvalidRepository(ValueError):
    """The requested edit would produce an invalid repository configuration."""

class RepositoryNotFound(LookupError):
    """The requested repository does not exist in the locked configuration."""

class RepositoryConflict(ValueError):
    """The requested repository identifier is already in use."""

def delete_repo(config_path: str | Path, current: Config, repo_id: str) -> dict:
    """Remove a repository; an empty installation remains available for administration."""
    if current.repo_by_id(repo_id) is None:
        raise RepositoryNotFound(f'unknown repo_id: {repo_id}')

    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    repos_raw = raw.get("repos", [])
    new_repos_raw = [r for r in repos_raw if r.get("id") != repo_id]

    raw["repos"] = new_repos_raw

    _save_repositories(path, raw)

    logger.info("repository deleted in configuration: id=%s", repo_id)
    return {'deleted': repo_id}


def update_repo(config_path: str | Path, current: Config, repo_id: str, body: dict) -> dict:
    """Replace a repository while preserving its identifier."""
    if current.repo_by_id(repo_id) is None:
        raise RepositoryNotFound(f'unknown repo_id: {repo_id}')

    if not isinstance(body, dict):
        raise InvalidRepository('request body must be a JSON object')

    if body.get("id", repo_id) != repo_id:
        raise InvalidRepository('cannot change id via edit — delete and re-add instead')

    new_body = {**body, "id": repo_id}
    try:
        updated_repo = RepoConfig(**new_body)
    except (ConfigError, TypeError) as exc:
        raise InvalidRepository(f'invalid repository data: {exc}')

    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    repos_raw = raw.get("repos", [])
    for i, r in enumerate(repos_raw):
        if r.get("id") == repo_id:
            repos_raw[i] = new_body
            break
    else:
        raise RepositoryNotFound(f'unknown repo_id: {repo_id}')

    _save_repositories(path, raw)

    logger.info("repository edited in configuration: id=%s", repo_id)
    return {'id': updated_repo.id, 'type': updated_repo.type, 'upstream': updated_repo.upstream}


def add_repo(config_path: str | Path, current: Config, body: dict) -> dict:
    """Validate and append a repository."""
    if not isinstance(body, dict):
        raise InvalidRepository('request body must be a JSON object')

    try:
        new_repo = RepoConfig(**body)
    except (ConfigError, TypeError) as exc:
        raise InvalidRepository(f'invalid repository data: {exc}')

    if any(r.id == new_repo.id for r in current.repos):
        raise RepositoryConflict(f'repository with id={new_repo.id!r} already exists')

    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw.setdefault("repos", []).append(body)

    # Atomic write: to a temp file next to the target, then os.replace() —
    # so config.yaml is never left in a broken state by a write that fails
    # partway through.
    # NOTE: yaml.safe_dump does not preserve comments in the existing file —
    # if the operator hand-wrote config.yaml with comments, they will be
    # lost after the first add through the dashboard (see README).
    _save_repositories(path, raw)

    logger.info("repository added in configuration: id=%s type=%s", new_repo.id, new_repo.type)
    return {'id': new_repo.id, 'type': new_repo.type, 'upstream': new_repo.upstream}


def _save_repositories(path: Path, raw: dict) -> None:
    """Publish a validated repo edit while the caller holds config_lock."""
    try:
        atomic_config(path, yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    except ConfigError as exc:
        raise InvalidRepository(f'invalid configuration: {exc}')
