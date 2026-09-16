# Quick start

Install the cache server once, then point package-manager clients at it. The
server OS does not restrict the repository formats it can cache. Start with the
repositories actually used by your clients; a new installation has `repos: []`
and contacts no package upstream until you add one.

## 1. Prepare the server

Use a source checkout and run the Make commands below from its root. Python
3.11 or later is required. Install nginx and its **matching** cache-purge and
njs HTTP modules for the shipped full-feature configuration. Installing a module
package and loading that module into nginx are separate requirements.

### Debian 12/13 and Ubuntu 24.04

```sh
sudo apt update
sudo apt install make python3 python3-venv python3-pip nginx \
  libnginx-mod-http-cache-purge libnginx-mod-http-js \
  gpg gpgv openssl zstd sqlite3 ca-certificates curl
```

Ubuntu module packages are in `universe`; enable that distribution component if
APT cannot locate them. Use packages from the same distribution repository as
nginx. See the official [Debian packages](https://packages.debian.org/stable/httpd/)
and Ubuntu [purge](https://packages.ubuntu.com/noble/libnginx-mod-http-cache-purge)
and [njs](https://packages.ubuntu.com/noble/libnginx-mod-http-js) packages.

Distribution packages normally load modules through `/etc/nginx/modules-enabled/`.
Check that your main nginx configuration includes them. For the common installation
below, use `NGINX_ENABLED_DIR=/etc/nginx/sites-enabled` if auto-detection reports
multiple possible HTTP include directories.

### Arch Linux

```sh
sudo pacman -Syu --needed make python python-pip nginx nginx-mod-cache_purge \
  nginx-mod-njs gnupg openssl zstd sqlite ca-certificates curl
```

Check `/etc/nginx/nginx.conf`: module configuration must be included in the
**main context**, before `events`/`http`, and generated sites inside `http`:

```nginx
include /etc/nginx/modules.d/*.conf;
# Inside the existing http { ... } block:
# include /etc/nginx/conf.d/*.conf;
```

Create `/etc/nginx/conf.d` if necessary and add the uncommented site include
inside the existing `http` block. Do not add a second `http` block. Use
`NGINX_ENABLED_DIR=/etc/nginx/conf.d` for installation. Module package layouts:
[purge](https://archlinux.org/packages/extra/x86_64/nginx-mod-cache_purge/files/),
[njs](https://archlinux.org/packages/extra/x86_64/nginx-mod-njs/files/).

### Fedora and Rocky Linux 9

For Fedora:

```sh
sudo dnf install make python3 python3-pip nginx gnupg2 openssl zstd sqlite curl
```

For Rocky Linux 9, install a newer Python alongside the system Python:

```sh
sudo dnf install make python3.11 python3.11-pip nginx gnupg2 openssl zstd sqlite curl
```

Pass `PYTHON=python3.11` to **every** Make command on Rocky 9. Its system Python
3.9 is too old; Python 3.11 is available in AppStream. See the official
[Rocky 9 release notes](https://docs.rockylinux.org/releases/release_notes/9_0/)
and [AppStream package directory](https://dl.rockylinux.org/pub/rocky/9/AppStream/x86_64/os/Packages/p/).

The commands above do not supply cache-purge and njs. Obtain modules built for
that exact nginx package before activating the full profile. There is no automatic
module installation or silent fallback. If suitable modules are unavailable,
explicitly set `nginx.enable_purge: false` and `nginx.enable_cache_probe: false`
in the installed YAML before activation. This loses purge/inventory features;
replacement refresh stays pending without purge. Deduplication does not require
these modules. See the [nginx reference](configuration.md#nginx).

Use `/etc/nginx/conf.d` as the HTTP include directory. Keep SELinux enabled and
adjust the host's nginx port, network and file-access policy for your chosen
layout; an AVC denial is a host policy issue, not an upstream index error.

### Alpine Linux 3.22 (without systemd)

As root, install prerequisites:

```sh
apk add make python3 py3-pip nginx nginx-mod-http-cache-purge nginx-mod-http-js \
  gnupg openssl zstd sqlite shadow bash ca-certificates curl
```

`shadow` supplies the user/group management tools used by activation. Verify
`python3 -m venv /tmp/repowatch-venv-check` succeeds, then remove that test directory.
Check module loading and the HTTP include `/etc/nginx/http.d` in the installed
nginx configuration. Package availability is listed in the official
[Alpine 3.22 repository](https://dl-cdn.alpinelinux.org/alpine/v3.22/main/x86_64/);
see also [Alpine nginx setup](https://wiki.alpinelinux.org/wiki/Nginx).

Use `WITH_SYSTEMD=0 NGINX_ENABLED_DIR=/etc/nginx/http.d` with Make commands.
Activation installs configuration but does not supervise processes or start nginx.
Use OpenRC to start nginx, and follow [Running without systemd](deployment.md#running-without-systemd)
for the application, backups and nginx reconciliation. The systemd commands in
the next sections apply only to systemd hosts.

## 2. Install and activate

```sh
sudo make check
sudo make install PREFIX=/usr/local
sudoedit /etc/repowatch/config.yaml
sudo /usr/local/bin/repowatch check-config
sudo make activate
sudo -u repowatch /usr/local/bin/repowatch set-password
```

Run the diagnostic with the installation user's privileges: an unprivileged
`make check` against `/etc` and `/usr/local` correctly reports unwritable paths.
It remains a read-only check even when invoked through sudo.

Carry any variables from the OS-specific instructions through each Make command.
`install` writes files; `activate` creates the service account and starts services
on systemd hosts. Existing YAML and state are preserved on reinstallation; an
upgrade does **not** opt an existing installation into new defaults.

Before activation, review:

- `repos: []`: leave empty until you choose the first source.
- `public_cache_url`: set your reachable LAN URL, for example
  `http://cache.example.net:8080`. Keep `cache_base_url` as the server's local URL.
- nginx DNS resolvers, cache size (`100g`) and storage capacity. This size is a
  cache-manager target, not a hard disk quota or preallocated disk reservation.
- Warming budget: 10 MiB/s per application process, four downloads per warm run,
  four concurrent index checks, with a 15-minute default check interval. These
  limits do not cap client downloads or metadata traffic.
- All nginx features and syslog tracking are enabled. Trusted signing keys,
  TLS certificates, webhook destinations and optional Nix are site-specific;
  configure those when needed rather than inserting placeholder credentials.

`make check` checks installation prerequisites but does not prove nginx module
compatibility. Activation runs `nginx -t`; an unknown `proxy_cache_purge` or
`js_*` directive means a required module is missing or unloaded. Resolve it
before starting the full profile. See [deployment](deployment.md) for custom paths,
backups and upgrades.

## 3. Open the dashboard and add a source

The shipped status server binds to `127.0.0.1:8085`, with guest access disabled.
From your workstation, open an SSH tunnel (replace `user@cache-server`):

```sh
ssh -N -L 8085:127.0.0.1:8085 user@cache-server
```

Open `http://127.0.0.1:8085`, log in with the password set above, and choose
**+ Add repository**. Follow [Repository management](repositories.md) for a first
source, bounded warming, YAML alternatives and format examples. An empty
dashboard is normal; `/healthz` can be healthy with zero repositories.

For shared dashboard access, configure HTTPS and authentication as described in
[Access](access.md), rather than enabling plaintext remote password login.
The package cache on port 8080 has separate access rules: dashboard authentication
does not protect package downloads. Restrict that port to the intended clients.

## 4. Connect a client and check the result

Follow [Client configuration](clients.md) for your distribution. Configure every
suite/component/architecture that client uses. After saving a repository, allow
for the index check and nginx reconciliation (the systemd timer runs every 15s).
A successful save alone does not prove that the upstream is reachable.

```sh
systemctl status repowatch.service repowatch-nginx.timer
journalctl -u repowatch.service -u repowatch-nginx.service -n 80 --no-pager
curl --fail http://127.0.0.1:8085/healthz
```

Check the repository's last successful check and package count in the dashboard,
then refresh the client's package indexes and download a small package. Requests
should appear in the dashboard when syslog tracking is running. A repeated
cacheable GET can become a HIT; HEAD requests and mutable-index expiry are not
proof that package bytes have been warmed.

For a cache 404, check the generated URL, configured repositories and nginx
reconciliation logs. For a 502, check upstream reachability and DNS from nginx.
For signature failures, check the selected release, keyring and service-user read
permissions; do not disable client signature verification to make a test pass.
