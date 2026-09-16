"""Translate authenticated HTTP repository edits into configuration operations."""

from __future__ import annotations

from pathlib import Path
from repowatch.config.edit import locked_config
from repowatch.operations.repositories import add_repo, update_repo, delete_repo, InvalidRepository, RepositoryNotFound, RepositoryConflict
from repowatch.storage.access import AdminSession
from repowatch.web.access import load_admin_config



@locked_config
def add_repo_payload(
    config_path: str | Path, admin_session: AdminSession | None, body: dict
) -> tuple[int, dict]:
    """Add a repository to config.yaml (read-modify-write). config.yaml
    stays the single source of truth — run_forever() and /api/repos both
    re-read the file themselves, so there's no in-memory state to keep in
    sync; the new repository is picked up within check_interval.

    Returns (status, payload) — see repos_list_payload for why this is
    factored out into a plain function (testable without a real socket).
    """
    current, error = load_admin_config(config_path, admin_session)
    if error is not None:
        return error

    try:
        return 201, add_repo(config_path, current, body)
    except RepositoryNotFound as exc:
        return 404, {"error": str(exc)}
    except RepositoryConflict as exc:
        return 409, {"error": str(exc)}
    except InvalidRepository as exc:
        return 400, {"error": str(exc)}


@locked_config
def update_repo_payload(
    config_path: str | Path, admin_session: AdminSession | None, repo_id: str, body: dict
) -> tuple[int, dict]:
    """Edit an existing repository — full object replacement (the dashboard
    pre-fills the form with current values and sends the whole object, same
    as when adding, see add_repo_payload). The id cannot be changed through
    this endpoint: repo_state/warmed_packages/prefetch_bans in sqlite are
    keyed by repo_id — a silent rename would orphan all history from the
    repository. To change the id, delete and re-add.
    """
    current, error = load_admin_config(config_path, admin_session)
    if error is not None:
        return error

    try:
        return 200, update_repo(config_path, current, repo_id, body)
    except RepositoryNotFound as exc:
        return 404, {"error": str(exc)}
    except RepositoryConflict as exc:
        return 409, {"error": str(exc)}
    except InvalidRepository as exc:
        return 400, {"error": str(exc)}


@locked_config
def delete_repo_payload(
    config_path: str | Path, admin_session: AdminSession | None, repo_id: str
) -> tuple[int, dict]:
    """Remove a repository from config.yaml. Does not touch sqlite
    (repo_state/repo_events/warmed_packages/prefetch_bans for this repo_id) —
    orphaned rows are harmless: the dashboard and status API both build their
    repository list from config.yaml (see repos_list_payload), so a deleted
    repository simply stops showing up anywhere."""
    current, error = load_admin_config(config_path, admin_session)
    if error is not None:
        return error

    try:
        return 200, delete_repo(config_path, current, repo_id)
    except RepositoryNotFound as exc:
        return 404, {"error": str(exc)}
    except RepositoryConflict as exc:
        return 409, {"error": str(exc)}
    except InvalidRepository as exc:
        return 400, {"error": str(exc)}
