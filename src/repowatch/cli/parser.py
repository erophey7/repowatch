"""Declare command-line arguments without side effects."""

from __future__ import annotations

import argparse
from repowatch._paths import DEFAULT_CONFIG_PATH

def build_parser() -> argparse.ArgumentParser:
    """Declare CLI commands without opening configuration or runtime resources."""
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
    stats_parser = sub.add_parser(
        "stats",
        help="print state_db size and per-table row counts (docs_dev/ROADMAP.md item 27)",
    )
    stats_parser.add_argument(
        "--cache-dir", action="store_true",
        help="also walk nginx.cache_dir (if set in config.yaml) and report its total size — "
             "can be slow for a large cache, so it's opt-in, not part of the default output",
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

    return parser
