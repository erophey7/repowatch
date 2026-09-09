# Configuration reference

repowatch is configured through a single YAML file, `config.yaml`. There is
no auto-discovery: every repository you want watched/cached has to be listed
explicitly. The CLI reads `/etc/repowatch/config.yaml` by default; pass
`-c`/`--config` to use a different path.

This file documents every field. For a working starting point, copy an
example config and edit it (see the main README's Quick start).

- [Top-level fields](#top-level-fields)
- [`repos[]`](#repos)
- [`url_template` / `url_variables`](#url_template--url_variables)
- [`status_server`](#status_server)
- [`syslog_listener`](#syslog_listener)
- [`nginx`](#nginx)
- [Fields editable from the dashboard](#fields-editable-from-the-dashboard)
- [Setting the admin password](#setting-the-admin-password)
- [Validating a config](#validating-a-config)

## Top-level fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `state_db` | path | — (required) | SQLite file for snapshots, history, warmed-package state. Must be on local disk (not NFS) — see [`nginx`](#nginx) for the package cache itself, which can live elsewhere. |
| `cache_base_url` | URL | — (required) | Where repowatch itself sends warm-up requests — the address of your nginx cache, from repowatch's point of view. Often `http://127.0.0.1:8080`. |
| `public_cache_url` | URL or `null` | `null` | The address a human would use to browse the cache — shown as clickable links in the dashboard. Separate from `cache_base_url` because that one is usually a loopback address, useless as a link in your browser. If unset, `cache_base_url` is reused. |
| `check_interval` | int (seconds) | `300` | How often each repository's index is checked, unless overridden per-repository (see `repos[].check_interval`). |
| `check_concurrency` | int | `8` | How many repositories to check at once per scheduler tick. Repositories are checked concurrently, not one at a time — a slow/hung upstream for one repo won't delay checking the others. |
| `prefetch_concurrency` | int | `8` | How many files to warm in parallel per warm-up run. |
| `prefetch_bandwidth_limit` | float (bytes/sec) or `null` | `null` | Global cap on total warm-up bandwidth, across all concurrently warming files — not a requests/sec limit (file sizes vary from a few hundred bytes to hundreds of megabytes). `null` means no limit. Overridable per-repository, see `repos[].prefetch_bandwidth_limit`. |
| `event_retention_days` | int | `90` | How many days to keep the per-repository change log (`repo_events` — what changed and when). |
| `request_retention_days` | int | `7` | How many days to keep the log of real client requests (only populated if `syslog_listener.enabled`). |
| `warmed_retention_days` | int | `180` | How many days to keep a `warmed_packages` row that hasn't been updated. Deliberately much longer than `event_retention_days`: this is "last known warm state", not an event log, and a package can legitimately go unwarmed for months if its version doesn't change. |
| `event_max_rows_per_repo` | int or `null` | `null` | Size-based cap on `repo_events`, per repository, on top of the day-based retention above. Use when traffic is high enough that day-based retention alone doesn't bound growth. `null` = no cap. |
| `request_max_rows` | int or `null` | `null` | Size-based cap on `request_events`, global (not per-repository — some rows aren't attributable to one repository). `null` = no cap. |
| `admin_password_hash` | string or `null` | `null` | PBKDF2 hash of the dashboard admin password. Generate it with `repowatch hash-password` — see [Setting the admin password](#setting-the-admin-password). Until this is set, the dashboard and administrative API are closed. |
| `notify_webhook_url` | URL or `null` | `null` | Webhook for notifications about repeated warm-up/signature-verification failures — a generic JSON POST, compatible with Slack/Mattermost/Discord incoming webhooks. Treat this as a secret (the URL itself is a bearer token): it is **not** exposed through `GET /api/config` or the dashboard, only editable by hand in `config.yaml`. |
| `notify_after_failures` | int | `3` | How many *consecutive* failures (per repository, per failure kind — signature verification or warm-up) before sending a notification. Fires once at the threshold and once on recovery, not on every failure. |
| `status_server` | mapping | — | See [`status_server`](#status_server). |
| `syslog_listener` | mapping | — | See [`syslog_listener`](#syslog_listener). |
| `nginx` | mapping | — | See [`nginx`](#nginx). |
| `repos` | list | — (required, non-empty) | See [`repos[]`](#repos). |

## `repos[]`

Each entry describes one repository to watch and cache. `id` is permanent —
it's the primary key for this repository's history, warmed-package state,
and bans in `state_db`. Changing it is the same as deleting the repository
and adding a new one; there's no rename.

| Field | Type | Default | Applies to | Notes |
|---|---|---|---|---|
| `id` | string | — (required) | all | Stable identifier. Immutable in practice — see above. |
| `type` | `pacman` \| `apt` \| `apk` \| `dnf` \| `apt-rpm` | — (required) | all | Selects the index parser. `dnf` covers any RPM-MD repository (Rocky, Fedora, openSUSE, …), not just Fedora/DNF-branded ones. `apt-rpm` is for ALT Linux-style apt-over-RPM repositories, not RPM-MD. |
| `upstream` | URL | — (required) | all | The real upstream mirror address. repowatch's own index checks go straight here; warm-up requests go through `cache_base_url` instead (see architecture note in the README). |
| `arch` | string | — (required) | all | Target architecture (`x86_64`, `amd64`, `i686`, `noarch`, …). One `RepoConfig` = one architecture; to mirror multiple architectures of the same repository, add multiple entries (see `group` below for grouping them visually). |
| `prefetch` | bool | `true` | all | Whether repowatch actively warms new packages into the cache. Set `false` for repositories you only want indexed/tracked in `status.json` without eagerly pulling every new package (useful for very large or rarely-used repos). |
| `repo_name` | string | — (required for `pacman`) | pacman | e.g. `core`, `extra`, `community`. |
| `distribution` | string | — (required for `apt`) | apt | e.g. `bookworm`, `jammy`, `noble-updates`. |
| `component` | string | — (required for `apt`, `apt-rpm`) | apt, apt-rpm | e.g. `main`, `contrib`, `non-free`. For `apt-rpm` it must match `[A-Za-z0-9_-]+` (e.g. `classic`, `checkinstall`). |
| `verify_signature` | bool | `false` | apt, pacman, dnf, apk | Enables GPG (or, for apk, RSA) verification of the index before it's parsed. See per-type key fields below. |
| `keyring_path` | path or `null` | `null` | apt, pacman, dnf | Path to an **exported keyring** (`gpg --export ... > keyring.gpg`), not a `.asc`/armored key file. Required when `verify_signature: true` for these types. |
| `apk_signature_backend` | `openssl` \| `apk-tools` | `openssl` | apk | Which system tool performs the embedded-RSA signature check (apk uses its own scheme, not OpenPGP — `keyring_path`/`gpgv` don't apply to it). |
| `apk_keys_dir` | path or `null` | `null` | apk | Directory of trusted apk public keys (the same format/layout as `/etc/apk/keys`). Required when `verify_signature: true` for apk. |
| `check_interval` | int (seconds) or `null` | `null` | all | Per-repository override of the top-level `check_interval`. `null` means "use the global value". |
| `prefetch_bandwidth_limit` | float (bytes/sec) or `null` | `null` | all | Per-repository override of the top-level `prefetch_bandwidth_limit`. `null` means "use the global value" (which may itself be unlimited). |
| `group` | string or `null` | `null` | all | Free-text label for grouping repositories in the dashboard into collapsible sections. Purely cosmetic — no validation, any string is allowed, and it isn't tied to `type`/distribution automatically. Repositories without a group show up under "Ungrouped". |
| `url_template` | string or `null` | `null` | all | Custom local URL layout — see [`url_template` / `url_variables`](#url_template--url_variables). |
| `url_variables` | mapping (string → string) | `{}` | all | Extra substitution values for `url_template`. |

### Signature verification: what's actually checked

- **apt**: the `InRelease` (or detached `Release`/`Release.gpg`) file is
  checked with `gpgv` against `keyring_path`. `Packages.gz`'s checksum is
  cross-verified against the (verified) Release file. apt's by-hash mode is
  used automatically when the upstream advertises it, with a fallback to the
  plain `Packages.gz` path when the by-hash object briefly 404s.
- **pacman**: the detached `<repo>.db.tar.gz.sig` is checked with `gpgv`
  against `keyring_path`.
- **dnf** (RPM-MD): `repomd.xml.asc` is checked with `gpgv` against
  `keyring_path`. The primary XML's checksum (and size, when advertised) is
  always cross-checked against `repomd.xml`, independent of
  `verify_signature`.
- **apt-rpm** (ALT Linux): the detached signature over the exact bytes of
  `release` is checked with `gpgv`. Binary RPM headers inside the package
  list are read directly (no RPM database, no package installation);
  SHA256/BLAKE2b checksums are verified against the release file.
- **apk**: a different, non-GPG signature scheme (an RSA/RSA256 signature
  embedded as a second gzip member inside `APKINDEX.tar.gz`). Verified via
  the system `openssl` binary, or via `apk-tools >= 3.0` if you set
  `apk_signature_backend: apk-tools`. There is no plain-OpenPGP option for
  apk — `keyring_path` is not used here.

A repository with `verify_signature: true` and a missing/wrong keyring or
key is not silently skipped — the check fails, the last good snapshot is
kept, and the failure is counted for `notify_after_failures`/`/healthz`.

## `url_template` / `url_variables`

By default, each parser type has a fixed local path layout under
`cache_base_url` (e.g. `/arch/<repo_name>/os/<arch>/...` for pacman). If your
nginx or client configuration expects a different layout,
`url_template`/`url_variables` let you rearrange it per repository, without
touching parser or nginx-generator code.

```yaml
repos:
  - id: archlinux-core
    type: pacman
    upstream: https://geo.mirror.pkgbuild.com/core/os/x86_64
    repo_name: core
    arch: x86_64
    url_template: "/pacman/{repo_name}/{arch}"
```

- Available substitution names: `id`, `type`, `repo_name`, `arch`,
  `distribution`, `component` (whichever are set for this repository), plus
  anything you add in `url_variables`.
- `url_variables` keys must be lowercase identifiers (`[a-z][a-z0-9_]*`) and
  can't shadow the built-in names above. Values must be a single safe URL
  path segment (`[A-Za-z0-9_+~.-]+`, no `.`/`..`).
- The expanded template must resolve to an absolute local path with no
  traversal and no nginx-specific syntax — this is a restricted substitution
  into a fixed set of safe segments, not a general template language or a
  place to inject raw nginx config.
- The same resolved path is used consistently for warm-up requests, dashboard
  "browse" links, and (if `syslog_listener` is enabled) matching real client
  requests back to a repository — one source of truth, not three.
- Two repositories that resolve to overlapping/conflicting local paths are
  rejected at config-validation time (`check-config`, and the dashboard's
  `POST /api/repos`), whether or not `nginx.enabled` is set — a path conflict
  is a bug in your config either way.
- Repositories without `url_template` are unaffected — the classic fixed
  layout keeps working exactly as before.

## `status_server`

Configures the built-in HTTP server (`repowatch run` / `serve-status`) that
exposes `status.json`, the dashboard, and the administrative API.

| Field | Type | Default | Notes |
|---|---|---|---|
| `bind` | string | `0.0.0.0` | Listen address. |
| `port` | int | `8085` | Listen port. |
| `tls_cert_path` / `tls_key_path` | path or `null` | `null` | Both set (PEM cert and key) to enable built-in TLS, or both left `null` for plain HTTP. Setting only one is an error. Loaded once at startup — rotating a certificate needs a restart, which is why these two fields aren't in the dashboard-editable set (see below). A reverse proxy handling TLS termination is generally the recommended setup; built-in TLS exists for simple single-host deployments. |
| `allow_insecure_http` | bool | `false` | Must be explicitly set to allow serving the admin login/session and Bearer-token status API over plain, unencrypted HTTP. Without it (and without TLS configured), authenticated routes refuse to serve over HTTP. This is an explicit operator opt-in, not something inferred from a `Forwarded`/`X-Forwarded-Proto` header. |
| `guest_read_only` | bool | `false` | When `true`, the dashboard and a fixed set of read-only routes (status, packages, history, request stats) are served to anyone, without login — everything that changes state still requires an authenticated admin session. Does not affect `/api/config`, `/api/tokens`, or `/metrics`, which have their own rules (see below). |
| `token_repo_restrictions` | bool | `false` | Enables scoping host tokens (see below) to a specific set of repository IDs. Tokens issued before this is enabled (or issued without a scope) are unrestricted — this only lets you *start* restricting new tokens, it doesn't retroactively narrow old ones. |
| `trusted_proxies` | list of IP/CIDR | `[]` | Which peers' `X-Forwarded-For`-style headers are trusted for determining the real client IP (used by `metrics_allowed_networks` and request logging). The chain is parsed right-to-left; a peer not in this list can't spoof another client's IP. |
| `metrics_allowed_networks` | list of IP/CIDR | `["127.0.0.1/32", "::1/128"]` | Which client networks may fetch `GET /metrics`. Loopback-only by default; widen it (e.g. to your monitoring subnet) if Prometheus scrapes from elsewhere. |

Host tokens (for the `status.json` API used by pacman/apt/apk/dnf clients
polling for updates) are issued and revoked from the dashboard, not from
this file — see the main README / dashboard for that flow.

## `syslog_listener`

Optional UDP listener for nginx's `access_log` (syslog format), which is how
the dashboard learns about *real client downloads* (as opposed to
repowatch's own warm-up requests) — feeding `warmed_packages` and the
request-history charts. Off by default: repowatch doesn't open an extra
network port unless you've actually pointed nginx at it.

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | Turn the listener on. Also required for `GET /api/requests` and `/api/requests/summary` to return data. |
| `bind` | string | `127.0.0.1` | Listen address — loopback by default, since this is meant to receive from a local nginx. |
| `port` | int | `1514` | Listen port. Point nginx's `access_log syslog:server=...` at this. |

## `nginx`

Controls the built-in nginx config generator (`repowatch nginx-render` /
`nginx-apply`), which renders a complete caching `server` block from your
`repos[]` list instead of you hand-writing nginx location blocks yourself.
Optional — you can run repowatch against your own hand-written nginx config
without ever setting `nginx.enabled: true`; the parts of the URL layout that
matter to repowatch (`RepoConfig.url_template`, or the fixed per-type default
paths) are documented under [`repos[]`](#repos) regardless of which nginx
config actually serves them.

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | Turn on generation. When enabled (or when any repository sets `url_template`), `config.yaml` validation also renders the nginx config to catch path conflicts early. |
| `listen` | string | `8080` | Port, or `IPv4:port`, for the generated cache `server` block. |
| `server_name` | string | `repo-cache.local` | A single hostname, no nginx pattern syntax. |
| `resolvers` | list of IPv4 | `["127.0.0.53"]` | DNS resolver(s) nginx uses for upstream hostnames. IPv4-only by design — see the note below. |
| `cache_max_size` | string | `100g` | `proxy_cache_path max_size`, e.g. `50g`, `500m`. |
| `cache_key_version` | string | `""` | Bump this (any short string) to invalidate all cached objects by changing the cache key prefix, without clearing the disk cache by hand. |
| `index_ttl` | int (seconds) | `300` | `proxy_cache_valid` for mutable index/metadata files (the whole point of active watching is that these go stale quickly). |
| `package_ttl` | int (seconds) | `15552000` (180 days) | `proxy_cache_valid` for immutable package files, addressed by exact version/checksum. |
| `cache_dir` | path or `null` | `null` | **Read-only/informational** — the actual cache directory nginx writes to is set once, at install time, in the root-owned `policy.json` (see `CACHE_DIR` in [deployment.md](deployment.md)), not here. Setting this makes the path visible in `config.yaml` instead of hidden inside a file the service user can't read; `nginx-apply` cross-checks it against `policy.json` and refuses to apply on a mismatch, so it can't silently go stale. To actually change the cache directory: `CACHE_DIR=... make install && sudo make activate`. |

**Why `resolvers` is IPv4-only:** upstream resolution happens once via
`nginx -t`/generation-time DNS, not a live `resolver` directive per request,
and mixing that with dynamic re-resolution isn't something plain nginx
supports well without nginx Plus. If your network has no IPv6 route but an
upstream mirror's hostname also resolves to an AAAA record, an IPv6-capable
setup can produce real `Network is unreachable` errors on warm-up — IPv4-only
resolution sidesteps that at the cost of not using IPv6 upstream peers even
when they'd work.

If you're hand-writing nginx config instead of using the generator, run
`repowatch nginx-render -c config.yaml` to print what the generator would
produce for your own `repos[]` — a concrete starting point for the location
blocks you'd need to write by hand.

## Fields editable from the dashboard

`GET`/`POST /api/config` exposes a subset of top-level fields for editing
without hand-editing YAML — deliberately not all of them. A field is
included only if changing it takes effect immediately (no restart needed):

`check_interval`, `event_retention_days`, `request_retention_days`,
`warmed_retention_days`, `event_max_rows_per_repo`, `request_max_rows`,
`prefetch_concurrency`, `check_concurrency`, `prefetch_bandwidth_limit`,
`cache_base_url`, `public_cache_url`, `notify_after_failures`.

Notably **not** editable from the dashboard (edit `config.yaml` by hand and
restart instead):

- `admin_password_hash` — secret, only ever written via
  `repowatch set-password`/`hash-password`, never round-tripped through the
  API.
- `notify_webhook_url` — secret (the URL is itself a bearer token).
- `state_db`, `status_server`, `syslog_listener`, `nginx` — read once at
  process startup; editing them through the dashboard would silently not
  take effect until a restart, which is worse than not offering the option.

Repositories themselves (`repos[]`) are managed separately, through
`POST /api/repos` (add), `POST /api/repos/<id>` (edit — full replacement,
`id` immutable), and `POST /api/repos/<id>/delete`.

## Setting the admin password

```bash
repowatch hash-password
# prompts for a password, prints a PBKDF2 hash to stdout
```

Paste the printed hash into `config.yaml` as `admin_password_hash`. The
plaintext password itself is never stored anywhere — if `config.yaml` leaks,
only the hash is exposed, not the password.

To change the password later without hand-editing the hash in place (and to
invalidate existing admin sessions at the same time), use:

```bash
repowatch set-password
```

## Validating a config

```bash
repowatch check-config              # or: repowatch -c path/to/config.yaml check-config
```

Validates the file and exits — it does not create `state_db`, connect to any
network, or otherwise touch disk beyond reading `config.yaml` itself. Use
this in CI or before rolling out a config change.
