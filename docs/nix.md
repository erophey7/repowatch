# Nix repositories

Nix support uses the optional system Nix CLI to discover package outputs from
a source tarball. nginx caches binary objects from a separate binary-cache
upstream. Other repository types do not require Nix. No Python dependencies
are added.

## Setup

Install Nix with `nix`, `nix-env`, and `nix-instantiate` on the service's PATH.
The service account must be able to use the Nix store or daemon, including
adding source trees and evaluation results. `check-config` checks executable
availability; it does not prove daemon permissions or evaluate a source.
The standard repowatch installer and base container image do not install Nix.
The experimental container build can explicitly include it; see [Docker builds](docker.md).
Native integration tests exercise Nix 2.24.11; production tests also passed
with Ubuntu's Nix 2.18.1 daemon.

For the supplied systemd unit, prefer a multi-user Nix daemon: the unit uses
`ProtectSystem=strict` and cannot write directly to `/nix`. If Nix executables
are installed under `/nix/var/nix/profiles/default/bin`, add that directory to
the unit's PATH using a systemd override. A single-user Nix installation needs
an explicit writable store override as well; repowatch does not weaken the
unit's sandbox automatically. `ProtectHome=true` also hides user-home profiles. Nix may
inspect the account's home even with user config disabled. Give the dedicated
service account a home accessible inside the sandbox, such as its existing
`/var/lib/repowatch` state directory. On Ubuntu, membership in `nix-users`
provides access to the daemon socket; restart repowatch after changing groups
or the account home. Keep `ProtectHome` and `ProtectSystem` enabled.

```yaml
repos:
  - id: nix-unstable
    type: nix
    upstream: https://cache.nixos.org
    arch: x86_64-linux
    nix_source: https://channels.nixos.org/nixos-unstable/nixexprs.tar.xz
    nix_attributes: [hello, jq]
    verify_signature: true
    nix_public_keys:
      - "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
    prefetch: true
```

Use the generated `/nix/nix-unstable/` route as a client substituter, for
example `http://cache.example:8080/nix/nix-unstable`. Keep the upstream's
trusted public key in the client's Nix configuration and retain client
signature checks. repowatch forwards the original metadata and NAR bytes.

`nix_source` accepts an HTTP(S) tarball whose extracted root is evaluable by
`nix-env --query --available`; a nixpkgs channel is the usual source. It is
separate from `upstream`, which serves `nix-cache-info`, `.narinfo`, and NARs.
Flake URLs and arbitrary local expressions are not accepted as source values.
The source is evaluated code: configure sources you trust. Each check resolves
the source once, so selected attributes come from the same tree. Local builds,
remote builders, and import-from-derivation are disabled. repowatch does not
install packages, change profiles, or update the operator's channels.

## Discovery and warming

Each attribute/output pair is a catalog entry, such as `hello:out`. Its version
identity includes the store hash, so a rebuild with an unchanged package
version still appears in history. Multiple outputs and aliases remain
separate entries. Normal API browsing, search, history, manual warming,
prefetch bans, and dashboard configuration apply.

An empty `nix_attributes` list evaluates the whole available catalog for the
selected Nix system. Start with an explicit selection: full nixpkgs evaluation
can consume substantial CPU, memory, and store space, and warming all outputs
can consume substantial bandwidth and cache space. `nix_timeout` defaults to
600 seconds per CLI invocation; `nix_max_paths` defaults to 500000 catalog
outputs and independently limits each dependency closure. Evaluation failure
preserves the previous catalog. A valid output can lack a published binary;
that is a failed warm, retried on later checks even if the catalog is unchanged.

Warming follows `.narinfo` references and fetches every dependency's metadata
and NAR through nginx, plus `nix-cache-info`. Shared HTTP artifacts are fetched
once per warm operation. Bandwidth and concurrency settings apply to artifact
downloads. Nix warming shares the application bandwidth budget with other
repository types; schedule windows and additional per-repository ceilings
apply to its artifact downloads. Source evaluation and direct metadata
discovery are outside that budget. Bans select root entries; dependencies needed by an allowed root
are still fetched. A successful warm means the entire discovered closure was
downloaded successfully, not merely its root metadata.

With `verify_signature: true`, Nix checks the exact downloaded dependency
metadata against `nix_public_keys` before artifact warming. Changing this
policy invalidates completed warm records for rechecking. Signatures cover
the store path, uncompressed NAR hash/size, and references. Compressed
`FileHash` and `FileSize`, when present, are checked during downloads but are
not themselves signed. repowatch does not decompress NARs to check `NarHash`;
the consuming Nix client performs that final verification. With prefetch
disabled, signature checks occur only on explicit warm operations.

## Purge and activity

Purge uses persisted closure artifacts, including those from failed warming
attempts. Files shared with other current catalog entries are retained and
reported as `retained_shared`. If another current entry's closure has never
been discovered, retention is conservative: it could depend on any selected
artifact. Removing a warm record does not establish that all its shared
bytes were evicted. nginx's ordinary cache expiration still applies.

Real client requests refresh existing completed closure records. A metadata
request alone does not create a completed warm record; client-only downloads
remain visible in request statistics without claiming closure completeness.
Closure ownership is tracked per repository. NAR URLs may be relative or
absolute within the configured upstream; query strings are preserved in
cache and purge keys. Cross-origin NAR URLs are rejected. Cross-repository
content-hash deduplication is not populated from Nix catalog entries.

Protocol reference: [Nix binary-cache metadata](https://nix.dev/manual/nix/2.35/protocols/binary-cache/narinfo.html).
