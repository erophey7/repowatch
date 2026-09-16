# Connecting package-manager clients

These examples assume the cache is reachable at `http://cache.example.net:8080`.
Replace that hostname with yours. Configure the corresponding
[server repositories](repositories.md) first and wait for successful metadata
checks and nginx reconciliation. A cache route does not choose the correct
release or architecture for a client automatically.

Back up the client's source files before editing. Preserve signing keys,
signature policies, components and release selection. To undo a change, restore
the original files and refresh package indexes. Dashboard credentials/host
status tokens are **not** package-manager credentials: package-cache access is
controlled separately at nginx/network level.

## Debian 12/13

For Debian 13 (`trixie`), configure server entries for `trixie`, `trixie-updates`
and `trixie-security`, for every required component and architecture. This
example uses only `main`/`amd64`; keep any other components your system requires.
Back up `/etc/apt/sources.list` and `/etc/apt/sources.list.d`.

Use deb822 entries in `/etc/apt/sources.list.d/debian.sources`:

```text
Types: deb
URIs: http://cache.example.net:8080/debian
Suites: trixie trixie-updates
Components: main
Architectures: amd64
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://cache.example.net:8080/debian-security
Suites: trixie-security
Components: main
Architectures: amd64
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
```

Replace the corresponding original entries rather than leaving duplicate sources
active. For Debian 12, keep `bookworm`, `bookworm-updates`, `bookworm-security`
on both server and client. Source packages (`deb-src`) are not tracked as binary
packages by these entries; leave separate source-package sources as needed.

```sh
sudo apt update
apt-cache policy curl
apt download curl
```

`apt download` writes into the current directory without installing the package.
For deb822 syntax and signing configuration, see
[Debian SourcesList](https://wiki.debian.org/SourcesList).

## Ubuntu 24.04 LTS

For amd64 clients, use `/etc/apt/sources.list.d/ubuntu.sources`. This `main`-only
example matches the three Ubuntu entries in [Repository management](repositories.md#ubuntu-and-ppas):

```text
Types: deb
URIs: http://cache.example.net:8080/ubuntu
Suites: noble noble-updates
Components: main
Architectures: amd64
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: http://cache.example.net:8080/ubuntu-security
Suites: noble-security
Components: main
Architectures: amd64
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
```

Preserve `restricted universe multiverse` if your client uses them, adding matching
server entries first. Treat `noble-backports` similarly. Do not send security
traffic to the archive route. Keep third-party sources and their keys separate.
Use `sudo apt update`, then `apt download curl` to test. Ubuntu documents its
source layout in [Package management](https://ubuntu.com/server/docs/how-to/software/package-management/).

## Arch Linux

Back up `/etc/pacman.d/mirrorlist`. For the repositories configured on the cache,
use this mirror line:

```text
Server = http://cache.example.net:8080/arch/$repo/os/$arch
```

Configure `core` and `extra` on the server; add `multilib` if enabled in
`/etc/pacman.conf`. Keep `SigLevel` and the installed Arch keyring unchanged.
An upstream fallback later in the mirrorlist improves availability, but can
bypass the cache when its route fails; account for that when testing.

Run your normal complete update (`sudo pacman -Syu`) when ready to update the
machine. Avoid refreshing databases and then installing individual packages
without the corresponding system upgrade. To test a small download separately,
`pacman -Sw` downloads without installing, using the current synchronized state.
See [Arch mirror configuration](https://wiki.archlinux.org/title/Mirrors).

## Alpine Linux

Back up `/etc/apk/repositories`. For the Alpine 3.22 server examples:

```text
http://cache.example.net:8080/alpine/v3.22/main
http://cache.example.net:8080/alpine/v3.22/community
```

Keep the client's actual release (for example `v3.22`) consistent with the server;
changing it is a distribution upgrade. The client appends its architecture, so
do not append `/x86_64` here. Preserve `/etc/apk/keys` and package verification.

```sh
apk update
apk fetch curl
```

Run index updates with the privileges required by your installation. `apk fetch`
downloads a package to the current directory. See
[Alpine Package Keeper](https://wiki.alpinelinux.org/wiki/Apk).

## Rocky Linux 9 / RPM-MD clients

Back up `/etc/yum.repos.d`. Edit the existing distribution-provided entries;
retain their `gpgcheck`, `gpgkey`, `enabled`, exclusions and other policy fields.
For Rocky 9 x86_64, replace the selected entries' mirrorlist/metalink with these
base URLs (disable their old `mirrorlist=`/`metalink=` lines):

| Client repository | `baseurl` |
| --- | --- |
| BaseOS | `http://cache.example.net:8080/rpm/rocky9-baseos-x86_64/` |
| AppStream | `http://cache.example.net:8080/rpm/rocky9-appstream-x86_64/` |
| extras | `http://cache.example.net:8080/rpm/rocky9-extras-x86_64/` |

Do not point every repository at BaseOS. Each prefix must match its own server
entry and upstream. Keep `gpgcheck=1`; do not enable metadata `repo_gpgcheck`
unless the selected upstream actually publishes the required metadata signature.

```sh
sudo dnf makecache --refresh
dnf repolist -v
```

Confirm the selected base URLs and perform your usual package transaction when
ready. Fedora/openSUSE use their own keys, repository roots and release/update
entries; the same `/rpm/REPO_ID/` routing applies. Distribution repository files
are described in [Rocky's software management guide](https://docs.rockylinux.org/books/admin_guide/13-softwares/).

## Other formats and status-aware automation

See [Nix clients](nix.md) and [Gentoo/Slackware clients](gentoo-slackware.md).
For other formats, use the generated browse URL as the starting point and retain
the package manager's existing trust configuration. An architecture or ABI
mismatch is not repaired by caching.

For automation that checks whether a repository changed before updating, issue a
host token through the dashboard and follow [Access](access.md). With the shipped
`token_repo_restrictions: true`, select the repositories that host may read.
Never embed an administrator password in a package-manager source URL.

Automatic client detection/configuration is planned as **fast client setup**;
there is no client bootstrap command to run yet.
