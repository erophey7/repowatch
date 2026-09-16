"""UDP listener for nginx's syslog access_log — the source of "what was
requested" for the dashboard (see nginx/render.py's access_log syslog:... directive,
or hand-write the equivalent if you're not using the generator).

A push channel instead of parsing a file: no rotation/inode issues, no need
to track a position in a growing file. Optional — enabled via
syslog_listener.enabled in config.yaml; the port is not opened by default."""

from __future__ import annotations

import logging
import re
import socket
import time
import threading
from dataclasses import dataclass
from repowatch.config.load import load_config
from repowatch.config.models import Config, RepoConfig
from repowatch.errors import ConfigError
from repowatch.routing import repo_prefix, package_path
from urllib.parse import urlsplit, unquote
from repowatch.runtime.context import ServiceState

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
    # see nginx/render.py's $repowatch_is_prefetch map, keyed off parsers.base.USER_AGENT),
    # False for everything else, including when the field is absent entirely
    # (an older/hand-written access_log that doesn't emit it — see run_listener).
    is_prefetch: bool = False


def parse_syslog_line(raw: bytes, tag: str = "repowatch") -> ParsedRequest | None:
    """Strip the syslog envelope (nginx sends RFC3164-style: "<PRI>timestamp
    host tag[pid]: message") and parse the message as
    "$remote_addr $request_method $request_uri $status $upstream_cache_status
    $repowatch_is_prefetch" (see the repowatch_requests log_format in
    nginx/render.py's render()) — the last field is optional, for compatibility
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
    prefixes as routing.repo_prefix uses for warming. If several
    repositories share the same prefix (e.g. two apk repos with the same
    arch), we deliberately return None instead of guessing."""
    candidates = []
    for repo in repos:
        try:
            prefix = repo_prefix(repo)
        except ValueError:
            continue
        if path == prefix or path.startswith(prefix + "/"):
            candidates.append(repo.id)

    return candidates[0] if len(candidates) == 1 else None


def package_path_index(repo: RepoConfig, packages: dict[str, str]) -> dict[str, list[str]]:
    """Index complete local routes, preserving every owner of the same file."""
    index: dict[str, list[str]] = {}
    for key, filename in packages.items():
        if filename:
            path = unquote(urlsplit(package_path(repo, filename)).path)
            index.setdefault(path, []).append(key)
    return index


def match_all_package_keys(path: str, by_path: dict[str, dict[str, list[str]]]) -> list[tuple[str, str]]:
    """Match the actual route; shared pool URLs can have several catalog owners."""
    path = unquote(urlsplit(path).path)
    return [(repo_id, key) for repo_id, index in by_path.items() for key in index.get(path, [])]


def refresh_package_indexes(
    repos: list[RepoConfig],
    store: ServiceState,
    packages_by_repo: dict[str, dict[str, str]],
    by_path: dict[str, dict[str, list[str]]],
    last_revision: dict[str, tuple[int | None, str]],
) -> None:
    """Refresh changed generations/routes and discard removed repositories.

    Integer revisions detect multiple snapshots within one clock tick. Reading
    revisions before packages may cause one extra refresh during a concurrent
    snapshot, but can never label old packages with a newer revision.
    """
    active = {repo.id for repo in repos}
    for mapping in (packages_by_repo, by_path, last_revision):
        for repo_id in mapping.keys() - active:
            del mapping[repo_id]
    revisions = store.repositories.get_snapshot_revisions()
    for repo in repos:
        revision = (revisions.get(repo.id), repo.catalog_identity())
        if last_revision.get(repo.id) == revision:
            continue
        packages = store.repositories.get_packages(repo.id) or {}
        packages_by_repo[repo.id] = packages
        by_path[repo.id] = package_path_index(repo, packages)
        last_revision[repo.id] = revision


def open_socket(config: Config) -> socket.socket:
    """Bind the UDP listener before a service reports successful startup."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((config.syslog_listener.bind, config.syslog_listener.port))
        sock.settimeout(0.2)
        return sock
    except BaseException:
        sock.close()
        raise


def run_listener(config_path: str, initial_config: Config, store: ServiceState,
                 *, sock: socket.socket | None = None,
                 stop: threading.Event | None = None,
                 ready: threading.Event | None = None) -> None:
    """Meant to run in its own daemon thread (see cli/main.py). bind/port come
    from initial_config once at startup (the socket is not rebound on the
    fly); the repository list and the full-path package index are
    periodically re-read from config_path/ServiceState, so repositories added
    through the dashboard and new packages are picked up without a process
    restart.

    Real successful client requests (not just repowatch's own warming) also
    land in warmed_packages — otherwise the dashboard would only show
    "warmed" for what repowatch itself warmed, even though the nginx cache
    may hold much more (everything clients have ever actually downloaded)."""
    listener_config = initial_config.syslog_listener
    if sock is None:
        with open_socket(initial_config) as owned:
            return run_listener(config_path, initial_config, store, sock=owned, stop=stop, ready=ready)
    stop = stop if stop is not None else threading.Event()
    logger.info(
        "syslog listener listening on %s:%d", listener_config.bind, listener_config.port
    )

    repos = initial_config.repos
    packages_by_repo: dict[str, dict[str, str]] = {}
    by_path: dict[str, dict[str, list[str]]] = {}
    last_revision: dict[str, tuple[int | None, str]] = {}

    refresh_package_indexes(repos, store, packages_by_repo, by_path, last_revision)
    last_reload = time.monotonic()
    if ready is not None:
        ready.set()

    while not stop.is_set():
        try:
            data, _addr = sock.recvfrom(65535)
        except socket.timeout:
            continue

        if time.monotonic() - last_reload > _REPOS_REFRESH_INTERVAL:
            try:
                repos = load_config(config_path).repos
            except ConfigError:
                logger.warning(
                    "failed to reload %s in the syslog listener, keeping the previous repository list",
                    config_path,
                )
            refresh_package_indexes(repos, store, packages_by_repo, by_path, last_revision)
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
            # already records that directly (see operations/warm.py) when it's
            # actually repowatch doing the warming, so redoing it here from
            # the syslog line would just be a redundant write, not new
            # information.
            continue

        repo_id = match_repo_id(parsed.path, repos)
        # Computed once, used for both request_events (below) and the
        # warmed_packages loop further down — same matching, two consumers.
        matches = match_all_package_keys(parsed.path, by_path)
        # Only recorded on request_events when unambiguous: with more than
        # one match we'd have to pick one repo_id/package_key pair to put in
        # these two columns, which would silently favor whichever repo
        # happens to come first — see get_prefetch_efficiency, which needs
        # this link to be exact, not a guess.
        package_repo_id, package_key = matches[0] if len(matches) == 1 else (None, None)
        try:
            store.requests.record_request(
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
                    prefix = repo_prefix(candidate).rstrip('/') + '/'
                    if parsed.path.startswith(prefix):
                        try:
                            store.cache.touch_nix_artifact(candidate.id, parsed.path[len(prefix):])
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
                    store.cache.record_warmed_package(matched_repo_id, key, filename, True, 200, source="client")
                except Exception:
                    logger.exception("failed to update warmed_packages from a real request")
