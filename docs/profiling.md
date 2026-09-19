# Profiling and bounded load tests

Use measurements before choosing an optimization or native implementation.
The tools below use the application's existing Python dependencies. They do not
install a profiler into the running service or add a background profiling daemon.

## CPU, allocation and database workloads

From a source checkout matching the installed application:

```sh
sudo -u repowatch nice -n 15 /usr/local/lib/repowatch/venv/bin/python - \
  --config /etc/repowatch/config.yaml --samples 3 --synthetic-packages 10000 \
  --profile-dir /tmp/repowatch-profiles < scripts/profile-workloads.py \
  > /tmp/repowatch-profile.jsonl
```

The invoking user reads the script; the service account reads its own config and
database. Run on the cache host. To exercise only synthetic workloads locally,
use your development Python and omit `--config`.

Real catalog operations use SQLite read-only mode without schema migration.
Synthetic APT/Gentoo parsing and unchanged/replacement snapshot writes use generated
inputs and an automatically removed temporary database. There are no package
requests, production database writes or nginx reloads. The tool measures config
loading, status/list/page/search queries, dedup, catalog materialization,
syslog cold/unchanged refresh and individual/batched cache-key construction.

JSON lines contain uninstrumented wall/CPU timings, separate cProfile call
statistics and selected Python allocation peaks. Raw `.prof` files can be read
with `python -m pstats FILE`. Allocation profiling has its own overhead and is
not included in the uninstrumented timing samples. Process peak RSS includes
imports and earlier workloads; it is not a per-operation allocation delta.
The process has a 240-second wall deadline, 180-second CPU limit and 1 GiB
address-space limit. Limit exits can leave partial JSON output, so require the
final `kind: complete` record before treating a run as complete.

Use the same dataset, sample count, machine and workload parameters for comparisons.
A live catalog can change between reads; this tool does not hold a long SQLite
transaction or represent an atomic production snapshot. Synthetic writes are
not a measurement of production disk contention or client update throughput.

## Load against a running service

This is an explicit operator action, suitable for a reviewed test window. It
sends real HTTP requests to the running service and nginx on loopback only:

```sh
sudo -u repowatch /usr/local/lib/repowatch/venv/bin/python - \
  --config /etc/repowatch/config.yaml \
  --pid "$(systemctl show repowatch --property=MainPID --value)" \
  --temporary-admin-session --levels 1 4 8 --seconds 6 --rate 40 \
  < scripts/load-live.py > /tmp/repowatch-live-load.jsonl
```

`--temporary-admin-session` explicitly authorizes inserting one ten-minute admin
session directly into the existing session table; the tool removes its own row
in `finally`, including ordinary failures and interruptions. It does not prune
other sessions, change passwords or log the secret. SIGKILL/power loss cannot
run cleanup; the session still expires. Use the service account's existing
permissions rather than making the database world-readable/writable.

Stages exercise status/repository listing, dashboard pagination/search/history/
request statistics/metrics, and the Arch core metadata cache route if configured.
There is no bulk warm or purge. The metadata GET can fill/refresh that one cache
entry. No other distribution is silently configured for the test.

Requests are capped globally per stage at the lower of `--rate` and five times
the concurrency level. Defaults are 1/4/8 workers, six seconds per stage, at most
40 requests/second. Health is checked every half second; a failed request,
unhealthy result, lost service PID or latency above `--max-latency` (default two
seconds) stops subsequent stages. In-flight requests may finish before exit.
Response bodies are capped at 2 MiB. The hard CLI bounds are 16 workers,
100 requests/second and 30 seconds per stage; this is a bounded operational
check, not an attempt to find a production server's breaking point.

Results include throughput, latency percentiles, health latency, statuses,
cache HIT/MISS indicators and service memory/thread/CPU counters. Loopback removes
client-network latency; session creation bypasses the password-login cost.
These numbers cannot be presented as external client latency or maximum capacity.
For sustained saturation tests on an isolated synthetic server, use the existing
`scripts/load-test.py` instead.

## Routing and configuration optimizations

Bulk cache operations prepare validated routes once per operation and bind
repository prefixes before iterating filenames. Their lifetime ends with the
operation; new requests/configurations build new contexts. Syslog likewise
prepares invariant prefixes per catalog rebuild while preserving decoded paths
and multiple owners. Dedup selects only cross-repository duplicate groups from
SQLite; canonical selection and ordering remain deterministic.

Config reads still reopen and validate YAML every time. When the existing
PyYAML installation supplies `CSafeLoader`, it accelerates parsing; otherwise
`SafeLoader` remains the fallback. Neither permits arbitrary Python object tags.
No persistent config cache delays password revocation or repository changes.

## Request statistics and retained memory

The request charts and cache-hit metrics are read from two small SQLite tables,
`request_counters` and `request_hourly`, that triggers keep equal to the retained
`request_events` rows: each recorded request adds to them and each deleted one
(time or row-limit retention, or direct SQL) subtracts. A poll therefore reads a
bounded set of counters instead of grouping the whole history, and its cost follows
the number of distinct clients and request paths, not the number of retained
requests. There is no statistics response cache: new requests and retention
deletions are visible on the next read.

The tables are created and filled from the existing history the first time a
database is opened for normal service startup; the time this takes grows with
the amount of history. The price is paid on writes: recording a request updates
several counters, and removing old rows is several times slower than a bare
delete, so retention runs in a worker thread and in bounded transactions.
To see the cost for your own request volume, run `scripts/load-test.py` (its
`summary` scenario covers these charts). On a synthetic 300000-request history the
summary took about 13 ms instead of about 640 ms, while recording one request took
roughly twice as long.

Compare memory after repeated equivalent workloads, not just before and after
one burst. Native SQLite/allocator working memory can remain in RSS after Python
objects are released. Measure RSS and Python allocations separately, include an
idle observation, and record thread concurrency. A lower allocation peak or a
single stable interval does not by itself prove the absence of a leak.

The syslog listener prepares repository prefixes at startup and rebuilds them
when it reloads configuration. Catalog indexing skips URL parsing for ordinary
absolute paths; encoded paths, query/fragment suffixes, authorities and stripped
control characters retain the standard URL-parser behavior.
