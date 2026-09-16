"""Build paginated views and operational statistics."""

from __future__ import annotations

import asyncio
import httpx
import os
import repowatch.cache.probe as cache_probe
from pathlib import Path
from repowatch.config.load import load_config
from repowatch.config.models import Config
from repowatch.errors import ConfigError
from repowatch.runtime.context import ServiceState
from urllib.parse import parse_qs

def paged_payload(store: ServiceState, kind: str, repo_id: str | None, query: str) -> tuple[int, dict]:
    try:
        params = parse_qs(query)
        limit = int(params.get("limit", ["100"])[0])
        if kind != "requests" and not store.repositories.has_snapshot(repo_id):
            return 404, {"error": "unknown repo_id"}
        return 200, store.queries.get_page(kind, repo_id, limit=limit,
            q=params.get("q", [""])[0], cursor=params.get("cursor", [None])[0])
    except ValueError as exc:
        return 400, {"error": str(exc)}


def requests_summary_payload(store: ServiceState, repo_id: str | None, timeline_hours: int = 24) -> tuple[int, dict]:
    """Aggregates for the dashboard's request charts (top IPs/paths, requests
    per repository, an hourly timeline, and per-repo cache HIT/MISS) —
    already-computed counters, separate from the paged request list. by_repo
    and cache_hit_stats ignore the repo_id filter (already broken down by
    repository); timeline respects it (a per-repo or global chart, per the
    dashboard's current selection)."""
    return 200, {
        "by_client_ip": store.requests.get_top_client_ips(repo_id=repo_id),
        "by_path": store.requests.get_top_request_paths(repo_id=repo_id),
        "by_repo": store.requests.get_requests_by_repo(),
        "timeline": store.requests.get_requests_timeline(repo_id=repo_id, hours=timeline_hours),
        "cache_hit_stats": [
            {"repo_id": rid, "total": v["total"], "hits": v["hits"]}
            for rid, v in store.requests.get_request_hit_stats().items()
        ],
    }


def prefetch_efficiency_payload(store: ServiceState) -> tuple[int, dict]:
    """docs_dev/ROADMAP.md item 19 — of what repowatch actively prefetched
    ahead of demand per repo, how much was actually requested by a client
    afterward. See ServiceState.requests.get_prefetch_efficiency for the exact
    correlation. Always 200: an empty list (no repo has prefetched anything
    yet, or syslog_listener is disabled so nothing can be linked to a
    request) is a normal state, not an error."""
    return 200, {"items": store.requests.get_prefetch_efficiency()}


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
    config_path: str | Path, store: ServiceState, *, include_cache_dir: bool = False, current: Config | None = None
) -> tuple[int, dict]:
    """GET /api/stats — state_db size/row counts are always cheap and
    included; the nginx package cache directory's size is only walked when
    include_cache_dir is set (see cache_dir_stats).

    docs_dev/ROADMAP.md item 8: when nginx.enable_cache_probe is on, this
    prefers cache.probe.cache_dir_size() (the njs-based ground-truth scan)
    over the os.walk()-based cache_dir_stats() below — it runs inside the
    nginx worker itself (already the cache's own owner), so it never hits
    the 0700-subdirectory permission gap cache_dir_stats() has to work
    around and warn about. That path doesn't need NginxConfig.cache_dir to
    be set at all (it only needs cache_base_url, always present) — the
    field is used purely for display if available.

    Without enable_cache_probe, behavior is unchanged: cache_dir_stats()
    (os.walk()) is used, and it's only reported when the operator has
    opted into NginxConfig.cache_dir being visible to repowatch at all (see
    docs_dev/ROADMAP.md item 12) — otherwise there is no path to walk, and
    that's a normal, expected configuration, not an error.
    """
    payload = store.database.get_storage_stats()
    if include_cache_dir:
        try:
            current = current if current is not None else load_config(config_path)
        except ConfigError:
            return 500, {"error": "config.yaml is currently invalid"}
        if current.nginx.enable_cache_probe:
            try:
                stats = asyncio.run(cache_probe.cache_dir_size(current.cache_base_url))
            except httpx.HTTPError as exc:
                payload["cache_dir"] = {"error": f"cache-probe unreachable: {exc}"}
            else:
                stats["path"] = current.nginx.cache_dir or current.cache_base_url
                stats["source"] = "cache_probe"
                payload["cache_dir"] = stats
        elif not current.nginx.cache_dir:
            payload["cache_dir"] = None
        else:
            try:
                payload["cache_dir"] = cache_dir_stats(current.nginx.cache_dir)
            except OSError as exc:
                payload["cache_dir"] = {"error": str(exc)}
    return 200, payload
