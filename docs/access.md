# Access control

repowatch's HTTP server (`repowatch run` / `serve-status`) exposes three
different kinds of access, each with its own credential:

1. **The dashboard and its administrative API** — a single admin password.
2. **`status.json` only** (the global one and `/status/<repo_id>.json`),
   meant for automated clients (hosts polling before `pacman -Syu`/`apt
   update`/`apk update`) — per-host bearer tokens. History, packages, and
   everything else the dashboard shows are administrative data, gated by
   the admin session in (1), not by a bearer token — a host token cannot
   read them (verified: `/status.json` returns `200`, `/status/<id>/history`
   and `/api/repos/<id>/packages` return `401` for the same token).
3. **An optional, fully anonymous read-only mode** (`guest_read_only`) —
   for internal networks where even a shared token is more friction than
   you want. This is what actually opens history/packages/warmed/bans for
   unauthenticated reading, independent of bearer tokens entirely — see
   [Guest read-only mode](#guest-read-only-mode) below.

There's no framework underneath this — it's `http.server` plus a small
amount of hand-written session/token/CSRF logic (`auth.py`, `access.py`).
This document describes the resulting behavior; see those two files for the
implementation.

- [Admin login](#admin-login)
- [Host tokens (`status.json` clients)](#host-tokens-statusjson-clients)
- [Versioned status API (`/api/v1/...`)](#versioned-status-api-apiv1)
- [Guest read-only mode](#guest-read-only-mode)
- [TLS and `allow_insecure_http`](#tls-and-allow_insecure_http)
- [Reverse proxies and `trusted_proxies`](#reverse-proxies-and-trusted_proxies)
- [`/metrics` and `/healthz`](#metrics-and-healthz)
- [What's deliberately not there](#whats-deliberately-not-there)

## Admin login

There is exactly one administrator password, hashed with PBKDF2-HMAC-SHA256
(600,000 iterations, matching OWASP's current Password Storage Cheat Sheet
recommendation for that algorithm as of this writing, for newly generated
hashes) and stored as
`admin_password_hash` in `config.yaml`. The plaintext password itself is
never stored anywhere. Existing hashes retain their original iteration count
until the password is set again. Set it with:

```bash
repowatch hash-password      # prints a hash to paste into config.yaml
repowatch set-password       # changes it in place, also revokes existing sessions
```

Until `admin_password_hash` is set, the dashboard and every administrative
route are closed (there's no "no password configured = wide open" mode).

**Sessions.** `POST /api/auth/login` with the password creates a 12-hour
session, stored in `state_db`, identified by an `HttpOnly` cookie
(`repowatch_session`; `Secure` is set whenever the connection is HTTPS —
see [TLS](#tls-and-allow_insecure_http)). The cookie carries an opaque
random secret, not the password or a JWT — the server looks it up in SQLite
on every request and checks it against the current password's fingerprint,
so changing the password (or running `set-password`) invalidates every
existing session immediately, without needing to enumerate or delete them.

**CSRF.** Every state-changing (`POST`) admin request must also include an
`X-CSRF-Token` header matching a per-session token, obtained from
`GET /api/auth/session`. The cookie alone isn't enough to make a change —
this is what stops a malicious page the admin happens to have open in
another tab from silently POSTing to the dashboard using the ambient
session cookie. `POST` requests are additionally checked for same-origin
(`Origin`/`Sec-Fetch-Site`) as a second, independent layer.

**Login itself requires HTTPS** — either terminated by the server itself,
by a `trusted_proxies`-listed reverse proxy declaring `X-Forwarded-Proto:
https`, or by `allow_insecure_http` as an explicit opt-out (see
[TLS](#tls-and-allow_insecure_http) for all three) — with one standing
exception: a connection arriving *directly* on loopback (no forwarding
headers, not through a proxy) is always allowed to use credentials over
plain HTTP, on the reasoning that the server can be certain there's no
untrusted network hop between it and itself. That's what lets
`curl http://127.0.0.1:.../api/auth/login` work out of the box on the same
host repowatch runs on, without setting anything.

## Host tokens (`status.json` clients)

The machines that actually poll `status.json` before running their package
manager don't get the admin password — they get a separate, revocable
**host token**, issued from the dashboard (`POST /api/tokens`, admin-only)
or its API:

- The token itself (`rw_...`, a random secret) is shown **once**, at issue
  time — only its SHA-256 hash is stored in `state_db`. If you lose it,
  revoke it and issue a new one.
- Tokens can optionally expire (`expires_at`) and can be named freely, so
  you can tell "the token on `db-primary`" apart from "the token on
  `web-03`" in the token list.
- Revocation (`POST /api/tokens/<id>/revoke`) is checked on every request
  against SQLite — never cached in a running process — so a revoked token
  stops working immediately, not "after the next restart".
- A client authenticates with a standard `Authorization: Bearer <token>`
  header against `status.json` (the global one and `/status/<repo_id>.json`)
  — that's the only thing a bearer token unlocks; see below.

**Scoping tokens to specific repositories.** By default a token can read
status for every repository. If you want a host to only be able to see
repositories it's actually supposed to use, turn on
`status_server.token_repo_restrictions: true` in `config.yaml`, then issue
new tokens with an explicit `repo_ids` list. This is opt-in and
non-retroactive: tokens issued before you turn the setting on (or issued
without a `repo_ids` list afterward) keep unrestricted access — turning the
setting on doesn't quietly narrow anything that already exists. A scoped
token gets a `403` for a repository outside its list, and its
`GET /status.json` response is filtered down to only the repositories it's
allowed to see, rather than erroring.

Note what host tokens *don't* grant: they unlock `status.json` specifically
and nothing else — not the per-repository history endpoint
(`/status/<repo_id>/history`), not `/api/repos/<repo_id>/packages`, not
`warmed`/`bans`, none of it. Those are administrative reads and require
either the admin session below or `guest_read_only` (see [Guest read-only
mode](#guest-read-only-mode)) — a bearer token gets neither. Tokens also
cannot log into the dashboard, trigger a warm-up, add a repository, or
change any config — those all require the admin session and CSRF token
above.

## Versioned status API (`/api/v1/...`)

Every route a host client actually needs — `status.json`, per-repository
status, per-repository history, `/healthz`, `/metrics` — is also reachable
under an `/api/v1/` prefix, with identical behavior and the same
authentication rules described above:

```
GET /api/v1/status.json
GET /api/v1/status/<repo_id>.json
GET /api/v1/status/<repo_id>/history
GET /api/v1/healthz
GET /api/v1/metrics
```

Today `/api/v1/...` and the unversioned paths above are the same thing —
`/api/v1/` is a stable alias, not a different implementation. The point of
having it is forward-looking: if this specific client-facing contract ever
needs an incompatible change, that change lands in `/api/v2/...` instead of
breaking what's already polling `/api/v1/...` (or the unversioned routes,
which keep working as a permanent alias of `v1`). New integrations should
prefer the `/api/v1/` form for that reason, but nothing currently pointed at
the unversioned paths needs to change.

This versioning applies only to this client-facing status API. The
dashboard's own API (`/api/repos`, `/api/config`, `/api/tokens`, and so on)
is intentionally **not** versioned — it's an implementation detail of the
bundled dashboard, released and upgraded together with it, not a contract
promised to outside consumers.

## Guest read-only mode

`status_server.guest_read_only: true` (default `false`) opens the dashboard
and a fixed, explicit set of read routes — status, per-repo history,
packages, warmed-package lists, ban lists, request statistics, the safe
config fields — to anyone, with no login and no token at all. It's meant for
trusted internal networks where even distributing a token is unnecessary
friction, not for exposing repowatch to the open internet.

What it does **not** do:

- It's an *allowlist* of specific GET routes, not "every GET route" —
  `/api/tokens` (the list of issued host tokens) stays admin-only regardless,
  since that list is itself sensitive.
- Nothing that changes state is affected — every `POST` route still requires
  a real admin session and CSRF token. A guest can watch the dashboard
  render but can't add a repository, trigger a warm-up, or edit anything;
  the write buttons are hidden in the UI, and the server enforces the same
  restriction independently if you bypass the UI and call the API directly.
- `/metrics` has its own separate IP-based rule (see below) — guest mode
  doesn't widen or narrow it.
- Turning `guest_read_only` back off closes anonymous access on the very
  next request; nothing is cached that would keep it open.

If you enable this, treat `status.json` itself as public information for
anyone who can reach the port — it's the explicit tradeoff you're making.

## TLS and `allow_insecure_http`

The built-in server can terminate TLS itself
(`status_server.tls_cert_path`/`tls_key_path` in `config.yaml`, see
[configuration.md](configuration.md)) or you can put a reverse proxy in
front of it and leave those unset. Either way, anything credentialed —
admin login/session, CSRF, and Bearer-token status requests — is refused
over a connection the server can't otherwise vouch for. Concretely, one of
the following three has to be true:

1. The connection to repowatch itself uses TLS. For a proxy this requires
   an HTTPS backend connection to the built-in TLS server; terminating TLS
   at the proxy and forwarding plain HTTP instead uses rule 2.
2. It arrives through a **`trusted_proxies`-listed** peer that declares
   `X-Forwarded-Proto: https` — this is real, live-checked support for a
   TLS-terminating reverse proxy, not something you need
   `allow_insecure_http` for. An UNLISTED peer's `X-Forwarded-Proto` header
   is never trusted for this (see the next section for why).
3. It arrives **directly on loopback** — no forwarding headers, not routed
   through any proxy. The server can be certain there's no untrusted
   network hop between it and itself in this specific case, so this is
   always allowed regardless of `allow_insecure_http`. This is what lets
   `curl http://127.0.0.1:.../api/auth/login` work on the same host
   out of the box, with nothing configured.

If none of those three hold — a REMOTE, un-proxied, plain-HTTP
connection — `status_server.allow_insecure_http: true` is the explicit
opt-out: set it if you deliberately want to run that way (e.g. a
network you already trust end-to-end without TLS). Passwords and tokens
travel unencrypted on the wire whenever this is what's actually in effect
— it's a conscious tradeoff you're opting into, not a default.

`GET /login` (the login page itself, not the dashboard) and `GET /healthz`
are reachable over plain HTTP unconditionally, from anywhere (there's
nothing secret in either). `GET /`/`GET /dashboard` are NOT unconditionally
reachable, though: without an admin session (or a satisfied
`guest_read_only`), they respond with a `303` redirect to `/login` rather
than serving the dashboard's actual HTML — so "the dashboard's static HTML
shell is public" isn't quite accurate; what's public is the separate login
page you get redirected to.

## Reverse proxies and `trusted_proxies`

If you run repowatch behind a reverse proxy, the proxy's own address is what
the server sees as the "client" by default — which matters for
`metrics_allowed_networks` (below) and for what's logged as the requesting
IP. List your proxy's address(es) in `status_server.trusted_proxies` (see
[configuration.md](configuration.md)) to have `X-Forwarded-For` /
`X-Forwarded-Proto` / `X-Forwarded-Host` from that peer honored instead.

This is deliberately conservative: a peer that isn't in `trusted_proxies`
can't just claim to be forwarding for someone else by sending those headers
itself — they're only honored from peers you've explicitly named. An
unconfigured, non-loopback peer sending forwarding headers is treated as
suspicious input, not trusted data.

## `/metrics` and `/healthz`

- `GET /metrics` (Prometheus text format) is restricted by client network,
  not by password or token — `status_server.metrics_allowed_networks`
  (default: loopback only). Widen it to your monitoring subnet if
  Prometheus scrapes from somewhere else. This is deliberately a different
  mechanism from the admin/token/guest system above: a scraper is a
  different kind of client than a browser or a package-manager host, and
  tying it to the admin password or a host token would mean either sharing
  admin credentials with your monitoring stack or teaching every scraped
  target's token about metrics it has nothing to do with.
- `GET /healthz` is completely open (a single `{"healthy": true/false}`,
  200/503) — it's meant for load balancers and process supervisors, and
  reveals nothing beyond "is this repowatch instance keeping up with its
  check schedule".

## What's deliberately not there

- **No per-IP or per-token rate limiting on reads.** Read access (status API,
  dashboard, packages/history) is not rate-limited by client IP. A single
  address can legitimately represent an entire NAT'd network or reverse
  proxy fronting many real clients, so an IP-based quota would punish
  shared infrastructure, not abuse. If you need to defend against actual
  abusive traffic, do it at the reverse proxy / firewall layer, which has
  better visibility into real client identity than repowatch does.
- **No OAuth/SSO/multi-user accounts.** There's one administrator password,
  not a user database — this is a small self-hosted tool for a small
  operations team, not a multi-tenant service. If you need per-person
  audit trails for admin actions, that's out of scope today.
