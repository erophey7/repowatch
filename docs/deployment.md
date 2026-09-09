# Production install and deployment

This document covers taking repowatch from source to a running system
service: prerequisites, the `make install`/`make activate` flow, the two
ways to run it (a single daemon vs. split status API + timer), the nginx
cache in front of it, backups, and upgrades. For `config.yaml` itself, see
[configuration.md](configuration.md); for authentication and access control,
see [access.md](access.md).

- [Prerequisites](#prerequisites)
- [Install](#install)
- [What `make activate` does](#what-make-activate-does)
- [Two ways to run it](#two-ways-to-run-it)
- [nginx cache and auto-reconciliation](#nginx-cache-and-auto-reconciliation)
- [Backups](#backups)
- [Upgrading](#upgrading)
- [Running without systemd](#running-without-systemd)
- [Packaging (`DESTDIR`)](#packaging-destdir)
- [Hardening notes](#hardening-notes)

## Prerequisites

- Python 3.11+ with `venv` and `pip` available (some distros split `pip` out
  of the base Python package — `make check` will tell you if it's missing).
- `nginx`, unless you're running the caching layer yourself and setting
  `WITH_NGINX=0` (see below).
- `systemd`, unless you're supervising the process yourself with
  `WITH_SYSTEMD=0`.
- `gpgv`, if any repository has `verify_signature: true` (apt, pacman,
  RPM-MD/dnf, apt-rpm).
- `openssl`, or `apk-tools >= 3.0`, if any apk repository has
  `verify_signature: true` — see `apk_signature_backend` in
  [configuration.md](configuration.md).
- Enough local disk for `state_db` (SQLite — must be local disk, not NFS;
  see [configuration.md](configuration.md)) and for the nginx package cache
  itself (NFS is fine for the cache directory, just not for `state_db`).

Run `make check` at any point — it's a read-only diagnostic (`OK`/`WARN`/
`FAIL` per item: Python version, required stdlib modules, `gpgv`/`openssl`/
`nginx`/`systemctl` availability, filesystem permissions, an existing
installation's consistency, nginx `include` conflicts) and never touches
disk or the network beyond reading local files.

## Install

```bash
make check                                    # diagnostics only, safe to run any time
make install PREFIX=/usr/local                # builds wheels, lays down files — no services touched
sudo make activate                            # creates the system user, enables and starts services
```

`make install` is intentionally inert with respect to any *running* system:
it builds/installs into a venv under `PREFIX`, writes the CLI wrapper,
copies systemd unit templates and scripts, and writes an install manifest
(`$PREFIX/share/repowatch/install.json`) — but it does not create the
`repowatch` system user, does not touch `systemctl`, and does not write or
link any nginx config. `make activate` is the separate, explicit step that
does all of that; it requires root and refuses to run under `DESTDIR`.

Paths are independently configurable (all via `make` variables, forwarded
as environment variables to the installer):

| Variable | Default | Meaning |
|---|---|---|
| `PREFIX` | `/usr/local` | Application venv, CLI wrapper, install manifest. |
| `SYSCONFDIR` | `/etc` | `config.yaml` lives at `$SYSCONFDIR/repowatch/config.yaml`. |
| `LOCALSTATEDIR` | `/var` | `state_db`/backups at `$LOCALSTATEDIR/lib/repowatch`. |
| `CACHE_DIR` | `$LOCALSTATEDIR/cache/nginx/repowatch` | nginx's `proxy_cache_path`, when `WITH_NGINX=1`. |
| `SYSTEMD_UNIT_DIR` | `/etc/systemd/system` | Where unit files are linked. |
| `NGINX_CONF` / `NGINX_ENABLED_DIR` | auto-detected | Explicit override if `make check` can't unambiguously find your nginx's main config / `sites-enabled`-style include directory. |
| `WITH_NGINX` | `1` | Set to `0` to skip nginx integration entirely — bring your own caching layer/config. |
| `WITH_SYSTEMD` | `1` | Set to `0` to skip systemd unit installation/activation — `repowatch run` doesn't depend on systemd at all, run it under any supervisor. |
| `DESTDIR` | (empty) | Staging root for packaging — see [Packaging](#packaging-destdir). |

Re-running `make install` against an already-installed layout is safe (it
detects and preserves an existing `config.yaml` rather than overwriting it),
but changing `PREFIX`/`SYSCONFDIR`/`LOCALSTATEDIR` after the fact is refused
by `make check` — that's a data-migration decision the installer won't make
silently on your behalf.

## What `make activate` does

1. Creates the `repowatch` system user/group if they don't already exist
   (`--system`, no login shell, no home directory).
2. `chown`s `config.yaml`'s directory, `config.yaml` itself, and the state
   directory (including `backups/`) to that user — deliberately **not**
   recursive over an existing package cache (which may be large, and may
   sit on NFS).
3. If `WITH_NGINX=1`: renders the cache `server` block from your
   `config.yaml` (see `nginx.py` / [configuration.md](configuration.md)),
   validates it with `nginx -t`, and symlinks it into your nginx's enabled-
   sites directory — rolling back the symlink/file if validation fails.
   Also writes a small root-owned `policy.json` (cache dir, access log
   path, nginx binary, site-link path) that later reconciliation runs
   against — see the next section.
4. If `WITH_SYSTEMD=1`: links the unit files from
   `$PREFIX/share/repowatch/systemd/` into `$SYSTEMD_UNIT_DIR`, reloads
   systemd, and enables+starts `repowatch.service` and
   `repowatch-backup.timer` (and, if nginx is also managed,
   `repowatch-nginx.timer`).

`make activate` re-runs `check-config` against the installed venv before
touching anything, and refuses to proceed if `make check` reports any
failure — it will not activate a broken installation.

## Two ways to run it

**`repowatch.service`** (the default from `make activate`) runs
`repowatch run` — a single process that does everything: the scheduled
index checks, cache warming, the syslog listener (if `syslog_listener.
enabled`), and the `status.json`/dashboard HTTP server, all together.

**`repowatch-status.service` + `repowatch-check.timer`** is the split
alternative: a long-running `serve-status` process for the HTTP side, paired
with a timer that runs `check-once` periodically instead of the built-in
scheduler. The real tradeoff here is that this combination has **no syslog
listener** — that only runs inside `repowatch run` — so `warmed_packages`
won't reflect real client downloads and `/api/requests` stays empty, not
"no dashboard" as the naming might suggest (the dashboard is served by
`repowatch-status.service` either way). Reach for this split if you want
independent restart/scaling of "answer status queries" vs. "go check
upstreams", or if you'd rather drive checks from your own scheduler instead
of `repowatch run`'s internal one.

Only one of the two shapes should be enabled against the same `state_db` at
a time — `make activate` enables the first one; switching to the second is a
manual `systemctl disable --now repowatch.service` followed by enabling the
other two units.

`repowatch.service` ships with baseline systemd hardening
(`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`),
with `ReadWritePaths` scoped to exactly `state_db`'s directory and
`config.yaml`'s directory — the latter is required because the dashboard's
`POST /api/repos` writes `config.yaml` directly at runtime.

Neither shape needs systemd at all, actually — see
[Running without systemd](#running-without-systemd) for `repowatch
supervise`, a third option for environments without it.

## nginx cache and auto-reconciliation

The nginx `server` block itself is rendered once at `make activate` time
(see above), from your `config.yaml`. But `config.yaml` can keep changing
afterward — new repositories, changed `url_template`s, edited nginx
settings — through the dashboard, without a redeploy. `repowatch-nginx.timer`
(every 15s) re-renders the config from the current `config.yaml` and, only
if the result actually differs from what's active, validates it with
`nginx -t` and reloads. No-op ticks (the overwhelming majority) do nothing
observable — no reload, no log noise.

This reconciliation runs as a **separate root-owned oneshot systemd unit**
(`repowatch-nginx.service`, invoked by the timer), not as a `sudo` call from
the main `repowatch.service` process — the main process keeps running as the
unprivileged `repowatch` user with no elevated privileges of its own. If a
render or `nginx -t` fails, the previously-active config is left in place
and the failure surfaces in that unit's systemd logs
(`journalctl -u repowatch-nginx.service`); the daemon itself isn't affected.

If you're not using the generator (`WITH_NGINX=0`, or `nginx.enabled: false`
in `config.yaml`), none of this applies — write and manage your own nginx
config. `repowatch nginx-render -c config.yaml` prints a concrete example for
your own `repos[]` (the caching approach: `proxy_cache`, short TTL on mutable
index files, long TTL on immutable package files, IPv4-only upstream
resolution — see [configuration.md](configuration.md#nginx) for why).

## Backups

`repowatch-backup.timer` runs daily (with up to 30 minutes of randomized
delay, and `Persistent=true` to catch up after downtime) and backs up
`state_db` — the SQLite file holding repository snapshots, change history,
and warmed-package state — using SQLite's own online backup API
(`sqlite3 <db> ".backup '<dest>'"`), not a plain file copy. That matters
because a plain `cp`/`rsync` over a file the daemon is actively writing to
(WAL mode) risks capturing it mid-transaction; the backup API produces a
consistent snapshot without blocking the running service. Backups are
gzip-compressed and rotated by age (`RETENTION_DAYS`, default 14 —
noticeably longer than `event_retention_days`, since this is a full-state
snapshot for disaster recovery, not a per-repository event log).

`config.yaml` itself is not backed up by this timer — back it up the way you
back up any other config file under version control or your usual config-
management tooling. (The upgrade procedure below separately snapshots it as
part of every upgrade attempt, but that's incidental to the upgrade, not a
substitute for your own config backups.)

**Restoring** a backup:

```bash
sudo systemctl stop repowatch.service          # or repowatch-status.service + repowatch-check.timer
sudo -u repowatch gunzip -k /var/lib/repowatch/backups/state-<timestamp>.sqlite3.gz
sqlite3 /var/lib/repowatch/backups/state-<timestamp>.sqlite3 'PRAGMA integrity_check;'
sudo -u repowatch cp /var/lib/repowatch/backups/state-<timestamp>.sqlite3 /var/lib/repowatch/state.sqlite3
sudo systemctl start repowatch.service
```

Restoring only rolls back `state_db` (history, warmed-package tracking,
issued tokens, admin sessions) — it does not touch `config.yaml`, so make
sure the config you're running matches what you expect separately.

## Upgrading

```bash
make upgrade-plan MANIFEST=/usr/local/share/repowatch/install.json WHEELHOUSE=build/wheels
sudo make upgrade  MANIFEST=/usr/local/share/repowatch/install.json WHEELHOUSE=build/wheels
```

This is an **offline** upgrade path — it expects a pre-built wheelhouse with
target-compatible dependencies already present (`make wheel` on a matching
environment, or copied over ahead of time); it never reaches out to the
network itself. `upgrade-plan` runs the same validation `upgrade --apply`
would, without stopping anything or writing anything, so you can check it
will actually work before scheduling downtime.

`make upgrade` (root required):

1. Refuses to run unless the current installation is active and healthy
   (services up, `make check` passing) and unless `state_db` lives outside
   the installation/config directories being replaced.
2. Builds a *candidate* venv in a temp directory and validates it against
   the current `config.yaml` *before* touching the running service — an
   incompatible candidate is caught with zero downtime.
3. Only then: stops services, snapshots the installed files and does an
   online backup of `state_db` into a timestamped directory under
   `state_db`'s `backups/`, installs the new version, re-activates
   (including nginx re-render/reload if applicable), and runs a smoke check.
4. **On any failure at any point**, it restores the snapshotted files and
   database from that same backup directory and resumes the services in
   their prior enabled/active state — you aren't left with a half-upgraded
   installation.

It refuses to run against a `DESTDIR`-staged layout or with `WITH_SYSTEMD=0`
— this specific automated path assumes a systemd-managed installation is
what's being upgraded in place.

### Checking GitHub for a new release (`repowatch self-update`)

```bash
repowatch self-update --repo owner/name --check   # just report, don't install
repowatch self-update --repo owner/name            # install if newer
```

A separate, lighter mechanism from `make upgrade` above: it checks the given
GitHub repository's latest (non-draft, non-prerelease) release, downloads its
wheel and the `<wheel>.sha256` checksum file published alongside it in the
same release, verifies the checksum, and `pip install`s the wheel into
whichever environment `repowatch` itself is currently running from. It never
touches `config.yaml`, `state_db`, or nginx, and it doesn't require the
source repository or a wheelhouse to be present — just network access to
GitHub.

A release without exactly one `.whl` asset, without a matching `.sha256`
asset, or with a checksum that doesn't match is refused outright — there is
no "install anyway" fallback. This protects against a corrupted download or
a network-level tamper, but **not** against a compromised GitHub account:
the checksum file travels in the same release as the wheel it describes, so
whoever could replace one could replace both. If that stronger guarantee
matters for your deployment, use the offline `make upgrade` path above (with
a wheelhouse you built and reviewed yourself) instead.

`self-update` only replaces the installed package — it does not restart
`repowatch` for you. Restart it afterwards the same way you normally would
(`systemctl restart repowatch`, or restart your `supervise` process).

## Running without systemd

`repowatch run` itself has no systemd dependency at all — it's a single
process with its own `asyncio` scheduler (`watcher.run_forever`), same as
`make install WITH_SYSTEMD=0` already assumes. What's missing without
systemd is a replacement for the *other* two units: `repowatch-nginx.timer`
(periodic nginx reconciliation) and `repowatch-backup.timer` (daily backup).
`repowatch supervise` is a single foreground command that replaces all
three, for any environment with no systemd (and, in the general case
covered here, not even cron — a minimal or container environment being the
main target):

```bash
repowatch -c /etc/repowatch/config.yaml supervise \
  --pid-file /var/run/repowatch/repowatch.pid \
  --backup-dir /var/lib/repowatch/backups --backup-interval-hours 24 \
  --nginx --nginx-policy /etc/nginx/repowatch/policy.json
```

This runs exactly what `repowatch run` runs (status API, optional syslog
listener, the check scheduler) plus, if the corresponding flags are given,
a backup loop and an nginx-reconciliation loop in the same process — see
`repowatch supervise --help` for the full flag list (intervals, retention).
Nothing is auto-detected: leave out `--backup-dir`/`--nginx` to skip those
parts entirely, same as not enabling the corresponding systemd timer.

**The `--nginx` root requirement, and what you give up without systemd.**
Under systemd, nginx reconciliation deliberately runs as a *separate*
root-owned oneshot unit (`repowatch-nginx.service`), so the main daemon
never needs elevated privileges (see
[nginx cache and auto-reconciliation](#nginx-cache-and-auto-reconciliation)
and [Hardening notes](#hardening-notes) above) — that split exists because
systemd can supervise two independently-privileged units against one
`config.yaml`. Without systemd there's no equivalent OS-level mechanism, so
`supervise --nginx` requires the **whole process** to run as root (it
refuses to start otherwise, same check `nginx-apply` already makes) — there
is no way to keep the rest of `supervise` unprivileged while it also
reconciles nginx in-process.

If that tradeoff isn't acceptable, don't pass `--nginx`: run `supervise`
unprivileged for the main daemon (and, if you want, `--backup-dir` too —
backups only need write access to `state_db`'s directory, not root), and
apply nginx changes through whatever periodic mechanism you *do* have
available — `repowatch nginx-apply --policy ...` as root from cron if
you have it, or your own small root-owned loop/timer if you don't. The
pieces are independent; `--nginx` is only a convenience for the case where
running as root isn't a concern (a common posture for a single-purpose
container, for instance).

**PID file and manual start/stop**, for environments with no supervisor to
hand the command to at all:

```bash
repowatch -c /etc/repowatch/config.yaml supervise --pid-file /var/run/repowatch/repowatch.pid &
# ...later:
kill "$(cat /var/run/repowatch/repowatch.pid)"
```

`supervise` writes its PID to `--pid-file` on start and removes it on a
clean shutdown (`SIGTERM`/`SIGINT`); it doesn't restart itself if it
crashes — if you have *any* process supervisor available (even a minimal
one), point it at `repowatch supervise` as the command to run, and let it
handle restart-on-crash. Two short examples:

<details>
<summary>OpenRC</summary>

```sh
#!/sbin/openrc-run
command="/usr/local/bin/repowatch"
command_args="-c /etc/repowatch/config.yaml supervise --pid-file /var/run/repowatch/repowatch.pid"
command_background=true
pidfile="/var/run/repowatch/repowatch.pid"
```
</details>

<details>
<summary>runit</summary>

```sh
#!/bin/sh
# /etc/sv/repowatch/run
exec /usr/local/bin/repowatch -c /etc/repowatch/config.yaml supervise
```

(runit itself tracks the process's own PID via the pipe it holds open, so
`--pid-file` is optional here — pass it only if something else also wants
to read it, e.g. a health check script.)
</details>

**Standalone backups without `supervise`.** `repowatch backup <dir>
[--retention-days N]` is also a first-class, one-shot command on its own —
useful if you have cron but not systemd: `0 3 * * * repowatch -c
/etc/repowatch/config.yaml backup /var/lib/repowatch/backups`. It performs
the same pure-Python online SQLite backup (no dependency on the `sqlite3`
CLI binary or `scripts/backup-state.sh`, which requires the `make install`
layout) that `supervise --backup-dir` uses internally.

## Packaging (`DESTDIR`)

`make install DESTDIR=/path/to/stage PREFIX=/usr/local ...` lays out files
under the staging root for downstream packaging (e.g. building a distro
package), without writing anything outside it and without any of the
`activate` steps (which are refused entirely under `DESTDIR`). Runtime paths
baked into the installed files (the CLI wrapper, systemd units, nginx
policy) are always the *target* paths (`PREFIX`, etc.) — `DESTDIR` never
leaks into them, only into where `install` itself writes on the build host.

## Hardening notes

- `repowatch.service` runs as the dedicated, unprivileged `repowatch` system
  user, with `ProtectSystem=strict` and a minimal `ReadWritePaths`.
- Privileged operations — writing the nginx site config, reloading nginx —
  are isolated in a separate root-owned oneshot unit
  (`repowatch-nginx.service`) triggered by its own timer, not by granting
  the main process `sudo` or filesystem access outside its state/config
  directories.
- `admin_password_hash` is a PBKDF2 hash, never the plaintext password —
  see [access.md](access.md) for the full authentication model, session/CSRF
  handling, and host-token scoping.
- The dashboard and status API themselves aren't rate-limited by design —
  see the "What's deliberately not there" section of
  [access.md](access.md) for the reasoning; put a reverse proxy or firewall
  in front if you need that.
