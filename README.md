# repowatch

A lightweight, self-hosted "smart cache" for package repositories: Arch
(pacman), Debian/Ubuntu (apt), Alpine (apk), Void (xbps), Gentoo binary packages, Slackware, Nix, and RPM-based
distributions (RPM-MD: Rocky, Fedora, openSUSE, etc.). Unlike a plain caching proxy
(apt-cacher-ng, pacoloco, and similar), repowatch actively watches upstream
indexes on a schedule, can prefetch new packages ahead of any client
request, and exposes machine-readable state ("what changed and when") over
HTTP as `status.json`, so agents on hosts (or any external system —
Ansible, cron jobs, whatever) can decide for themselves whether an update
is needed.

The actual HTTP cache is served by **nginx**, configured by repowatch's own
generator (`repowatch nginx-render`/`nginx-apply`, see
[docs/configuration.md](docs/configuration.md#nginx)). repowatch doesn't
compete with nginx — it watches upstream metadata and calls into the same
nginx to warm the cache. No database clusters, no worker queues, no heavy UI:
a single Python process (stdlib + PyYAML for config + httpx for async HTTP),
state stored in one SQLite file.

## Requirements

**Build** (only to build a wheel or install from source):

- Python 3.11+ with `pip` and `venv`.
- `setuptools >= 68` — the build backend declared in `pyproject.toml`, pulled
  in automatically by `pip`; nothing to install by hand.

**Runtime, required:**

- Python 3.11+.
- `PyYAML >= 6.0`, `httpx >= 0.27` — installed automatically as package
  dependencies.
- SQLite, via Python's stdlib `sqlite3` — no separate database server.

**Runtime, recommended** (each gates one specific feature; without it, the
rest of repowatch runs normally):

- `nginx` — the actual caching layer. repowatch generates and applies its
  config (`nginx-render`/`nginx-apply`) but doesn't serve HTTP itself; you
  can run a hand-written nginx config instead and skip this.
  - third-party `ngx_cache_purge` module (Debian/Ubuntu:
    `libnginx-mod-http-cache-purge`; Arch: `nginx-mod-cache_purge`) — only if
    `nginx.enable_purge` is on.
  - third-party `ngx_http_js_module` (njs) (Debian/Ubuntu:
    `libnginx-mod-http-js`; Arch: `nginx-mod-njs`) — only if
    `nginx.enable_cache_probe` is on; see
    [docs/configuration.md](docs/configuration.md#nginx).
- Nix CLI (`nix`, `nix-env`, `nix-instantiate`) — optional system dependency
  required only for `type: nix`. It evaluates package output paths and verifies
  binary-cache signatures; no new Python dependency is needed. The service user
  needs a usable Nix store/daemon. See [Nix repositories](docs/nix.md).
- `gpg` — optional key-expiry warnings for GPG-signed repositories;
  signature verification itself only needs `gpgv`.
- `gpgv` — signature verification for apt/pacman/RPM-MD/apt-rpm repositories
  with `verify_signature: true`.
- `openssl`, or `apk-tools >= 3.0` — signature verification for apk
  repositories with `verify_signature: true` (apk uses a different, non-GPG
  scheme; see `apk_signature_backend` in
  [docs/configuration.md](docs/configuration.md)).
- `systemd` — production process supervision. Without it, run
  `repowatch supervise` instead (see
  [docs/deployment.md](docs/deployment.md#running-without-systemd)).
- `zstd` — required if an RPM-MD repository publishes Zstandard-compressed
  metadata, or for any `xbps` (Void Linux) repository (its repodata is
  always Zstandard-compressed). A system binary, shelled out to the same
  way as `gpgv`/`openssl` — no Python version requirement beyond the
  3.11+ above.

Run `make check` for a read-only diagnostic of what's actually present on a
given host (`OK`/`WARN`/`FAIL` per item).

## Quick start

A fresh installation starts with **no repositories** (`repos: []`). Add only
what your clients use, through the dashboard or YAML. The shipped system profile
enables purge, deduplication, cache inventory and request tracking; install the
matching nginx modules before activation. Repository keys, TLS certificates,
webhook destinations and optional Nix need site-specific setup.

1. [Install the server](docs/quick-start.md): Debian/Ubuntu, Arch, Fedora/Rocky
   and Alpine prerequisites, activation and first login.
2. [Add repositories](docs/repositories.md): dashboard/YAML workflows, format
   examples and bounded warming.
3. [Connect clients](docs/clients.md): package-manager configuration and checks.

Existing installations keep their YAML on upgrade. The empty seed and new
feature settings are first-install choices, not automatic changes to your setup.

## Install

### Development

```bash
make dev
source .venv/bin/activate
make test
cp config/config.example.yaml config/config.yaml
# Edit config.yaml for your repositories/mirrors. Also change state_db: it
# defaults to /var/lib/repowatch/state.sqlite3, a real system path a normal
# user can't write to — that value is a placeholder a system install (`make
# install`) substitutes automatically, not something meant to work as-is
# for a local trial. For running locally (from this directory), point it
# somewhere writable instead, e.g. state_db: ./state.sqlite3 — resolved
# relative to wherever you run `repowatch` from, not to config.yaml itself,
# so keep running commands from here, or use an absolute path.

repowatch -c config/config.yaml check-config   # validate, no state_db created
repowatch -c config/config.yaml nginx-render   # print the generated nginx server block (no files touched)
repowatch -c config/config.yaml check-once     # single pass, no daemon
repowatch -c config/config.yaml serve-status   # HTTP server with status.json
repowatch -c config/config.yaml run            # daemon with scheduled checks (systemd-oriented)
repowatch -c config/config.yaml supervise      # same, for environments without systemd
```

`nginx-render` above only prints what a real cache `server` block would look
like for your `repos[]` — it doesn't write files or touch nginx. Actually
applying it (`nginx-apply`) needs a root-owned policy that only a system
install creates; see [Production](#production-system-install) below.

By default the CLI reads `/etc/repowatch/config.yaml`; on a system install,
`repowatch check-config` or `repowatch run` works without extra flags. For a
different file, pass `-c`/`--config` before the subcommand, as above.

### Production (system install)

```bash
make check                                    # environment diagnostics only
sudo make install PREFIX=/usr/local
sudoedit /etc/repowatch/config.yaml            # review the empty, full-feature seed
sudo make activate                            # enables and starts services
sudo -u repowatch /usr/local/bin/repowatch set-password
```

`PREFIX`, `SYSCONFDIR` (default `/etc`), `LOCALSTATEDIR` (default `/var`),
and `DESTDIR` (staging/packaging) are configured independently. `make
install` only lays down files and doesn't touch a running process; starting
or restarting services is a separate, explicit `make activate`. See
[docs/deployment.md](docs/deployment.md) for the full procedure, backups,
and nginx integration.

## Updating

```bash
repowatch self-update --repo erophey7/repowatch --check  # check GitHub Releases; no config.yaml needed
repowatch self-update --repo erophey7/repowatch           # install if newer
```

`--repo` is always required and always "owner/name" — point it at a fork
instead if you're tracking one.

A network-based path that reinstalls repowatch and resolves its Python dependencies
(does not touch `config.yaml`, `state_db`, or nginx, and does not restart
the service for you). For an offline, systemd-managed system install with
automatic backup/rollback on failure, use `make upgrade-plan`/`make upgrade`
instead — see
[docs/deployment.md#upgrading](docs/deployment.md#upgrading) for both paths
in detail.

## Documentation

- [Quick start](docs/quick-start.md), [repositories](docs/repositories.md) and
  [clients](docs/clients.md) — from an empty installation to a working cache.
- [Warming policies](docs/warming-policy.md), [Nix](docs/nix.md),
  [Gentoo/Slackware](docs/gentoo-slackware.md), [webhooks](docs/webhooks.md)
  and [Docker](docs/docker.md) — feature-specific examples.

- [docs/configuration.md](docs/configuration.md) — full `config.yaml`
  reference.
- [docs/access.md](docs/access.md) — admin login, host tokens, guest mode,
  TLS.
- [docs/deployment.md](docs/deployment.md) — production install, systemd,
  nginx, backups, upgrades.

- [Profiling and bounded load tests](docs/profiling.md) — reproducible measurements
  and explicit operational checks.

## Contributing

Issues and pull requests are welcome. There's no CI pipeline yet — run
`make test` (and `make check` if your change touches installation) before
sending a PR; see [Install → Development](#development) above for the dev
setup.

## Experimental container builds

See [Docker builds](docs/docker.md) for generated build files, Make targets and
optional experimental features and [docker-compose.dev.yml](docker-compose.dev.yml).
The Docker Hub release Compose draft stays local until release preparation. Container runtime
validation is still pending.

## License

[MIT](LICENSE).

See [Gentoo and Slackware repositories](docs/gentoo-slackware.md) for binhost and
release/component setup, client URLs and signature limitations.
