"""SQLite schema, migrations, and search-index initialization."""

from __future__ import annotations

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_sessions (
    secret_hash TEXT PRIMARY KEY,
    password_fingerprint TEXT NOT NULL,
    expires_at REAL NOT NULL,
    secure INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS host_tokens (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    secret_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    expires_at REAL,
    last_used_at REAL,
    revoked_at REAL
);

-- Nix closure artifacts are retained with warmed tombstones after catalog removal.
CREATE TABLE IF NOT EXISTS nix_artifacts (
    repo_id TEXT NOT NULL,
    package_key TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_hash TEXT,
    size INTEGER,
    PRIMARY KEY (repo_id, package_key, filename)
);
CREATE INDEX IF NOT EXISTS nix_artifacts_file ON nix_artifacts(repo_id, filename);
CREATE TABLE IF NOT EXISTS nix_trust (
    repo_id TEXT PRIMARY KEY,
    policy TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repo_state (
    repo_id       TEXT PRIMARY KEY,
    last_check    TEXT NOT NULL,
    changed_at    TEXT,
    index_etag    TEXT,           -- index ETag from the previous check (for HEAD comparison)
    index_last_modified TEXT,     -- index Last-Modified from the previous check
    package_count INTEGER,
    key_expires_at TEXT           -- soonest GPG key expiry in this repo's keyring, from the
                                   -- last check cycle (see gpgverify.soonest_key_expiry); NULL
                                   -- for apk repos, unsigned repos, or when unknown
);

-- Current snapshot: the single source of truth for package data.
CREATE TABLE IF NOT EXISTS repo_packages (
    repo_id      TEXT NOT NULL,
    package_key  TEXT NOT NULL,
    package_name TEXT,
    filename     TEXT NOT NULL,
    content_hash TEXT,
    PRIMARY KEY (repo_id, package_key)
);

CREATE INDEX IF NOT EXISTS idx_repo_packages_name
    ON repo_packages(repo_id, package_name, package_key);

CREATE TABLE IF NOT EXISTS repo_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id    TEXT NOT NULL,
    ts         TEXT NOT NULL,
    new_pkgs_json TEXT NOT NULL,   -- list of package-version strings that appeared
    removed_pkgs_json TEXT NOT NULL DEFAULT '[]',
    modified_pkgs_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_repo_events_repo_ts ON repo_events(repo_id, ts DESC);

CREATE TABLE IF NOT EXISTS pending_replacements (
    repo_id TEXT NOT NULL,
    package_key TEXT NOT NULL,
    targets_json TEXT NOT NULL,
    expected_hash TEXT,
    revision TEXT NOT NULL,
    last_error TEXT,
    PRIMARY KEY (repo_id, package_key)
);

CREATE TABLE IF NOT EXISTS request_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    repo_id          TEXT,           -- best-effort match by path prefix, may be NULL
    client_ip        TEXT,           -- $remote_addr from nginx, may be NULL (old log_format)
    method           TEXT NOT NULL,
    path             TEXT NOT NULL,
    status           TEXT,
    cache_status     TEXT,
    package_key      TEXT,           -- set only when exactly one repo's known packages
    package_repo_id  TEXT            -- contain this basename (see match_all_package_keys);
                                      -- independent of repo_id above, which is prefix-based
                                      -- and can disagree (e.g. NULL on an ambiguous shared
                                      -- pool/) or point at a different repo entirely
);

CREATE INDEX IF NOT EXISTS idx_request_events_repo_ts ON request_events(repo_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_request_events_ts ON request_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_request_events_repo_id ON request_events(repo_id, id DESC);
-- idx_request_events_package is created in _migrate(), not here: it
-- references package_key/package_repo_id, which on an existing (pre-item-19)
-- database only exist after _migrate()'s ALTER TABLE runs — see the
-- identical reasoning on idx_repo_packages_hash below.

-- Current warm state (not an event log — one row per package, upserted on
-- every warm attempt). Packages long gone from upstream are never written
-- again — without retention the row would stay forever, hence the
-- time-based cleanup on warmed_at (see ServiceState.prune_warmed_packages),
-- same as repo_events/request_events.
CREATE TABLE IF NOT EXISTS warmed_packages (
    repo_id     TEXT NOT NULL,
    package_key TEXT NOT NULL,
    filename    TEXT NOT NULL,
    warmed_at   TEXT NOT NULL,
    status      TEXT NOT NULL,   -- "ok" | "failed"
    http_status INTEGER,
    source      TEXT,            -- "prefetch" (repowatch warmed it ahead of demand) |
                                  -- "client" (first seen via a real client request) |
                                  -- NULL for rows written before this column existed
    PRIMARY KEY (repo_id, package_key)
);

CREATE INDEX IF NOT EXISTS idx_warmed_packages_repo ON warmed_packages(repo_id, warmed_at DESC);
CREATE INDEX IF NOT EXISTS idx_warmed_page ON warmed_packages(repo_id, warmed_at DESC, package_key DESC);

-- Ban on auto-warming by package name (not by a specific version — otherwise
-- the ban wouldn't survive a new release). Checked for both automatic and
-- manual warming (see prefetch.warm_cache) — the ban behaves the same
-- everywhere, no surprises.
CREATE TABLE IF NOT EXISTS prefetch_bans (
    repo_id      TEXT NOT NULL,
    package_name TEXT NOT NULL,
    banned_at    TEXT NOT NULL,
    PRIMARY KEY (repo_id, package_name)
);

-- Current "consecutive failure streak" state for a (repo_id, kind) pair —
-- not a log (that's repo_events), just the current counter, see
-- notifications.py. kind: "prefetch" (at least one failure in the last
-- warm_cache run) | "gpg" (SignatureError while checking the index).
-- The row is deleted entirely on the first success after a streak (see
-- reset_failure) — there's no separate "consecutive_failures = 0"; a
-- failure streak only makes sense while it hasn't been broken.
-- notified — whether a notification was already sent for the CURRENT
-- streak, so we don't resend on every following failed cycle (see
-- notifications.record_failure_and_maybe_notify).
CREATE TABLE IF NOT EXISTS failure_state (
    repo_id              TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    last_failure_at      TEXT,
    notified             INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo_id, kind)
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Migrate existing schemas inside the initialization transaction."""
    if 'modified_pkgs_json' not in {r[1] for r in conn.execute('PRAGMA table_info(repo_events)')}:
        conn.execute("ALTER TABLE repo_events ADD COLUMN modified_pkgs_json TEXT NOT NULL DEFAULT '[]'")
    if 'repo_ids' not in {r[1] for r in conn.execute('PRAGMA table_info(host_tokens)')}:
        conn.execute('ALTER TABLE host_tokens ADD COLUMN repo_ids TEXT')

    if 'content_hash' not in {r[1] for r in conn.execute('PRAGMA table_info(repo_packages)')}:
        conn.execute('ALTER TABLE repo_packages ADD COLUMN content_hash TEXT')
    # Created here, not in SCHEMA, so it always runs after the column above
    # is guaranteed to exist — SCHEMA's own CREATE TABLE IF NOT EXISTS is a
    # no-op against a pre-existing table and would otherwise leave this
    # index creation racing an as-yet-missing column on upgrade.
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_repo_packages_hash '
        'ON repo_packages(filename, content_hash) WHERE content_hash IS NOT NULL'
    )

    existing = {row[1] for row in conn.execute("PRAGMA table_info(repo_state)")}
    for column in ("index_etag", "index_last_modified"):
        if column not in existing:
            conn.execute(f"ALTER TABLE repo_state ADD COLUMN {column} TEXT")
    if "package_count" not in existing:
        conn.execute("ALTER TABLE repo_state ADD COLUMN package_count INTEGER")
    if "key_expires_at" not in existing:
        conn.execute("ALTER TABLE repo_state ADD COLUMN key_expires_at TEXT")

    if "snapshot_revision" not in existing:
        conn.execute("ALTER TABLE repo_state ADD COLUMN snapshot_revision INTEGER NOT NULL DEFAULT 0")
    if "source_identity" not in existing:
        conn.execute("ALTER TABLE repo_state ADD COLUMN source_identity TEXT")

    existing_request_events = {row[1] for row in conn.execute("PRAGMA table_info(request_events)")}
    if "client_ip" not in existing_request_events:
        conn.execute("ALTER TABLE request_events ADD COLUMN client_ip TEXT")
    if "package_key" not in existing_request_events:
        conn.execute("ALTER TABLE request_events ADD COLUMN package_key TEXT")
        conn.execute("ALTER TABLE request_events ADD COLUMN package_repo_id TEXT")
    # Same reasoning as idx_repo_packages_hash above: only safe to create
    # once package_key/package_repo_id are guaranteed to exist, which for an
    # upgraded database is only true after the ALTER TABLE calls just above.
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_request_events_package '
        'ON request_events(package_repo_id, package_key) WHERE package_key IS NOT NULL'
    )

    existing_warmed = {row[1] for row in conn.execute("PRAGMA table_info(warmed_packages)")}
    if "source" not in existing_warmed:
        conn.execute("ALTER TABLE warmed_packages ADD COLUMN source TEXT")

    if "revision" not in existing_warmed:
        conn.execute("ALTER TABLE warmed_packages ADD COLUMN revision TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE warmed_packages SET revision=hex(randomblob(16))")

    if "packages_json" in existing:
        # JSON is authoritative for the dual-write release, including after
        # rollback. Rebuild every repo: counts alone cannot detect stale rows.
        names_column = "names_json" if "names_json" in existing else "NULL"
        for repo_id, raw, names_raw in conn.execute(
            f"SELECT repo_id, packages_json, {names_column} FROM repo_state"
        ).fetchall():
            packages = json.loads(raw)
            names = json.loads(names_raw or "{}")
            if not isinstance(packages, dict) or not isinstance(names, dict):
                raise ValueError(f"Invalid legacy snapshot: {repo_id}")
            if any(not isinstance(k, str) or not isinstance(v, str) for k, v in packages.items()):
                raise ValueError(f"Invalid legacy package filenames: {repo_id}")
            conn.execute("DELETE FROM repo_packages WHERE repo_id = ?", (repo_id,))
            conn.executemany(
                "INSERT INTO repo_packages (repo_id, package_key, package_name, filename) VALUES (?, ?, ?, ?)",
                ((repo_id, k, names.get(k), v) for k, v in packages.items()),
            )
            conn.execute("UPDATE repo_state SET package_count = ? WHERE repo_id = ?",
                         (len(packages), repo_id))
        conn.execute("ALTER TABLE repo_state DROP COLUMN packages_json")
        if "names_json" in existing:
            conn.execute("ALTER TABLE repo_state DROP COLUMN names_json")


def _search_index(conn: sqlite3.Connection) -> bool:
    """Transactional derived index, rebuilt once for existing databases.

    No extension is loaded. SQLite without FTS5/trigram keeps the scan path.
    External content avoids another copy of names/keys; triggers cover every
    mutation, including pruning and direct SQL imports.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='package_search'").fetchone():
        return True
    try:
        conn.execute("CREATE VIRTUAL TABLE package_search USING fts5(package_key, package_name, "
                     "content='repo_packages', content_rowid='rowid', tokenize='trigram')")
    except sqlite3.OperationalError as exc:
        if 'no such module' in str(exc) or 'no such tokenizer' in str(exc):
            return False
        raise
    conn.execute("INSERT INTO package_search(package_search) VALUES ('rebuild')")
    conn.execute("""CREATE TRIGGER package_search_insert AFTER INSERT ON repo_packages BEGIN
        INSERT INTO package_search(rowid, package_key, package_name)
        VALUES (new.rowid, new.package_key, new.package_name); END""")
    conn.execute("""CREATE TRIGGER package_search_delete AFTER DELETE ON repo_packages BEGIN
        INSERT INTO package_search(package_search, rowid, package_key, package_name)
        VALUES ('delete', old.rowid, old.package_key, old.package_name); END""")
    conn.execute("""CREATE TRIGGER package_search_update AFTER UPDATE ON repo_packages
        WHEN old.package_key IS NOT new.package_key OR old.package_name IS NOT new.package_name
        BEGIN
        INSERT INTO package_search(package_search, rowid, package_key, package_name)
        VALUES ('delete', old.rowid, old.package_key, old.package_name);
        INSERT INTO package_search(rowid, package_key, package_name)
        VALUES (new.rowid, new.package_key, new.package_name); END""")
    return True


# Derived request statistics. request_events stays the log (recent-requests
# list, prefetch efficiency); the dashboard and /metrics aggregates are read
# from these tables instead of grouping the whole retained history on every
# poll. Triggers maintain them for every writer, like package_search above, so
# pruning, direct SQL and older code that only knows request_events cannot
# desynchronize them.
#
# request_counters.repo_key scopes a counter: 'r:<repo_id>' for one repository,
# '' for requests that matched no repository, '*' for all requests (only the
# 'ip' and 'path' kinds keep '*' rows, so a global top-N is a single index range).
# Rows are only ever updated in place by the triggers; a count that reaches
# zero stays until RequestsStore's prune sweep removes it, and readers ignore it.
_ROLLUP_TABLES = (
    """CREATE TABLE IF NOT EXISTS request_counters (
    kind     TEXT NOT NULL,             -- 'repo' | 'ip' | 'path'
    repo_key TEXT NOT NULL,
    key      TEXT NOT NULL,             -- client IP / request path / '' for 'repo'
    total    INTEGER NOT NULL,
    hits     INTEGER NOT NULL DEFAULT 0, -- cache HITs; only maintained for 'repo'
    PRIMARY KEY (kind, repo_key, key)
) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS request_hourly (
    hour     TEXT NOT NULL,             -- 'YYYY-MM-DDTHH' prefix of request_events.ts (UTC)
    repo_key TEXT NOT NULL,             -- as above, never '*'
    total    INTEGER NOT NULL,
    hits     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour, repo_key)
) WITHOUT ROWID""",
)


def _rollup_triggers() -> list[str]:
    """Trigger DDL: one pair (always / only with a client_ip) per row event."""
    triggers = []
    for name, event, row, add in (("insert", "INSERT", "new", True), ("delete", "DELETE", "old", False)):
        repo_key = f"CASE WHEN {row}.repo_id IS NULL THEN '' ELSE 'r:' || {row}.repo_id END"
        hit = f"CASE WHEN {row}.cache_status = 'HIT' THEN 1 ELSE 0 END"

        def counter(kind: str, scope: str, key: str, hits: str = "0") -> str:
            """One counter statement: an upsert for an insert trigger, an in-place decrement for a delete trigger."""
            if add:
                return (f"INSERT INTO request_counters (kind, repo_key, key, total, hits) "
                        f"VALUES ('{kind}', {scope}, {key}, 1, {hits}) "
                        f"ON CONFLICT (kind, repo_key, key) DO UPDATE SET total = total + 1, hits = hits + {hits};")
            return (f"UPDATE request_counters SET total = total - 1, hits = hits - {hits} "
                    f"WHERE kind = '{kind}' AND repo_key = {scope} AND key = {key};")

        if add:
            hourly = (f"INSERT INTO request_hourly (hour, repo_key, total, hits) "
                      f"VALUES (substr({row}.ts, 1, 13), {repo_key}, 1, {hit}) "
                      f"ON CONFLICT (hour, repo_key) DO UPDATE SET total = total + 1, hits = hits + {hit};")
        else:
            hourly = (f"UPDATE request_hourly SET total = total - 1, hits = hits - {hit} "
                      f"WHERE hour = substr({row}.ts, 1, 13) AND repo_key = {repo_key};")
        always = [counter("repo", repo_key, "''", hit),
                  counter("path", repo_key, f"{row}.path"), counter("path", "'*'", f"{row}.path"), hourly]
        with_ip = [counter("ip", repo_key, f"{row}.client_ip"), counter("ip", "'*'", f"{row}.client_ip")]
        triggers.append(f"CREATE TRIGGER request_rollup_{name} AFTER {event} ON request_events BEGIN "
                        + " ".join(always) + " END")
        triggers.append(f"CREATE TRIGGER request_rollup_ip_{name} AFTER {event} ON request_events "
                        f"WHEN {row}.client_ip IS NOT NULL BEGIN " + " ".join(with_ip) + " END")
    return triggers


def _request_rollups(conn: sqlite3.Connection) -> None:
    """Create the rollup tables/triggers once and fill them from existing history.

    Runs inside the initialization transaction, so a crash leaves either no
    rollups or complete ones. request_events rows are append/delete-only; nothing
    updates ts/repo_id/client_ip/path/cache_status in place."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'request_rollup_insert'").fetchone():
        return
    for statement in _ROLLUP_TABLES:
        conn.execute(statement)
    conn.execute("DELETE FROM request_counters")
    conn.execute("DELETE FROM request_hourly")
    scope = "CASE WHEN repo_id IS NULL THEN '' ELSE 'r:' || repo_id END"
    hit = "SUM(CASE WHEN cache_status = 'HIT' THEN 1 ELSE 0 END)"
    conn.execute(f"INSERT INTO request_counters (kind, repo_key, key, total, hits) "
                 f"SELECT 'repo', {scope}, '', COUNT(*), {hit} FROM request_events GROUP BY 2")
    for kind, column, where in (("ip", "client_ip", "WHERE client_ip IS NOT NULL"), ("path", "path", "")):
        conn.execute(f"INSERT INTO request_counters (kind, repo_key, key, total, hits) "
                     f"SELECT '{kind}', {scope}, {column}, COUNT(*), 0 FROM request_events {where} GROUP BY 2, 3")
        conn.execute(f"INSERT INTO request_counters (kind, repo_key, key, total, hits) "
                     f"SELECT '{kind}', '*', {column}, COUNT(*), 0 FROM request_events {where} GROUP BY 3")
    conn.execute(f"INSERT INTO request_hourly (hour, repo_key, total, hits) "
                 f"SELECT substr(ts, 1, 13), {scope}, COUNT(*), {hit} FROM request_events GROUP BY 1, 2")
    for trigger in _rollup_triggers():
        conn.execute(trigger)
