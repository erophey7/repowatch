"""Translate accepted nginx dedup routes into their final physical cache keys."""

from repowatch.config.models import Config
from repowatch.nginx.render import resolve_dedup_pairs
from repowatch.routing import CacheKeyBuilder, package_path, package_prefix


def dedup_keys(config: Config, rows: list[tuple[str, str, str, str]],
               builder: CacheKeyBuilder, *, pairs: list[tuple[str, str]] | None = None) -> dict[str, str]:
    """Resolve chains and conflicts exactly as the nginx renderer does."""
    if not config.nginx.enable_dedup:
        return {}
    if pairs is None:
        pairs = resolve_dedup_pairs(config, rows)
    rewrites = {source.lower(): target for source, target in pairs}
    repos = {repo.id: repo for repo in config.repos}
    bound = {repo.id: builder.for_repo(repo) for repo in config.repos}
    targets = {package_path(repos[canonical], filename): bound[canonical](filename)
               for _, canonical, _, filename in rows if canonical in repos}
    keys = {}
    for source, target in rewrites.items():
        seen = {source}
        while target.lower() in rewrites and target.lower() not in seen:
            seen.add(target.lower())
            target = rewrites[target.lower()]
        # An empty key denotes an unresolved cycle, never a cache hit.
        keys[source] = '' if target.lower() in seen else targets.get(target, '')
    return keys



def accepted_pairs(config: Config, cache, rows) -> list[tuple[str, str]]:
    """Reject redirects whose shared source or target URL has conflicting owners.

    The duplicate query alone cannot see owners with another or unknown hash.
    Keep evidence only for proposed URLs while streaming the whole catalog.
    """
    if not config.nginx.enable_dedup:
        return []
    pairs = resolve_dedup_pairs(config, rows)
    unseen = object()
    hashes = {uri.lower(): unseen for pair in pairs for uri in pair}
    if not hashes:
        return []
    prefixes = {repo.id: package_prefix(repo) + '/' for repo in config.repos}
    for repo_id, filename, content_hash in cache.iter_file_hashes():
        prefix = prefixes.get(repo_id)
        if prefix is None:
            continue
        uri = (prefix + filename).lower()
        if uri in hashes:
            previous = hashes[uri]
            hashes[uri] = (content_hash or None) if previous is unseen or previous == content_hash else None
    return [(source, target) for source, target in pairs
            if hashes[source.lower()] is not unseen
            and hashes[source.lower()] is not None
            and hashes[source.lower()] == hashes[target.lower()]]
