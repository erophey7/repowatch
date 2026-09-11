"""Minimal HTTP status server — no third-party web framework.

Endpoints:
    GET /                             — dashboard (static/dashboard.html)
    GET /dashboard                    — same as above
    GET /status.json                  — status for all repositories
    GET /status/<repo_id>.json        — status for a single repository
    GET /status/<repo_id>/history     — last N events (query ?limit=)
    GET /api/repos                    — repository list from the current config.yaml
                                         + status for each (for the dashboard)
    GET /api/repos/<repo_id>/packages — repository packages (from upstream), paginated:
                                         query ?q=<name/key substring>&limit=1..200&cursor=<opaque>,
                                         response {"items": [...], "next_cursor": str|null} —
                                         see state.StateStore.get_page
    GET /api/repos/<repo_id>/warmed   — packages actually warmed (either by repowatch's own
                                         prefetch OR a real client request, see syslog_listener) —
                                         not the same as "all packages" above; same paginated
                                         format as .../packages (query ?limit=&cursor=)
    POST /api/repos/<repo_id>/warm    — warm specific packages manually, without waiting for
                                         either the auto-diff or real client requests
    GET /api/repos/<repo_id>/purge-candidates — manual cache purge, step 1: warmed_packages
                                         entries whose package no longer exists in the current
                                         index (see StateStore.find_stale_warmed) — no nginx/
                                         network call yet, response also includes whether
                                         nginx.enable_purge is even on
    POST /api/repos/<repo_id>/purge   — manual cache purge, step 2: attempt to purge the
                                         operator-selected subset — body {"package_keys": [...]},
                                         response {"results": {key: "purged"|"not_cached"|
                                         "error (...)"}, "not_found": [...]} (see
                                         prefetch.purge_selected); 400 if enable_purge is off
    POST /api/repos/<repo_id>/warmed/remove — drop package(s) from the "warmed" tracking (does
                                         not touch the actual file in the nginx cache) —
                                         body {"package_keys": [...]}, also the dashboard's
                                         "remove selected" bulk action
    GET /api/repos/<repo_id>/bans     — which package names are banned from auto-warming
    POST /api/repos/<repo_id>/bans    — ban package name(s) from auto-warming — body
                                         {"package_names": [...]}, also "ban selected"
    POST /api/repos/<repo_id>/bans/remove — lift the ban(s) — body {"package_names": [...]},
                                         also "unban selected"
    POST /api/repos                   — add a repository (see add_repo_payload)
    POST /api/repos/<repo_id>         — edit a repository: full object replacement
                                         (same as adding — the dashboard form is pre-filled
                                         with current values), id cannot be changed
                                         (see update_repo_payload)
    POST /api/repos/<repo_id>/delete  — remove a repository from config.yaml (does not touch
                                         the state already stored in sqlite for that repo_id,
                                         see delete_repo_payload)
    GET /api/config                   — current values of the "safe" global config.yaml
                                         fields — not secret and don't require a process
                                         restart to take effect (see SAFE_CONFIG_FIELDS)
    POST /api/config                  — change one or more safe fields
                                         (see update_safe_config_payload)
    POST /api/auth/login              — password login, 12-hour session
    GET /api/auth/session             — CSRF token for the current session
    POST /api/auth/logout             — revoke the session
    GET/POST /api/tokens              — list/issue host tokens
    POST /api/tokens/<id>/revoke      — revoke a token
    GET /api/requests                 — client requests, paginated (query
                                         ?repo_id=&limit=1..200&cursor=<opaque>, same format
                                         {"items": [...], "next_cursor": ...} as .../packages
                                         and .../warmed above) — requires syslog_listener.enabled
                                         in config.yaml
    GET /api/requests/summary         — aggregates for dashboard charts: top IPs,
                                         top paths, requests per repository, an hourly
                                         timeline, and per-repo cache HIT/MISS counts
                                         (query ?repo_id=&timeline_hours=1..720, default 24)
    GET /api/prefetch-efficiency      — per repo, of what repowatch actively prefetched
                                         ahead of demand, how much a client actually went
                                         on to request (see StateStore.get_prefetch_efficiency,
                                         docs_dev/ROADMAP.md item 19) — requires
                                         syslog_listener.enabled to have any data to show
    GET /healthz                      — 200 if every repository was checked within the last
                                         3×check_interval (see healthz_payload), otherwise 503
    GET /metrics                      — Prometheus text format (see metrics_payload) —
                                         per-repo gauges + repowatch_healthy
    GET /api/stats                    — state_db size + row counts (always cheap); add
                                         ?cache_dir=1 to also walk the nginx package cache
                                         directory (only if NginxConfig.cache_dir is set —
                                         see docs_dev/ROADMAP.md item 27) — this can be slow
                                         for a large cache, so it's opt-in per request, not
                                         included by default

The config for /api/repos* is re-read from disk on every request (see
config.load_config) — config.yaml stays the single source of truth, with no
need to keep it in sync with the watcher thread's in-memory state.
"""

from __future__ import annotations

import asyncio
import json
import hmac
from http.cookies import SimpleCookie, CookieError
import logging
import os
import ssl
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import yaml

from repowatch.auth import verify_password
from repowatch.access import (AccessStore, AdminSession, COOKIE_NAME, SESSION_SECONDS,
                              client_context, digest, in_networks)
from repowatch.config_edit import atomic_config, locked_config
from repowatch.config import Config, ConfigError, RepoConfig, load_config
from repowatch.prefetch import _repo_url_prefix, purge_selected, warm_cache
from repowatch.state import StateStore

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class StatusHTTPServer(ThreadingHTTPServer):
    # HTTP/1.0 opens one connection per request. The stdlib default backlog
    # of 5 can cause TCP retransmits under load; raising it in a local test
    # with 32 clients cut page p95 from ~1.65s to ~0.78s. We don't add
    # per-IP or GET rate limits; actual request handling still runs in
    # stdlib threads.
    request_queue_size = 128


def repos_list_payload(config_path: str | Path, store: StateStore) -> tuple[int, list | dict]:
    """List of repositories from the current config.yaml, merged with status
    from sqlite. Factored out of Handler into a plain function so it's
    testable via a direct call, without spinning up a real HTTP server."""
    try:
        current = load_config(config_path)
    except ConfigError:
        logger.exception("failed to reload config.yaml for API request")
        return 500, {"error": "config.yaml is currently invalid"}

    statuses = store.get_repo_summaries(include_warmed=True)
    result = []
    for repo in current.repos:
        status = statuses.get(repo.id, {})
        # package_count lives in repo_state (see StateStore.record_snapshot) —
        # no need to parse the whole packages_json just for a count. Rows
        # written before this column existed have it NULL — only then do we
        # fall back to counting the old way (a one-off cost, self-heals on
        # the next snapshot).
        package_count = status.get("package_count")
        if package_count is None:
            package_count = len(store.get_packages(repo.id) or {})
        warmed_count = status.get("warmed_count", 0)
        try:
            browse_url = f"{current.browse_base_url}{_repo_url_prefix(repo)}/"
        except ValueError:
            browse_url = None
        key_expires_at = status.get("key_expires_at")
        result.append(
            {
                "id": repo.id,
                "type": repo.type,
                "upstream": repo.upstream,
                "prefetch": repo.prefetch,
                "package_count": package_count,
                "warmed_count": warmed_count,
                "browse_url": browse_url,
                "check_interval": current.effective_check_interval(repo),
                "last_check": status.get("last_check"),
                "changed_at": status.get("changed_at"),
                # Trust state (docs_dev/ROADMAP.md item 20) — the soonest
                # expiring key in this repo's keyring, from the last check
                # cycle (see gpgverify.soonest_key_expiry). None for apk
                # repos, repos without verify_signature, or when unknown
                # (e.g. the full `gpg` binary isn't installed). Computed
                # once per check_interval by the watcher, not on this
                # request — a live `gpg` call on every dashboard poll would
                # be wasteful, see repowatch_repo_key_expires_at in metrics.
                "key_expires_at": key_expires_at,
                "key_expiring_soon": (
                    key_expires_at is not None
                    and datetime.fromisoformat(key_expires_at) - datetime.now(timezone.utc)
                    < timedelta(days=current.key_expiry_warning_days)
                ),
                # Raw RepoConfig fields as-is (unlike check_interval above,
                # which is already merged with the global default) — used
                # to pre-fill the dashboard edit form, so "inherits the
                # global value" doesn't turn into an explicit override on
                # the next save.
                "config": asdict(repo),
            }
        )
    return 200, result


def paged_payload(store: StateStore, kind: str, repo_id: str | None, query: str) -> tuple[int, dict]:
    try:
        params = parse_qs(query)
        limit = int(params.get("limit", ["100"])[0])
        if kind != "requests" and not store.has_snapshot(repo_id):
            return 404, {"error": "unknown repo_id"}
        return 200, store.get_page(kind, repo_id, limit=limit,
            q=params.get("q", [""])[0], cursor=params.get("cursor", [None])[0])
    except ValueError as exc:
        return 400, {"error": str(exc)}


def _check_admin_session(current: Config, admin_session: AdminSession | None) -> tuple[int, dict] | None:
    """Recheck the authenticated identity against the current config during writes."""
    if not current.admin_password_hash:
        return 501, {"error": "set an administrator password with repowatch set-password"}
    if not isinstance(admin_session, AdminSession) or not hmac.compare_digest(
            admin_session.password_fingerprint, digest(current.admin_password_hash)):
        return 401, {"error": "administrator session required"}
    return None


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
    try:
        current = load_config(config_path)
    except ConfigError:
        logger.exception("failed to reload config.yaml for API request")
        return 500, {"error": "config.yaml is currently invalid"}

    password_error = _check_admin_session(current, admin_session)
    if password_error is not None:
        return password_error

    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}

    try:
        new_repo = RepoConfig(**body)
    except (ConfigError, TypeError) as exc:
        return 400, {"error": f"invalid repository data: {exc}"}

    if any(r.id == new_repo.id for r in current.repos):
        return 409, {"error": f"repository with id={new_repo.id!r} already exists"}

    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw.setdefault("repos", []).append(body)

    # Atomic write: to a temp file next to the target, then os.replace() —
    # so config.yaml is never left in a broken state by a write that fails
    # partway through.
    # NOTE: yaml.safe_dump does not preserve comments in the existing file —
    # if the operator hand-wrote config.yaml with comments, they will be
    # lost after the first add through the dashboard (see README).
    try:
        atomic_config(path, yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    except ConfigError as exc:
        return 400, {"error": f"invalid configuration: {exc}"}

    logger.info("repository added via API: id=%s type=%s", new_repo.id, new_repo.type)
    return 201, {"id": new_repo.id, "type": new_repo.type, "upstream": new_repo.upstream}


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
    try:
        current = load_config(config_path)
    except ConfigError:
        logger.exception("failed to reload config.yaml for API request")
        return 500, {"error": "config.yaml is currently invalid"}

    password_error = _check_admin_session(current, admin_session)
    if password_error is not None:
        return password_error

    if current.repo_by_id(repo_id) is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}

    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}

    if body.get("id", repo_id) != repo_id:
        return 400, {"error": "cannot change id via edit — delete and re-add instead"}

    new_body = {**body, "id": repo_id}
    try:
        updated_repo = RepoConfig(**new_body)
    except (ConfigError, TypeError) as exc:
        return 400, {"error": f"invalid repository data: {exc}"}

    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    repos_raw = raw.get("repos", [])
    for i, r in enumerate(repos_raw):
        if r.get("id") == repo_id:
            repos_raw[i] = new_body
            break
    else:
        return 404, {"error": f"unknown repo_id: {repo_id}"}

    try:
        atomic_config(path, yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    except ConfigError as exc:
        return 400, {"error": f"invalid configuration: {exc}"}

    logger.info("repository edited via API: id=%s", repo_id)
    return 200, {"id": updated_repo.id, "type": updated_repo.type, "upstream": updated_repo.upstream}


@locked_config
def delete_repo_payload(
    config_path: str | Path, admin_session: AdminSession | None, repo_id: str
) -> tuple[int, dict]:
    """Remove a repository from config.yaml. Does not touch sqlite
    (repo_state/repo_events/warmed_packages/prefetch_bans for this repo_id) —
    orphaned rows are harmless: the dashboard and status API both build their
    repository list from config.yaml (see repos_list_payload), so a deleted
    repository simply stops showing up anywhere."""
    try:
        current = load_config(config_path)
    except ConfigError:
        logger.exception("failed to reload config.yaml for API request")
        return 500, {"error": "config.yaml is currently invalid"}

    password_error = _check_admin_session(current, admin_session)
    if password_error is not None:
        return password_error

    if current.repo_by_id(repo_id) is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}

    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    repos_raw = raw.get("repos", [])
    new_repos_raw = [r for r in repos_raw if r.get("id") != repo_id]

    if not new_repos_raw:
        return 400, {"error": "cannot delete the last repository — config cannot be empty"}

    raw["repos"] = new_repos_raw

    try:
        atomic_config(path, yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    except ConfigError as exc:
        return 400, {"error": f"invalid configuration: {exc}"}

    logger.info("repository deleted via API: id=%s", repo_id)
    return 200, {"deleted": repo_id}


def requests_summary_payload(store: StateStore, repo_id: str | None, timeline_hours: int = 24) -> tuple[int, dict]:
    """Aggregates for the dashboard's request charts (top IPs/paths, requests
    per repository, an hourly timeline, and per-repo cache HIT/MISS) —
    already-computed counters, separate from the paged request list. by_repo
    and cache_hit_stats ignore the repo_id filter (already broken down by
    repository); timeline respects it (a per-repo or global chart, per the
    dashboard's current selection)."""
    return 200, {
        "by_client_ip": store.get_top_client_ips(repo_id=repo_id),
        "by_path": store.get_top_request_paths(repo_id=repo_id),
        "by_repo": store.get_requests_by_repo(),
        "timeline": store.get_requests_timeline(repo_id=repo_id, hours=timeline_hours),
        "cache_hit_stats": [
            {"repo_id": rid, "total": v["total"], "hits": v["hits"]}
            for rid, v in store.get_request_hit_stats().items()
        ],
    }


def prefetch_efficiency_payload(store: StateStore) -> tuple[int, dict]:
    """docs_dev/ROADMAP.md item 19 — of what repowatch actively prefetched
    ahead of demand per repo, how much was actually requested by a client
    afterward. See StateStore.get_prefetch_efficiency for the exact
    correlation. Always 200: an empty list (no repo has prefetched anything
    yet, or syslog_listener is disabled so nothing can be linked to a
    request) is a normal state, not an error."""
    return 200, {"items": store.get_prefetch_efficiency()}


def warm_packages_payload(
    config_path: str | Path,
    store: StateStore,
    repo_id: str,
    admin_session: AdminSession | None,
    body: dict,
) -> tuple[int, dict]:
    """Warm specific packages manually (dashboard: "warm selected"),
    regardless of repo.prefetch and without waiting for an auto-diff or
    real client requests. body: {"package_keys": ["name-version", ...]}.
    """
    try:
        current = load_config(config_path)
    except ConfigError:
        logger.exception("failed to reload config.yaml for API request")
        return 500, {"error": "config.yaml is currently invalid"}

    password_error = _check_admin_session(current, admin_session)
    if password_error is not None:
        return password_error

    repo = current.repo_by_id(repo_id)
    if repo is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}

    if not isinstance(body, dict) or not isinstance(body.get("package_keys"), list):
        return 400, {"error": 'request body must be {"package_keys": ["name-version", ...]}'}

    requested_keys = [str(k) for k in body["package_keys"]]
    known_packages = store.get_packages_by_keys(repo_id, requested_keys) or {}

    to_warm = {k: known_packages[k] for k in requested_keys if k in known_packages}
    not_found = [k for k in requested_keys if k not in known_packages]

    if to_warm:
        # warm_cache is now async (see prefetch.py) — this handler itself
        # runs in a plain ThreadingHTTPServer thread with no event loop of
        # its own, so we bridge sync->async with a fresh one-off loop.
        asyncio.run(warm_cache(current, repo, store, to_warm, force=True))

    return 200, {"warmed": sorted(to_warm), "not_found": not_found}


def purge_candidates_payload(
    config_path: str | Path, store: StateStore, repo_id: str
) -> tuple[int, dict]:
    """Manual cache purge, step 1 (dashboard "Scan for stale entries",
    docs_dev/ROADMAP.md): candidates computed from repowatch's own records
    (see StateStore.find_stale_warmed) — no nginx/network call yet. Reports
    whether nginx.enable_purge is even on, so the dashboard can show a clear
    "enable it first" message instead of a confusing empty list — read-only,
    so no admin_session check here (matches banned_packages_payload's own
    reasoning), though the route itself is still admin-only (not in
    api.py's do_GET guest_route allowlist)."""
    try:
        current = load_config(config_path)
    except ConfigError:
        logger.exception("failed to reload config.yaml for API request")
        return 500, {"error": "config.yaml is currently invalid"}

    if current.repo_by_id(repo_id) is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}

    return 200, {
        "enable_purge": current.nginx.enable_purge,
        "candidates": store.find_stale_warmed(repo_id),
    }


def purge_selected_payload(
    config_path: str | Path,
    store: StateStore,
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
    prefetch.purge_selected confirmed is no longer (or never was) actually
    cached ("purged"/"not_cached") — that record was only ever useful as a
    "possibly still cached" signal, and nginx has now given a definitive
    answer either way. Keys reported as "error (...)" are left alone so a
    retry later still finds them as candidates.
    """
    try:
        current = load_config(config_path)
    except ConfigError:
        logger.exception("failed to reload config.yaml for API request")
        return 500, {"error": "config.yaml is currently invalid"}

    password_error = _check_admin_session(current, admin_session)
    if password_error is not None:
        return password_error

    repo = current.repo_by_id(repo_id)
    if repo is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}

    if not current.nginx.enable_purge:
        return 400, {"error": "nginx.enable_purge is not enabled in config.yaml — nothing to purge"}

    if not isinstance(body, dict) or not isinstance(body.get("package_keys"), list) or not body["package_keys"]:
        return 400, {"error": 'request body must be {"package_keys": ["name-version", ...]}'}

    requested_keys = [str(k) for k in body["package_keys"]]
    filenames = store.get_warmed_filenames(repo_id, requested_keys)
    not_found = [k for k in requested_keys if k not in filenames]

    # purge_selected is async (see prefetch.py) — bridge sync->async the
    # same way warm_packages_payload does, for the same reason (this
    # handler runs in a plain ThreadingHTTPServer thread).
    results = asyncio.run(purge_selected(current, repo, filenames)) if filenames else {}
    resolved = [key for key, outcome in results.items() if outcome in ("purged", "not_cached")]
    if resolved:
        store.remove_warmed_packages(repo_id, resolved)

    return 200, {"results": results, "not_found": not_found}


def remove_warmed_package_payload(
    config_path: str | Path,
    store: StateStore,
    repo_id: str,
    admin_session: AdminSession | None,
    body: dict,
) -> tuple[int, dict]:
    """"Remove from warmed" (dashboard, including "remove selected" bulk
    action) — only deletes the warmed_packages tracking entry (repowatch's
    own bookkeeping); the actual file in the nginx cache is left untouched
    (see StateStore.remove_warmed_packages).
    body: {"package_keys": ["name-version", ...]}."""
    try:
        current = load_config(config_path)
    except ConfigError:
        logger.exception("failed to reload config.yaml for API request")
        return 500, {"error": "config.yaml is currently invalid"}

    password_error = _check_admin_session(current, admin_session)
    if password_error is not None:
        return password_error

    if current.repo_by_id(repo_id) is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}

    if not isinstance(body, dict) or not isinstance(body.get("package_keys"), list) or not body["package_keys"]:
        return 400, {"error": 'request body must be {"package_keys": ["name-version", ...]}'}

    package_keys = [str(k) for k in body["package_keys"]]
    removed = store.remove_warmed_packages(repo_id, package_keys)
    return 200, {"removed": removed}


def banned_packages_payload(
    config_path: str | Path, store: StateStore, repo_id: str
) -> tuple[int, list | dict]:
    """Repository existence is checked against config.yaml, not against a
    snapshot in the store — a package can be banned by name even for a
    repository that was just added and has never been checked yet."""
    try:
        current = load_config(config_path)
    except ConfigError:
        logger.exception("failed to reload config.yaml for API request")
        return 500, {"error": "config.yaml is currently invalid"}

    if current.repo_by_id(repo_id) is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}
    return 200, store.get_banned_packages(repo_id)


def ban_package_payload(
    config_path: str | Path,
    store: StateStore,
    repo_id: str,
    admin_session: AdminSession | None,
    body: dict,
) -> tuple[int, dict]:
    """Ban package name(s) from auto-warming (applies to both future
    versions and manual warming — see prefetch.warm_cache), including the
    dashboard's "ban selected" bulk action from the warm-queue picker. Does
    not touch anything already in the nginx cache or already marked warmed
    — it only stops repowatch from attempting to warm it going forward.
    body: {"package_names": ["package-name", ...]}."""
    try:
        current = load_config(config_path)
    except ConfigError:
        logger.exception("failed to reload config.yaml for API request")
        return 500, {"error": "config.yaml is currently invalid"}

    password_error = _check_admin_session(current, admin_session)
    if password_error is not None:
        return password_error

    if current.repo_by_id(repo_id) is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}

    if not isinstance(body, dict) or not isinstance(body.get("package_names"), list) or not body["package_names"]:
        return 400, {"error": 'request body must be {"package_names": ["package-name", ...]}'}

    store.ban_packages(repo_id, [str(n) for n in body["package_names"]])
    return 200, {"banned": store.get_banned_packages(repo_id)}


def unban_package_payload(
    config_path: str | Path,
    store: StateStore,
    repo_id: str,
    admin_session: AdminSession | None,
    body: dict,
) -> tuple[int, dict]:
    """body: {"package_names": ["package-name", ...]} — including the
    dashboard's "unban selected" bulk action."""
    try:
        current = load_config(config_path)
    except ConfigError:
        logger.exception("failed to reload config.yaml for API request")
        return 500, {"error": "config.yaml is currently invalid"}

    password_error = _check_admin_session(current, admin_session)
    if password_error is not None:
        return password_error

    if current.repo_by_id(repo_id) is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}

    if not isinstance(body, dict) or not isinstance(body.get("package_names"), list) or not body["package_names"]:
        return 400, {"error": 'request body must be {"package_names": ["package-name", ...]}'}

    store.unban_packages(repo_id, [str(n) for n in body["package_names"]])
    return 200, {"banned": store.get_banned_packages(repo_id)}


def cache_dir_stats(cache_dir: str) -> dict:
    """docs_dev/ROADMAP.md item 27 — a full filesystem walk (proxy_cache_path's
    levels=1:2 tree can be hundreds of thousands of small files), so this is
    deliberately NOT part of the always-on stats_payload below: it's only
    run when explicitly requested (dashboard "Calculate cache size" button /
    `repowatch stats --cache-dir`), never on a poll interval. No background
    scheduler/cache of the result — a real periodic recompute would need its
    own interval setting and a place to store the last value, which isn't
    justified for an action an operator triggers occasionally by hand.
    """
    if not os.path.isdir(cache_dir):
        # os.walk() silently yields nothing for a missing/unreadable top
        # directory (its default onerror is a no-op) — without this check,
        # a real misconfiguration (wrong path, permission denied) would be
        # indistinguishable from "cache is genuinely empty".
        raise OSError(f"not a directory or not accessible: {cache_dir}")
    total_bytes, file_count, inaccessible = 0, 0, 0

    def _on_error(exc: OSError) -> None:
        # Real, observed case (2026-09-10 production check): nginx's own
        # proxy_cache_path levels=1:2 subdirectories are created 0700,
        # owned by the nginx worker user (e.g. www-data) — regardless of
        # the top-level cache_dir's own permissions. repowatch usually runs
        # as a DIFFERENT, unprivileged service user (privilege separation
        # from nginx is deliberate, see CLAUDE.md), so it typically cannot
        # descend into any of them at all. os.walk()'s default onerror is a
        # silent no-op, which would make a permission-blocked cache look
        # EXACTLY like a genuinely empty one (0 bytes) — actively
        # misleading, worse than an error. Count these instead of raising:
        # a top-level directory that's at least listable should still
        # report what it can, with the gap made visible.
        nonlocal inaccessible
        inaccessible += 1

    for root, _dirs, files in os.walk(cache_dir, onerror=_on_error):
        for name in files:
            try:
                total_bytes += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            file_count += 1
    result = {"path": cache_dir, "size_bytes": total_bytes, "file_count": file_count}
    if inaccessible:
        result["inaccessible_directories"] = inaccessible
    return result


def stats_payload(
    config_path: str | Path, store: StateStore, *, include_cache_dir: bool = False
) -> tuple[int, dict]:
    """GET /api/stats — state_db size/row counts are always cheap and
    included; the nginx package cache directory's size is only walked when
    include_cache_dir is set (see cache_dir_stats) and only reported when
    the operator has opted into NginxConfig.cache_dir being visible to
    repowatch at all (see docs_dev/ROADMAP.md item 12) — otherwise there is
    no path to walk, and that's a normal, expected configuration, not an
    error."""
    payload = store.get_storage_stats()
    if include_cache_dir:
        try:
            current = load_config(config_path)
        except ConfigError:
            return 500, {"error": "config.yaml is currently invalid"}
        if not current.nginx.cache_dir:
            payload["cache_dir"] = None
        else:
            try:
                payload["cache_dir"] = cache_dir_stats(current.nginx.cache_dir)
            except OSError as exc:
                payload["cache_dir"] = {"error": str(exc)}
    return 200, payload


def _cast_int(value: Any) -> int:
    return int(value)


def _cast_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _cast_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _cast_url(value: Any) -> str:
    return str(value).rstrip("/")


def _cast_optional_url(value: Any) -> str | None:
    if not value:
        return None
    return str(value).rstrip("/")


# Global Config fields that can be edited through the dashboard: not secret
# (unlike admin_password_hash) and take effect without a process restart,
# since config.yaml is re-read every cycle (see watcher.run_forever).
# Deliberately NOT included here: state_db/status_server/syslog_listener —
# they are only read once at startup, so an edit through the dashboard would
# silently do nothing until a restart and would be misleading;
# admin_password_hash — a secret, changed only via `repowatch hash-password`;
# notify_webhook_url — ALSO a secret (the incoming webhook URL is itself a
# bearer token), and this list is what safe_config_payload returns WITHOUT a
# password — the value would leak to any dashboard visitor. Edited only by
# hand in config.yaml.
SAFE_CONFIG_FIELDS: dict[str, Callable[[Any], Any]] = {
    "check_interval": _cast_int,
    "event_retention_days": _cast_int,
    "request_retention_days": _cast_int,
    "warmed_retention_days": _cast_int,
    "event_max_rows_per_repo": _cast_optional_int,
    "request_max_rows": _cast_optional_int,
    "prefetch_concurrency": _cast_int,
    "check_concurrency": _cast_int,
    "prefetch_bandwidth_limit": _cast_optional_float,
    "cache_base_url": _cast_url,
    "public_cache_url": _cast_optional_url,
    "notify_after_failures": _cast_int,
    "key_expiry_warning_days": _cast_int,
}


def safe_config_payload(config_path: str | Path) -> tuple[int, dict]:
    """Current values of the safe global fields — used to pre-fill the
    settings form in the dashboard. Requires no password: none of these
    fields are secret (see SAFE_CONFIG_FIELDS)."""
    try:
        current = load_config(config_path)
    except ConfigError:
        logger.exception("failed to reload config.yaml for API request")
        return 500, {"error": "config.yaml is currently invalid"}

    return 200, {field: getattr(current, field) for field in SAFE_CONFIG_FIELDS}


@locked_config
def update_safe_config_payload(
    config_path: str | Path, admin_session: AdminSession | None, body: dict
) -> tuple[int, dict]:
    """Change one or more safe global fields (a partial body — only the keys
    actually being changed). The written file is validated by a full reload
    (load_config) BEFORE it replaces the original — if anything is wrong,
    the original is left untouched."""
    try:
        current = load_config(config_path)
    except ConfigError:
        logger.exception("failed to reload config.yaml for API request")
        return 500, {"error": "config.yaml is currently invalid"}

    password_error = _check_admin_session(current, admin_session)
    if password_error is not None:
        return password_error

    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}

    unknown = set(body) - set(SAFE_CONFIG_FIELDS)
    if unknown:
        return 400, {"error": f"not editable through the dashboard: {sorted(unknown)}"}

    casted: dict[str, Any] = {}
    for key, raw_value in body.items():
        try:
            casted[key] = SAFE_CONFIG_FIELDS[key](raw_value)
        except (TypeError, ValueError) as exc:
            return 400, {"error": f"{key}: invalid value ({exc})"}

    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw.update(casted)

    try:
        atomic_config(path, yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
        reloaded = load_config(path)
    except ConfigError as exc:
        return 500, {"error": f"result fails validation: {exc}"}

    logger.info("global settings changed via API: %s", sorted(casted))
    return 200, {field: getattr(reloaded, field) for field in SAFE_CONFIG_FIELDS}


# How many times a repository's effective_check_interval it may exceed
# before /healthz considers it "suspiciously stale". check_repo() does not
# update last_check on failure (see watcher.check_repo), so a steadily
# growing age specifically means "checks aren't actually succeeding", not a
# single upstream hiccup for one cycle.
_HEALTHZ_STALE_MULTIPLIER = 3


def _repo_staleness(
    config: Config, store: StateStore, statuses: dict[str, dict] | None = None,
) -> list[dict]:
    """List of repositories whose last check is older than
    interval * _HEALTHZ_STALE_MULTIPLIER — shared by healthz_payload and
    metrics_payload so the "suspiciously stale" threshold isn't duplicated.
    A repository that has never been checked at all (just added / service
    just started) is not considered stale — that's an expected state, not
    a failure."""
    now = datetime.now(timezone.utc)
    stale = []
    if statuses is None:
        statuses = store.get_repo_summaries()
    for repo in config.repos:
        status = statuses.get(repo.id)
        if not status or not status.get("last_check"):
            continue
        last_check = datetime.fromisoformat(status["last_check"])
        interval = config.effective_check_interval(repo)
        age_seconds = (now - last_check).total_seconds()
        if age_seconds > interval * _HEALTHZ_STALE_MULTIPLIER:
            stale.append(
                {
                    "repo_id": repo.id,
                    "last_check": status["last_check"],
                    "age_seconds": int(age_seconds),
                    "check_interval": interval,
                }
            )
    return stale


def status_payload(config_path: str | Path, store: StateStore,
                   repo_id: str | None = None) -> tuple[int, dict]:
    """Small polling signal; freshness is not a guarantee of cache readiness."""
    try:
        current = load_config(config_path)
    except ConfigError:
        return 503, {"error": "status unavailable: invalid configuration"}
    if repo_id is not None and current.repo_by_id(repo_id) is None:
        return 404, {"error": f"unknown repo_id: {repo_id}"}
    summaries = store.get_repo_summaries()
    stale_ids = {item["repo_id"] for item in _repo_staleness(current, store, summaries)}
    result = {}
    for repo in current.repos:
        if repo_id is not None and repo.id != repo_id:
            continue
        summary = summaries.get(repo.id, {})
        last_check = summary.get("last_check")
        result[repo.id] = {
            "last_check": last_check,
            "changed_at": summary.get("changed_at"),
            "stale": not last_check or repo.id in stale_ids,
        }
    return 200, result if repo_id is None else result[repo_id]


def healthz_payload(config_path: str | Path, store: StateStore) -> tuple[int, dict]:
    """200 if every repository has been checked recently relative to its own
    check_interval; otherwise 503 with the list of stale ones.

    A broken config.yaml is not by itself considered an unhealthy state —
    run_forever() keeps running on the last valid config in that case (see
    watcher.py), so here we simply can't check freshness and silently
    answer ok.
    """
    try:
        current = load_config(config_path)
    except ConfigError:
        return 200, {"ok": True, "warning": "config.yaml is currently invalid, check skipped"}

    stale = _repo_staleness(current, store)
    if stale:
        return 503, {"ok": False, "stale_repos": stale}
    return 200, {"ok": True}


def _escape_label_value(value: str) -> str:
    """Escape a label value per the Prometheus text exposition format
    (https://prometheus.io/docs/instrumenting/exposition_formats/)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def metrics_payload(config_path: str | Path, store: StateStore) -> tuple[int, str]:
    """Prometheus text format — for Grafana/alerting integration, separate
    from status.json/the dashboard (those are for humans and host agents,
    this one is for monitoring systems).

    Every metric is a gauge, none is a counter: values here can DECREASE
    (retention, process restart), and a Prometheus counter must be
    monotonically increasing — so, for example, the size of request_events
    is deliberately not exposed here (it would be a mistyped counter)."""
    try:
        current = load_config(config_path)
    except ConfigError:
        return 500, "# config.yaml is currently invalid, metrics unavailable\n"

    summaries = store.get_repo_summaries(include_warmed=True)
    statuses = {repo.id: summaries.get(repo.id, {}) for repo in current.repos}
    lines: list[str] = []

    lines.append(
        "# HELP repowatch_repo_package_count Number of packages known in this repo's last snapshot."
    )
    lines.append("# TYPE repowatch_repo_package_count gauge")
    for repo in current.repos:
        package_count = statuses[repo.id].get("package_count")
        if package_count is None:
            package_count = len(store.get_packages(repo.id) or {})
        label = _escape_label_value(repo.id)
        lines.append(f'repowatch_repo_package_count{{repo_id="{label}"}} {package_count}')

    lines.append(
        "# HELP repowatch_repo_warmed_count Number of packages repowatch has attempted to warm for this repo."
    )
    lines.append("# TYPE repowatch_repo_warmed_count gauge")
    for repo in current.repos:
        label = _escape_label_value(repo.id)
        lines.append(f'repowatch_repo_warmed_count{{repo_id="{label}"}} {statuses[repo.id].get("warmed_count", 0)}')

    lines.append(
        "# HELP repowatch_repo_last_check_timestamp_seconds Unix timestamp of the last check for this repo."
    )
    lines.append("# TYPE repowatch_repo_last_check_timestamp_seconds gauge")
    for repo in current.repos:
        last_check = statuses[repo.id].get("last_check")
        if last_check:
            ts = datetime.fromisoformat(last_check).timestamp()
            label = _escape_label_value(repo.id)
            lines.append(f'repowatch_repo_last_check_timestamp_seconds{{repo_id="{label}"}} {ts}')

    lines.append(
        "# HELP repowatch_repo_changed_at_timestamp_seconds "
        "Unix timestamp of the last detected package change for this repo."
    )
    lines.append("# TYPE repowatch_repo_changed_at_timestamp_seconds gauge")
    for repo in current.repos:
        changed_at = statuses[repo.id].get("changed_at")
        if changed_at:
            ts = datetime.fromisoformat(changed_at).timestamp()
            label = _escape_label_value(repo.id)
            lines.append(f'repowatch_repo_changed_at_timestamp_seconds{{repo_id="{label}"}} {ts}')

    lines.append(
        "# HELP repowatch_repo_key_expires_at_timestamp_seconds Unix timestamp of the soonest "
        "expiring GPG key in this repo's keyring (docs_dev/ROADMAP.md item 20) — absent for apk "
        "repos, repos without verify_signature, or when unknown."
    )
    lines.append("# TYPE repowatch_repo_key_expires_at_timestamp_seconds gauge")
    for repo in current.repos:
        key_expires_at = statuses[repo.id].get("key_expires_at")
        if key_expires_at:
            ts = datetime.fromisoformat(key_expires_at).timestamp()
            label = _escape_label_value(repo.id)
            lines.append(f'repowatch_repo_key_expires_at_timestamp_seconds{{repo_id="{label}"}} {ts}')

    lines.append(
        "# HELP repowatch_repo_key_expiring_soon Whether the soonest expiring key is within "
        "key_expiry_warning_days (1) or not (0) — absent (not 0) when there is no known expiry "
        "to compare, same reasoning as repowatch_repo_stale."
    )
    lines.append("# TYPE repowatch_repo_key_expiring_soon gauge")
    for repo in current.repos:
        key_expires_at = statuses[repo.id].get("key_expires_at")
        if key_expires_at:
            soon = datetime.fromisoformat(key_expires_at) - datetime.now(timezone.utc) < timedelta(
                days=current.key_expiry_warning_days)
            label = _escape_label_value(repo.id)
            lines.append(f'repowatch_repo_key_expiring_soon{{repo_id="{label}"}} {1 if soon else 0}')

    stale = _repo_staleness(current, store, statuses)
    stale_ids = {item["repo_id"] for item in stale}
    lines.append(
        "# HELP repowatch_repo_stale Whether this repo's last check is older than "
        f"{_HEALTHZ_STALE_MULTIPLIER}x its check_interval (1) or not (0) — a repo never "
        "checked at all is not stale, see /healthz."
    )
    lines.append("# TYPE repowatch_repo_stale gauge")
    for repo in current.repos:
        label = _escape_label_value(repo.id)
        lines.append(f'repowatch_repo_stale{{repo_id="{label}"}} {1 if repo.id in stale_ids else 0}')

    lines.append(
        "# HELP repowatch_healthy Whether repowatch considers itself healthy (1) or not (0) — see /healthz."
    )
    lines.append("# TYPE repowatch_healthy gauge")
    lines.append(f"repowatch_healthy {0 if stale else 1}")

    failure_counts = store.get_failure_counts()
    lines.append(
        "# HELP repowatch_repo_consecutive_failures Current consecutive-failure streak for this "
        "repo and kind (gpg: signature/checksum verification, prefetch: at least one warm failure "
        "in a check cycle) — 0 once the streak is reset by a success, see notifications.py."
    )
    lines.append("# TYPE repowatch_repo_consecutive_failures gauge")
    for repo in current.repos:
        label = _escape_label_value(repo.id)
        for kind in ("gpg", "prefetch"):
            count = failure_counts.get((repo.id, kind), 0)
            lines.append(f'repowatch_repo_consecutive_failures{{repo_id="{label}",kind="{kind}"}} {count}')

    ban_counts = store.get_ban_counts()
    lines.append(
        "# HELP repowatch_repo_banned_packages Number of package names currently excluded "
        "from auto-warm for this repo (prefetch_bans)."
    )
    lines.append("# TYPE repowatch_repo_banned_packages gauge")
    for repo in current.repos:
        label = _escape_label_value(repo.id)
        lines.append(f'repowatch_repo_banned_packages{{repo_id="{label}"}} {ban_counts.get(repo.id, 0)}')

    type_counts: dict[str, int] = {}
    for repo in current.repos:
        type_counts[repo.type] = type_counts.get(repo.type, 0) + 1
    lines.append(
        "# HELP repowatch_repos_by_type Number of configured repositories of this type."
    )
    lines.append("# TYPE repowatch_repos_by_type gauge")
    for repo_type, count in sorted(type_counts.items()):
        label = _escape_label_value(repo_type)
        lines.append(f'repowatch_repos_by_type{{type="{label}"}} {count}')

    lines.append(
        "# HELP repowatch_state_db_bytes Size in bytes of the state_db SQLite file."
    )
    lines.append("# TYPE repowatch_state_db_bytes gauge")
    lines.append(f"repowatch_state_db_bytes {store.get_storage_stats()['state_db_bytes']}")

    if current.syslog_listener.enabled:
        # Only emitted when the syslog listener is on — otherwise request_events
        # stays empty and these gauges would be a misleading, permanent 0 rather
        # than "not tracked". repo_id is bounded by the number of configured
        # repos (already used as a label above); the unmatched (repo_id IS NULL)
        # bucket is skipped here, same as everywhere else, to avoid a label
        # that isn't one of the fixed repo_ids.
        hit_stats = store.get_request_hit_stats()
        lines.append(
            "# HELP repowatch_repo_requests Number of client requests recorded for this repo "
            "within the current request_events retention window (see request_retention_days/"
            "request_max_rows) — a gauge, not a running total."
        )
        lines.append("# TYPE repowatch_repo_requests gauge")
        for repo in current.repos:
            label = _escape_label_value(repo.id)
            total = hit_stats.get(repo.id, {}).get("total", 0)
            lines.append(f'repowatch_repo_requests{{repo_id="{label}"}} {total}')

        lines.append(
            "# HELP repowatch_repo_requests_cache_hit Of repowatch_repo_requests, how many nginx "
            "reported as a cache HIT (cache_status)."
        )
        lines.append("# TYPE repowatch_repo_requests_cache_hit gauge")
        for repo in current.repos:
            label = _escape_label_value(repo.id)
            hits = hit_stats.get(repo.id, {}).get("hits", 0)
            lines.append(f'repowatch_repo_requests_cache_hit{{repo_id="{label}"}} {hits}')

    return 200, "\n".join(lines) + "\n"


# The versioned prefix covers only the client-facing status API — the one
# external host agents poll (`status.json`, `/healthz`, `/metrics`) — not the
# admin/dashboard API, which stays tied to the dashboard's own version and is
# never promised stable to outside consumers. `/api/v1/...` is currently a
# pure alias for the same unversioned routes below; a future breaking change
# to this specific contract would land in `/api/v2/` instead, leaving `v1`
# (and the unversioned routes, kept as a permanent alias of it) working.
_V1_PREFIX = "/api/v1"


def _normalize_v1_path(path: str) -> str:
    if not path.startswith(_V1_PREFIX + "/"):
        return path
    rest = path[len(_V1_PREFIX):]
    if rest in ("/healthz", "/metrics", "/status.json") or rest.startswith("/status/"):
        return rest
    return path


def make_handler(
    config: Config, store: StateStore, config_path: str | Path
) -> type[BaseHTTPRequestHandler]:
    access = AccessStore(store)

    class Handler(BaseHTTPRequestHandler):
        server_version = "repowatch/0.1"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def end_headers(self) -> None:
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            for name, value in getattr(self, '_extra_headers', []):
                self.send_header(name, value)
            super().end_headers()

        def _context(self) -> bool:
            try:
                self.current = load_config(config_path)
                for name in ('Host', 'X-Forwarded-For', 'X-Forwarded-Proto', 'X-Forwarded-Host', 'Authorization', 'Cookie', 'Origin', 'X-CSRF-Token'):
                    if len(self.headers.get_all(name, [])) > 1:
                        raise ValueError('duplicate security header')
                self.client = client_context(self.client_address[0], self.headers,
                                             isinstance(self.connection, ssl.SSLSocket), self.current.status_server)
                cookies = SimpleCookie()
                cookies.load(self.headers.get('Cookie', ''))
                self.session_secret = cookies[COOKIE_NAME].value if COOKIE_NAME in cookies else ''
                self.admin = access.session(self.session_secret, self.current.admin_password_hash, self.client.secure)
                return True
            except (ConfigError, OSError):
                self._json({'error': 'configuration unavailable'}, status=503)
            except (ValueError, CookieError):
                self._json({'error': 'invalid request headers'}, status=400)
            return False

        def _admin_required(self, csrf: bool = False) -> bool:
            if not (self.client.credentials_allowed or self.current.status_server.allow_insecure_http):
                self._json({'error': 'HTTPS required'}, status=403)
                return False
            if not self.admin:
                self._json({'error': 'administrator login required'}, status=401)
                return False
            if csrf and not hmac.compare_digest(self.headers.get('X-CSRF-Token', '').encode(), self.admin.csrf.encode()):
                self._json({'error': 'CSRF token required'}, status=403)
                return False
            return True

        def _same_origin(self) -> bool:
            origin = self.headers.get('Origin')
            expected = ('https' if self.client.secure else 'http') + '://' + self.client.host
            return (not origin or origin.lower() == expected) and self.headers.get('Sec-Fetch-Site') != 'cross-site'

        def _json(self, payload: dict | list, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, body: bytes, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text(self, body: str, status: int = 200) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            parsed = parsed._replace(path=_normalize_v1_path(parsed.path))
            parts = [p for p in parsed.path.split("/") if p]

            if parsed.path == '/healthz':
                status, _ = healthz_payload(config_path, store)
                self._json({'healthy': status == 200}, status=status)
                return
            if parsed.path == '/login':
                self._html((STATIC_DIR / 'login.html').read_bytes())
                return
            if not self._context():
                return
            if parsed.path == '/metrics':
                if not self.client.client_ip or not in_networks(self.client.client_ip, self.current.status_server.metrics_allowed_networks):
                    self._json({'error': 'metrics access denied'}, status=403)
                    return
                status, body = metrics_payload(config_path, store)
                self._text(body, status=status)
                return
            is_status = parsed.path == '/status.json' or (len(parts) == 2 and parts[0] == 'status' and parts[1].endswith('.json'))
            # Explicit read allowlist: future administrative GET routes stay closed.
            guest_route = (
                parsed.path in ('/', '/dashboard', '/api/auth/session', '/api/repos',
                                '/api/requests', '/api/requests/summary', '/api/config',
                                '/api/prefetch-efficiency')
                or is_status
                or (len(parts) == 3 and parts[0] == 'status' and parts[2] == 'history')
                or (len(parts) == 4 and parts[:2] == ['api', 'repos']
                    and parts[3] in ('packages', 'warmed', 'bans'))
            )
            guest = self.current.status_server.guest_read_only and not self.admin and guest_route
            allowed_repos = None
            if guest:
                pass
            elif is_status and not self.admin:
                authorization = self.headers.get('Authorization', '')
                grant = access.token_access(authorization[7:]) if authorization.startswith('Bearer ') else None
                if not (self.client.credentials_allowed or self.current.status_server.allow_insecure_http) or not authorization.startswith('Bearer ') or grant is None:
                    self._extra_headers = [('WWW-Authenticate', 'Bearer')]
                    self._json({'error': 'valid host token required'}, status=401)
                    return
                if self.current.status_server.token_repo_restrictions:
                    allowed_repos = grant['repo_ids']
                    if allowed_repos is not None and parsed.path != '/status.json' and parts[1][:-5] not in allowed_repos:
                        self._json({'error': 'repository access denied'}, status=403)
                        return
            else:
                if parsed.path in ('/', '/dashboard') and not self.admin:
                    self._extra_headers = [('Location', '/login')]
                    self._html(b'', status=303)
                    return
                if not self._admin_required():
                    return
            if parsed.path in ('/', '/dashboard'):
                self._html((STATIC_DIR / 'dashboard.html').read_bytes())
                return
            if parsed.path == '/api/auth/session':
                if guest:
                    self._json({'role': 'guest'})
                else:
                    self._json({'role': 'admin', 'csrf_token': self.admin.csrf, 'expires_at': self.admin.expires_at})
                return
            if parsed.path == '/api/tokens':
                self._json(access.tokens())
                return

            if parsed.path == "/status.json":
                status, payload = status_payload(config_path, store)
                if status == 200 and allowed_repos is not None:
                    payload = {k: v for k, v in payload.items() if k in allowed_repos}
                self._json(payload, status=status)
                return

            if len(parts) == 2 and parts[0] == "status" and parts[1].endswith(".json"):
                repo_id = parts[1][: -len(".json")]
                status, payload = status_payload(config_path, store, repo_id)
                self._json(payload, status=status)
                return

            if len(parts) == 3 and parts[0] == "status" and parts[2] == "history":
                repo_id = parts[1]
                qs = parse_qs(parsed.query)
                try:
                    limit = int(qs.get("limit", ["20"])[0])
                    if not 1 <= limit <= 200:
                        raise ValueError()
                except ValueError:
                    self._json({'error': 'limit must be 1..200'}, status=400)
                    return
                self._json(store.get_history(repo_id, limit=limit))
                return

            if parsed.path == "/api/repos":
                status, payload = repos_list_payload(config_path, store)
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "packages":
                status, payload = paged_payload(store, "packages", parts[2], parsed.query)
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "warmed":
                status, payload = paged_payload(store, "warmed", parts[2], parsed.query)
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "bans":
                status, payload = banned_packages_payload(config_path, store, parts[2])
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "purge-candidates":
                status, payload = purge_candidates_payload(config_path, store, parts[2])
                self._json(payload, status=status)
                return

            if parsed.path == "/api/config":
                status, payload = safe_config_payload(config_path)
                self._json(payload, status=status)
                return

            if parsed.path == "/api/stats":
                qs = parse_qs(parsed.query)
                status, payload = stats_payload(
                    config_path, store, include_cache_dir=qs.get("cache_dir", ["0"])[0] == "1")
                self._json(payload, status=status)
                return

            if parsed.path == "/api/requests/summary":
                qs = parse_qs(parsed.query)
                repo_id = qs.get("repo_id", [None])[0]
                try:
                    timeline_hours = int(qs.get("timeline_hours", ["24"])[0])
                    if not 1 <= timeline_hours <= 24 * 30:
                        raise ValueError()
                except ValueError:
                    self._json({'error': 'timeline_hours must be 1..720'}, status=400)
                    return
                status, payload = requests_summary_payload(store, repo_id, timeline_hours)
                self._json(payload, status=status)
                return

            if parsed.path == "/api/requests":
                qs = parse_qs(parsed.query)
                repo_id = qs.get("repo_id", [None])[0]
                status, payload = paged_payload(store, "requests", repo_id, parsed.query)
                self._json(payload, status=status)
                return

            if parsed.path == "/api/prefetch-efficiency":
                status, payload = prefetch_efficiency_payload(store)
                self._json(payload, status=status)
                return

            self._json({"error": "not found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._post()
            except (ConfigError, OSError) as exc:
                logger.error("API operation failed: %s", type(exc).__name__)
                self._json({'error': 'operation unavailable'}, status=503)

        def _post(self) -> None:
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]

            if not self._context():
                return
            if not self._same_origin():
                self._json({'error': 'cross-origin request denied'}, status=403)
                return
            if parsed.path != '/api/auth/login' and not self._admin_required(csrf=True):
                return
            if not (self.client.credentials_allowed or self.current.status_server.allow_insecure_http):
                self._json({'error': 'HTTPS required'}, status=403)
                return
            try:
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) > 1:
                    raise ValueError('invalid body framing')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 1024 * 1024:
                    self._json({'error': 'request body too large'}, status=413)
                    return
                if length and self.headers.get_content_type() != 'application/json':
                    self._json({'error': 'application/json required'}, status=415)
                    return
                self.connection.settimeout(15)
                raw_body = self.rfile.read(length) if length else b''
                if len(raw_body) != length:
                    raise ValueError('incomplete body')
                body = json.loads(raw_body) if raw_body else {}
                if not isinstance(body, dict):
                    raise ValueError('JSON object required')
            except (ValueError, TimeoutError):
                self._json({'error': 'invalid JSON request body'}, status=400)
                return
            if parsed.path == '/api/auth/login':
                password = body.get('password')
                if not self.current.admin_password_hash:
                    self._json({'error': 'set administrator password with repowatch set-password'}, status=503)
                    return
                if not isinstance(password, str) or len(password) > 1024 or not verify_password(password, self.current.admin_password_hash):
                    self._json({'error': 'invalid password'}, status=401)
                    return
                secret, session = access.create_session(self.current.admin_password_hash, self.client.secure)
                cookie = f'{COOKIE_NAME}={secret}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}'
                if self.client.secure:
                    cookie += '; Secure'
                self._extra_headers = [('Set-Cookie', cookie)]
                self._json({'csrf_token': session.csrf, 'expires_at': session.expires_at})
                return
            if parsed.path == '/api/auth/logout':
                access.logout(self.session_secret)
                cookie = f'{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'
                if self.client.secure:
                    cookie += '; Secure'
                self._extra_headers = [('Set-Cookie', cookie)]
                self._json({'ok': True})
                return
            if parsed.path == '/api/tokens':
                try:
                    if set(body) - {'name', 'expires_at', 'repo_ids'}:
                        raise ValueError('only name, expires_at and repo_ids are supported')
                    repo_ids = body.get('repo_ids')
                    if repo_ids is not None:
                        if not self.current.status_server.token_repo_restrictions:
                            raise ValueError('enable status_server.token_repo_restrictions before issuing scoped tokens')
                        if not isinstance(repo_ids, list) or any(not isinstance(r, str) or self.current.repo_by_id(r) is None for r in repo_ids):
                            raise ValueError('repo_ids must contain existing repository ids')
                    token = access.create_token(body.get('name'), body.get('expires_at'), repo_ids)
                except ValueError as exc:
                    self._json({'error': str(exc)}, status=400)
                    return
                self._json(token, status=201)
                return
            if len(parts) == 4 and parts[:2] == ['api', 'tokens'] and parts[3] == 'revoke':
                found = access.revoke_token(parts[2])
                self._json({'ok': found}, status=200 if found else 404)
                return
            password = self.admin  # internal identity, never taken from a request header

            if parsed.path == "/api/repos":
                status, payload = add_repo_payload(config_path, password, body)
                self._json(payload, status=status)
                return

            if parsed.path == "/api/config":
                status, payload = update_safe_config_payload(config_path, password, body)
                self._json(payload, status=status)
                return

            if len(parts) == 3 and parts[0] == "api" and parts[1] == "repos":
                status, payload = update_repo_payload(config_path, password, parts[2], body)
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "delete":
                status, payload = delete_repo_payload(config_path, password, parts[2])
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "warm":
                status, payload = warm_packages_payload(config_path, store, parts[2], password, body)
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "purge":
                status, payload = purge_selected_payload(config_path, store, parts[2], password, body)
                self._json(payload, status=status)
                return

            if (
                len(parts) == 5
                and parts[0] == "api" and parts[1] == "repos" and parts[3] == "warmed"
                and parts[4] == "remove"
            ):
                status, payload = remove_warmed_package_payload(
                    config_path, store, parts[2], password, body
                )
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "bans":
                status, payload = ban_package_payload(config_path, store, parts[2], password, body)
                self._json(payload, status=status)
                return

            if (
                len(parts) == 5
                and parts[0] == "api" and parts[1] == "repos" and parts[3] == "bans"
                and parts[4] == "remove"
            ):
                status, payload = unban_package_payload(config_path, store, parts[2], password, body)
                self._json(payload, status=status)
                return

            self._json({"error": "not found"}, status=404)

    return Handler


def serve(config: Config, store: StateStore, config_path: str | Path) -> None:
    handler_cls = make_handler(config, store, config_path)
    addr = (config.status_server.bind, config.status_server.port)
    context = None
    if config.status_server.tls_cert_path is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            config.status_server.tls_cert_path, config.status_server.tls_key_path,
        )
    with StatusHTTPServer(addr, handler_cls) as httpd:
        if context is not None:
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        logger.info("status API listening on %s://%s:%d", "https" if context else "http", *addr)
        httpd.serve_forever()
