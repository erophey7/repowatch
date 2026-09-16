"""Validate HTTP package actions and invoke cache operations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from repowatch.config.models import Config
from repowatch.operations.cleanup import purge_items, resolved_purge_keys
from repowatch.operations.warm import warm_selected
from repowatch.runtime.context import ServiceState
from repowatch.storage.access import AdminSession
from repowatch.web.access import load_admin_config, load_request_config



def warm_packages_payload(
    config_path: str | Path,
    store: ServiceState,
    repo_id: str,
    admin_session: AdminSession | None,
    body: dict,
) -> tuple[int, dict]:
    """Warm specific packages manually (dashboard: "warm selected"),
    regardless of repo.prefetch and without waiting for an auto-diff or
    real client requests. body: {"package_keys": ["name-version", ...]}.
    """
    current, error = load_admin_config(config_path, admin_session)
    if error is not None:
        return error

    repo = current.repo_by_id(repo_id)
    if repo is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}

    if not isinstance(body, dict) or not isinstance(body.get("package_keys"), list):
        return 400, {"error": 'request body must be {"package_keys": ["name-version", ...]}'}

    requested_keys = [str(k) for k in body["package_keys"]]
    store.bandwidth.bind(config_path)
    return 200, asyncio.run(warm_selected(current, repo, store, requested_keys))


def purge_candidates_payload(
    config_path: str | Path, store: ServiceState, repo_id: str,
    *, current: Config | None = None,
) -> tuple[int, dict]:
    """Manual cache purge, step 1 (dashboard "Scan for stale entries",
    docs_dev/ROADMAP.md): candidates computed from repowatch's own records
    (see ServiceState.cache.find_stale_warmed) — no nginx/network call yet. Reports
    whether nginx.enable_purge is even on, so the dashboard can show a clear
    "enable it first" message instead of a confusing empty list — read-only,
    so no admin_session check here (matches banned_packages_payload's own
    reasoning), though the route itself is still admin-only (not in
    web/handler.py's do_GET guest_route allowlist)."""
    current, error = load_request_config(config_path, current)
    if error is not None:
        return error

    if current.repo_by_id(repo_id) is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}

    return 200, {
        "enable_purge": current.nginx.enable_purge,
        "candidates": store.cache.find_stale_warmed(repo_id),
    }


def purge_selected_payload(
    config_path: str | Path,
    store: ServiceState,
    repo_id: str,
    admin_session: AdminSession | None,
    body: dict,
) -> tuple[int, dict]:
    """Manual cache purge, step 2 (dashboard "Purge selected"): the operator
    has reviewed the candidates from purge_candidates_payload and picked a
    subset. body: {"package_keys": [...]}. Filenames are re-derived from
    warmed_packages here, server-side — never trusted from the request body,
    since that would let a client purge an arbitrary path under this repo's
    prefix by supplying a made-up filename.

    On success, also drops the warmed_packages bookkeeping row for every key
    purge_items() confirmed is no longer (or never was) actually cached
    ("purged"/"not_cached") — that record was only ever useful as a
    "possibly still cached" signal, and nginx has now given a definitive
    answer either way. Keys reported as "error (...)" are left alone so a
    retry later still finds them as candidates.
    """
    current, error = load_admin_config(config_path, admin_session)
    if error is not None:
        return error

    repo = current.repo_by_id(repo_id)
    if repo is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}

    if not current.nginx.enable_purge:
        return 400, {"error": "nginx.enable_purge is not enabled in config.yaml — nothing to purge"}

    if not isinstance(body, dict) or not isinstance(body.get("package_keys"), list) or not body["package_keys"]:
        return 400, {"error": 'request body must be {"package_keys": ["name-version", ...]}'}

    requested_keys = [str(k) for k in body["package_keys"]]
    records = store.cache.get_warmed_records(repo_id, requested_keys)
    filenames = {key: row[0] for key, row in records.items()}
    not_found = [k for k in requested_keys if k not in filenames]

    # purge_items is async (see above) — bridge sync->async the same way
    # warm_packages_payload does, for the same reason (this handler runs in
    # a plain ThreadingHTTPServer thread).
    results = asyncio.run(purge_items(current, repo, filenames, store)) if filenames else {}
    resolved = resolved_purge_keys(results)
    if resolved:
        store.cache.remove_purged_records(repo_id, records, resolved)

    return 200, {"results": results, "not_found": not_found}


def remove_warmed_package_payload(
    config_path: str | Path,
    store: ServiceState,
    repo_id: str,
    admin_session: AdminSession | None,
    body: dict,
) -> tuple[int, dict]:
    """"Remove from warmed" (dashboard, including "remove selected" bulk
    action).

    When nginx.enable_purge is on, this now ALSO evicts the real cache
    entry — via the exact same purge_items() call "Purge selected"
    already uses — before dropping the bookkeeping row.
    Previously this only ever touched the warmed_packages tracking row and
    left the actual cached file alone, which was the precise disconnect
    docs_dev/ROADMAP.md item 33 flagged: "un-warming" a package that's
    still physically on disk made it invisible to future stale-scans
    (find_stale_warmed() needs the row to exist to find the file at all),
    turning it into a permanent, undiscoverable orphan until nginx's own
    inactive=180d eventually noticed. Pointed out directly by the user
    (2026-09-13): the name "Remove from warmed" implies un-caching it, not
    just editing a database row.

    Matches purge_selected_payload()'s own conservative semantics: the
    bookkeeping row is only dropped for keys nginx confirms are gone
    ("purged"/"not_cached") — a key that errored during purge is left
    tracked so a retry later still finds it, rather than silently losing
    the one remaining way to find it again.

    Without enable_purge (no purge mechanism exists to call at all), falls
    back to the original bookkeeping-only behavior — same "opt-in,
    degrades gracefully" posture as the rest of the purge feature; a
    config that never enabled purge sees no change here.

    body: {"package_keys": ["name-version", ...]}."""
    current, error = load_admin_config(config_path, admin_session)
    if error is not None:
        return error

    repo = current.repo_by_id(repo_id)
    if repo is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}

    if not isinstance(body, dict) or not isinstance(body.get("package_keys"), list) or not body["package_keys"]:
        return 400, {"error": 'request body must be {"package_keys": ["name-version", ...]}'}

    package_keys = [str(k) for k in body["package_keys"]]

    if not current.nginx.enable_purge:
        removed = store.cache.remove_warmed_packages(repo_id, package_keys)
        return 200, {"removed": removed}

    records = store.cache.get_warmed_records(repo_id, package_keys)
    filenames = {key: row[0] for key, row in records.items()}
    # purge_items is async (see above) — bridge sync->async the same way
    # warm_packages_payload/purge_selected_payload do, for the same reason
    # (this handler runs in a plain ThreadingHTTPServer thread).
    results = asyncio.run(purge_items(current, repo, filenames, store)) if filenames else {}
    resolved = resolved_purge_keys(results)
    removed = store.cache.remove_purged_records(repo_id, records, resolved)
    return 200, {"removed": removed, "purge_results": results}


def banned_packages_payload(
    config_path: str | Path, store: ServiceState, repo_id: str,
    *, current: Config | None = None,
) -> tuple[int, list | dict]:
    """Repository existence is checked against config.yaml, not against a
    snapshot in the store — a package can be banned by name even for a
    repository that was just added and has never been checked yet."""
    current, error = load_request_config(config_path, current)
    if error is not None:
        return error

    if current.repo_by_id(repo_id) is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}
    return 200, store.cache.get_banned_packages(repo_id)


def ban_package_payload(
    config_path: str | Path,
    store: ServiceState,
    repo_id: str,
    admin_session: AdminSession | None,
    body: dict,
) -> tuple[int, dict]:
    """Ban package name(s) from auto-warming (applies to both future
    versions and manual warming — see operations.warm.warm_cache), including the
    dashboard's "ban selected" bulk action from the warm-queue picker. Does
    not touch anything already in the nginx cache or already marked warmed
    — it only stops repowatch from attempting to warm it going forward.
    body: {"package_names": ["package-name", ...]}."""
    current, error = load_admin_config(config_path, admin_session)
    if error is not None:
        return error

    if current.repo_by_id(repo_id) is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}

    if not isinstance(body, dict) or not isinstance(body.get("package_names"), list) or not body["package_names"]:
        return 400, {"error": 'request body must be {"package_names": ["package-name", ...]}'}

    store.cache.ban_packages(repo_id, [str(n) for n in body["package_names"]])
    return 200, {"banned": store.cache.get_banned_packages(repo_id)}


def unban_package_payload(
    config_path: str | Path,
    store: ServiceState,
    repo_id: str,
    admin_session: AdminSession | None,
    body: dict,
) -> tuple[int, dict]:
    """body: {"package_names": ["package-name", ...]} — including the
    dashboard's "unban selected" bulk action."""
    current, error = load_admin_config(config_path, admin_session)
    if error is not None:
        return error

    if current.repo_by_id(repo_id) is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}

    if not isinstance(body, dict) or not isinstance(body.get("package_names"), list) or not body["package_names"]:
        return 400, {"error": 'request body must be {"package_names": ["package-name", ...]}'}

    store.cache.unban_packages(repo_id, [str(n) for n in body["package_names"]])
    return 200, {"banned": store.cache.get_banned_packages(repo_id)}
