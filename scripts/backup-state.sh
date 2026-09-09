#!/usr/bin/env bash
# Online backup of state.sqlite3 (repositories, change history,
# warmed_packages, request_events) via sqlite3's built-in `.backup` command —
# safe for a database that repowatch keeps writing to concurrently (not a
# plain cp/rsync, which risks copying the file mid-transaction/WAL).
#
# Usage: backup-state.sh [state_db] [backup_dir] [retention_days]
# Environment variables (all optional, positional args take priority):
#   STATE_DB       — path to state.sqlite3 (default /var/lib/repowatch/state.sqlite3)
#   BACKUP_DIR     — where to put backups (default /var/lib/repowatch/backups)
#   RETENTION_DAYS — how many days to keep backups (default 14 — noticeably
#                    longer than event_retention_days: this is a snapshot of
#                    ALL state for restoration, not one repository's log)
#
# For restore instructions, see docs_dev/DEPLOYMENT.md, section "Backup and
# restore of state.sqlite3".

set -euo pipefail

STATE_DB="${1:-${STATE_DB:-/var/lib/repowatch/state.sqlite3}}"
BACKUP_DIR="${2:-${BACKUP_DIR:-/var/lib/repowatch/backups}}"
RETENTION_DAYS="${3:-${RETENTION_DAYS:-14}}"

if [ ! -f "$STATE_DB" ]; then
    echo "state.sqlite3 not found: $STATE_DB" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
dest="$BACKUP_DIR/state-$ts.sqlite3"

# `.backup` — the sqlite online backup API (the same mechanism sqlite3
# itself uses internally), a consistent snapshot with no reader/writer
# locking for the whole duration of the copy (unlike cp/rsync over a live
# file).
sqlite3 "$STATE_DB" ".backup '$dest'"
gzip "$dest"

echo "backup: $dest.gz"

# Rotation — anything older than RETENTION_DAYS days is cleaned up on every
# run, with no separate step/cron job.
find "$BACKUP_DIR" -maxdepth 1 -name 'state-*.sqlite3.gz' -mtime "+$RETENTION_DAYS" -print -delete
