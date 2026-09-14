"""UDP listener for nginx's syslog access_log — the source of "what was
requested" for the dashboard (see nginx.py's access_log syslog:... directive,
or hand-write the equivalent if you're not using the generator).

A push channel instead of parsing a file: no rotation/inode issues, no need
to track a position in a growing file. Optional — enabled via
syslog_listener.enabled in config.yaml; the port is not opened by default.
"""

from __future__ import annotations

import logging
import re
import socket
import time
from dataclasses import dataclass

from repowatch.config import Config, ConfigError, RepoConfig, load_config
from repowatch.prefetch import _repo_url_prefix
from repowatch.state import StateStore

logger = logging.getLogger(__name__)

_REPOS_REFRESH_INTERVAL = 60  # seconds — how often to re-read config.yaml for repo_id matching


@dataclass
class ParsedRequest:
    client_ip: str | None
    method: str
    path: str
    status: str | None
    cache_status: str | None
    # True for repowatch's own traffic (index checks + prefetch/warm_cache —
    # see nginx.py's $repowatch_is_prefetch map, keyed off parsers.base.USER_AGENT),
    # False for everything else, including when the field is absent entirely
    # (an older/hand-written access_log that doesn't emit it — see run_listener).
    is_prefetch: bool = False


def parse_syslog_line(raw: bytes, tag: str = "repowatch") -> ParsedRequest | None:
    """Strip the syslog envelope (nginx sends RFC3164-style: "<PRI>timestamp
    host tag[pid]: message") and parse the message as
    "$remote_addr $request_method $request_uri $status $upstream_cache_status
    $repowatch_is_prefetch" (see the repowatch_requests log_format in
    nginx.py's render()) — the last field is optional, for compatibility
    with a hand-written access_log that predates it.

    Returns None for any unrecognized input — the caller simply skips such a
    packet instead of crashing.
    """
    try:
        text = raw.decode("utf-8", errors="replace").strip()
    except Exception:
        return None

    if not text:
        return None

    match = re.search(re.escape(tag) + r"(?:\[\d+\])?:\s*(.*)$", text)
    if not match:
        return None

    fields = match.group(1).strip().split()
    if len(fields) < 3:
        return None

    client_ip, method, path = fields[0], fields[1], fields[2]
    status = fields[3] if len(fields) > 3 else None
    cache_status = fields[4] if len(fields) > 4 else None
    is_prefetch = len(fields) > 5 and fields[5] == "1"
    return ParsedRequest(
        client_ip=client_ip, method=method, path=path, status=status, cache_status=cache_status,
        is_prefetch=is_prefetch,
    )


def match_repo_id(path: str, repos: list[RepoConfig]) -> str | None:
    """Best-effort match of a request path to a repo_id, using the same
    prefixes as prefetch._repo_url_prefix uses for warming. If several
    repositories share the same prefix (e.g. two apk repos with the same
    arch), we deliberately return None instead of guessing."""
    candidates = []
    for repo in repos:
        try:
            prefix = _repo_url_prefix(repo)
        except ValueError:
            continue
        if path == prefix or path.startswith(prefix + "/"):
            candidates.append(repo.id)

    return candidates[0] if len(candidates) == 1 else None


def basename_index(packages: dict[str, str]) -> dict[str, str]:
    """{file basename: package_key} — built once per package-list update
    (not per request: debian, for example, has tens of thousands of
    packages, so scanning this on every UDP datagram isn't viable).

    We match on basename rather than the full filename because apt's
    filename is a relative path like
    "pool/main/l/linux/linux_....deb" (which matches the tail of the real
    request path), while pacman/apk just use a bare filename with no
    directories; basename is the common denominator for both cases.
    """
    index: dict[str, str] = {}
    for key, filename in packages.items():
        if not filename:
            continue
        index[filename.rsplit("/", 1)[-1]] = key
    return index


def match_all_package_keys(
    path: str, by_basename: dict[str, dict[str, str]]
) -> list[tuple[str, str]]:
    """All (repo_id, package_key) pairs whose KNOWN packages contain a file
    with this basename — across ALL repositories, not just the single one
    match_repo_id would return.

    Why not via match_repo_id: apt repositories sharing one upstream and
    component but different suites (e.g. ubuntu-noble-main/
    ubuntu-noble-updates-main/ubuntu-noble-backports-main) physically share
    the same pool/ on upstream — match_repo_id then deliberately returns
    None (multiple candidates by prefix, "we don't guess", see its
    docstring), and NONE of them would get a warmed_packages entry, even
    though we know for certain the file was actually downloaded. Here,
    instead of a prefix, we do an exact basename match against each
    repository's already-known package list: if the file is listed among
    repo_id's packages, it's warmed for that repo, regardless of how many
    other repositories share the same URL namespace."""
    basename = path.rsplit("/", 1)[-1]
    matches: list[tuple[str, str]] = []
    for repo_id, index in by_basename.items():
        key = index.get(basename)
        if key:
            matches.append((repo_id, key))
    return matches


def refresh_package_indexes(
    repos: list[RepoConfig],
    store: StateStore,
    packages_by_repo: dict[str, dict[str, str]],
    by_basename: dict[str, dict[str, str]],
    last_changed_at: dict[str, str | None],
) -> None:
    """Updates packages_by_repo/by_basename only for repositories whose
    packages actually changed since the last call (see repo_state.changed_at
    in state.py) — it doesn't blindly rebuild the index for every repository
    on every tick (see run_listener), even when none of them changed.

    changed_at (not last_check!) is the right signal: last_check is bumped
    by the watcher on EVERY check regardless of outcome (see
    watcher.touch_last_check), while changed_at only moves when the package
    set actually changed.

    last_changed_at is mutated in place and used as memory between calls;
    for a repo_id not yet present in it (the very first call, or a
    repository that was just added), the update always happens — there's no
    need for a separate "first build" case, an empty dict already gives the
    right behavior."""
    for repo in repos:
        status = store.get_status(repo.id) or {}
        changed_at = status.get("changed_at")
        if repo.id in last_changed_at and last_changed_at[repo.id] == changed_at:
            continue

        packages = store.get_packages(repo.id) or {}
        packages_by_repo[repo.id] = packages
        by_basename[repo.id] = basename_index(packages)
        last_changed_at[repo.id] = changed_at


def run_listener(config_path: str, initial_config: Config, store: StateStore) -> None:
    """Meant to run in its own daemon thread (see cli.py). bind/port come
    from initial_config once at startup (the socket is not rebound on the
    fly); the repository list and the basename package index are
    periodically re-read from config_path/StateStore, so repositories added
    through the dashboard and new packages are picked up without a process
    restart.

    Real successful client requests (not just repowatch's own warming) also
    land in warmed_packages — otherwise the dashboard would only show
    "warmed" for what repowatch itself warmed, even though the nginx cache
    may hold much more (everything clients have ever actually downloaded)."""
    listener_config = initial_config.syslog_listener
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((listener_config.bind, listener_config.port))
    logger.info(
        "syslog listener listening on %s:%d", listener_config.bind, listener_config.port
    )

    repos = initial_config.repos
    packages_by_repo: dict[str, dict[str, str]] = {}
    by_basename: dict[str, dict[str, str]] = {}
    last_changed_at: dict[str, str | None] = {}

    refresh_package_indexes(repos, store, packages_by_repo, by_basename, last_changed_at)
    last_reload = time.monotonic()

    while True:
        try:
            data, _addr = sock.recvfrom(65535)
        except OSError:
            logger.exception("error reading the syslog UDP socket")
            continue

        if time.monotonic() - last_reload > _REPOS_REFRESH_INTERVAL:
            try:
                repos = load_config(config_path).repos
            except ConfigError:
                logger.warning(
                    "failed to reload %s in the syslog listener, keeping the previous repository list",
                    config_path,
                )
            refresh_package_indexes(repos, store, packages_by_repo, by_basename, last_changed_at)
            last_reload = time.monotonic()

        parsed = parse_syslog_line(data)
        if parsed is None:
            logger.debug("failed to parse syslog datagram: %r", data[:200])
            continue
        if parsed.is_prefetch:
            # repowatch's own index checks/prefetch, not a real client —
            # "Recent client requests" should show what clients actually
            # asked for, not our own background activity. Skip
            # warmed_packages too, not just request_events: warm_cache()
            # already records that directly (see prefetch.py) when it's
            # actually repowatch doing the warming, so redoing it here from
            # the syslog line would just be a redundant write, not new
            # information.
            continue

        repo_id = match_repo_id(parsed.path, repos)
        # Computed once, used for both request_events (below) and the
        # warmed_packages loop further down — same matching, two consumers.
        matches = match_all_package_keys(parsed.path, by_basename)
        # Only recorded on request_events when unambiguous: with more than
        # one match we'd have to pick one repo_id/package_key pair to put in
        # these two columns, which would silently favor whichever repo
        # happens to come first — see get_prefetch_efficiency, which needs
        # this link to be exact, not a guess.
        package_repo_id, package_key = matches[0] if len(matches) == 1 else (None, None)
        try:
            store.record_request(
                repo_id=repo_id,
                client_ip=parsed.client_ip,
                method=parsed.method,
                path=parsed.path,
                status=parsed.status,
                cache_status=parsed.cache_status,
                package_key=package_key,
                package_repo_id=package_repo_id,
            )
        except Exception:
            logger.exception("failed to record request_event")

        # status == "200" — the file was actually served to the client in
        # full (and is in the nginx cache: either it was already there —
        # HIT, or was just written — MISS, either way it's in the cache
        # now). Any other status (404/5xx) is not recorded — there's
        # nothing to show as "warmed".
        #
        # Deliberately NOT tied to repo_id above (match_repo_id can return
        # None on an ambiguous prefix — see match_all_package_keys) — we
        # mark EVERY repository whose known packages actually contain this
        # file as warmed; there can be more than one.
        if parsed.status == "200":
            for candidate in repos:
                if candidate.type == 'nix':
                    prefix = _repo_url_prefix(candidate).rstrip('/') + '/'
                    if parsed.path.startswith(prefix):
                        try:
                            store.touch_nix_artifact(candidate.id, parsed.path[len(prefix):])
                        except Exception:
                            logger.exception('failed to refresh Nix artifact activity')
            for matched_repo_id, key in matches:
                matched_repo = next((r for r in repos if r.id == matched_repo_id), None)
                if matched_repo is not None and matched_repo.type == 'nix':
                    # A narinfo GET cannot establish that every archive/reference
                    # was downloaded. Only closure warming records Nix success.
                    continue
                filename = packages_by_repo.get(matched_repo_id, {}).get(key, "")
                try:
                    store.record_warmed_package(matched_repo_id, key, filename, True, 200, source="client")
                except Exception:
                    logger.exception("failed to update warmed_packages from a real request")
