"""Implement command-line commands and their output conventions."""

from __future__ import annotations

import asyncio
import repowatch.cache.probe as cache_probe
import sys
from repowatch.errors import ConfigError
from repowatch.operations.cleanup import prune_all
from repowatch.runtime.context import ServiceState
from repowatch.runtime.scheduler import check_all
from repowatch.runtime.service import start_listeners

def command_hash_password(args) -> int:
    """Execute the hash-password command."""
    import getpass

    from repowatch.auth import hash_password

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if not password:
        print("password must not be empty", file=sys.stderr)
        return 1
    if password != confirm:
        print("passwords do not match", file=sys.stderr)
        return 1
    print(hash_password(password))
    return 0


def command_self_update(args) -> int:
    """Execute the self-update command."""
    from repowatch.selfupdate import SelfUpdateError, run_self_update

    try:
        message = asyncio.run(run_self_update(args.repo, check_only=args.check, force=args.force))
    except SelfUpdateError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(message)
    return 0


def command_check_config(args, config) -> int:
    """Execute the check-config command."""
    if any(repo.type == 'nix' for repo in config.repos):
        import shutil
        missing = [tool for tool in ('nix', 'nix-env', 'nix-instantiate') if shutil.which(tool) is None]
        if missing:
            print('Nix repositories require optional system tools: ' + ', '.join(missing), file=sys.stderr)
            return 1
    # Validation only — deliberately does not create a ServiceState
    # (doesn't touch state_db/directories on disk), unlike the other
    # commands below.
    print(f"config.yaml is valid: {args.config}")
    print(f"repositories: {len(config.repos)}")
    for repo in config.repos:
        print(f"  - {repo.id} ({repo.type}) upstream={repo.upstream}")
    return 0


def command_stats(args, config) -> int:
    """Execute the stats command."""
    import httpx
    from repowatch.reporting.statistics import cache_dir_stats
    import repowatch.cache.probe as cache_probe

    store = ServiceState(config.state_db)
    store.bandwidth.bind(args.config)
    stats = store.database.get_storage_stats()
    print(f"state_db: {config.state_db} ({stats['state_db_bytes']:,} bytes)")
    for table, count in stats["tables"].items():
        print(f"  {table}: {count:,} row(s)")
    if args.cache_dir:
        if config.nginx.enable_cache_probe:
            # docs_dev/ROADMAP.md item 8/27 — the njs-based ground-truth
            # scan runs inside the nginx worker itself, so it doesn't
            # need nginx.cache_dir set locally at all and never hits the
            # 0700-subdirectory permission gap the os.walk() path below
            # has to warn about; see cache.probe.cache_dir_size().
            try:
                cache = asyncio.run(cache_probe.cache_dir_size(config.cache_base_url))
                path = config.nginx.cache_dir or config.cache_base_url
                print(f"cache_dir: {path} (via cache-probe) — {cache['size_bytes']:,} bytes, "
                      f"{cache['file_count']:,} file(s)")
                if cache.get("unreadable_keys"):
                    print(f"  NOTE: {cache['unreadable_keys']:,} file(s) had an unreadable "
                          "stored key (not identifiable individually).",
                          file=sys.stderr)
                if cache.get("unreadable_sizes"):
                    print(f"  WARNING: {cache['unreadable_sizes']:,} file size(s) could not be read; "
                          "reported bytes are incomplete.", file=sys.stderr)
                if cache.get("incomplete_leaves"):
                    print(f"  WARNING: {cache['incomplete_leaves']:,} of 4096 cache directories "
                          "could not be scanned (transient failure) — the numbers above are an "
                          "UNDERCOUNT.", file=sys.stderr)
            except httpx.HTTPError as exc:
                print(f"cache_dir: cache-probe unreachable: {exc}", file=sys.stderr)
                return 1
        elif not config.nginx.cache_dir:
            print("cache_dir: not set in config.yaml (nginx.cache_dir) — nothing to walk")
        else:
            try:
                cache = cache_dir_stats(config.nginx.cache_dir)
                print(f"cache_dir: {cache['path']} — {cache['size_bytes']:,} bytes, "
                      f"{cache['file_count']:,} file(s)")
                if cache.get("inaccessible_directories"):
                    print(f"  WARNING: {cache['inaccessible_directories']:,} subdirector"
                          "y/ies could not be read (permission denied) — the numbers above "
                          "are an UNDERCOUNT. nginx creates proxy_cache_path's levels=1:2 "
                          "subdirectories 0700, owned by the nginx worker user; the "
                          "repowatch service user typically can't read them at all.",
                          file=sys.stderr)
            except OSError as exc:
                print(f"cache_dir: {config.nginx.cache_dir} — error: {exc}", file=sys.stderr)
                return 1
    return 0


def command_set_password(args, config) -> int:
    """Execute the set-password command."""
    import getpass
    from repowatch.auth import hash_password
    from repowatch.config.edit import set_password_hash
    try:
        password = getpass.getpass("New password: ")
        confirm = getpass.getpass("Confirm password: ")
        if not password or len(password) > 1024 or password != confirm:
            print("password must be 1-1024 characters; confirmation must match", file=sys.stderr)
            return 1
        set_password_hash(args.config, hash_password(password))
    except (ConfigError, OSError, EOFError) as exc:
        print(f"failed to change password: {exc}", file=sys.stderr)
        return 1
    print("Password changed. Administrator sessions were revoked; host tokens were kept.")
    return 0


def command_nginx(args, config) -> int:
    """Execute the nginx command."""
    from repowatch.nginx.render import render
    from repowatch.nginx.render import render_purge
    from repowatch.nginx.render import render_dedup
    from repowatch.nginx.apply import apply
    try:
        if args.command == "nginx-render":
            paths = {}
            purge_conf = None
            dedup_conf = None
            if args.policy:
                import json
                from pathlib import Path
                policy_path = Path(args.policy)
                policy = json.loads(policy_path.read_text())
                paths = {key: policy[key] for key in ("cache_dir", "access_log")}
                purge_conf = str(policy_path.parent / "purge.conf")
                dedup_conf = str(policy_path.parent / "dedup.map")
                paths["purge_conf"] = purge_conf
                paths["dedup_conf"] = dedup_conf
            print(render(config, **paths), end="")
            if config.nginx.enable_purge:
                # Purge locations are `include`d from a separate file
                # (see nginx.render.render_purge) rather than being printed
                # inline here — surface both parts so a manual/no-policy
                # setup knows where to save the second one.
                destination = purge_conf or "<same directory as active.conf>/purge.conf"
                print(f"\n# --- save the following as {destination} ---\n")
                print(render_purge(config), end="")
            if config.nginx.enable_dedup:
                # Real (duplicate -> canonical) pairs come from a
                # ServiceState query (see nginx-apply/nginx.apply.apply.apply) — this
                # preview command deliberately never opens the database
                # (see check-config's same rule), so it always shows an
                # empty map here; only the include line/map{} block in
                # the printed config above is meaningful for a preview.
                destination = dedup_conf or "<same directory as active.conf>/dedup.map"
                print(f"\n# --- save the following as {destination} "
                      f"(actual pairs are computed by nginx-apply from state_db, not shown here) ---\n")
                print(render_dedup(config, []), end="")
        else:
            import os
            if os.geteuid() != 0:
                raise ConfigError("nginx-apply requires root; automatic application is done by the systemd timer")
            changed = apply(args.config, args.policy, force=args.force)
            print("nginx applied" if changed else "nginx unchanged/disabled")
    except (ConfigError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def command_check_once(args, config, store) -> int:
    """Execute the check-once command."""
    failed = asyncio.run(check_all(config, store))
    asyncio.run(prune_all(config, store))
    return 1 if failed else 0


def command_serve_status(args, config, store) -> int:
    """Execute the serve-status command."""
    from repowatch.web.server import serve

    serve(config, store, args.config)
    return 0


def command_run(args, config, store) -> int:
    """Execute the run command."""
    from repowatch.runtime.service import supervise

    with start_listeners(args.config, config, store) as listeners:
        asyncio.run(supervise(args.config, config, store, listeners=listeners))
    return 0


def command_backup(args, config) -> int:
    """Execute the backup command."""
    from repowatch.backup import backup_once

    try:
        path = backup_once(config.state_db, args.backup_dir, args.retention_days)
    except OSError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"backup: {path}")
    return 0


def command_supervise(args, config, store) -> int:
    """Execute the supervise command."""
    from repowatch.runtime.service import supervise

    try:
        with start_listeners(args.config, config, store) as listeners:
            asyncio.run(supervise(
                args.config, config, store, listeners=listeners,
                pid_file=args.pid_file,
                backup_dir=args.backup_dir,
                backup_interval_hours=args.backup_interval_hours,
                backup_retention_days=args.backup_retention_days,
                nginx=args.nginx,
                nginx_policy=args.nginx_policy,
                nginx_interval_seconds=args.nginx_interval_seconds,
            ))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0
