# Adding and managing repositories

A fresh installation contains `repos: []`. This is valid: the dashboard, status
API and administration remain available, but no upstream indexes or packages
are fetched. Deleting the last repository returns to this state.

One entry describes one index: usually a release/component/architecture
combination. Give each a unique, stable `id`. The client-facing URL follows the
format's routing rules; it is not necessarily the repository ID. The server OS
and client OS need not match.

## Through the dashboard

1. [Set the administrator password and log in](quick-start.md#3-open-the-dashboard-and-add-a-source).
2. Choose **+ Add repository**. Select a type and enter the ID, upstream and
   architecture. Fill in the format-specific fields from the examples below.
3. Choose a group for display, if useful. It does not change routing or security.
4. Decide whether to enable **warm the cache (prefetch)**. It is selected in
   the form by default. An empty warming whitelist allows **all** packages;
   for a first experiment, uncheck prefetch or enter a small whitelist such as
   `curl, wget`. Package names differ between distributions.
5. Save. The operation validates and persists the YAML. The daemon reloads it;
   automatic nginx reconciliation separately validates and applies routing.
6. Check the next index result before pointing clients at the new route.

Use the repository's edit action to change its upstream, interval, warming
policy, group or other supported fields. The ID is fixed while editing. An
upstream/architecture/index-selection change forces a fresh metadata fetch on
the next scheduled check; the previous snapshot remains if it fails.

Delete removes the configuration entry. It does **not** erase SQLite history or
physical nginx cache files. If reclaiming disk is the goal, inspect and purge
cache contents while the old route is still configured, then delete the entry.
Historical cache keys can survive source/route changes. Disabling prefetch also
does not purge existing files or disable client downloads.

Dashboard edits and YAML edits address the **same file**. Dashboard saves use
atomic replacement and may reformat YAML/remove comments. Keep your own backup;
avoid simultaneous edits through an editor and the dashboard.

## Through YAML

Start from the installed full configuration. Replace its `repos: []` with a
`repos:` list, for example:

```yaml
repos:
  - id: debian-trixie-main-amd64
    type: apt
    upstream: https://deb.debian.org/debian
    distribution: trixie
    component: main
    arch: amd64
    group: debian-trixie
    prefetch: true
    prefetch_whitelist: [curl, wget]
    prefetch_blacklist: []
```

This is a snippet, not a complete configuration: preserve `state_db`,
`cache_base_url` and your other top-level settings. Back up the existing file,
edit, then validate:

```sh
sudo cp -a /etc/repowatch/config.yaml /etc/repowatch/config.yaml.before-repos
sudoedit /etc/repowatch/config.yaml
sudo -u repowatch /usr/local/bin/repowatch check-config
sudo -u repowatch /usr/local/bin/repowatch nginx-render
```

`check-config` validates local settings without fetching indexes or creating a
state database. `nginx-render` prints the candidate; it does not apply it. On a
standard systemd installation the daemon reloads YAML and the nginx timer
applies routing. For custom/no-systemd supervision, arrange reconciliation as
explained in [deployment](deployment.md). Restore the backup if validation fails.
To remove all sources, use `repos: []`, not `repos: null`.

## Repository examples

Each block below is a **list fragment to append under `repos:`**. Use only the
sources/releases your clients need. Examples use `prefetch: false` to make
initial metadata validation independent of bulk package downloads. Enable
warming after checking [warming policy](warming-policy.md).

### Arch Linux

```yaml
- id: arch-core-x86_64
  type: pacman
  upstream: https://geo.mirror.pkgbuild.com/core/os/x86_64
  repo_name: core
  arch: x86_64
  prefetch: false
- id: arch-extra-x86_64
  type: pacman
  upstream: https://geo.mirror.pkgbuild.com/extra/os/x86_64
  repo_name: extra
  arch: x86_64
  prefetch: false
```

Add a matching `multilib` entry if the client enables it. The upstream is the
repository directory, not the mirror root. Client prefix: `/arch/$repo/os/$arch`.
Use the distribution's archive signing keyring for server-side verification if
your chosen mirror publishes the detached database signature required by the
parser. Client package signature checks remain enabled independently.

### Debian

```yaml
- id: debian-trixie-updates-main-amd64
  type: apt
  upstream: https://deb.debian.org/debian
  distribution: trixie-updates
  component: main
  arch: amd64
  prefetch: false
- id: debian-trixie-security-main-amd64
  type: apt
  upstream: https://security.debian.org/debian-security
  distribution: trixie-security
  component: main
  arch: amd64
  prefetch: false
```

Together with the first example, these cover `main` for three suites. Add one
entry for every additional component (`contrib`, `non-free`,
`non-free-firmware`) and architecture your clients use. Client bases:
`/debian` and `/debian-security`. For Debian 12, consistently substitute
`bookworm` for `trixie`; do not silently change a client's release.

For server-side APT signature verification, provision a trusted, readable
Debian archive keyring and add these fields to **each** entry:

```yaml
verify_signature: true
keyring_path: /usr/share/keyrings/debian-archive-keyring.gpg
```

That path is conventional on Debian; on another server OS provision the same
trusted key material explicitly. A client keyring is not automatically copied
to the cache server. Key expiry warnings additionally need the `gpg` CLI.

### Ubuntu and PPAs

```yaml
- id: ubuntu-noble-main-amd64
  type: apt
  upstream: https://archive.ubuntu.com/ubuntu
  distribution: noble
  component: main
  arch: amd64
  prefetch: false
- id: ubuntu-noble-updates-main-amd64
  type: apt
  upstream: https://archive.ubuntu.com/ubuntu
  distribution: noble-updates
  component: main
  arch: amd64
  prefetch: false
- id: ubuntu-noble-security-main-amd64
  type: apt
  upstream: https://security.ubuntu.com/ubuntu
  distribution: noble-security
  component: main
  arch: amd64
  prefetch: false
```

Repeat these entries for `restricted`, `universe` and `multiverse` if enabled on
clients. Base/updates use `/ubuntu`; security uses **`/ubuntu-security`**.
The conventional server keyring is
`/usr/share/keyrings/ubuntu-archive-keyring.gpg`, if installed and trusted.
ARM/other Ubuntu ports need their actual mirror/layout rather than replacing
`amd64` in an archive.ubuntu.com example blindly.

A Launchpad PPA is also `type: apt`: use
`https://ppa.launchpadcontent.net/OWNER/PPA/ubuntu`, the PPA's supported suite,
`component: main`, and its own verified keyring. The default local prefix is
`/ppa-OWNER-PPA/`. Replace OWNER/PPA with real values; adding a PPA is a trust
decision, not part of the default installation.

### Alpine Linux

```yaml
- id: alpine-322-main-x86_64
  type: apk
  upstream: https://dl-cdn.alpinelinux.org/alpine/v3.22/main
  arch: x86_64
  prefetch: false
- id: alpine-322-community-x86_64
  type: apk
  upstream: https://dl-cdn.alpinelinux.org/alpine/v3.22/community
  arch: x86_64
  prefetch: false
```

Upstream excludes the architecture; the parser appends it. Local bases are
`/alpine/v3.22/main` and `/alpine/v3.22/community`. Server-side verification
uses `verify_signature: true`, `apk_signature_backend: openssl` (or `apk-tools`)
and `apk_keys_dir` pointing to trusted Alpine public keys. Do not use `gpgv` for
APK signatures; see [configuration](configuration.md).

### Rocky Linux / other RPM-MD sources

```yaml
- id: rocky9-baseos-x86_64
  type: dnf
  upstream: https://dl.rockylinux.org/pub/rocky/9/BaseOS/x86_64/os
  arch: x86_64
  prefetch: false
- id: rocky9-appstream-x86_64
  type: dnf
  upstream: https://dl.rockylinux.org/pub/rocky/9/AppStream/x86_64/os
  arch: x86_64
  prefetch: false
- id: rocky9-extras-x86_64
  type: dnf
  upstream: https://dl.rockylinux.org/pub/rocky/9/extras/x86_64/os
  arch: x86_64
  prefetch: false
```

The upstream must directly contain `repodata/repomd.xml`; a mirrorlist/metalink
URL is not a repository root. Local base: `/rpm/REPO_ID/`. Fedora and openSUSE
use the same parser with their actual release, updates and architecture roots.
Zstandard metadata needs the system `zstd` binary. Server-side metadata signature
verification needs a published `repomd.xml.asc` and trusted GPG keyring; RPM
package verification on the client is separate and must remain enabled.

### Void Linux

```yaml
- id: void-current-x86_64
  type: xbps
  upstream: https://repo-default.voidlinux.org/current
  arch: x86_64
  prefetch: false
```

Local base: `/xbps/void-current-x86_64/`. Install `zstd` on the server.
Musl clients need the matching `/current/musl` source and `x86_64-musl`
architecture; do not mix ABI families. Server-side `verify_signature: true`
is not supported for XBPS; keep the client's native verification enabled.

### Nix, Gentoo, Slackware and ALT Linux

[Nix](nix.md) covers optional CLI installation, service-user store access,
source tarballs, selected attributes, signatures and closure warming.
[Gentoo and Slackware](gentoo-slackware.md) covers binhost/release roots,
components and client configuration. ALT uses `type: apt-rpm`, not `dnf`;
its layout and fields are described in the [configuration reference](configuration.md#repos).

## Warming, cache contents and verification

A whitelist filters package names for background/manual warming. It is neither
a download access list nor a complete inventory of nginx files. Clients can
cache other packages, metadata is cached too, and UDP request logs can be lost.
Use physical cache inventory when investigating disk usage.

Enabling prefetch with an unrestricted whitelist can queue an entire initial
catalog. Start with a few names, review bandwidth schedules, then expand.
Changing a whitelist alone does not replay all unchanged packages: use a manual
warm action for packages already in the snapshot. Blacklists and exact bans
still apply. [Warming policy](warming-policy.md) describes precedence and retries.

After adding or editing a source, validate the configuration, inspect the last
check/error, confirm nginx reconciliation, and test the generated client URL.
A successful YAML save proves neither upstream availability nor signing trust.
