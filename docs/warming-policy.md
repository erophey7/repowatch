# Warming whitelist and blacklist

Each repository can define package-name glob patterns:

```yaml
repos:
  - id: arch-core
    type: pacman
    upstream: https://geo.mirror.pkgbuild.com/core/os/x86_64
    arch: x86_64
    repo_name: core
    prefetch_whitelist: ["linux-*", "bash", "glibc"]
    prefetch_blacklist: ["*-debug", "*-doc"]
```

The repository editor exposes **Warming whitelist** and **Warming blacklist**,
with one pattern per line. They can also be edited through the existing
repository API. Both default to empty lists, adding no filtering.

A package is eligible when all these conditions hold:

1. The whitelist is empty, or at least one whitelist pattern matches its name.
2. No blacklist pattern matches its name.
3. Its exact name is not in the existing repository bans list.

The blacklist and exact bans always take priority. Existing bans remain literal
names in SQLite, not patterns: banning `foo*` through the bans API does not
ban `foobar`. The YAML lists are stored with the repository configuration;
they do not rewrite or migrate those bans. The exact-ban count in the API and
Prometheus still counts exact bans, not patterns or their current matches.

## Automatic updates and demand

`prefetch: true` downloads updates to **demanded names**, not every new package
in the repository. `prefetch: false` retains index monitoring, invalidation,
retention, deduplication and passive nginx caching, without automatic package
downloads. Manual warming works with either value.

A successful client `GET` (HTTP 200) observed through the nginx syslog listener
registers the matched catalog name. Manual warming registers each known,
non-excluded selected name, including an attempt that later fails. A HEAD,
failed request, unknown name or repowatch's own download does not register demand.
A later client request for a previously prefetched package does register demand,
independently of its original warm-source statistics. Enable syslog ingestion
for client-driven tracking; lost UDP messages and requests made before the index
is known cannot reliably establish demand. Manual warming works without syslog.

Demand is stored separately from physical cache inventory and warm history. It
survives version changes, cache eviction, purging, warmed-record retention and
service restarts. It is scoped to the repository id and catalog source identity;
a source/architecture/index-selection change does not inherit the old source's
selection. Automatic downloads never expand the selection. Use bans or warming
lists to exclude a name, or disable prefetch for the repository. Deleting orphaned
repository bookkeeping also removes its saved demand.

A first index establishes the baseline and produces normal change history, but
an empty demand set produces no package downloads. Subsequent new versions of
selected names are warmed; new unrelated packages are ignored. In-place content
replacements are invalidated even for unselected names, and only demanded names
are downloaded again automatically. Lists and bans further restrict that work.
No dependency resolution is added for ordinary package formats.

On upgrade, demand is seeded once from retained successful, attributable client
GET history that still maps to a named package in the current catalog. Historical
manual and automatic warming shared the same source marker, so old `prefetch`
rows cannot identify manual selections and are not imported on their own.
Expired history, removed package versions and ambiguous request ownership cannot
be reconstructed; download or manually warm those packages to establish demand.
This deliberately avoids subscribing packages from earlier accidental full warms.

## Pattern syntax and names

Patterns match the whole, case-sensitive catalog name, without its version or
package filename. `*` matches any sequence, `?` one character, `[abc]` one listed
character, and `[!abc]` one character outside the set. This is Python fnmatch
syntax, not a regular expression or a filesystem traversal. Slashes are ordinary
characters; `*` can match them. An unmatched `[` is literal. Quote YAML patterns,
especially patterns beginning with `*`, to avoid YAML alias syntax.

- Typical package: `linux-headers`, matched by `linux-*`.
- Gentoo: `app-editors/vim`, matched by `app-editors/*`.
- Nix: attribute and output, e.g. `hello:out` or `openssl:dev`.
  Use `hello:*` to select every output of that attribute, or `*:dev` to exclude
  development outputs as independent roots. These are not Nix store basenames.

Each list accepts at most 128 patterns, each 1–256 characters. Non-string,
empty, control-character and surrounding-whitespace entries are rejected.
The dashboard trims lines and removes blank ones before submission. Clearing
both fields saves `[]` for both lists. API validation rejects malformed updates
without changing the config. If any filter or exact ban is active but a package
has no catalog name, warming skips it rather than guessing a name from its key.

## Scope and timing

Automatic warming, manual warming and replacement warming use the same rules.
Manual warming can bypass `prefetch: false`, but cannot bypass either list or
an exact ban. The warm API returns separate `warmed`, `failed`, `skipped`
and `not_found` arrays; an excluded or failed package is never reported as
successfully warmed. Change the rule or lift the ban before manually warming an
excluded package. Skipped packages produce no new warm success/failure record.

For Nix, filtering selects **roots**. An allowed root still downloads its complete
reference closure, even if a dependency would be excluded as a standalone root.
Otherwise the selected root would be unusable. This is a warming selection
policy, not an access-control or storage-exclusion boundary.

Each warm operation takes a snapshot of the rules and bans at its start; edits
affect subsequent operations using the updated repository configuration. They
do not cancel downloads already in progress. Disabling prefetch is special: once
the daemon observes the edit (normally within its ten-second scheduler tick),
queued automatic package downloads stop starting; active transfers may finish.
Manual downloads remain allowed. Changes of source also stop queued automatic
work captured under the previous source. Unlike the bandwidth schedule,
the lists are not reloaded between HTTP chunks.

Lists do not change index parsing, package counts, change history, client
requests or nginx cache access. They do not purge existing files. The existing
same-version replacement flow still invalidates stale bytes when purge is
enabled, even if the replacement is excluded from warming. This keeps clients
from receiving the stale version without forcing an excluded download.

Relaxing a list does not itself enqueue old packages for ordinary repository
formats: automatic warming normally reacts to index changes. Use manual warming
to fill newly allowed existing entries. Nix's existing retry scan also considers
demanded, allowed catalog roots without a successful warm record on subsequent checks.

To measure what is actually present on disk, use **Storage → Measure cache
coverage and size** in the administrator dashboard. Its denominator is the complete
current catalog, not the warming whitelist: intentionally warming a subset can
therefore produce a low percentage without indicating a warming failure. Warm
bookkeeping alone is not proof that nginx still has a file. See
[physical cache completeness](access.md#physical-cache-completeness) for scan
limits, deduplication and Nix closure handling.

Ordinary new-version warming still processes a one-shot index diff. A failed or
stopped ordinary download is not automatically replayed on an unchanged index;
retry it manually. Same-path replacements retain their existing pending retry
state, and Nix retries only demanded roots. Disabling and reenabling prefetch
alone does not replay all versions missed while it was off.
