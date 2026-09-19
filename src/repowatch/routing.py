"""Repository paths and cache keys shared by rendering, warming, and access tracking."""

from __future__ import annotations

import re
from collections.abc import Callable
import urllib.parse
from repowatch.config.models import Config, RepoConfig
from repowatch.errors import ConfigError
from urllib.parse import urlsplit

def apt_prefix(repo: RepoConfig) -> str:
    """Local apt prefix — nginx/render.py's render() imports this same function to
    build the matching location block, so there's a single source of truth.

    Usually the same as the upstream path. Security uses the same /ubuntu
    but a different host: the path alone would collapse two different
    sources. PPA gets its own prefix; each PPA needs its own nginx location
    (see the deadsnakes/ppa example) — we don't open up an arbitrary
    upstream in nginx.
    """
    from repowatch.url_templates import expand
    custom = expand(repo)
    if custom is not None:
        return custom
    upstream = urllib.parse.urlparse(repo.upstream)
    path = upstream.path.rstrip("/")
    if upstream.hostname == "security.ubuntu.com" and path == "/ubuntu":
        return "/ubuntu-security"
    parts = path.strip("/").split("/")
    if upstream.hostname == "ppa.launchpadcontent.net" and len(parts) == 3 and parts[2] == "ubuntu":
        return f"/ppa-{parts[0]}-{parts[1]}"
    return path


def apt_rpm_prefix(repo: RepoConfig) -> str:
    from repowatch.url_templates import expand
    return expand(repo) or f"/apt-rpm/{urllib.parse.quote(repo.id, safe='')}"


def repo_prefix(repo: RepoConfig) -> str:
    """Relative repository path to append to cache_base_url.

    nginx/render.py's render() imports this same function to build the matching
    location block — a single source of truth, not two configs to keep
    in sync by hand.
    """
    from repowatch.url_templates import expand
    custom = expand(repo)
    if custom is not None and repo.type not in ("apt", "apt-rpm"):
        return custom
    if repo.type == "pacman":
        return f"/arch/{repo.repo_name}/os/{repo.arch}"
    if repo.type == "apt-rpm":
        return f"{apt_rpm_prefix(repo)}/RPMS.{repo.component}"
    if repo.type == "dnf":
        # RPM-MD upstream paths have no common structure. An explicit
        # namespace by repo_id avoids mixing hosts/architectures that
        # happen to share a path.
        return f"/rpm/{urllib.parse.quote(repo.id, safe='')}"
    if repo.type in ('gentoo', 'slackware'):
        return f"/{repo.type}/{urllib.parse.quote(repo.id, safe='')}"
    if repo.type == "xbps":
        # Same reasoning as dnf above — Void mirrors/components (nonfree,
        # multilib, multilib/nonfree, debug) have no common upstream
        # structure either, and packages sit flat next to <arch>-repodata,
        # so a plain repo_id namespace is enough (no component suffix
        # needed here, unlike apt-rpm's RPMS.<component> — a Void
        # component is just a different upstream directory).
        return f"/xbps/{urllib.parse.quote(repo.id, safe='')}"
    if repo.type == "apt":
        # apt packages live under pool/<component>/..., where component is
        # the first segment after pool/ (that's upstream's actual
        # structure, not our own convention) — we include it in the prefix
        # so different components of the same distribution
        # (main/restricted/universe/multiverse) don't collapse into the
        # same prefix in match_repo_id (see runtime/syslog.py). This
        # doesn't remove ambiguity entirely — several suites of the same
        # component (noble/noble-updates/noble-backports) physically share
        # the same pool/, and there's nothing to be done about that (see
        # CLAUDE.md/ROADMAP).
        return f"{apt_prefix(repo)}/pool/{repo.component}"
    if repo.type == "nix":
        return f"/nix/{repo.id}"
    if repo.type == "apk":
        # upstream already includes version+component (e.g.
        # ".../alpine/v3.20/main") — this path segment is also needed in
        # the local warm URL, otherwise you'd get .../alpine/{arch}/...
        # without v3.20/main, and upstream has no such path (this used to
        # cause 404s while warming apk).
        upstream_path = urllib.parse.urlparse(repo.upstream).path.rstrip("/")
        return f"{upstream_path}/{repo.arch}"
    raise ValueError(f"unknown repository type: {repo.type}")


def package_prefix(repo: RepoConfig) -> str:
    """Prefix before a catalog filename, including APT's already-prefixed pool paths."""
    if repo.type == "apt-rpm":
        return apt_rpm_prefix(repo)
    if repo.type == "apt":
        return apt_prefix(repo)
    return repo_prefix(repo)


def package_path(repo: RepoConfig, filename: str) -> str:
    """Local package path shared by warming, purging and access tracking."""
    return f"{package_prefix(repo)}/{filename}"


class CacheKeyBuilder:
    """Prepare validated routes once per operation, never across configuration reloads."""

    def __init__(self, config: Config):
        """Snapshot route validation and the default dedup basis for one operation."""
        self.key_prefix, self.routes, _ = compute_routes(config)
        self.dedup = config.nginx.enable_dedup

    def for_repo(self, repo: RepoConfig) -> Callable[..., str]:
        """Bind invariant local/remote prefixes; preserve filenames and query strings."""
        local = package_prefix(repo) + '/'
        for route, (_scheme, host, remote, _ttl) in self.routes.items():
            if local.startswith(route):
                remote = remote + local[len(route):]
                key_base = self.key_prefix + 'http' + host
                default_dedup = self.dedup

                def key(filename: str, *, dedup: bool | None = None) -> str:
                    """Build a key under the selected current or historical dedup basis."""
                    use_remote = default_dedup if dedup is None else dedup
                    return key_base + (remote if use_remote else local) + filename

                return key
        raise ValueError(f'{repo.id}: {local!r} does not match any current nginx route')


def warm_url(config: Config, repo: RepoConfig, filename: str) -> str:
    return f"{config.cache_base_url}{package_path(repo, filename)}"


def purge_url(config: Config, repo: RepoConfig, filename: str) -> str:
    """Local URL that, when GET-requested, purges this package's
    proxy_cache entry — see nginx/render.py's purge location, only emitted when
    nginx.enable_purge is set (docs_dev/ROADMAP.md item 24). Same local
    path as warm_url, under a /purge prefix nginx/render.py matches with a
    dedicated, loopback-only location per repository."""
    return f"{config.cache_base_url}/purge{package_path(repo, filename)}"


def compute_cache_key(config: Config, repo: RepoConfig, filename: str) -> str:
    """The exact literal proxy_cache_key string for one package file — the
    value for this repository's own content location, before any cross-repo
    canonical rewrite. For a deduplicated request, resolve the canonical
    repository first; this function does not consult the package database.

    Used by cache/probe.py: to ask /cache-probe whether one SPECIFIC known
    package is really on disk (a non-destructive complement to
    cache.purge.purge_selected(), which can only tell you by actually deleting
    the entry — see its own docstring on why a separate non-destructive
    check was rejected for a live HTTP GET/HEAD, a concern that doesn't
    apply here since this never touches upstream or proxy_cache at all, only
    stat()s a file); and to recognize which raw keys /cache-scan reads back
    from disk belong to a still-configured package, so anything left over is
    an orphan candidate for docs_dev/ROADMAP.md item 33.

    Reuses compute_routes() — the same single source of truth render() and
    render_purge() already draw from — specifically to avoid reintroducing
    the dedup-basis mismatch bug documented on render_purge() above: this
    function and that one MUST compute identical keys for the same file.
    `local`'s scheme component is deliberately hardcoded to "http", not the
    route's (upstream) scheme — see the same point in render_purge()'s
    comment: it's nginx's own listening scheme at runtime, and this
    generator never emits `listen ... ssl`.

    Raises ValueError if `repo` isn't actually part of `config.repos`'
    current routing (e.g. a stale RepoConfig from before a reload) —
    callers always pass a repo drawn from the same `config` they pass here.
    """
    return CacheKeyBuilder(config).for_repo(repo)(filename)


def _uri(value: str) -> str:
    if not re.fullmatch(r'/[A-Za-z0-9_./+~-]*', value) or any(x in value.split('/') for x in ('.', '..')):
        raise ConfigError('nginx: upstream/cache path contains unsupported characters')
    return value


def compute_routes(config: Config) -> tuple[str, dict[str, tuple[str, str, str, int]], dict[str, str]]:
    """Shared route computation for render() and render_purge() — one source
    of truth for validation and for the (scheme, host, remote, ttl) per local
    prefix, so the purge key (built separately, see render_purge()) can never
    drift from the content locations it must exactly match."""
    settings = config.nginx
    routes: dict[str, tuple[str, str, str, int]] = {}
    kinds: dict[str, str] = {}
    for repo in config.repos:
        try:
            upstream = urlsplit(repo.upstream)
        except ValueError as exc:
            raise ConfigError(f"{repo.id}: invalid upstream URL") from exc
        if (upstream.scheme not in ('http', 'https') or not upstream.hostname
                or upstream.username or upstream.password or upstream.query or upstream.fragment
                or not re.fullmatch(r'[A-Za-z0-9.-]+(?::[0-9]{1,5})?', upstream.netloc)):
            raise ConfigError(f'{repo.id}: nginx requires a plain HTTP(S) upstream')
        try:
            upstream.port
        except ValueError as exc:
            raise ConfigError(f'{repo.id}: invalid upstream port') from exc
        remote = _uri(upstream.path.rstrip('/') + '/')
        if repo.type == 'apt-rpm':
            local = _uri(apt_rpm_prefix(repo).rstrip('/') + '/')
        elif repo.type == 'apt':
            local = _uri(apt_prefix(repo).rstrip('/') + '/')
        else:
            local = _uri(repo_prefix(repo).rstrip('/') + '/')
            if repo.type == 'apk':
                # Parser appends the same suffix to upstream as prefetch does locally.
                suffix = f'{repo.arch}/'
                remote = _uri(remote + suffix)
        if local == '/':
            raise ConfigError(f'{repo.id}: root cache prefix is reserved')
        ttl = min(settings.index_ttl, config.effective_check_interval(repo))
        if ttl < 1:
            raise ConfigError('nginx: index TTL/check interval must be positive')
        route = (upstream.scheme, upstream.netloc, remote, ttl)
        if local in kinds and kinds[local] != repo.type:
            raise ConfigError(f"nginx: conflicting repository types for {local}")
        kinds[local] = repo.type
        previous = routes.get(local)
        if previous and previous[:3] != route[:3]:
            raise ConfigError(f'nginx: conflicting upstreams for {local}')
        if previous:
            route = (*route[:3], min(ttl, previous[3]))
        routes[local] = route
    prefixes = sorted(routes)
    if any(b.startswith(a) for i, a in enumerate(prefixes) for b in prefixes[i + 1:]):
        raise ConfigError('nginx: overlapping repository prefixes')
    key_prefix = settings.cache_key_version + ':' if settings.cache_key_version else ''
    return key_prefix, routes, kinds
