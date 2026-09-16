"""Validate CLI input and dispatch the selected command."""

from __future__ import annotations

import logging
import sys
from repowatch.cli.commands import command_hash_password, command_self_update, command_check_config, command_stats, command_set_password, command_nginx, command_check_once, command_serve_status, command_run, command_backup, command_supervise
from repowatch.cli.parser import build_parser
from repowatch.config.load import load_config
from repowatch.errors import ConfigError
from repowatch.runtime.context import ServiceState

def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, validate configuration, and dispatch a command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.command == 'hash-password':
        return command_hash_password(args)

    if args.command == 'self-update':
        return command_self_update(args)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    if args.command == 'check-config':
        return command_check_config(args, config)

    if args.command == 'stats':
        return command_stats(args, config)

    if args.command == 'set-password':
        return command_set_password(args, config)

    if args.command in ('nginx-render', 'nginx-apply'):
        return command_nginx(args, config)

    store = ServiceState(config.state_db)
    store.bandwidth.bind(args.config)

    if args.command == 'check-once':
        return command_check_once(args, config, store)

    if args.command == 'serve-status':
        return command_serve_status(args, config, store)

    if args.command == 'run':
        return command_run(args, config, store)

    if args.command == 'backup':
        return command_backup(args, config)

    if args.command == 'supervise':
        return command_supervise(args, config, store)

    parser.error(f"unknown command: {args.command}")
    return 2
