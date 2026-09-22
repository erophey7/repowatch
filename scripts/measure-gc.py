#!/usr/bin/env python3
"""Measure garbage-collector pauses on a heap shaped like the daemon's.

The service keeps every catalog in memory as syslog lookup tables and, while an
index is checked, builds tens of thousands of short-lived package objects. A full
(generation 2) collection over such a heap stops every thread of the process for
its whole duration. This tool builds a comparable heap, repeats index-parsing
work, and reports each collection's pause by generation, with and without
gc.freeze() applied to the long-lived tables.

Nothing is installed into the running service. With --config the catalogs are read
from the existing database in read-only mode (no migration, no writes); without it
a synthetic heap is used. Run it in a separate process, on the same Python
version as the service: the collector differs between interpreter releases.
"""
import argparse
import gc
import gzip
import json
import resource
import signal
import statistics
import sys
import time
from types import SimpleNamespace

from repowatch.config.models import RepoConfig
from repowatch.parsers.apt import _parse_packages_gz
from repowatch.runtime.syslog import package_path_index


class Pauses:
    """gc callback that records how long each collection took."""

    def __init__(self) -> None:
        """Start with no samples."""
        self.samples: list[tuple[int, float]] = []
        self._started = 0.0

    def __call__(self, phase: str, info: dict) -> None:
        """Record the duration between a collection's start and stop callbacks."""
        if phase == "start":
            self._started = time.perf_counter()
        else:
            self.samples.append((info["generation"], (time.perf_counter() - self._started) * 1000))

    def summary(self) -> dict:
        """Per generation: collection count and total/median/p99/maximum pause in ms."""
        result = {}
        for generation in (0, 1, 2):
            values = sorted(ms for gen, ms in self.samples if gen == generation)
            if values:
                result[f"gen{generation}"] = dict(
                    collections=len(values), total_ms=round(sum(values), 2),
                    median_ms=round(statistics.median(values), 2),
                    p99_ms=round(values[min(len(values) - 1, int(len(values) * 0.99))], 2),
                    max_ms=round(values[-1], 2))
        return result


def synthetic_heap(packages: int) -> tuple[dict, dict]:
    """Package and route tables for one large catalog, built by the service's own code."""
    repo = RepoConfig(id="synthetic", type="apt", upstream="https://example.invalid/debian",
                      arch="amd64", distribution="bookworm", component="main")
    catalog = {f"p{i}-1": f"pool/main/p/p{i}.deb" for i in range(packages)}
    return {repo.id: catalog}, {repo.id: package_path_index(repo, catalog)}


def real_heap(config_path: str) -> tuple[dict, dict]:
    """The tables the syslog listener holds, read from the existing database (read-only)."""
    from repowatch.config.load import load_config
    from repowatch.runtime.syslog import refresh_package_indexes
    from repowatch.storage.database import Database
    from repowatch.storage.repositories import RepositoriesStore

    config = load_config(config_path)
    store = SimpleNamespace(repositories=RepositoriesStore(Database(config.state_db, read_only=True)))
    packages_by_repo: dict = {}
    by_path: dict = {}
    refresh_package_indexes(config.repos, store, packages_by_repo, by_path, {})
    return packages_by_repo, by_path


def transient_source(count: int) -> bytes:
    """A compressed APT index whose parse creates `count` short-lived package objects."""
    text = "".join(f"Package: t{i}\nVersion: 1\nFilename: pool/main/t/t{i}.deb\nSHA256: {'a' * 64}\n\n"
                   for i in range(count))
    return gzip.compress(text.encode(), mtime=0)


def run_variant(name: str, source: bytes, rounds: int) -> dict:
    """Parse the index `rounds` times under the pause recorder and summarize the pauses."""
    pauses = Pauses()
    gc.callbacks.append(pauses)
    try:
        started = time.perf_counter()
        for _ in range(rounds):
            packages = _parse_packages_gz(source)
            snapshot = {package.key: package.filename for package in packages}  # what a snapshot keeps
            del packages, snapshot
        elapsed = time.perf_counter() - started
    finally:
        gc.callbacks.remove(pauses)
    return dict(kind="gc_pauses", variant=name, rounds=rounds, seconds=round(elapsed, 2), **pauses.summary())


def forced_full_collections(repeat: int = 5) -> list[float]:
    """Duration in ms of `repeat` explicit full collections on the current heap."""
    times = []
    for _ in range(repeat):
        started = time.perf_counter()
        gc.collect()
        times.append(round((time.perf_counter() - started) * 1000, 2))
    return times


def main() -> None:
    """Build the heap, then report pauses as JSON lines with and without gc.freeze()."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="real config: read the catalogs from its database, read-only")
    parser.add_argument("--synthetic-heap", type=int, default=200000,
                        help="packages in the synthetic long-lived tables (without --config)")
    parser.add_argument("--parse-packages", type=int, default=64000,
                        help="packages per simulated index parse")
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.synthetic_heap <= 1000000 or not 1 <= args.parse_packages <= 100000 \
            or not 1 <= args.rounds <= 20:
        parser.error("--synthetic-heap 1..1000000, --parse-packages 1..100000, --rounds 1..20")
    signal.alarm(240)
    resource.setrlimit(resource.RLIMIT_CPU, (180, 180))
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))

    packages_by_repo, by_path = real_heap(args.config) if args.config else synthetic_heap(args.synthetic_heap)
    source = transient_source(args.parse_packages)
    gc.collect()
    print(json.dumps(dict(
        kind="environment", python=sys.version.split()[0], thresholds=gc.get_threshold(),
        heap_source="real catalogs" if args.config else "synthetic",
        catalog_packages=sum(len(catalog) for catalog in packages_by_repo.values()),
        tracked_objects=len(gc.get_objects()))), flush=True)
    print(json.dumps(dict(kind="forced_full_collection_ms", frozen=False, runs=forced_full_collections())), flush=True)
    print(json.dumps(run_variant("as_is", source, args.rounds)), flush=True)
    gc.freeze()  # long-lived tables leave the collector's working set
    print(json.dumps(dict(kind="forced_full_collection_ms", frozen=True, runs=forced_full_collections())), flush=True)
    print(json.dumps(run_variant("frozen_heap", source, args.rounds)), flush=True)
    gc.unfreeze()
    print(json.dumps(dict(kind="complete", process_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)),
          flush=True)
    del packages_by_repo, by_path


if __name__ == "__main__":
    main()
