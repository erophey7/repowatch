"""Nix package discovery using the optional system Nix CLI.

The CLI evaluates a source expression; it never builds or installs packages.
Binary metadata and NARs are handled separately by cache/nix.py."""

from __future__ import annotations

import asyncio
import httpx
import json
import os
import re
import tempfile
from functools import lru_cache
from repowatch.models import PackageRef
from repowatch.parsers.base import IndexHeadResult, IndexParser
from repowatch.processes import run

STORE_PATH = re.compile(r'/nix/store/([0123456789abcdfghijklmnpqrsvwxyz]{32})-([^/\s]+)\Z')
MAX_OUTPUT = 128 * 1024 * 1024


@lru_cache(maxsize=1)
def _evaluation_cache() -> tempfile.TemporaryDirectory:
    # Reuse HTTP validators between checks without touching a user's cache.
    # PrivateTmp provides the same writable location under the system unit.
    return tempfile.TemporaryDirectory(prefix='repowatch-nix-evaluation-')


def run_nix(arguments: list[str], timeout: int) -> bytes:
    """Bound the lifetime of an evaluation and its children; no shell parsing.

    Nix is an optional system dependency agreed for this repository type.
    Disallow import-from-derivation and both local and remote builds. Use a
    process-local cache and isolated config, leaving operator profiles and channels alone.
    A configured Nix store/daemon must already be usable by the service user.
    """
    env = dict(os.environ, NIX_PATH='', NIX_USER_CONF_FILES='/dev/null',
               XDG_CACHE_HOME=_evaluation_cache().name, NIX_CONFIG='')
    command = arguments + ['--option', 'allow-import-from-derivation', 'false',
                           '--option', 'max-jobs', '0', '--option', 'builders', '',
                           '--option', 'accept-flake-config', 'false']
    result = run(command, timeout=timeout, env=env, max_stdout=MAX_OUTPUT)
    diagnostic = result.stderr[:8192].decode('utf-8', errors='replace')
    if result.returncode:
        raise ValueError(f'Nix command failed ({result.returncode}): {diagnostic}')
    # nix-env can report ignored evaluation failures with exit status 0.
    if any(marker in result.stderr for marker in
           (b'error:', b'failed to evaluate', b'errors were encountered')):
        raise ValueError(f'Nix evaluation was incomplete: {diagnostic}')
    return result.stdout


def parse_catalog(data: bytes, system: str, maximum: int) -> list[PackageRef]:
    catalog = json.loads(data)
    if not isinstance(catalog, dict):
        raise ValueError('Nix catalog must be an attribute-to-package object')
    result = []
    for attribute, package in sorted(catalog.items()):
        if not isinstance(package, dict) or not isinstance(package.get('outputs'), dict):
            raise ValueError(f'Nix catalog has invalid outputs for {attribute}')
        if package.get('system') != system:
            raise ValueError(f'Nix catalog returned the wrong system for {attribute}')
        for output, path in sorted(package['outputs'].items()):
            match = STORE_PATH.fullmatch(path) if isinstance(path, str) else None
            if not match:
                raise ValueError(f'Nix output path is unavailable or invalid for {attribute}.{output}')
            # The store hash identifies a rebuild even when its version is unchanged.
            # Aliases and multiple outputs remain distinct logical catalog entries.
            name = f'{attribute}:{output}'
            result.append(PackageRef(name, f'{package.get("name", attribute)}@{match[1]}',
                                     f'{match[1]}.narinfo'))
            if len(result) > maximum:
                raise ValueError(f'Nix catalog exceeds nix_max_paths={maximum}')
    return result


class NixParser(IndexParser):
    def index_url(self) -> str:
        return self.repo.nix_source

    async def check_index_changed(self, client: httpx.AsyncClient,
                                  prev_etag: str | None, prev_last_modified: str | None) -> IndexHeadResult:
        # A source validator does not cover architecture/attribute configuration.
        # Evaluate every cycle; do not mistake nix-cache-info for a package index.
        return IndexHeadResult(False, None, None)

    async def fetch_packages(self, client: httpx.AsyncClient) -> list[PackageRef]:
        # Resolve a moving source once before evaluating selected subtrees.
        # All outputs in one snapshot must belong to the same source tree.
        resolved = await asyncio.to_thread(run_nix, [
            'nix-instantiate', '--eval', '--json', '--expr',
            '{ url }: builtins.fetchTarball url', '--argstr', 'url', self.repo.nix_source,
            '--option', 'tarball-ttl', '0'], self.repo.nix_timeout)
        source = json.loads(resolved)
        if not isinstance(source, str) or not STORE_PATH.fullmatch(source):
            raise ValueError('Nix source resolution did not return a store path')
        command = ['nix-env', '--query', '--available', '--json', '--out-path',
                   '--file', source, '--argstr', 'system', self.repo.arch,
                   '--system-filter', self.repo.arch, '--option', 'tarball-ttl', '0']
        # Repeated --attr options are not a union in nix-env; evaluate selected
        # subtrees separately and prefix returned names to keep their identities.
        if self.repo.nix_attributes:
            catalog = {}
            for attribute in self.repo.nix_attributes:
                data = await asyncio.to_thread(run_nix, command + ['--attr', attribute], self.repo.nix_timeout)
                part = json.loads(data)
                for name, value in part.items():
                    catalog[name if name.startswith(attribute) else attribute + ('.' + name if name else '')] = value
            data = json.dumps(catalog).encode()
        else:
            data = await asyncio.to_thread(run_nix, command, self.repo.nix_timeout)
        return await asyncio.to_thread(parse_catalog, data, self.repo.arch, self.repo.nix_max_paths)
