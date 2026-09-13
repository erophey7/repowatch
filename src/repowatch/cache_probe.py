"""Client for the optional /cache-probe, /cache-scan and /purge-raw
endpoints served by nginx's njs module (see nginx.render_probe_js()/
render_probe_conf(), docs_dev/ROADMAP.md item 8, which unifies items 23 and
33) — ground-truth cache introspection, running inside the nginx worker
itself so it can read proxy_cache_path's 0700-owned subdirectories
repowatch's own unprivileged process cannot.

Every function here assumes nginx.enable_cache_probe is actually on and the
locations exist — callers check the flag themselves before ever calling in,
same convention as prefetch.purge_removed's own enable_purge check.

/cache-probe is a non-destructive complement to prefetch.purge_selected():
that one can only learn whether something is cached by actually deleting it
(a live GET/HEAD probe against the real content was rejected there for a
real correctness risk — see its own docstring); this one only stat()s a
file on disk through njs, never touches proxy_cache or upstream at all.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from repowatch.config import Config, RepoConfig
from repowatch.nginx import compute_cache_key
from repowatch.parsers.base import USER_AGENT

logger = logging.getLogger(__name__)

# Every leaf directory the hardcoded `levels=1:2` proxy_cache_path scheme
# (nginx.py's render()) can ever produce: 16 possible 1-char first levels x
# 256 possible 2-char second levels = 4096, fully deterministic — no
# discovery step needed to learn the tree SHAPE, only its per-leaf CONTENTS
# via /cache-scan. Kept in this module (not nginx.py) since it's a
# scanning-client concern, not something render_probe_js() itself needs to
# know (the njs side is handed one already-validated `dir` per call).
_HEX = "0123456789abcdef"
LEAF_DIRS: list[str] = [f"{a}/{b}{c}" for a in _HEX for b in _HEX for c in _HEX]


@dataclass(frozen=True)
class ProbeResult:
    exists: bool
    size: int | None = None
    mtime: str | None = None


@dataclass(frozen=True)
class ScanEntry:
    leaf: str
    file: str
    key: str | None
    size: int | None = None
    mtime: str | None = None
    error: str | None = None


async def probe(client: httpx.AsyncClient, base_url: str, key: str) -> ProbeResult:
    """Non-destructive check: is a specific already-known cache key really
    on disk right now? `key` must be the exact literal proxy_cache_key
    string — see nginx.compute_cache_key() to build one for a package.

    Safe to use httpx's dict-based `params={"key": key}` here (unlike
    purge_raw()) — this hits njs's r.args.key (nginx.render_probe_js()'s
    `probe` handler), which DOES url-decode query parameters, so a
    percent-encoded "/" round-trips correctly. Confirmed live on
    production (2026-09-13) against real slash-containing keys."""
    resp = await client.get(
        f"{base_url}/cache-probe", params={"key": key},
        headers={"User-Agent": USER_AGENT}, timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("exists"):
        return ProbeResult(exists=False)
    return ProbeResult(exists=True, size=data.get("size"), mtime=data.get("mtime"))


async def scan_leaf(client: httpx.AsyncClient, base_url: str, leaf: str) -> list[ScanEntry]:
    """Lists every cache file under exactly one leaf directory (e.g. "c/29")
    together with its real on-disk stored key — bounded to that one
    directory's files so a single call can never block the nginx worker for
    the whole cache at once (see nginx.render_probe_js()'s docstring)."""
    resp = await client.get(
        f"{base_url}/cache-scan", params={"dir": leaf},
        headers={"User-Agent": USER_AGENT}, timeout=30,
    )
    resp.raise_for_status()
    return [
        ScanEntry(
            leaf=leaf, file=item["file"], key=item.get("key"),
            size=item.get("size"), mtime=item.get("mtime"), error=item.get("error"),
        )
        for item in resp.json()
    ]


async def full_inventory(base_url: str, *, concurrency: int = 8) -> list[ScanEntry]:
    """The complete real inventory of what nginx's cache actually holds —
    drives scan_leaf() over all 4096 possible leaf directories concurrently
    (bounded by `concurrency`), so no stateful cursor is needed on either
    side: Python already knows the whole namespace from the hardcoded
    levels=1:2 scheme, njs only ever answers "what's in this one directory".

    This is the expensive, on-demand-only operation both docs_dev/ROADMAP.md
    item 23 (completeness) and item 33 (real orphan discovery) build on —
    never call this on a background timer, same posture as
    StateStore.cache_dir_stats()'s full os.walk().

    A leaf directory that doesn't exist yet (most of them, on a small cache)
    comes back as an empty list from scan_leaf(), not an error — only a real
    HTTP failure for that one leaf is logged and skipped, so one bad request
    doesn't abort the whole inventory.
    """
    semaphore = asyncio.Semaphore(concurrency)
    results: list[ScanEntry] = []

    async def _run(client: httpx.AsyncClient, leaf: str) -> None:
        async with semaphore:
            try:
                entries = await scan_leaf(client, base_url, leaf)
            except httpx.HTTPError as exc:
                logger.warning("cache-scan failed for leaf %s: %s", leaf, exc)
                return
        results.extend(entries)

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(_run(client, leaf) for leaf in LEAF_DIRS))
    return results


async def cache_dir_size(base_url: str, *, concurrency: int = 8) -> dict:
    """Ground-truth cache directory size/file count via the njs full
    inventory (docs_dev/ROADMAP.md item 27, hooked up to item 8's
    infrastructure) — the accurate counterpart to api.cache_dir_stats()'s
    os.walk(), for when nginx.enable_cache_probe is on. Unlike that
    os.walk(), this runs inside the nginx worker itself (already the
    cache's own owner), so it never hits the 0700-subdirectory permission
    gap documented there — no `inaccessible_directories` possible, the
    count is always complete.

    Same expensive/on-demand-only posture as cache_dir_stats() and
    full_inventory() itself: only call this from an explicit operator
    action (dashboard "Calculate cache directory size" / `repowatch stats
    --cache-dir`), never on a background timer or poll interval.

    Files whose key couldn't be read (scan()'s `error` field set — a rare,
    real possibility, e.g. a file mid-write) are still counted for size
    (the stat() succeeded even if the key line didn't parse) but flagged
    via `unreadable_keys`, mirroring cache_dir_stats()'s
    `inaccessible_directories` as a visible "this number might be an
    undercount of METADATA, not necessarily of bytes" signal — distinct
    from a permission gap, which this approach doesn't have.
    """
    async with httpx.AsyncClient() as client:
        # Fails loudly (propagates) if /cache-scan isn't actually reachable
        # — e.g. enable_cache_probe is set in config.yaml but the
        # background nginx-apply timer hasn't reconciled it yet, or the njs
        # module failed to load. Without this check, every one of the 4096
        # leaf requests full_inventory() makes below would fail identically
        # and get silently swallowed there (by design, so one bad leaf
        # doesn't abort the whole scan) — indistinguishable from "the cache
        # is genuinely empty", the exact pitfall api.cache_dir_stats()
        # already guards against for os.walk() with its own explicit
        # is-a-directory check.
        await scan_leaf(client, base_url, "0/00")

    entries = await full_inventory(base_url, concurrency=concurrency)
    total_bytes = sum(e.size or 0 for e in entries)
    result = {"size_bytes": total_bytes, "file_count": len(entries)}
    unreadable = sum(1 for e in entries if e.key is None)
    if unreadable:
        result["unreadable_keys"] = unreadable
    return result


async def purge_raw(client: httpx.AsyncClient, base_url: str, key: str) -> str:
    """Evicts a cache entry by its exact literal key, independent of any
    current repo route (see nginx.render_purge()'s /purge-raw location,
    only emitted when both enable_purge and enable_cache_probe are on) —
    the only way to clean up a genuine orphan (docs_dev/ROADMAP.md item 33)
    whose repo/config no longer exists to rebuild a normal per-route purge
    URL for. Same result contract as prefetch.purge_selected(): "purged" |
    "not_cached" | "error (...)".

    Deliberately does NOT use httpx's `params={"key": key}` (unlike
    probe()/scan_leaf()) — real bug found on production (2026-09-13):
    httpx percent-encodes "/" in dict-based query params (key= becomes
    ...%2Farch%2F...), and unlike njs's r.args (which DOES url-decode, so
    probe()/scan_leaf() work fine that way), nginx's native $arg_key
    variable — used directly by the third-party ngx_cache_purge module's
    `proxy_cache_purge repo_cache $arg_key;`, not by njs at all — does NOT
    decode percent-encoding. The literal %2F-containing string was hashed
    instead of the real key, so every purge silently reported 404
    ("not_cached") without deleting anything, confirmed on a real
    still-cached file (probe() before/after showed no change). Building
    the URL as a plain string keeps "/" and everything else exactly as
    given, matching what a real client hitting nginx's raw variable needs
    byte-for-byte.
    """
    try:
        resp = await client.get(
            f"{base_url}/purge-raw?key={key}",
            headers={"User-Agent": USER_AGENT}, timeout=30,
        )
    except httpx.RequestError as exc:
        return f"error ({exc})"
    if resp.status_code == 200:
        return "purged"
    if resp.status_code == 404:
        return "not_cached"
    return f"error (HTTP {resp.status_code})"


async def purge_selected_raw(config: Config, repo: RepoConfig, items: dict[str, str]) -> dict[str, str]:
    """Drop-in alternative to prefetch.purge_selected() — same inputs
    ({package_key: filename}), same result contract per key
    ("purged"/"not_cached"/"error (...)") — used by api.py's dashboard
    purge handlers (purge_selected_payload, remove_warmed_package_payload)
    INSTEAD of prefetch.purge_selected() whenever nginx.enable_cache_probe
    is on (2026-09-14, by direct user request: "по умолчанию пурж через
    дашборд работает стандартным механизмом, но если включен cache_probe
    применяем purge_raw").

    Goes through the route-independent /purge-raw location (see
    nginx.render_purge()) instead of the per-repo /purge<prefix> one, using
    nginx.compute_cache_key() to build each item's exact literal key in
    Python — no location/regex match needed on the nginx side at all. This
    isn't just a style choice: prefetch.purge_selected()'s per-route purge
    always computes the key under the repo's CURRENT nginx.enable_dedup
    basis, so a file that predates a dedup toggle (real production case
    found 2026-09-13 — most of a 3804-entry "zombie" batch turned out to be
    exactly this) reports "not_cached" and is never actually evicted, even
    though it's still sitting on disk. compute_cache_key() is exactly the
    same function that lets cache_probe's scan classify those entries
    correctly, so purging through the same path keeps both sides
    consistent — no separate basis-guessing logic duplicated here.

    Callers gate on enable_cache_probe themselves (same convention as the
    rest of this module) — this function does not check the flag.
    """
    if not items:
        return {}
    semaphore = asyncio.Semaphore(config.prefetch_concurrency)
    results: dict[str, str] = {}

    async def _run(client: httpx.AsyncClient, key: str, filename: str) -> None:
        cache_key = compute_cache_key(config, repo, filename)
        async with semaphore:
            results[key] = await purge_raw(client, config.cache_base_url, cache_key)

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(_run(client, key, filename) for key, filename in items.items()))
    return results
