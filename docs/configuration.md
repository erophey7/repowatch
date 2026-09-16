# Configuration reference

repowatch is configured through a single YAML file, `config.yaml`. There is
no auto-discovery: every repository you want watched/cached has to be listed
explicitly. The CLI reads `/etc/repowatch/config.yaml` by default; pass
`-c`/`--config` to use a different path.

This file documents every field. Start with the [quick start](quick-start.md)
and [repository management examples](repositories.md).

**Schema defaults versus installation defaults:** the tables below describe
what happens when a field is omitted. The shipped `config/config.example.yaml`
sets an explicit profile: no repositories (`repos: []`), 900-second checks,
four index checks/four downloads per warm run, a shared 10 MiB/s warm budget,
100 GiB nginx cache target, purge/dedup/cache probe and syslog enabled, loopback
status binding, no guest access, and repository-scoped host tokens. Omitting
nginx feature flags still leaves them disabled for compatibility with existing
configurations. Upgrades preserve existing YAML rather than replacing it.

An empty repository list is valid. It keeps administration available and starts
no upstream checks. `/healthz` can report healthy with zero sources; it does not
assert that clients have usable repositories. Use `repos: []`, not `null`.
Signature verification requires repository-specific trusted keys, TLS requires
certificates, webhooks require a destination, and Nix requires its optional CLI;
these cannot be made operational by a universal default.

- [Top-level fields](#top-level-fields)
- [Bandwidth schedules](#bandwidth-schedules)
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
| `prefetch_bandwidth_limit` | positive float (bytes/sec) or `null` | `null` | Shared warm-body read budget for the whole application process, including all repositories and manual/Nix warming. `null` means unlimited globally. See [Bandwidth schedules](#bandwidth-schedules). |
| `prefetch_bandwidth_timezone` | IANA timezone name | `UTC` | Timezone for schedule windows. Non-UTC zones require system timezone data; no Python timezone package is added. |
| `prefetch_bandwidth_schedule` | list of windows | `[]` | Non-overlapping weekly windows replacing the global limit, each with `start`, `end`, `limit`, and optional `days`. |
| `event_retention_days` | int | `90` | How many days to keep the per-repository change log (`repo_events` — what changed and when). |
| `request_retention_days` | int | `7` | How many days to keep the log of real client requests (only populated if `syslog_listener.enabled`). |
| `warmed_retention_days` | int | `180` | How many days to keep a `warmed_packages` row that hasn't been updated. Deliberately much longer than `event_retention_days`: this is "last known warm state", not an event log, and a package can legitimately go unwarmed for months if its version doesn't change. A real client request refreshes the timer (see `syslog_listener` below); this whole cleanup is skipped entirely when `syslog_listener.enabled` is off, since without it there's no way to tell "nobody wants this" from "we can't see it". When `nginx.enable_purge` is also on, an expired row's real cache entry is purged too, not just the bookkeeping row. |
| `event_max_rows_per_repo` | int or `null` | `null` | Size-based cap on `repo_events`, per repository, on top of the day-based retention above. Use when traffic is high enough that day-based retention alone doesn't bound growth. `null` = no cap. |
| `request_max_rows` | int or `null` | `null` | Size-based cap on `request_events`, global (not per-repository — some rows aren't attributable to one repository). `null` = no cap. |
| `admin_password_hash` | string or `null` | `null` | PBKDF2 hash of the dashboard admin password. Generate it with `repowatch hash-password` — see [Setting the admin password](#setting-the-admin-password). Until this is set, the dashboard and administrative API are closed. |
| `notify_webhook_url` | URL or `null` | `null` | Webhook for selected repository and warming events, with bounded retries (see [webhooks](webhooks.md)) — a generic JSON POST with a `text` field, the format Slack and Mattermost incoming webhooks read directly. **Discord's own webhook endpoint does not read `text` at all** (it expects `content`) — point this at Discord's separate Slack-compatible endpoint instead, `https://discord.com/api/webhooks/<id>/<token>/slack`, not the plain webhook URL Discord's UI gives you by default. Treat this as a secret (the URL itself is a bearer token): it is **not** exposed through `GET /api/config` or the dashboard, only editable by hand in `config.yaml`. |
| `notify_events` | list of event names | `[repository.failing, repository.recovered]` | YAML-only selection of webhook events. Add `repository.changed`, `warm.started`, `warm.completed` to opt in; `[]` disables delivery. See [events and retry guarantees](webhooks.md). |
| `notify_after_failures` | int | `3` | How many *consecutive* failures (per repository, per failure kind — signature verification or warm-up) before sending a notification. Fires once at the threshold and once on recovery, not on every failure. |
| `key_expiry_warning_days` | int | `30` | Warning window before the earliest expiry in a GPG keyring, for repositories with `verify_signature: true`. Requires the full `gpg` binary; without it expiry is unknown, while `gpgv` verification continues normally. Exposed in the dashboard and the `repowatch_repo_key_expiring_soon` / `repowatch_repo_key_expires_at_timestamp_seconds` metrics. Webhook notification uses `kind="key_expiry"` and fires after `notify_after_failures` consecutive checks within the window (third by default), then once on recovery after a delivered warning. An unknown expiry does not clear the warning streak or send recovery; recovery requires a known expiry outside the window. Does not apply to apk, xbps, or nix. |
| `status_server` | mapping | — | See [`status_server`](#status_server). |
| `syslog_listener` | mapping | — | See [`syslog_listener`](#syslog_listener). |
| `nginx` | mapping | — | See [`nginx`](#nginx). |
| `repos` | list | `[]` | Empty installations are valid; add sources through YAML or the dashboard. See [`repos[]`](#repos). |

## Bandwidth schedules

```yaml
# Outside schedule windows, share 5 MiB/s across all warm operations.
prefetch_bandwidth_limit: 5242880
prefetch_bandwidth_timezone: Asia/Vladivostok
prefetch_bandwidth_schedule:
  - days: [mon, tue, wed, thu, fri, sat, sun]
    start: "20:00"
    end: "09:00"
    limit: 52428800  # 50 MiB/s overnight; null would remove the global cap.
```

Days use `mon` through `sun`; omitted days mean every day. Windows include
`start` and exclude `end`. An overnight window belongs to its starting day:
Friday 20:00–09:00 includes Saturday morning. `00:00`–`24:00` means the full
day. Equal start/end times and overlapping windows (including week wrap) are
rejected. All rates are positive finite bytes/sec or `null`; zero is not a
pause setting. Outside windows the top-level limit applies. Per-repository
limits remain additional ceilings even in an unlimited global window.

Rules use the named zone's local wall clock. During a daylight-saving fall
back, both occurrences of a repeated local time use the same matching rule;
skipped spring-forward times have no duration. UTC works without an external
timezone database. Edit windows and timezone in dashboard settings or YAML.

The running application observes policy edits within approximately one
second, including ongoing warms; invalid edits retain the last valid policy.
Schedule boundaries are evaluated while waiting, without restarting downloads.
Waiters are served in rotation, with up to 100 ms of rate credit retained.
Manual operations, simultaneous warm runs of the same repository, automatic
checks and Nix artifact warming use the same budget in `run`/`supervise`.
A separate `check-once` or `serve-status` process has its own budget; this
mechanism does not coordinate multiple application processes or hosts.

This paces response-body consumption by repowatch. HTTP/socket/nginx buffering
can allow bursts and upstream fetching ahead of consumption, so this is not
an exact WAN traffic shaper. Client downloads, index checks, Nix source
resolution and direct closure-metadata discovery are outside this budget.
Successful and failed artifact downloads both spend bytes already consumed;
cancellation removes outstanding work without reserving future bandwidth.

**Migration:** previously the global value was a default independently used
by each warm run, and a repository value replaced it. It is now one shared
budget plus optional repository ceilings, so concurrent warming may become
slower with the same configuration. Use `null` for unlimited speed; old zero
values must be replaced with `null`.

## `repos[]`

Each entry describes one repository to watch and cache. `id` is permanent —
it's the primary key for this repository's history, warmed-package state,
and bans in `state_db`. Changing it is the same as deleting the repository
and adding a new one; there's no rename.

| Field | Type | Default | Applies to | Notes |
|---|---|---|---|---|
| `id` | string | — (required) | all | Stable identifier. Immutable in practice — see above. |
| `type` | `pacman` \| `apt` \| `apk` \| `dnf` \| `apt-rpm` \| `xbps` \| `nix` \| `gentoo` \| `slackware` | — (required) | all | Selects the index parser. `dnf` covers any RPM-MD repository (Rocky, Fedora, openSUSE, …), not just Fedora/DNF-branded ones. `apt-rpm` is for ALT Linux-style apt-over-RPM repositories, not RPM-MD. `xbps` is Void Linux; it requires the system `zstd` binary (see the README) and does not support `verify_signature`. |
| `upstream` | URL | — (required) | all | The real upstream mirror address. repowatch's own index checks go straight here; warm-up requests go through `cache_base_url` instead (see architecture note in the README). |
| `arch` | string | — (required) | all | Target architecture (Nix uses `x86_64-linux` or `aarch64-linux`; otherwise `x86_64`, `amd64`, `i686`, `noarch`, …). One `RepoConfig` = one architecture; to mirror multiple architectures of the same repository, add multiple entries (see `group` below for grouping them visually). |
| `prefetch` | bool | `true` | all | Whether repowatch actively warms new packages into the cache. Set `false` for repositories you only want indexed/tracked in `status.json` without eagerly pulling every new package (useful for very large or rarely-used repos). |
| `prefetch_whitelist` | list of globs | `[]` | all | Warm only matching package names; empty allows all. See [warming lists](warming-policy.md). |
| `prefetch_blacklist` | list of globs | `[]` | all | Exclude matching names, overriding the whitelist. Exact bans also win. Applies to automatic and manual warming; Nix filters roots, not their dependencies. |
| `repo_name` | string | — (required for `pacman`) | pacman | e.g. `core`, `extra`, `community`. |
| `distribution` | string | — (required for `apt`) | apt | e.g. `bookworm`, `jammy`, `noble-updates`. |
| `component` | string | — (required for `apt`, `apt-rpm`) | apt, apt-rpm, slackware | e.g. `main`, `contrib`, `non-free`. For `apt-rpm` it must match `[A-Za-z0-9_-]+` (e.g. `classic`, `checkinstall`). Slackware: omit for the main index, or use `patches`, `extra`, `pasture`, `testing`; keep upstream at the release root. |
| `verify_signature` | bool | `false` | apt, pacman, dnf, apt-rpm, apk, nix, slackware | Enables index signature verification, or Nix closure metadata signature verification during warming. See per-type key fields below. |
| `keyring_path` | path or `null` | `null` | apt, pacman, dnf, apt-rpm, slackware | Path to an **exported keyring** (`gpg --export ... > keyring.gpg`), not a `.asc`/armored key file. Required when `verify_signature: true` for these types. |
| `apk_signature_backend` | `openssl` \| `apk-tools` | `openssl` | apk | Which system tool performs the embedded-RSA signature check (apk uses its own scheme, not OpenPGP — `keyring_path`/`gpgv` don't apply to it). |
| `apk_keys_dir` | path or `null` | `null` | apk | Directory of trusted apk public keys (the same format/layout as `/etc/apk/keys`). Required when `verify_signature: true` for apk. |
| `nix_source` | HTTP(S) URL or `null` | `null` | nix | Required source tarball containing a Nix expression, normally a nixpkgs channel. See [Nix repositories](nix.md). |
| `nix_attributes` | list of attribute paths | `[]` | nix | Selected packages/subtrees, such as `[hello, jq]`. Empty evaluates the complete available catalog for the selected system. |
| `nix_public_keys` | list of `name:base64` keys | `[]` | nix | Trusted Ed25519 public keys; required when `verify_signature: true`. |
| `nix_timeout` | positive int (seconds) | `600` | nix | Timeout for each CLI invocation, including source resolution and signature batches. |
| `nix_max_paths` | positive int | `500000` | nix | Maximum catalog outputs and maximum store paths in any one dependency closure. |
| `check_interval` | int (seconds) or `null` | `null` | all | Per-repository override of the top-level `check_interval`. `null` means "use the global value". |
| `prefetch_bandwidth_limit` | positive float (bytes/sec) or `null` | `null` | all | Additional shared ceiling for all concurrent warm operations of this repository. It cannot bypass the global budget; `null` adds no repository ceiling. |
| `group` | string or `null` | `null` | all | Free-text label for grouping repositories in the dashboard into collapsible sections. Purely cosmetic — no validation, any string is allowed, and it isn't tied to `type`/distribution automatically. Repositories without a group show up under "Ungrouped". |
| `url_template` | string or `null` | `null` | all | Custom local URL layout — see [`url_template` / `url_variables`](#url_template--url_variables). |
| `url_variables` | mapping (string → string) | `{}` | all | Extra substitution values for `url_template`. |

### Signature verification: what's actually checked

- **Gentoo**: `verify_signature: true` is rejected. GPKG signatures are
  inside package containers and must be verified by Portage; repowatch only
  parses the unsigned `Packages` catalog.
- **Slackware**: `gpgv` verifies `CHECKSUMS.md5.asc`, then the exact selected
  `PACKAGES.TXT` bytes are checked against that signed manifest. This inherits
  MD5's collision weakness; it does not verify package signatures while warming.
  See [Gentoo and Slackware](gentoo-slackware.md) for setup and limitations.

- **Nix**: while warming, Nix CLI verifies the downloaded `.narinfo` signatures
  against `nix_public_keys`, including dependency metadata. Signatures cover
  store identity, NAR hash/size and references, not compressed `FileHash`.
  Downloads are checked against advertised `FileHash`/`FileSize` when present;
  final NAR content verification remains with the consuming Nix client.
  A catalog check without warming does not verify binary-cache signatures.

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
- **apk**: a different, non-GPG signature scheme (`abuild-sign` prepends the
  RSA/RSA256 signature as its OWN gzip member before the real, still-compressed
  index — so `APKINDEX.tar.gz` is two concatenated gzip streams, signature
  first, index second). Verified via
  the system `openssl` binary, or via `apk-tools >= 3.0` if you set
  `apk_signature_backend: apk-tools`. There is no plain-OpenPGP option for
  apk — `keyring_path` is not used here.
- **xbps** (Void Linux): `verify_signature: true` is rejected at config
  time. XBPS repositories don't publish a
  signed index at all (`<arch>-repodata` has no accompanying `.sig`/`.sig2`
  upstream); trust instead comes from a per-*package* RSA signature
  (`<file>.xbps.sig2`), checked by the real `xbps` client at install time —
  a different shape of verification (per package, at warm-time) that isn't
  implemented yet.

Signed indexes are downloaded and reverified on every check, even when HEAD
returns an unchanged ETag or Last-Modified. This makes local key changes
and enabling verification effective without waiting for upstream changes;
it costs a full index download per check. Unsigned repositories retain the
HEAD optimization for index-based formats. Nix evaluates its configured
source on every check and verifies binary metadata during warming, as
described above.

An index-based repository with `verify_signature: true` and a missing/wrong keyring or
key is not silently skipped — the check fails, the last good snapshot is
kept, and the failure is counted for `notify_after_failures` (a webhook
fires once the consecutive-failure threshold is reached). `/healthz`
reflects it too, but indirectly and only in one specific case: it flags a
repository whose *last known-good* check has gone stale relative to its
`check_interval` — a repository that has NEVER had a single successful
check (failing from the very first cycle) has no "last known-good"
timestamp to compare against at all, and is therefore not reported as
stale by `/healthz` (see `_repo_staleness()`'s own docstring: "not
considered stale — that's an expected state, not a failure", the same rule
that also covers a brand-new repository nobody's checked yet). For
persistent from-the-start failures, `notify_after_failures` is the signal
that actually fires — don't rely on `/healthz` alone to catch that case.

For Nix, a successful catalog evaluation can coexist with failed binary
signature checks during warming. Such failures mark the warm attempt as
failed and contribute to the signature notification streak; they do not
roll back the evaluated catalog. `/healthz` catalog freshness therefore does
not establish that Nix closures have been warmed or verified successfully.

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
| `allow_insecure_http` | bool | `false` | Permits credentials over otherwise disallowed plain HTTP. By default, credentials are accepted on TLS connections, through a configured trusted proxy reporting HTTPS, or on direct loopback connections without forwarding headers. See [access.md](access.md#tls-and-allow_insecure_http). |
| `guest_read_only` | bool | `false` | When `true`, the dashboard and a fixed set of read-only routes (status, packages, history, request stats, and the SAFE subset of `GET /api/config` — never secrets like `admin_password_hash`) are served to anyone, without login — everything that changes state still requires an authenticated admin session. Does not affect `/api/tokens` (the list of issued host tokens stays admin-only) or `/metrics`, which have their own separate rules (see below). |
| `token_repo_restrictions` | bool | `false` | Enables scoping host tokens (see below) to a specific set of repository IDs. Tokens issued before this is enabled (or issued without a scope) are unrestricted — this only lets you *start* restricting new tokens, it doesn't retroactively narrow old ones. |
| `trusted_proxies` | list of IP/CIDR | `[]` | Which peers' `X-Forwarded-For`-style headers are trusted for determining the real client IP (used by `metrics_allowed_networks` and request logging). The chain is parsed right-to-left; a peer not in this list can't spoof another client's IP. |
| `metrics_allowed_networks` | list of IP/CIDR | `["127.0.0.1/32", "::1/128"]` | Which client networks may fetch `GET /metrics`. Loopback-only by default; widen it (e.g. to your monitoring subnet) if Prometheus scrapes from elsewhere. |

Host tokens (for the `status.json` API used by pacman/apt/apk/dnf clients
polling for updates) are issued and revoked from the dashboard, not from
this file — see the main README / dashboard for that flow.

## `syslog_listener`

UDP listener for nginx's `access_log` (syslog format), which is how the
dashboard learns about *real client downloads* (as opposed to repowatch's
own warm-up requests) — feeding `warmed_packages` and the request-history
charts. On by default: the socket itself is loopback-only, and the
generated nginx config (`nginx.enabled`) automatically emits the matching
`access_log syslog:server=...` directive off this same flag, so the two
sides can't end up mismatched. If you're using a hand-written nginx config
instead of the generator, point its `access_log` at this listener yourself
(see the `port` row below) — until you do, this is just an idle loopback
socket.

This also feeds the automatic `warmed_packages` expiry
(`warmed_retention_days`, see below): a package's "still wanted" timer only
advances when a real client actually asks for it, observed here. With this
listener off, repowatch has no way to tell "nobody wants this anymore" from
"we just can't see it" — the automatic expiry (and, if `nginx.enable_purge`
is also on, the cache eviction that comes with it) is skipped entirely
rather than guess.

Repowatch's own traffic (index checks and prefetch/warm requests, both of
which already send a fixed `User-Agent: repowatch/...`) is filtered out
before it ever reaches `request_events` or the "Recent client requests"
view — the generated nginx config maps that `User-Agent` to a single digit
in the access log line specifically so the listener can tell the two apart
without parsing arbitrary, space-containing client user-agent strings.

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | Turn the listener on/off. Also required for `GET /api/requests`, `/api/requests/summary`, and the automatic `warmed_packages` expiry to do anything. |
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
| `cache_dir` | path or `null` | `null` | **Read-only/informational** — the actual cache directory nginx writes to is set once, at install time, in the root-owned `policy.json` (see `CACHE_DIR` in [deployment.md](deployment.md)), not here. Setting this makes the path visible in `config.yaml` instead of hidden inside a file the service user can't read; `nginx-apply` cross-checks it against `policy.json` and refuses to apply on a mismatch, so it can't silently go stale. To actually change the cache directory: `sudo make install CACHE_DIR=... && sudo make activate` (stop the repowatch services/timers first). Setting it also unlocks the cache directory's size in `repowatch stats --cache-dir` and the dashboard's Storage panel ("Calculate cache directory size") — without it there is no path to walk, so that number is simply omitted rather than guessed at or defaulted to zero. **A real permissions caveat, found on a live deployment**: nginx creates `proxy_cache_path`'s `levels=1:2` subdirectories `0700`, owned by the nginx worker user (e.g. `www-data`) — regardless of the top-level `cache_dir`'s own mode. repowatch deliberately runs as a separate, unprivileged service user (not the nginx worker's), so on most real installs it can list the top-level directory but cannot descend into any of the hashed subdirectories at all. When that happens the reported size is a real undercount, but it is never silently wrong: the result includes `inaccessible_directories` (CLI prints a `WARNING`, the dashboard shows it in red) whenever this happens, so a permission wall doesn't read as "the cache is empty". Turning on `enable_cache_probe` below sidesteps this permission gap entirely (the scan runs inside the nginx worker itself, not as the repowatch service user) — see its own row for what that trades off instead. |
| `enable_purge` | bool | `false` | Actively evict a package's cache entry the moment it disappears from the upstream index, instead of waiting for `inactive`/`max_size` to notice on their own. Requires the third-party `ngx_cache_purge` nginx module (Debian/Ubuntu: `libnginx-mod-http-cache-purge`; Arch: `nginx-mod-cache_purge`) — **not** the nginx-plus `proxy_cache_purge on` API, and not a real `PURGE` HTTP method (nginx core rejects unknown methods outright); the generator instead adds a dedicated, loopback-only `GET /purge<prefix>/...` location per repository. You must separately add `load_module ".../ngx_http_cache_purge_module.so";` to your own main `nginx.conf` — that's a main-context directive the generated file (which lives inside `http{}`/`sites-enabled`) can't emit itself. Without the module loaded, `nginx -t` fails clearly during `nginx-apply` and the usual atomic rollback applies — it doesn't silently do nothing. When it's on, the dashboard's per-repository panel also gets a "Cache purge (stale warmed entries)" section: "Scan for stale entries" computes candidates from repowatch's own records only (no nginx/network call), then "Purge selected" is the only point that actually asks nginx — its 200/404 response IS the "was this cached" answer, so there's no separate non-destructive pre-check (a normal HEAD/GET on a cache miss can fetch and populate the object, so it is not a read-only existence check; nginx normally converts HEAD to GET for caching via [proxy_cache_convert_head](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_cache_convert_head)). If `enable_cache_probe` is *also* on, "Purge selected" and un-warming go through that instead — see its own row below. |
| `enable_dedup` | bool | `false` | When two DIFFERENT repositories publish the byte-identical file (e.g. the same binary package shipped by both Debian and Ubuntu), serve and cache it once instead of twice. Detected from a per-package SHA256 that apt/pacman/dnf/xbps indexes (and Gentoo indexes that advertise SHA256) publish — apk and apt-rpm packages never participate (see below). No extra download or storage of file content is needed; only the already-parsed index metadata is compared. |
| `enable_cache_probe` | bool | `false` | Read-only cache introspection running *inside the nginx worker itself*, via the third-party `ngx_http_js_module` (njs) — Debian/Ubuntu: `libnginx-mod-http-js`; Arch: `nginx-mod-njs`. Same `load_module` caveat as `enable_purge` (a main-context directive the generated file can't emit itself). Solves a real permission problem: repowatch's own unprivileged process cannot read `proxy_cache_path`'s `0700`-owned subdirectories (see `cache_dir`'s own caveat above), but nginx's worker already owns them — so a small njs script running there can. Adds two loopback-only endpoints (`/cache-probe?key=...`: does this exact package's cache entry exist right now — a genuine non-destructive check, unlike asking via purge; `/cache-scan?dir=<a>/<bb>`: list one of the 4096 fixed leaf directories nginx's cache tree always has, together with each file's real on-disk key) and, when `enable_purge` is *also* on, one more: `/purge-raw?key=...` — evicts an arbitrary already-known key regardless of whether any current repository route still exists for it (the only way to clean up a repository removed from `config.yaml` whose files are still on disk). Two concrete effects on other features when this is on: the dashboard's "Calculate cache directory size" / `repowatch stats --cache-dir` use this instead of `os.walk()` — no `cache_dir` needed, no permission undercount (though a single leaf directory can still fail to respond on its own, e.g. a transient timeout; directory I/O errors also return HTTP 500 rather than an empty list; when a scan is partial the result carries `incomplete_leaves`, the same "you can see it's an undercount" signal `inaccessible_directories` gives the os.walk() path, not silence); and the dashboard's "Purge selected" and "Remove from warmed" buttons switch from the per-repository `/purge<prefix>` location to `/purge-raw`, checking both distinct keys obtained with `enable_dedup` on and off. This removes both old and current copies after a dedup toggle; an error for either key preserves the warmed record for retry. Identical keys are requested only once. With dedup enabled, manual purge also checks the canonical key resolved from the current package database, using the same duplicate groups as nginx-apply. Evicting that shared copy affects every repository using it; their next request can populate it again. This describes the current mapping after nginx reconciliation, not historical canonical keys after a mapping change. Changes to upstream, URL templates or `cache_key_version` require identifying the actual old keys separately. The automatic hourly `warmed_retention_days` expiry uses the same backend and key selection. Successful purge removes only the bookkeeping generation selected before the request; a concurrent warm or observed client request preserves its newer record. This protects bookkeeping, but does not make nginx eviction and SQLite updates a single transaction. |

**Purge locations live in a separate included file.** When `enable_purge` is
on, the generated `active.conf` doesn't inline the purge locations among the
per-repository routing — it adds a single `include .../purge.conf;` line
inside the `server {}` block, and `nginx-apply`/`make activate` write that
second file (`purge.conf`, next to `active.conf` in the same root-owned
directory) alongside it, atomically, in the same transaction (both are
validated by one `nginx -t` and rolled back together on failure). This keeps
the routing config that does the actual proxying free of purge/admin-only
clutter regardless of how many optional features like this one exist. If
you're generating the config manually with `nginx-render` (no `--policy`,
no root), the purge locations print as a separate labeled block after the
main config — save it to the path nginx will `include`.

**How dedup works and its limits.** apt, pacman, dnf, and
xbps packages carry a per-package SHA256 in their index (apt's
`SHA256:` field, pacman's `%SHA256SUM%`, dnf's `<checksum type="sha256">`,
xbps's `filename-sha256`). Gentoo also participates when its index advertises
`SHA256`; the inspected official binhost only advertises MD5/SHA1. Slackware's
MD5 manifest is not used for SHA256 deduplication. apk's
`APKINDEX` `C:` field is a different digest (SHA1, base64) that could never
match a real cross-format duplicate, and apt-rpm's binary pkglist doesn't
currently carry a verified whole-file digest; both are deliberately left out
rather than guessed at, since a false match would mean serving one
package's bytes under a different package's name. A match requires both identical `filename` (including its relative path)
and identical SHA256; equal bytes under different filenames are not merged.
When a match is found across two repos, the alphabetically-first `repo_id` is treated as
canonical; every other repo serving that same file gets an internal nginx
rewrite (`map`/`if`/`rewrite ... last`, generated in `dedup.map`, the same
included-sub-config pattern as `purge.conf` above) to the canonical repo's
own content location — so it reuses that repo's real cache entry, TTLs, and
upstream, rather than fetching or storing a second copy. `nginx-apply`
computes the actual pairs from `repo_packages` at apply time (a real SQL
query — skipped entirely when this flag is off, so leaving it off costs
nothing on every 15s apply cycle); `nginx-render` without `--policy` always
shows an empty map, since it deliberately never opens the database. Index
files (`Packages.gz`, `.db.tar.gz`, `repodata/*`, etc.) never participate —
each repository's own index must always reflect its own real state.

**Why `resolvers` is IPv4-only:** the generated config uses a genuinely
*live* `resolver ... valid=300s ipv6=off;` directive together with a
variable-based `proxy_pass` (`proxy_pass https://$some_var;`, not a literal
hostname) — that combination is what makes nginx actually consult
`resolver` and re-resolve the upstream hostname periodically at runtime,
rather than once at startup/reload via the system resolver, which is what
a literal `proxy_pass https://hostname;` would do instead. Plain
open-source nginx handles this on its own. Since nginx 1.27.3, dynamic
resolution with a named `upstream {}` block is also available without
nginx Plus (see the [upstream server documentation](https://nginx.org/en/docs/http/ngx_http_upstream_module.html#server)).
The generator uses variable-based `proxy_pass` and does not configure
upstream keepalive. The `ipv6=off` half of the
directive is the actual point: if your network has no IPv6 route but an
upstream mirror's hostname also resolves to an AAAA record, an IPv6-capable
setup can produce real `Network is unreachable` errors on warm-up — this
sidesteps that at the cost of not using IPv6 upstream peers even when
they'd work.

If you're hand-writing nginx config instead of using the generator, run
`repowatch -c config.yaml nginx-render` (`-c`/`--config` is a top-level
flag and must come before the subcommand, not after) to print what the
generator would produce for your own `repos[]` — a concrete starting point
for the location blocks you'd need to write by hand.

## Fields editable from the dashboard

`GET`/`POST /api/config` exposes a subset of top-level fields for editing
without hand-editing YAML — deliberately not all of them. A field is
included only if changing it takes effect immediately (no restart needed):

`check_interval`, `event_retention_days`, `request_retention_days`,
`warmed_retention_days`, `event_max_rows_per_repo`, `request_max_rows`,
`prefetch_concurrency`, `check_concurrency`, `prefetch_bandwidth_limit`,
`prefetch_bandwidth_timezone`, `prefetch_bandwidth_schedule`,
`cache_base_url`, `public_cache_url`, `notify_after_failures`,
`key_expiry_warning_days`.

Notably **not** editable from the dashboard (edit `config.yaml` by hand;
restart requirements depend on the field):

- `admin_password_hash` — secret, only ever written via
  `repowatch set-password`/`hash-password`, never round-tripped through the
  API.
- `notify_events` — YAML-only event selection, applied to subsequent operations.
- `notify_webhook_url` — secret (the URL is itself a bearer token).
- `state_db` — the SQLite connection path is fixed at process startup;
  changing it needs a restart.
- `syslog_listener` — the UDP socket itself is bound once at startup;
  changing `enabled`/`bind`/`port` needs a restart to actually rebind it.
- `nginx` — not restart-bound, but not instant either: it's reconciled by
  the separate root-owned `nginx-apply` mechanism (a timer, by default
  every 15s — see [deployment.md](deployment.md)), not the main process,
  and not on every dashboard request.
- `status_server` — this ONE actually IS re-read live: every request calls
  `load_config()` fresh, so `guest_read_only`, `trusted_proxies`,
  `token_repo_restrictions`, and `metrics_allowed_networks` all take effect
  on the very next request after a hand-edit to `config.yaml`, no restart
  needed. It's still not dashboard-editable — these are the settings that
  decide who gets ADMIN access at all, so changing them isn't something the
  dashboard's own (already-admin-gated) API exposes, restart or not.
  `bind`, `port`, `tls_cert_path` and `tls_key_path` require a restart:
  the listening socket and TLS context are created once at startup
  (see [access.md](access.md)).

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

Operational integer settings are validated without truncating floats or converting
booleans. In YAML, use integer scalars (`60`, not `"60"` or `60.5`).
`check_interval` (global and per repository), `check_concurrency`,
`prefetch_concurrency`, `notify_after_failures`, and non-null row limits accept
1..2147483647. Retention periods accept 1..365000 days; the upper bound protects
calendar arithmetic. `key_expiry_warning_days` accepts 0..365000. Use `null` to
clear an optional row limit; zero does not disable retention. Repository
`prefetch`/`verify_signature` and `syslog_listener.enabled` require YAML booleans.
The dashboard accepts integer text from form fields, but rejects booleans and
fractional numbers without replacing the existing YAML.

Unreadable files, invalid UTF-8, malformed YAML and invalid configuration values
are reported as configuration errors. A running scheduler keeps its last valid
configuration after a failed reload; startup still requires a valid file.
Authenticated/read API configuration loading fails closed when the current file
is unavailable. The existing `/healthz` exception described above is unchanged.

## Failed operations and cache observations

Automatic warming runs for newly detected package keys. A failed initial attempt is
recorded, but an unchanged index does not schedule a retry of that new package;
use manual warming to retry. Purge triggered by removed packages logs failures
without maintaining a retry queue. Hourly warmed retention is a separate
mechanism, not a guaranteed retry of that removal.

Warmed records describe attempts and observed client requests, not a permanent
guarantee that the file remains on disk. For cache scans, `unreadable_keys`
counts entries that cannot be identified, while `unreadable_sizes` counts
files missing from the byte total because stat failed. A successful stat
preserves the size even when reading the key fails. `incomplete_leaves`
counts whole directories whose scan request failed. Missing directories
(ENOENT) are empty; other filesystem errors are not treated as absence.

The njs scan reads cache-key headers in 4 KiB blocks, stopping at the complete
key line or a hard limit of 64 KiB per file. One 64 KiB buffer is reused per
leaf request; package size no longer determines the read buffer size. Missing,
truncated, or oversized key headers produce an entry error (`unreadable_keys`),
while a successful stat still contributes the file size. This requires njs
0.7.7 or newer for its descriptor-based filesystem API. Filesystem operations
remain synchronous inside nginx workers: a slow NFS operation or a directory
with many entries can still delay client requests.
Same-version replacements are handled separately as described below.

## Same-version package replacements

An existing name-version key is reported as `modified` when its filename
changes, or when a known SHA256 changes to another known SHA256. A hash
becoming known or unavailable is only a metadata update. History includes
`modified_packages` separately from `new_packages` and `removed_packages`;
`changed_at` advances for replacements too.

Replacement work is saved in SQLite in the same transaction as the snapshot.
The previous warmed record is removed because it does not confirm the new
bytes. With `nginx.enable_purge`, the watcher invalidates the old and new
filenames before warming the replacement. When dedup is enabled, recorded
previous canonical locations are invalidated too; this can evict shared copies.
Warming still respects `prefetch` and package bans. If warming is disabled,
successful invalidation completes the operation without downloading a package.
If a pending package disappears from the index, its stored purge targets are
retained for cleanup, with no subsequent warming.

A purge error prevents warming. A download error or SHA256 mismatch also
keeps the operation pending. If the index supplies SHA256, replacement
warming checks the actual response bytes against it; without SHA256 only
successful invalidation and the HTTP download can be checked. Pending work
survives restart and is retried on subsequent successful index checks,
including unchanged HEAD results. A later replacement preserves earlier
purge targets and cannot be cleared by completion of an older operation.
This retry behavior is specific to replacements, not initial warming.

With purge disabled, replacement detection and history still work, but cache
refresh stays pending and no replacement warm is attempted. Enable purge to
allow the watcher to finish invalidation. `pending_replacements` in the
status API and repository list counts unfinished operations; the dashboard
shows a warning. Zero means no tracked replacement remains, not that every
package is currently cached. `/healthz` continues to measure index freshness.
Historical nginx routing changes can leave purge targets that the current
configuration cannot resolve; those remain pending for operator investigation.

Existing databases gain the history field with an empty-list default and a
pending-work table on startup. Back up the database before upgrading. Old
history remains readable; restoring an earlier release should use the matching
pre-upgrade database backup as described in deployment.md.


HTTP index validators are bound to a fingerprint of the repository source and
parser parameters. Changing upstream, architecture, suite, component, URL template
or Nix catalog selection forces a full index fetch on the next scheduled check.
Existing databases without fingerprints also fetch once before reusing validators.
The previous snapshot remains available if that fetch fails. Historical physical
cache keys are not automatically relocated or erased by a source edit; use the
cache inventory to identify old origins before removing their files.

The warm list records observed warming, not every file in nginx. Client requests
can populate nginx independently, UDP access logs can be lost, and a cache may
predate the current database. Indexes and Nix closure artifacts also do not map
one-to-one to warm-list rows. An inventory entry missing from that list is not
by itself proof that the file is stale or safe to purge.
