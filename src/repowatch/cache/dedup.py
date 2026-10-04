"""Translate accepted nginx dedup routes into their final physical cache keys."""

from repowatch.config.models import Config
from repowatch.nginx.render import resolve_dedup_pairs
from repowatch.routing import CacheKeyBuilder, package_path


def dedup_keys(config: Config, rows: list[tuple[str, str, str]],
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
               for _, canonical, filename in rows if canonical in repos}
    keys = {}
    for source, target in rewrites.items():
        seen = {source}
        while target.lower() in rewrites and target.lower() not in seen:
            seen.add(target.lower())
            target = rewrites[target.lower()]
        # An empty key denotes an unresolved cycle, never a cache hit.
        keys[source] = '' if target.lower() in seen else targets.get(target, '')
    return keys

