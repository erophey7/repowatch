# repowatch

A lightweight, self-hosted "smart cache" for package repositories: Arch
(pacman), Debian/Ubuntu (apt), Alpine (apk), and RPM-based distributions
(RPM-MD: Rocky, Fedora, openSUSE, etc.). Unlike a plain caching proxy
(apt-cacher-ng, pacoloco, and similar), repowatch:

- actively watches upstream indexes on a schedule for new package versions,
  instead of waiting for a client to request a file;
- can prefetch new packages into the cache as soon as they appear, ahead of
  any client request;
- exposes machine-readable state ("what changed and when") over HTTP as
  `status.json`, so agents on hosts (or any external system — Ansible,
  cron jobs, whatever) can decide for themselves whether an update is needed.

The actual HTTP cache is served by **nginx**, configured by repowatch's own
generator (`repowatch nginx-render`/`nginx-apply`, see
[docs/configuration.md](docs/configuration.md#nginx)). repowatch doesn't
compete with nginx — it watches upstream metadata and calls into the same
nginx to warm the cache. No database clusters, no worker queues, no heavy UI:
a single Python process (stdlib + PyYAML for config + httpx for async HTTP),
state stored in one SQLite file.

**Contents:** [Features](#features) · [Repository layout](#repository-layout)
· [Quick start](#quick-start-dev) · [System install](#system-install) ·
[Requirements](#requirements) · [Project status](#project-status) ·
[Documentation](#documentation) · [Contributing](#contributing) ·
[License](#license)

## Features

- **Index parsers**: apt (`InRelease`/`Release` + `Packages.gz`, including
  APT by-hash), pacman (`.db.tar.gz`), apk (`APKINDEX.tar.gz`), RPM-MD
  (`repodata/repomd.xml` + primary XML, gzip/xz/bzip2/Zstandard).
- **Signature verification**: apt and pacman via `gpgv`; RPM-MD via
  `repomd.xml.asc`; apk via a separate non-GPG scheme (embedded RSA) using
  system `openssl` or `apk-tools >= 3.0`.
- **Active cache warming**: concurrent, with independent limits on total
  bandwidth (bytes/sec, not requests/sec) and parallelism; check interval is
  configurable per repository.
- **`status.json` / `/healthz` / `/metrics`**: a minimal HTTP contract for
  automated clients, a liveness check, and Prometheus-format metrics.
- **Web dashboard**: view/add/edit repositories, trigger manual warms, ban
  individual packages from auto-warming, group repositories into collapsible
  sections, see charts of what clients actually requested — a plain static
  page, no build step or JS frameworks.
- **Access control**: admin password (session + CSRF), separate revocable
  tokens for status-API host clients (optionally restricted to specific
  repo IDs), an optional guest read-only mode.
- **nginx generation**: a full caching `server` block rendered from
  declarative YAML, applied atomically with `nginx -t` validation and
  rollback on failure.
- **Configurable local URL layout** (`url_template`/`url_variables`) to
  match whatever path scheme your clients expect.
- **Webhook notifications** on repeated warm/signature-check failures
  (Slack/Mattermost/Discord-compatible JSON).
- **Self-update**: `repowatch self-update` checks GitHub Releases and
  installs a newer, checksum-verified version — no source checkout needed.

## Repository layout

```
src/repowatch/                — service source
  parsers/                    — one index parser per format
  static/dashboard.html       — web dashboard (no build step)
docs/                         — in-depth guides (configuration, access, deployment)
systemd/                      — unit files and timers for production
scripts/                      — install, backup, upgrade, load-testing tools
config/config.example.yaml   — example repository configuration
tests/                        — parser, state, API, and integration tests
```

## Quick start (dev)

```bash
make dev
source .venv/bin/activate
make test
cp config/config.example.yaml config/config.yaml
# edit config.yaml for your repositories/mirrors

repowatch -c config/config.yaml check-config   # validate, no state_db created
repowatch -c config/config.yaml check-once     # single pass, no daemon
repowatch -c config/config.yaml serve-status   # HTTP server with status.json
repowatch -c config/config.yaml run            # daemon with scheduled checks (systemd-oriented)
repowatch -c config/config.yaml supervise      # same, for environments without systemd
repowatch -c config/config.yaml backup ./out   # one-shot SQLite backup, no dependencies beyond stdlib
repowatch self-update --repo owner/name --check  # check GitHub Releases; no config.yaml needed
```

By default the CLI reads `/etc/repowatch/config.yaml`; on a system install,
`repowatch check-config` or `repowatch run` works without extra flags. For a
different file, pass `-c`/`--config` before the subcommand, as above. See
[docs/deployment.md](docs/deployment.md#running-without-systemd) for
`supervise`/`backup` in detail.

## System install

```bash
make check                                    # environment diagnostics only
make install PREFIX=/usr/local
sudo make activate                            # enables and starts services
```

`PREFIX`, `SYSCONFDIR` (default `/etc`), `LOCALSTATEDIR` (default `/var`),
and `DESTDIR` (staging/packaging) are configured independently. `make
install` only lays down files and doesn't touch a running process; starting
or restarting services is a separate, explicit `make activate`.

## Requirements

Python 3.11+, `PyYAML`, `httpx`; signature verification needs system `gpgv`
(apt/pacman/RPM-MD) and either `openssl` or `apk-tools >= 3.0` (apk). The
caching layer needs nginx (or a compatible proxy, if you're not using the
built-in config generator). No external database — state lives in a single
SQLite file.

## Project status

Actively developed. All parsers, cache warming, the dashboard, signature
verification, and nginx generation listed above are implemented, covered by
tests, and exercised against real mirrors.

## Documentation

This README covers quick start and core concepts. In-depth guides live under
[`docs/`](docs/):

- [docs/configuration.md](docs/configuration.md) — full `config.yaml`
  reference.
- [docs/access.md](docs/access.md) — admin login, host tokens, guest mode,
  TLS.
- [docs/deployment.md](docs/deployment.md) — production install, systemd,
  nginx, backups, upgrades.

## Contributing

Issues and pull requests are welcome. There's no CI pipeline yet — run
`make test` (and `make check` if your change touches installation) before
sending a PR; see [Quick start](#quick-start-dev) above for the dev setup.

## License

[MIT](LICENSE).
