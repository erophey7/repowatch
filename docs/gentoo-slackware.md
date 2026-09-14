# Gentoo and Slackware repositories

Both types use the existing watcher, snapshot history, warming, purge and
bandwidth controls. They require no additional Python dependencies. Index
requests go directly to upstream; package warming goes through nginx.

## Gentoo binary packages

```yaml
- id: gentoo-amd64
  type: gentoo
  upstream: https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/x86-64
  arch: amd64
  prefetch: false
```

Point `upstream` at the directory containing `Packages`. Select a binhost for
your architecture, profile and CPU baseline; `arch` does not filter USE flags,
resolve dependencies or select a compatible profile. Start with `prefetch: false`
unless you intend to download the entire catalog on the first check.

The parser reads the plain-text version-0 `Packages` index: a header followed
by blank-line-separated records. `CPV` supplies the category/name and version;
`BUILD_ID`, when present, adds `-build<N>` to the tracked version so different
binary builds cannot overwrite each other. `PATH` locates `.gpkg.tar`, `.xpak`
or `.tbz2` files; older records without it use `<CPV>.tbz2`. Package names in
API filters and bans include the category, for example `app-editors/vim`.

An advertised whole-file `SHA256` enables the existing hash-based deduplication.
MD5 and SHA1 are not substituted for SHA256. The inspected official index
publishes MD5/SHA1 rather than SHA256, so those packages do not participate.
Malformed records, duplicate identities, unsupported index versions, count
mismatches and unsafe paths reject the snapshot instead of dropping entries.
An index `URI` pointing elsewhere is unsupported: all package paths must be
relative to the configured upstream so warming and client requests share it.

The default local binhost URL is `http://CACHE:8080/gentoo/gentoo-amd64/`.
Use it as the `sync-uri` in the client's `/etc/portage/binrepos.conf` entry.
Keep Portage's package signature verification enabled. repowatch rejects
`verify_signature: true` for Gentoo: it does not verify the signatures inside
GPKG containers, and the `Packages` index is not authenticated by those
signatures. A successful warm means the file was fetched, not that Portage
has accepted its signature or compatibility. repowatch does not rewrite
indexes, build packages, cache source distfiles or implement dependency solving.
`Packages` and `Packages.gz` receive nginx's short metadata TTL.

Format references: [Portage's binhost implementation](https://github.com/gentoo/portage/blob/master/lib/portage/dbapi/bintree.py)
and [GLEP 78: binary package container and signatures](https://www.gentoo.org/glep/glep-0078.html).

## Slackware

```yaml
- id: slackware64-15
  type: slackware
  upstream: https://mirrors.kernel.org/slackware/slackware64-15.0
  arch: x86_64
  prefetch: false
  verify_signature: true
  keyring_path: /etc/repowatch/keys/slackware.gpg

- id: slackware64-15-patches
  type: slackware
  upstream: https://mirrors.kernel.org/slackware/slackware64-15.0
  arch: x86_64
  component: patches
  prefetch: false
  verify_signature: true
  keyring_path: /etc/repowatch/keys/slackware.gpg
```

Provision an exported, trusted GPG keyring before enabling verification; the
application does not download or trust keys automatically. `gpgv` is required
for verification, as for the existing GPG-backed repository types.

`upstream` is the release root on a concrete mirror. Avoid redirector URLs
such as `mirrors.slackware.com`: the watcher does not follow their redirects,
and nginx would forward redirects to clients, bypassing the intended cache.
Select a reachable mirror and keep signature verification enabled. An omitted `component` reads its `PACKAGES.TXT`;
`patches`, `extra`, `pasture` or `testing` reads `<component>/PACKAGES.TXT`.
These are separate snapshots: the main index does not include every component.
Do not change the patches upstream to end in `/patches`: its package locations
are already relative to the release root, e.g. `./patches/packages`.

Records contain `PACKAGE NAME`, `PACKAGE LOCATION`, sizes and description.
The filename is split from the right into name/version/architecture/build;
architecture and build remain in the tracked version. Exact `arch`, `noarch`
and `fw` packages are retained. `.tgz`, `.txz`, `.tlz` and `.tbz` are accepted.
Package paths come from the index; the application does not unpack packages.

With `verify_signature: true`, repowatch downloads the root `CHECKSUMS.md5`
and `CHECKSUMS.md5.asc`, verifies the detached signature with `gpgv`, then
checks the exact index bytes against their entry in the signed manifest.
Missing, duplicate or mismatched entries fail the check. Signed indexes are
reverified even after unchanged HEAD responses. A failed check preserves the
previous snapshot and uses the existing signature failure notification flow.

This follows Slackware's published MD5 manifest and inherits MD5's collision
weakness; it is not equivalent to a signed SHA256 index. repowatch does not
use those MD5 values as SHA256 deduplication hashes or verify individual
package signatures during warming. Keep the client's GPG checks enabled.
With verification disabled, no checksum manifest is fetched or verified.

Use `http://CACHE:8080/slackware/slackware64-15/` as the client's mirror root.
That nginx route proxies the whole release tree, including patches and signature
sidecars requested by the client. A separate patches watcher warms through
its own default prefix; to share the same cache URLs, set the same
`url_template` on both entries (same upstream and type required), for example
`url_template: /slackware/15.0`. Indexes, checksums, file lists, changelog,
manifest and the upstream public-key file receive the short metadata TTL;
package archives keep the package TTL. Warming does not eagerly fetch every
client metadata file or signature sidecar.

References: [Slackware release tree](https://mirrors.slackware.com/slackware/slackware64-15.0/),
[main index](https://mirrors.slackware.com/slackware/slackware64-15.0/PACKAGES.TXT)
and [signed checksum manifest](https://mirrors.slackware.com/slackware/slackware64-15.0/CHECKSUMS.md5).
