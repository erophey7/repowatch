#!/usr/bin/env python3
"""Explicit, capped loopback load against an already running repowatch service.

Read HTTP workloads only. --temporary-admin-session authorizes one short-lived
session row, removed on exit; it never changes passwords or existing sessions.
No configuration edits, package purge or bulk warming. Run on the target host
as the service user. Output contains aggregate metrics, never session secrets.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
import ipaddress
import json
import math
from pathlib import Path
import secrets
import signal
import sqlite3
import time
from urllib.parse import quote, urlsplit

import httpx

from repowatch.config.load import load_config
from repowatch.routing import repo_prefix
from repowatch.storage.access import digest
from repowatch.web.access import COOKIE_NAME


def loopback_url(value):
    """Reject remote targets, userinfo and path/query ambiguity before sending load."""
    parsed = urlsplit(value)
    try:
        valid = ipaddress.ip_address(parsed.hostname).is_loopback
        parsed.port
    except (ValueError, TypeError):
        valid = False
    if (not valid or parsed.scheme not in ('http', 'https') or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise argparse.ArgumentTypeError('use an HTTP(S) loopback IP origin without credentials or a path')
    return value.rstrip('/')


@contextmanager
def temporary_session(config):
    """Insert only this diagnostic session, without schema migration or session pruning."""
    if not config.admin_password_hash:
        raise ValueError('set the administrator password before authenticated load testing')
    secret = secrets.token_urlsafe(32)
    uri = config.state_db.resolve().as_uri() + '?mode=rw'
    with sqlite3.connect(uri, uri=True) as conn:
        conn.execute('INSERT INTO admin_sessions VALUES (?, ?, ?, ?)',
                     (digest(secret), digest(config.admin_password_hash), time.time() + 600, 0))
    try:
        yield COOKIE_NAME + '=' + secret
    finally:
        with sqlite3.connect(uri, uri=True) as conn:
            conn.execute('DELETE FROM admin_sessions WHERE secret_hash=?', (digest(secret),))


def process_sample(pid):
    """Read service memory and CPU counters; a missing PID stops the load run."""
    lines = Path(f'/proc/{pid}/status').read_text().splitlines()
    fields = {line.split(':')[0]: int(line.split()[1]) for line in lines
              if line.startswith(('VmRSS:', 'Threads:'))}
    stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    fields['cpu_ticks'] = int(stat[11]) + int(stat[12])
    return fields


def percentile(values, fraction):
    """Nearest-rank latency percentile, milliseconds."""
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] * 1000 if values else 0


async def run_case(client, health_client, origin, paths, cookie, name, concurrency,
                   seconds, rate, pid, max_latency):
    """Apply global rate/concurrency caps and stop a stage on its first failed request."""
    deadline = time.perf_counter() + seconds
    started = time.perf_counter()
    lock, stop = asyncio.Lock(), asyncio.Event()
    next_slot = started
    timings, health_times, process = [], [], []
    statuses, cache_statuses, errors = Counter(), Counter(), Counter()
    total_bytes = 0

    async def worker(index):
        """Reserve rate-limited slots, consume bounded responses and publish failures."""
        nonlocal next_slot, total_bytes
        sequence = index
        while not stop.is_set():
            async with lock:
                slot = max(time.perf_counter(), next_slot)
                next_slot = slot + 1 / rate
            if slot >= deadline:
                return
            await asyncio.sleep(max(0, slot - time.perf_counter()))
            if stop.is_set():
                return
            path = paths[sequence % len(paths)]
            sequence += 1
            begin = time.perf_counter()
            try:
                headers = {'Cookie': cookie} if name != 'cache' else {}
                async with client.stream('GET', origin + path, headers=headers) as response:
                    statuses[str(response.status_code)] += 1
                    response.raise_for_status()
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 2 * 1024 * 1024:
                            raise ValueError('response exceeds 2 MiB workload cap')
                    total_bytes += size
                    if name == 'cache':
                        cache_statuses[response.headers.get('X-Cache-Status', 'unknown')] += 1
                elapsed = time.perf_counter() - begin
                timings.append(elapsed)
                if elapsed > max_latency:
                    raise TimeoutError('latency ceiling exceeded')
            except Exception as exc:
                errors[type(exc).__name__] += 1
                stop.set()

    async def monitor():
        """Observe health and service counters independently of workload traffic."""
        while not stop.is_set() and time.perf_counter() < deadline:
            begin = time.perf_counter()
            try:
                response = await health_client.get('/healthz')
                response.raise_for_status()
                if response.json() != {'healthy': True}:
                    raise ValueError('unhealthy service')
                health_times.append(time.perf_counter() - begin)
                if health_times[-1] > max_latency:
                    raise TimeoutError('health latency ceiling exceeded')
                process.append(process_sample(pid))
            except Exception as exc:
                errors['health_' + type(exc).__name__] += 1
                stop.set()
            await asyncio.sleep(0.5)

    await asyncio.gather(monitor(), *(worker(i) for i in range(concurrency)))
    elapsed = time.perf_counter() - started
    result = dict(workload=name,concurrency=concurrency,rate_cap=rate,seconds=elapsed,
        completed=len(timings),requests_per_second=len(timings)/elapsed,
        p50_ms=percentile(timings,.5),p95_ms=percentile(timings,.95),p99_ms=percentile(timings,.99),
        health_p95_ms=percentile(health_times,.95),statuses=dict(statuses),errors=dict(errors),
        cache_statuses=dict(cache_statuses),bytes=total_bytes,
        peak_rss_kib=max((p.get('VmRSS',0) for p in process),default=0),
        peak_threads=max((p.get('Threads',0) for p in process),default=0),
        service_cpu_ticks=process[-1]['cpu_ticks']-process[0]['cpu_ticks'] if len(process)>1 else 0)
    print(json.dumps(result),flush=True)
    if errors:
        raise RuntimeError('load stopped after a failed request or health check')


async def run(args, config, cookie):
    """Exercise status, paginated/search dashboard reads and a small metadata cache route."""
    if not config.repos:
        raise ValueError('configure repositories before load testing')
    repo_id = quote(config.repos[0].id, safe='')
    cases = [('status',args.status_url,['/status.json','/api/repos']),
             ('dashboard',args.status_url,[f'/api/repos/{repo_id}/packages?limit=100',
                 f'/api/repos/{repo_id}/packages?q=curl&limit=100',
                 f'/status/{repo_id}/history?limit=10','/api/requests?limit=100',
                 '/api/requests/summary','/metrics'])]
    cache_repo = next((repo for repo in config.repos if repo.type == 'pacman' and repo.repo_name == 'core'),None)
    if cache_repo is not None:
        cases.append(('cache',args.cache_url,[repo_prefix(cache_repo)+'/'+cache_repo.repo_name+'.db']))
    timeout = httpx.Timeout(args.max_latency)
    async with httpx.AsyncClient(timeout=timeout,trust_env=False) as client, httpx.AsyncClient(
            base_url=args.status_url,timeout=timeout,trust_env=False) as health:
        for name, origin, paths in cases:
            for level in args.levels:
                await run_case(client,health,origin,paths,cookie,name,level,args.seconds,
                               min(args.rate,level*5),args.pid,args.max_latency)
                await asyncio.sleep(1)


def main():
    """Require explicit opt-in before creating the temporary diagnostic session."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='/etc/repowatch/config.yaml')
    parser.add_argument('--status-url',type=loopback_url,default='http://127.0.0.1:8085')
    parser.add_argument('--cache-url',type=loopback_url,default='http://127.0.0.1:8080')
    parser.add_argument('--temporary-admin-session',action='store_true',required=True)
    parser.add_argument('--pid',type=int,required=True)
    parser.add_argument('--levels',type=int,nargs='+',default=[1,4,8])
    parser.add_argument('--seconds',type=float,default=6)
    parser.add_argument('--rate',type=float,default=40)
    parser.add_argument('--max-latency',type=float,default=2)
    args = parser.parse_args()
    if (not all(1 <= n <= 16 for n in args.levels) or not 1 <= args.seconds <= 30
            or not 1 <= args.rate <= 100 or not .1 <= args.max_latency <= 10):
        parser.error('limits: concurrency 1..16, seconds 1..30, rate 1..100, latency .1..10')
    def interrupted(signum, frame):
        """Unwind the session context when the operator terminates the test."""
        raise KeyboardInterrupt('load interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    config = load_config(args.config)
    process_sample(args.pid)
    with temporary_session(config) as cookie:
        asyncio.run(run(args,config,cookie))


if __name__ == '__main__':
    main()
