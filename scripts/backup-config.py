"""Installed backup entry point: follow state_db from the active YAML."""
from __future__ import annotations

import logging
from pathlib import Path
import subprocess
import sys

from repowatch.errors import ConfigError
from repowatch.config.load import load_config


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    if len(sys.argv) != 3:
        logging.error('usage: backup-config.py CONFIG BACKUP_DIR')
        return 2
    try:
        config = load_config(sys.argv[1])
        return subprocess.run([
            'bash', str(Path(__file__).with_name('backup-state.sh')),
            str(config.state_db), sys.argv[2],
        ], check=False).returncode
    except (ConfigError, OSError) as exc:
        logging.error('backup failed: %s', exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
