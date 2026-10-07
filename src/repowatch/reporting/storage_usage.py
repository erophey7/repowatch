"""Attribute observed cache-file bytes without multiplying physical storage."""

from __future__ import annotations

from repowatch.cache.probe import Inventory
from repowatch.config.models import Config
from repowatch.routing import CacheKeyBuilder, package_path


def storage_usage(config: Config, catalogs: dict, inventory: Inventory,
                  builder: CacheKeyBuilder, dedup_keys: dict[str, str]) -> dict:
    """Report exclusive physical ownership and deduplicated accessible file sets.

    Ownership follows the direct key, not the set of repositories redirected to
    it. Shared direct routes use the smallest repository id as a stable owner.
    Historical warm records help attribute bytes but never prove file presence.
    """
    present = {entry.key for entry in inventory.entries if entry.key and not entry.error}
    owners: dict[str, str] = {}
    accessible: dict[str, set[str]] = {}
    items = {}
    groups = {}
    for repo in sorted(config.repos, key=lambda repo: repo.id):
        catalog = catalogs[repo.id]
        reason = ('no_snapshot' if not catalog['last_check'] else
                  'source_changed' if catalog['source_identity'] != repo.catalog_identity() else None)
        items[repo.id] = dict(repo_id=repo.id, group=repo.group, physical_bytes=0,
                             physical_files=0, available_bytes=0, available_files=0, reason=reason)
        accessible[repo.id] = set()
        groups.setdefault(repo.group, dict(group=repo.group, physical_bytes=0, physical_files=0,
                                          available_bytes=0, available_files=0, unknown_repositories=0))
        if reason:
            groups[repo.group]['unknown_repositories'] += 1
            continue
        key_for = builder.for_repo(repo)
        def record(filename: str, *, available: bool = False) -> None:
            # Retain only physically observed keys, including pre-toggle copies.
            direct = key_for(filename)
            previous = key_for(filename, dedup=not config.nginx.enable_dedup)
            for key in (direct, previous):
                if key in present:
                    owners.setdefault(key, repo.id)
            if available:
                key = dedup_keys.get(package_path(repo, filename).lower(), direct)
                if key and key in present:
                    accessible[repo.id].add(key)

        for package, filename in catalog['packages']:
            record(filename, available=True)
            if repo.type == 'nix':
                for artifact in catalog['artifacts'].get(package, ()):
                    record(artifact, available=True)
        for filename in catalog.get('warmed', ()):
            record(filename)
        # Orphaned/historical closure artifacts still occupy physical space,
        # but only artifacts of current Nix roots contribute to availability.
        for artifacts in catalog['artifacts'].values():
            for filename in artifacts:
                record(filename)

    total_bytes = unattributed_bytes = unattributed_files = unreadable_sizes = 0
    file_sizes = {}
    for entry in inventory.entries:
        readable_size = type(entry.size) is int and entry.size >= 0
        size = entry.size if readable_size else 0
        unreadable_sizes += not readable_size
        total_bytes += size
        owner = owners.get(entry.key) if entry.key and not entry.error else None
        if owner is None:
            unattributed_bytes += size
            unattributed_files += 1
        else:
            items[owner]['physical_bytes'] += size
            items[owner]['physical_files'] += 1
        if entry.key and not entry.error:
            # A key is accessible once even if a malformed/inconsistent scan
            # lists it repeatedly; physical accounting still counts each file.
            file_sizes[entry.key] = max(file_sizes.get(entry.key, 0), size)

    group_keys = {group: set() for group in groups}
    for repo in config.repos:
        row = items[repo.id]
        keys = accessible[repo.id] & file_sizes.keys()
        row['available_bytes'] = sum(file_sizes[key] for key in keys)
        row['available_files'] = len(keys)
        group = groups[repo.group]
        group['physical_bytes'] += row['physical_bytes']
        group['physical_files'] += row['physical_files']
        group_keys[repo.group].update(keys)
    for group, keys in group_keys.items():
        groups[group]['available_bytes'] = sum(file_sizes[key] for key in keys)
        groups[group]['available_files'] = len(keys)
    unreadable_keys = sum(1 for entry in inventory.entries if not entry.key or entry.error)
    return dict(
        total_bytes=total_bytes, file_count=len(inventory.entries),
        attributed_bytes=total_bytes - unattributed_bytes,
        unattributed_bytes=unattributed_bytes, unattributed_files=unattributed_files,
        unreadable_sizes=unreadable_sizes, unreadable_keys=unreadable_keys,
        failed_leaves=inventory.failed_leaves,
        inventory_complete=not (inventory.failed_leaves or unreadable_sizes or unreadable_keys),
        repositories=[items[repo.id] for repo in config.repos],
        groups=list(groups.values()),
    )
