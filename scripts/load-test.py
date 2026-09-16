#!/usr/bin/env python3
"""Repeatable load testing against its own temporary loopback server only.

No parameter for an external URL: this tool never sends load at production
or an upstream. Uses the project's own dependencies; the result is JSON
with latencies, errors, throughput, and memory of a separate status API
process.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import platform
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

import httpx
import yaml

from repowatch.models import RepoSnapshot
from repowatch.runtime.context import ServiceState
from repowatch.auth import hash_password
from repowatch.web.access import COOKIE_NAME

logger = logging.getLogger(__name__)


def seed(root: Path, repos: int, packages: int, events: int) -> tuple[ServiceState, dict, RepoSnapshot]:
    store = ServiceState(root / "state.sqlite3")
    config = {"state_db": str(store.database.db_path), "cache_base_url": "http://127.0.0.1:9", "repos": []}
    snapshot = None
    for r in range(repos):
        repo_id = f"r{r}"
        config["repos"].append({"id": repo_id, "type": "apt", "upstream": "https://example.invalid/debian",
                                "distribution": "test", "component": "main", "arch": "amd64", "prefetch": False})
        data = {f"pkg-{i:07}-1": f"pool/main/p/pkg-{i:07}_1_amd64.deb" for i in range(packages)}
        current = RepoSnapshot(repo_id, data, {k: k.removesuffix("-1") for k in data})
        store.repositories.record_snapshot(current)
        if r == 0:
            snapshot = current
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Bulk seed avoids timing thousands of fixture transactions as a benchmark.
    with sqlite3.connect(store.database.db_path) as conn:
        conn.executemany(
            "INSERT INTO request_events (ts,repo_id,client_ip,method,path,status,cache_status) VALUES (?,?,?,?,?,?,?)",
            ((now, f"r{i % repos}", f"192.0.2.{i % 200 + 1}", "GET", f"/debian/pool/main/p/pkg-{i % packages:07}_1_amd64.deb", "200", "HIT") for i in range(events)),
        )
        conn.execute("INSERT INTO warmed_packages SELECT repo_id,package_key,filename,?,'ok',200 FROM repo_packages WHERE rowid % 2 = 0", (now,))
    return store, config, snapshot


def process_memory(pid: int) -> dict[str, int]:
    status = Path(f"/proc/{pid}/status")
    if not status.exists():
        return {}
    return {line.split(':')[0] + ('' if line.startswith('Threads:') else '_kib'): int(line.split()[1]) for line in status.read_text().splitlines()
            if line.startswith(("VmRSS:", "VmHWM:", "Threads:"))}


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0
    return round(sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] * 1000, 2)


async def run_case(base: str, name: str, paths: list[str], concurrency: int, seconds: float, pid: int, session_cookie: str, host_token: str) -> dict[str, Any]:
    latencies: list[float] = []
    statuses: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    per_path: dict[str, list[float]] = {path: [] for path in paths}
    started = time.perf_counter()
    deadline = started + seconds
    async with httpx.AsyncClient(base_url=base, verify=False, trust_env=False, timeout=15,
                                 limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)) as client:
        async def worker(worker_id: int) -> None:
            count = worker_id
            while time.perf_counter() < deadline:
                path = paths[count % len(paths)]
                count += 1
                begin = time.perf_counter()
                try:
                    headers = ({'Authorization': 'Bearer ' + host_token} if path.startswith('/status')
                               else {'Cookie': session_cookie})
                    response = await client.get(path, headers=headers)
                    statuses[str(response.status_code)] += 1
                    response.raise_for_status()
                    if path != "/metrics":
                        payload = response.json()
                        if "limit=" in path and (not isinstance(payload, dict) or len(payload.get("items", [])) > 200):
                            raise ValueError("invalid page shape/size")
                except Exception as exc:
                    errors[type(exc).__name__ + ': ' + str(exc)[:160]] += 1
                elapsed = time.perf_counter() - begin
                latencies.append(elapsed)
                per_path[path].append(elapsed)
        await asyncio.gather(*(worker(i) for i in range(concurrency)))
    duration = time.perf_counter() - started
    result = {"name": name, "concurrency": concurrency, "duration_s": round(duration, 3),
              "requests": len(latencies), "requests_per_second": round(len(latencies) / duration, 2),
              "p50_ms": percentile(latencies, .5), "p95_ms": percentile(latencies, .95),
              "p99_ms": percentile(latencies, .99), "max_ms": percentile(latencies, 1),
              "statuses": dict(statuses), "errors": dict(errors), "server_memory": process_memory(pid),
              "paths": {path: {"requests": len(values), "p95_ms": percentile(values, .95)} for path, values in per_path.items()}}
    logger.info("%s c=%d: %.1f req/s, p95 %.1f ms, errors=%d", name, concurrency, result['requests_per_second'], result['p95_ms'], sum(errors.values()))
    return result


def writer(store: ServiceState, snapshot: RepoSnapshot, stop: threading.Event, result: dict) -> None:
    counter = 0
    while not stop.is_set():
        begin = time.perf_counter()
        try:
            for _ in range(10):
                store.requests.record_request("r0", "192.0.2.250", "GET", "/writer.deb", "200", "MISS")
            # A real snapshot replacement transaction runs alongside read traffic.
            packages = dict(snapshot.packages)
            packages[f"new-{counter}"] = f"pool/main/n/new-{counter}.deb"
            store.repositories.record_snapshot(RepoSnapshot("r0", packages, snapshot.names))
            result['commits'] += 1
        except Exception as exc:
            result['errors'].append(str(exc))
        result['latencies_s'].append(time.perf_counter() - begin)
        counter += 1
        stop.wait(.25)


async def benchmark(args: argparse.Namespace, root: Path) -> dict:
    store, config, snapshot = seed(root, args.repos, args.packages_per_repo, args.events)
    config['admin_password_hash'] = hash_password('benchmark-local-password')
    report = {"environment": {"python": platform.python_version(), "platform": platform.platform(),
                              "sqlite": sqlite3.sqlite_version, "cpu_count": os.cpu_count()},
              "dataset": {"repos": args.repos, "packages_per_repo": args.packages_per_repo,
                          "packages": args.repos * args.packages_per_repo, "request_events": args.events,
                          "database_bytes": store.database.db_path.stat().st_size}, "cases": []}
    paths = {
        "status": ["/status.json", "/healthz", "/metrics"],
        "dashboard": ["/api/repos"],
        "pages": ["/api/repos/r0/packages?limit=200", "/api/repos/r0/warmed?limit=200", "/api/requests?limit=200"],
        "search": ["/api/repos/r0/packages?limit=200&q=absent", "/api/repos/r0/packages?limit=200&q=pkg-000"],
        "summary": ["/api/requests/summary"],
    }
    for tls in (False, True):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config['status_server'] = {'bind': '127.0.0.1', 'port': port}
        if tls:
            cert, key = root/'cert.pem', root/'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=localhost'], check=True, capture_output=True)
            config['status_server'].update(tls_cert_path=str(cert), tls_key_path=str(key))
        config_path = root / 'config.yaml'
        config_path.write_text(yaml.safe_dump(config))
        base = f'{"https" if tls else "http"}://127.0.0.1:{port}'
        with (root/f'server-{tls}.log').open('w') as logs:
            proc = subprocess.Popen([sys.executable, '-m', 'repowatch.cli', '-c', str(config_path), 'serve-status'], stdout=logs, stderr=logs)
            try:
                async with httpx.AsyncClient(verify=False, trust_env=False, timeout=2) as client:
                    for _ in range(100):
                        try:
                            response = await client.get(base + '/healthz')
                            response.raise_for_status()
                            break
                        except httpx.HTTPError:
                            if proc.poll() is not None:
                                raise RuntimeError('status server exited during startup')
                            await asyncio.sleep(.05)
                    else:
                        raise RuntimeError('status server did not become ready')
                    response = await client.post(base + '/api/auth/login', json={'password': 'benchmark-local-password'})
                    response.raise_for_status()
                    csrf = response.json()['csrf_token']
                    session_cookie = f'{COOKIE_NAME}={client.cookies[COOKIE_NAME]}'
                    response = await client.post(base + '/api/tokens', json={'name': 'benchmark-host'}, headers={'X-CSRF-Token': csrf})
                    response.raise_for_status()
                    host_token = response.json()['token']
                selected = {'https-mixed' : sum(paths.values(), [])} if tls else paths
                for name, endpoints in selected.items():
                    for concurrency in args.concurrency:
                        report['cases'].append(await run_case(base, name, endpoints, concurrency, args.seconds, proc.pid, session_cookie, host_token))
                if not tls:
                    stop = threading.Event()
                    writes = {'commits': 0, 'errors': [], 'latencies_s': []}
                    thread = threading.Thread(target=writer, args=(store, snapshot, stop, writes))
                    thread.start()
                    try:
                        report['cases'].append(await run_case(base, 'mixed-with-writer', sum(paths.values(), []), max(args.concurrency), args.seconds * 2, proc.pid, session_cookie, host_token))
                    finally:
                        stop.set()
                        await asyncio.to_thread(thread.join)
                    writes['p95_ms'] = percentile(writes.pop('latencies_s'), .95)
                    report['writer'] = writes
                    # Exhaust the cursor chain and validate identity/count, not just HTTP 200.
                    seen: set[str] = set()
                    cursor = None
                    async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=15, headers={'Cookie': session_cookie}) as client:
                        while True:
                            params = {'limit': 200}
                            if cursor:
                                params['cursor'] = cursor
                            response = await client.get('/api/repos/r0/packages', params=params)
                            response.raise_for_status()
                            page = response.json()
                            keys = [row['package_key'] for row in page['items']]
                            assert not seen.intersection(keys), 'duplicate cursor results'
                            seen.update(keys)
                            cursor = page['next_cursor']
                            if not cursor:
                                break
                    assert len(seen) == len(store.repositories.get_packages('r0'))
                    report['cursor_walk'] = {'unique_packages': len(seen), 'ok': True}
            finally:
                proc.terminate()
                await asyncio.to_thread(proc.wait, 10)
    with sqlite3.connect(store.database.db_path) as conn:
        report['integrity_check'] = conn.execute('PRAGMA integrity_check').fetchone()[0]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repos', type=int, default=8)
    parser.add_argument('--packages-per-repo', type=int, default=12500)
    parser.add_argument('--events', type=int, default=100000)
    parser.add_argument('--seconds', type=float, default=3)
    parser.add_argument('--concurrency', type=int, nargs='+', default=[1, 8, 32])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if min(args.repos, args.packages_per_repo, args.events, args.seconds, *args.concurrency) <= 0:
        parser.error('sizes, concurrency and duration must be positive')
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix='repowatch-load-') as directory:
        result = asyncio.run(benchmark(args, Path(directory)))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return int(result['integrity_check'] != 'ok' or bool(result['writer']['errors']) or any(case['errors'] for case in result['cases']))


if __name__ == '__main__':
    raise SystemExit(main())
