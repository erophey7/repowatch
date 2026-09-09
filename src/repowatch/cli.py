from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading

from repowatch._paths import DEFAULT_CONFIG_PATH
from repowatch.config import ConfigError, load_config
from repowatch.state import StateStore
from repowatch.watcher import check_all, prune_all, run_forever


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repowatch")
    parser.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG_PATH,
        help=f"path to config.yaml (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="daemon: check loop + status API in one process")
    sub.add_parser("check-once", help="one pass over all repositories, then exit")
    sub.add_parser("serve-status", help="run only the HTTP status API")
    sub.add_parser(
        "check-config",
        help="validate config.yaml and exit without doing anything else (does not touch state_db)",
    )
    sub.add_parser(
        "hash-password",
        help="generate an admin_password_hash for config.yaml (prompts for a password interactively)",
    )

    sub.add_parser("set-password", help="change the administrator password in config.yaml (hidden input)")

    selfupdate_parser = sub.add_parser(
        "self-update",
        help="check GitHub Releases for a newer repowatch and, if the checksum "
             "matches, install it (see docs/deployment.md)",
    )
    selfupdate_parser.add_argument("--repo", required=True, help='GitHub "owner/name" to check for releases')
    selfupdate_parser.add_argument(
        "--check", action="store_true",
        help="only report whether a newer, verified release is available; do not install it",
    )
    selfupdate_parser.add_argument(
        "--force", action="store_true",
        help="install even if the release's version is not newer than the currently installed one",
    )

    nginx_render = sub.add_parser("nginx-render", help="print the nginx config without writing/applying it")
    nginx_render.add_argument("--policy", help="use paths from the installed nginx policy.json")
    nginx_apply = sub.add_parser("nginx-apply", help="validate and apply nginx (root)")
    nginx_apply.add_argument("--policy", default="/etc/nginx/repowatch/policy.json")
    nginx_apply.add_argument("--force", action="store_true")

    backup_parser = sub.add_parser("backup", help="one-shot online backup of state_db (see docs/deployment.md)")
    backup_parser.add_argument("backup_dir", help="directory to write the backup into")
    backup_parser.add_argument("--retention-days", type=int, default=14,
                                help="delete backups in backup_dir older than this many days (default: 14)")

    supervise_parser = sub.add_parser(
        "supervise",
        help="daemon for environments without systemd: repowatch run, plus optional "
             "periodic backup and nginx reconciliation in the same process "
             "(see docs/deployment.md, \"Running without systemd\")",
    )
    supervise_parser.add_argument("--pid-file", help="write this process's PID here; removed on clean exit")
    supervise_parser.add_argument("--backup-dir", help="enable periodic backups into this directory")
    supervise_parser.add_argument("--backup-interval-hours", type=float, default=24)
    supervise_parser.add_argument("--backup-retention-days", type=int, default=14)
    supervise_parser.add_argument("--nginx", action="store_true",
                                   help="also periodically reconcile/reload nginx (requires root)")
    supervise_parser.add_argument("--nginx-policy", default="/etc/nginx/repowatch/policy.json")
    supervise_parser.add_argument("--nginx-interval-seconds", type=float, default=15)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.command == "hash-password":
        # Doesn't require an existing config.yaml — a standalone utility.
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

    if args.command == "self-update":
        # Doesn't require an existing config.yaml — this only touches the
        # installed Python package, not config.yaml/state_db/nginx.
        from repowatch.selfupdate import SelfUpdateError, run_self_update

        try:
            message = asyncio.run(run_self_update(args.repo, check_only=args.check, force=args.force))
        except SelfUpdateError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(message)
        return 0

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    if args.command == "check-config":
        # Validation only — deliberately does not create a StateStore
        # (doesn't touch state_db/directories on disk), unlike the other
        # commands below.
        print(f"config.yaml is valid: {args.config}")
        print(f"repositories: {len(config.repos)}")
        for repo in config.repos:
            print(f"  - {repo.id} ({repo.type}) upstream={repo.upstream}")
        return 0

    if args.command == "set-password":
        import getpass
        from repowatch.auth import hash_password
        from repowatch.config_edit import set_password_hash
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

    if args.command in ("nginx-render", "nginx-apply"):
        from repowatch.nginx import render, apply
        try:
            if args.command == "nginx-render":
                paths = {}
                if args.policy:
                    import json
                    from pathlib import Path
                    policy = json.loads(Path(args.policy).read_text())
                    paths = {key: policy[key] for key in ("cache_dir", "access_log")}
                print(render(config, **paths), end="")
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

    store = StateStore(config.state_db)

    if args.command == "check-once":
        asyncio.run(check_all(config, store))
        prune_all(config, store)
        return 0

    if args.command == "serve-status":
        from repowatch.api import serve

        serve(config, store, args.config)
        return 0

    if args.command == "run":
        from repowatch.api import serve

        api_thread = threading.Thread(
            target=serve, args=(config, store, args.config), daemon=True
        )
        api_thread.start()

        if config.syslog_listener.enabled:
            from repowatch.syslog_listener import run_listener

            syslog_thread = threading.Thread(
                target=run_listener, args=(args.config, config, store), daemon=True
            )
            syslog_thread.start()

        asyncio.run(run_forever(args.config, store, initial_config=config))
        return 0

    if args.command == "backup":
        from repowatch.backup import backup_once

        try:
            path = backup_once(config.state_db, args.backup_dir, args.retention_days)
        except OSError as exc:
            print(f"backup failed: {exc}", file=sys.stderr)
            return 1
        print(f"backup: {path}")
        return 0

    if args.command == "supervise":
        from repowatch.api import serve
        from repowatch.supervisor import supervise

        api_thread = threading.Thread(
            target=serve, args=(config, store, args.config), daemon=True
        )
        api_thread.start()

        if config.syslog_listener.enabled:
            from repowatch.syslog_listener import run_listener

            syslog_thread = threading.Thread(
                target=run_listener, args=(args.config, config, store), daemon=True
            )
            syslog_thread.start()

        try:
            asyncio.run(supervise(
                args.config, config, store,
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

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
