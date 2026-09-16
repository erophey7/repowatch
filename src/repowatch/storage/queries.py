"""Cursor pagination and search across the supported record views."""

from __future__ import annotations

import base64
import json
from repowatch.storage.database import Database

class QueriesStore:
    """SQL operations for queries; the caller supplies the shared database."""

    def __init__(self, db: Database):
        self.db = db

    def get_page(self, kind: str, repo_id: str | None = None, *,
                 q: str = "", limit: int = 100, cursor: str | None = None) -> dict:
        """Bounded keyset pagination; cursors are bound to resource and filter.

        Pages reflect live state. Requests have immutable ids; warmed rows
        updated between pages can move to the first page (refresh to see them).
        """
        if not 1 <= limit <= 200 or len(q) > 200:
            raise ValueError("limit must be 1..200; q must be at most 200 characters")
        specs = {
            "packages": ("repo_packages", "package_key, package_name, filename", ["package_key"], "ASC"),
            "warmed": ("warmed_packages", "package_key, filename, warmed_at, status, http_status", ["warmed_at", "package_key"], "DESC"),
            "requests": ("request_events", "id, ts, repo_id, client_ip, method, path, status, cache_status", ["id"], "DESC"),
        }
        table, columns, keys, direction = specs[kind]
        scope = [kind, repo_id, q]
        where, args = [], []
        if repo_id is not None:
            where.append("repo_id = ?")
            args.append(repo_id)
        if kind == "warmed" and q:
            # No FTS here — warmed_packages is bounded by the count of
            # packages ever actually warmed/downloaded, not the whole
            # upstream index, so a plain substring scan stays cheap.
            where.append("(instr(lower(package_key), lower(?)) > 0 OR instr(lower(filename), lower(?)) > 0)")
            args.extend([q, q])
        if kind == "packages" and q:
            if self.db.search_index and len(q) >= 3 and '\x00' not in q:
                # FTS narrows candidates; the original predicate below remains
                # authoritative (SQLite lower is ASCII, FTS folds more Unicode).
                where.append('rowid IN (SELECT rowid FROM package_search WHERE package_search MATCH ?)')
                args.append('"' + q.replace('"', '""') + '"')
            # Literal substring search: '%' and '_' are not wildcards.
            where.append("(instr(lower(package_key), lower(?)) > 0 OR instr(lower(COALESCE(package_name, '')), lower(?)) > 0)")
            args.extend([q, q])
        if cursor:
            try:
                if len(cursor) > 4096:
                    raise ValueError()
                data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                values = data["after"]
                if not isinstance(values, list):
                    raise ValueError()
                if data["scope"] != scope or len(values) != len(keys):
                    raise ValueError()
                expected = int if kind == "requests" else str
                if any(type(v) is not expected for v in values):
                    raise ValueError()
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                raise ValueError("Invalid cursor for this filter") from exc
            op = ">" if direction == "ASC" else "<"
            where.append(f"({', '.join(keys)}) {op} ({', '.join('?' for _ in keys)})")
            args.extend(values)
        # Do not let SQLite scan the entire (repo_id, package_key) index to
        # preserve ordering for a rare/no-match query. Probe FTS first. For a
        # very common term keep keyset scanning: enumerating/sorting all FTS
        # matches would cost more than stopping at the first page.
        if kind == 'packages' and q and self.db.search_index and len(q) >= 3 and '\x00' not in q:
            with self.db.connect() as conn:
                matches = conn.execute('SELECT rowid FROM package_search WHERE package_search MATCH ? LIMIT 1001',
                                       ('"' + q.replace('"', '""') + '"',)).fetchall()
            if len(matches) <= 1000:
                table += ' NOT INDEXED'
            else:
                clause = 'rowid IN (SELECT rowid FROM package_search WHERE package_search MATCH ?)'
                index = where.index(clause)
                where.pop(index)
                args.pop(1 if repo_id is not None else 0)
        query = f"SELECT {columns} FROM {table}"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY " + ", ".join(f"{k} {direction}" for k in keys) + " LIMIT ?"
        with self.db.connect() as conn:
            rows = conn.execute(query, (*args, limit + 1)).fetchall()
        names = columns.split(", ")
        items = [dict(zip(names, row)) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = base64.urlsafe_b64encode(json.dumps({
                "scope": scope, "after": [items[-1][k] for k in keys]
            }).encode()).decode()
        return {"items": items, "next_cursor": next_cursor}
