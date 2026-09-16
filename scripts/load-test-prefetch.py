#!/usr/bin/env python3
"""Load-tests warm_cache against a temporary local HTTP server instead of nginx.

Exercises concurrency, the byte-rate pacer, partial failures, and recovery.
Does not measure nginx proxy_cache HIT/MISS and never talks to real upstreams.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from repowatch.web.server import StatusHTTPServer
from repowatch.config.models import Config
from repowatch.config.models import RepoConfig
from repowatch.config.models import StatusServerConfig
from repowatch.operations.warm import warm_cache
from repowatch.runtime.context import ServiceState


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    body = b'x' * (256 * 1024)
    failures = set()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *values) -> None:
            pass

        def do_GET(self) -> None:
            status = 503 if self.path.rsplit('/', 1)[-1] in failures else 200
            payload = b'unavailable' if status == 503 else body
            self.send_response(status)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    # Only this benchmark process changes proxy bypass; warm_cache itself
    # still uses its real AsyncClient and reads real HTTP response bodies.
    os.environ['NO_PROXY'] = '127.0.0.1'
    os.environ['no_proxy'] = '127.0.0.1'
    results = []
    with tempfile.TemporaryDirectory(prefix='repowatch-prefetch-load-') as directory:
        with StatusHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                repo = RepoConfig(id='rpm-load', type='dnf', upstream='https://example.invalid/repo', arch='x86_64')
                store = ServiceState(Path(directory) / 'state')
                config = Config(state_db=store.database.db_path, check_interval=300,
                                cache_base_url=f'http://127.0.0.1:{server.server_port}',
                                status_server=StatusServerConfig(), repos=[repo])
                for name, concurrency, count, bandwidth, bad in (
                    ('serial', 1, 256, None, False),
                    ('parallel-8', 8, 256, None, False),
                    ('parallel-32', 32, 256, None, False),
                    ('partial-failure', 32, 256, None, True),
                    ('recovery', 32, 256, None, False),
                    ('bandwidth-2MiB', 16, 64, 2 * 1024 * 1024, False),
                ):
                    failures.clear()
                    if bad:
                        failures.add('p0.rpm')
                    packages = {f'p{i}-1': f'Packages/p{i}.rpm' for i in range(count)}
                    current = replace(config, prefetch_concurrency=concurrency, prefetch_bandwidth_limit=bandwidth)
                    started = time.perf_counter()
                    asyncio.run(warm_cache(current, repo, store, packages))
                    elapsed = time.perf_counter() - started
                    rows = {row['package_key']: row for row in store.cache.get_warmed_packages(repo.id)}
                    failed = sum(rows[key]['status'] == 'failed' for key in packages)
                    expected_failed = 1 if bad else 0
                    assert failed == expected_failed, (name, failed)
                    with store.database.connect() as conn:
                        pending_failure = conn.execute('SELECT COUNT(*) FROM failure_state').fetchone()[0]
                    assert pending_failure == int(bad), (name, pending_failure)
                    transferred = (count - failed) * len(body)
                    if bandwidth:
                        # Existing byte pacing permits at most one 64KiB chunk
                        # ahead of schedule; allow small scheduler/clock tolerance.
                        assert elapsed >= (transferred - 65536) / bandwidth - .05
                    results.append({'name': name, 'concurrency': concurrency, 'packages': count,
                                    'expected_failures': expected_failed, 'actual_failures': failed,
                                    'bytes': transferred, 'elapsed_s': round(elapsed, 3),
                                    'MiB_per_second': round(transferred / elapsed / 1024**2, 2)})
            finally:
                server.shutdown()
                thread.join(timeout=5)
    args.output.write_text(json.dumps(results, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
