"""Nix reference-closure discovery and multi-artifact cache operations."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import tempfile
import subprocess
from urllib.parse import urlsplit, urljoin

import httpx

from repowatch.gpgverify import SignatureError
from repowatch.parsers.base import USER_AGENT
from repowatch.parsers.nix import STORE_PATH, run_nix

ALPHABET = '0123456789abcdfghijklmnpqrsvwxyz'
HASH = r'[0123456789abcdfghijklmnpqrsvwxyz]{32}'
NARINFO = re.compile(HASH + r'\.narinfo\Z')


def sha256_hex(value: str) -> str:
    algorithm, encoded = value.split(':', 1)
    if algorithm != 'sha256':
        raise ValueError('Nix FileHash must use SHA256')
    if re.fullmatch('[0-9a-f]{64}', encoded):
        return encoded
    if len(encoded) != 52 or any(c not in ALPHABET for c in encoded):
        raise ValueError('Invalid Nix SHA256 encoding')
    number = 0
    for char in encoded:
        number = (number << 5) | ALPHABET.index(char)
    try:
        return number.to_bytes(32, 'little').hex()
    except OverflowError as exc:
        raise ValueError('Invalid Nix SHA256 padding') from exc


def nar_filename(upstream: str, value: str) -> str:
    """Support relative and same-origin absolute URLs, preserving query bytes.

    Foreign origins and path traversal cannot be represented safely by the
    configured nginx route. Reject them explicitly instead of bypassing it.
    """
    base = urlsplit(upstream.rstrip('/') + '/')
    parsed = urlsplit(urljoin(upstream.rstrip('/') + '/', value))
    original = urlsplit(value)
    if (parsed.scheme != base.scheme or parsed.netloc != base.netloc
            or parsed.fragment or parsed.username or parsed.password
            or not parsed.path.startswith(base.path)
            or any(p in ('.', '..') for p in original.path.split('/'))
            or not re.fullmatch(r'/[A-Za-z0-9_./+~-]+', parsed.path)
            or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError('Nix NAR URL must stay within its configured binary cache')
    filename = parsed.path[len(base.path):]
    if not filename or filename.startswith('/'):
        raise ValueError('Invalid Nix NAR URL')
    return filename + ('?' + parsed.query if parsed.query else '')


def parse_narinfo(raw: bytes, filename: str, upstream: str) -> dict:
    if not NARINFO.fullmatch(filename):
        raise ValueError('Invalid narinfo filename')
    fields: dict[str, str] = {}
    for line in raw.decode('utf-8').splitlines():
        if not line:
            continue
        name, sep, value = line.partition(': ')
        if not sep or (name in fields and name != 'Sig'):
            raise ValueError('Malformed or duplicate narinfo field')
        fields[name] = value
    path = fields.get('StorePath', '')
    match = STORE_PATH.fullmatch(path)
    if not match or match[1] + '.narinfo' != filename:
        raise ValueError('narinfo StorePath does not match the requested key')
    if not fields.get('NarHash') or not fields.get('URL') or int(fields.get('NarSize', '-1')) < 0:
        raise ValueError('narinfo requires URL, NarHash and nonnegative NarSize')
    sha256_hex(fields['NarHash'])
    refs = []
    for reference in fields.get('References', '').split():
        ref = STORE_PATH.fullmatch('/nix/store/' + reference)
        if not ref:
            raise ValueError('Invalid narinfo reference')
        refs.append(ref[1] + '.narinfo')
    size = int(fields['FileSize']) if 'FileSize' in fields else None
    if size is not None and size < 0:
        raise ValueError('Negative Nix FileSize')
    return dict(path=path, refs=refs, filename=nar_filename(upstream, fields['URL']),
                content_hash=sha256_hex(fields['FileHash']) if 'FileHash' in fields else None,
                size=size)


def verify_metadata(records: dict[str, tuple[bytes, dict]], keys: list[str], timeout: int) -> None:
    """Ask Nix to verify exactly the downloaded narinfo bytes, offline.

    Nix signatures authenticate NarHash/NarSize/references, not FileHash.
    NAR content validation remains the consuming Nix client's responsibility.
    """
    with tempfile.TemporaryDirectory(prefix='repowatch-nix-signatures-') as directory:
        root = Path(directory)
        (root / 'nix-cache-info').write_text('StoreDir: /nix/store\nWantMassQuery: 0\nPriority: 40\n')
        paths = []
        for filename, (raw, info) in records.items():
            # Nix otherwise treats content-addressed objects as trusted without
            # checking Sig when --no-contents is used. CA is not part of the
            # signature fingerprint; omit it only in this verification copy to
            # require an actual trusted signature. Proxy bytes stay unchanged.
            signed_fields = b'\n'.join(line for line in raw.split(b'\n') if not line.startswith(b'CA: '))
            (root / filename).write_bytes(signed_fields)
            paths.append(info['path'])
        for offset in range(0, len(paths), 128):
            try:
                run_nix(['nix', '--extra-experimental-features', 'nix-command', 'store', 'verify',
                         '--store', root.as_uri(), '--no-contents', '--sigs-needed', '1',
                         '--option', 'trusted-public-keys', ' '.join(keys),
                         *paths[offset:offset + 128]], timeout)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                raise SignatureError(f'Nix narinfo signature verification failed: {exc}') from exc


async def discover(client: httpx.AsyncClient, repo, root: str, cached: dict | None = None) -> dict[str, tuple[bytes, dict]]:
    records = {}
    cached = cached if cached is not None else {}
    pending = [root]
    while pending:
        filename = pending.pop()
        if filename in records:
            continue
        if len(records) >= repo.nix_max_paths:
            raise ValueError('Nix closure exceeds nix_max_paths')
        if not NARINFO.fullmatch(filename):
            raise ValueError('Invalid Nix root filename')
        if filename in cached:
            records[filename] = cached[filename]
            pending.extend(cached[filename][1]['refs'])
            continue
        async with client.stream('GET', repo.upstream.rstrip('/') + '/' + filename,
                                 headers={'User-Agent': USER_AGENT}, timeout=30) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 1024 * 1024:
                    raise ValueError('narinfo exceeds 1 MiB')
        info = parse_narinfo(bytes(raw), filename, repo.upstream)
        records[filename] = (bytes(raw), info)
        pending.extend(info['refs'])
    if repo.verify_signature:
        await asyncio.to_thread(verify_metadata, records, repo.nix_public_keys, repo.nix_timeout)
    cached.update(records)
    return records


async def warm(config, repo, store, packages: dict[str, str], force: bool) -> dict[str, bool]:
    from repowatch.prefetch import BandwidthLimiter, _build_warm_url, _warm_one
    from repowatch.notifications import record_failure_and_maybe_notify, record_success_and_maybe_notify
    if not force and not repo.prefetch:
        return {}
    store.update_nix_trust(repo.id, repo.verify_signature, repo.nix_public_keys)
    banned = set(store.get_banned_packages(repo.id))
    names = store.get_names(repo.id)
    limiter = BandwidthLimiter(config.effective_prefetch_bandwidth_limit(repo))
    outcomes = {}
    discovered = {}
    signature_failed = False
    signature_checked = False
    # Shared HTTP artifacts are downloaded once per run, across root closures.
    warmed: dict[tuple, asyncio.Task] = {}
    async with httpx.AsyncClient() as client:
        cache_info_ok = None
        for key, filename in packages.items():
            if names.get(key) in banned:
                continue
            ok, status = False, None
            try:
                if cache_info_ok is None:
                    cache_info_ok, _ = await _warm_one(client, _build_warm_url(config, repo, 'nix-cache-info'), limiter)
                if not cache_info_ok:
                    raise ValueError('Nix cache-info could not be warmed')
                records = await discover(client, repo, filename, discovered)
                signature_checked = signature_checked or repo.verify_signature
                artifacts = []
                for metadata, (raw, info) in records.items():
                    artifacts.extend([dict(filename=metadata, content_hash=hashlib.sha256(raw).hexdigest(), size=len(raw)),
                                      {k: info[k] for k in ('filename', 'content_hash', 'size')}])
                store.record_nix_artifacts(repo.id, key, artifacts)
                semaphore = asyncio.Semaphore(config.prefetch_concurrency)
                async def download(artifact):
                    name = artifact['filename']
                    async with semaphore:
                        result = await _warm_one(client, _build_warm_url(config, repo, name), limiter,
                                                 expected_sha256=artifact.get('content_hash'),
                                                 expected_size=artifact.get('size'))
                        if not result[0] and result[1] == 200 and config.nginx.enable_purge:
                            # A cached 200 with wrong bytes would otherwise fail
                            # forever on every retry. Evict it before one refetch.
                            from repowatch.cache_probe import purge_selected_raw
                            from repowatch.prefetch import purge_selected
                            purge_file = purge_selected_raw if config.nginx.enable_cache_probe else purge_selected
                            removed = await purge_file(config, repo, {name: name})
                            if removed[name] in ('purged', 'not_cached'):
                                result = await _warm_one(client, _build_warm_url(config, repo, name), limiter,
                                                         expected_sha256=artifact.get('content_hash'),
                                                         expected_size=artifact.get('size'))
                        return result
                async def fetch(artifact):
                    identity = (artifact['filename'], artifact.get('content_hash'), artifact.get('size'))
                    if identity not in warmed:
                        warmed[identity] = asyncio.create_task(download(artifact))
                    return await warmed[identity]
                results = await asyncio.gather(*(fetch(a) for a in artifacts))
                ok = all(result[0] for result in results)
                status = 200 if ok else next((s for success, s in results if not success), None)
            except (ValueError, OSError, httpx.HTTPError, SignatureError, subprocess.SubprocessError) as exc:
                import logging
                signature_failed = signature_failed or isinstance(exc, SignatureError)
                logging.getLogger(__name__).warning('%s: Nix warm failed for %s: %s', repo.id, key, exc)
            store.record_warmed_package(repo.id, key, filename, ok, status, source='prefetch')
            outcomes[key] = ok
    if signature_failed:
        await record_failure_and_maybe_notify(config, store, repo.id, 'gpg', 'Nix narinfo signature verification failed')
    elif signature_checked:
        await record_success_and_maybe_notify(config, store, repo.id, 'gpg')
    if outcomes:
        if all(outcomes.values()):
            await record_success_and_maybe_notify(config, store, repo.id, 'prefetch')
        else:
            await record_failure_and_maybe_notify(config, store, repo.id, 'prefetch',
                                                 f'{sum(not ok for ok in outcomes.values())}/{len(outcomes)} Nix closures failed to warm')
    return outcomes


async def purge(config, repo, store, packages: dict[str, str]) -> dict[str, str]:
    from repowatch.cache_probe import purge_selected_raw
    from repowatch.prefetch import purge_selected
    # Preserve files needed by other current catalog entries. An explicit purge
    # evicts this selection's exclusive artifacts, never breaks shared closures.
    shared = store.nix_shared_files(repo.id, list(packages))
    unknown_owners = store.nix_has_unknown_owners(repo.id, list(packages))
    current = store.get_packages(repo.id) or {}
    shared.update(filename for key, filename in current.items() if key not in packages)
    selections = {}
    retained = {}
    files = {}
    for key, filename in packages.items():
        names = {filename} | {a['filename'] for a in store.get_nix_artifacts(repo.id, key)}
        if unknown_owners:
            shared.update(names)
        retained[key] = bool(names & shared)
        selections[key] = names - shared
        files.update((name, name) for name in selections[key])
    function = purge_selected_raw if config.nginx.enable_cache_probe else purge_selected
    results = await function(config, repo, files)
    outcomes = {}
    for key, names in selections.items():
        values = [results[name] for name in names]
        errors = [v for v in values if v not in ('purged', 'not_cached')]
        outcomes[key] = errors[0] if errors else ('retained_shared' if retained[key] else ('purged' if 'purged' in values else 'not_cached'))
    return outcomes
