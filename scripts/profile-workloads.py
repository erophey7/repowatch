"""Bounded profiling of real catalog reads and isolated synthetic parser/database work.

Run as the service user, in a separate nice process. No daemon instrumentation,
production state migrations, package downloads, config writes or database writes.
Synthetic snapshot writes use an automatically removed temporary database. Results
contain timing/counts/function locations only, never configuration contents.
"""
import argparse
import gzip
import tempfile
from pathlib import Path
import cProfile
import gc
import json
import pstats
import resource
import signal
import statistics
import time
import tracemalloc
from types import SimpleNamespace

from repowatch.config.load import load_config
from repowatch.reporting.status import repos_list_payload, status_payload
from repowatch import routing
from repowatch.routing import compute_cache_key
from repowatch.runtime.syslog import package_path_index, refresh_package_indexes
from repowatch.storage.cache import CacheStore
from repowatch.storage.database import Database
from repowatch.storage.queries import QueriesStore
from repowatch.storage.repositories import RepositoriesStore


SAMPLE_LIMIT = 5
PROFILE_DIR = None

def measure(name, function, samples=5, memory=False):
    """Separate uninstrumented timings, call profiling and optional allocation peak."""
    samples = min(samples, SAMPLE_LIMIT)
    wall, cpu = [], []
    for _ in range(samples):
        gc.collect()
        started, cpu_started = time.perf_counter(), time.process_time()
        result = function()
        wall.append(time.perf_counter() - started)
        cpu.append(time.process_time() - cpu_started)
        del result
    profiler = cProfile.Profile()
    profiler.runcall(function)
    if PROFILE_DIR is not None:
        profiler.dump_stats(str(PROFILE_DIR / (name + ".prof")))
    stats = pstats.Stats(profiler)
    top = []
    for (filename, line, func), (primitive, calls, own, cumulative, callers) in sorted(
            stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:12]:
        top.append(dict(function=filename.rsplit('/site-packages/', 1)[-1] + ':' + str(line) + ':' + func,
                        calls=calls, own_seconds=own, cumulative_seconds=cumulative))
    record = dict(name=name, samples=samples, wall_median=statistics.median(wall),
                  wall_min=min(wall), wall_max=max(wall), cpu_median=statistics.median(cpu),
                  profile_top=top)
    if memory:
        gc.collect()
        tracemalloc.start()
        result = function()
        record['python_allocation_peak_bytes'] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        del result
    print(json.dumps(record), flush=True)


def read_workloads(path):
    """Measure real catalog reads without initializing or migrating its database."""
    config = load_config(path)
    db = Database(config.state_db, read_only=True)
    with db.connect() as conn:
        db.search_index = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='package_search'").fetchone())
        counts = dict(conn.execute('SELECT repo_id, COUNT(*) FROM repo_packages GROUP BY repo_id'))
    store = SimpleNamespace(repositories=RepositoriesStore(db), cache=CacheStore(db), queries=QueriesStore(db))
    repo = max(config.repos, key=lambda item: counts.get(item.id, 0))
    print(json.dumps(dict(kind='environment',repositories=len(config.repos),packages=sum(counts.values()),
        largest_repo=repo.id,largest_repo_packages=counts.get(repo.id,0),fts=db.search_index,
        methodology='sequential low-priority read-only process; warm OS caches; production may change between reads')),flush=True)
    measure('load_config',lambda: load_config(path))
    measure('repository_summaries',lambda: store.repositories.get_repo_summaries(include_warmed=True))
    measure('status_payload_current_config',lambda: status_payload(path,store,current=config))
    measure('repos_list_current_config',lambda: repos_list_payload(path,store,current=config))
    measure('packages_page_100',lambda: store.queries.get_page('packages',repo.id,limit=100))
    measure('packages_search_curl',lambda: store.queries.get_page('packages',repo.id,q='curl',limit=100))
    measure('dedup_query',store.cache.find_duplicate_files,samples=3)
    measure('largest_catalog_read',lambda: store.repositories.get_packages(repo.id),samples=3,memory=True)
    packages = store.repositories.get_packages(repo.id) or {}
    measure('largest_syslog_path_index',lambda: package_path_index(repo,packages),samples=3,memory=True)
    files = list(packages.values())[:1000]
    measure('1000_cache_keys_single',lambda: [compute_cache_key(config,repo,filename) for filename in files],samples=3)
    def batch_keys():
        """Measure one prepared batch, falling back for a baseline installed version."""
        # The fallback permits the same harness to establish a pre-upgrade baseline.
        if hasattr(routing, 'CacheKeyBuilder'):
            key = routing.CacheKeyBuilder(config).for_repo(repo)
            return [key(filename) for filename in files]
        return [compute_cache_key(config, repo, filename) for filename in files]
    measure('1000_cache_keys_batch', batch_keys, samples=3)
    def cold_refresh():
        """Build all configured syslog indexes from empty process state."""
        mappings, paths, revisions = {}, {}, {}
        refresh_package_indexes(config.repos, store, mappings, paths, revisions)
        return paths
    measure('syslog_all_repos_cold_refresh', cold_refresh, samples=1)
    mappings, paths, revisions = {}, {}, {}
    refresh_package_indexes(config.repos, store, mappings, paths, revisions)
    measure('syslog_all_repos_unchanged_refresh',
            lambda: refresh_package_indexes(config.repos, store, mappings, paths, revisions))
    print(json.dumps(dict(kind='completion',process_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)),flush=True)


def synthetic_workloads(count):
    """Profile deterministic parser and snapshot work in a disposable local database."""
    from repowatch.models import RepoSnapshot
    from repowatch.parsers.apt import _parse_packages_gz
    from repowatch.parsers.gentoo import _parse_packages
    from repowatch.runtime.context import ServiceState

    apt = gzip.compress(''.join(
        f'Package: p{i}\nVersion: 1\nFilename: pool/main/p/p{i}.deb\nSHA256: {"a" * 64}\n\n'
        for i in range(count)).encode(), mtime=0)
    gentoo = (f'PACKAGES: {count}\nVERSION: 0\n\n' + ''.join(
        f'CPV: app-test/p{i}-1\nPATH: app-test/p{i}-1.tbz2\nSHA256: {"a" * 64}\n\n'
        for i in range(count))).encode()
    def parse_apt():
        """Parse a deterministic compressed APT catalog and verify cardinality."""
        result = _parse_packages_gz(apt)
        assert len(result) == count
        return result
    def parse_gentoo():
        """Parse a deterministic Gentoo catalog and verify cardinality."""
        result = _parse_packages(gentoo, 'https://example.invalid/binhost')
        assert len(result) == count
        return result
    measure('synthetic_apt_gzip_parse', parse_apt, samples=3, memory=True)
    measure('synthetic_gentoo_parse', parse_gentoo, samples=3, memory=True)
    with tempfile.TemporaryDirectory(prefix='repowatch-profile-db-') as directory:
        state = ServiceState(Path(directory) / 'state.sqlite3')
        packages = {f'p{i}-1': f'pool/main/p/p{i}.deb' for i in range(count)}
        snapshot = RepoSnapshot('profile', packages, content_hashes={key: 'a' * 64 for key in packages})
        state.repositories.record_snapshot(snapshot)
        measure('synthetic_snapshot_unchanged',lambda: state.repositories.record_snapshot(snapshot),samples=3)
        generation = 0
        def changed_snapshot():
            """Replace up to one hundred filenames per iteration in the temporary database."""
            nonlocal generation
            generation += 1
            updated = dict(packages)
            for i in range(min(100, count)):
                updated[f'p{i}-1'] = f'pool/main/p/p{i}-{generation}.deb'
            return state.repositories.record_snapshot(RepoSnapshot('profile',updated,
                content_hashes=snapshot.content_hashes))
        measure('synthetic_snapshot_100_replacements',changed_snapshot,samples=3)
    print(json.dumps(dict(kind='synthetic_complete',packages=count)),flush=True)


def main():
    """Run explicit bounded benchmarks, keeping synthetic writes outside production."""
    global SAMPLE_LIMIT, PROFILE_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', help='Optional real config for read-only catalog benchmarks')
    parser.add_argument('--samples', type=int, choices=range(1, 6), default=3)
    parser.add_argument('--synthetic-packages', type=int, default=10000)
    parser.add_argument('--profile-dir', type=Path, help='Optional directory for raw cProfile files')
    args = parser.parse_args()
    if not 1 <= args.synthetic_packages <= 100000:
        parser.error('--synthetic-packages must be between 1 and 100000')
    SAMPLE_LIMIT, PROFILE_DIR = args.samples, args.profile_dir
    if PROFILE_DIR is not None:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    signal.alarm(240)
    resource.setrlimit(resource.RLIMIT_CPU, (180, 180))
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    if args.config:
        read_workloads(args.config)
    synthetic_workloads(args.synthetic_packages)
    print(json.dumps(dict(kind='complete',process_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)),flush=True)


if __name__ == '__main__':
    main()
