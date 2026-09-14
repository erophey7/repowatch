"""APKINDEX v2 signatures via system OpenSSL or apk-tools, never Python RSA.

abuild-sign signs the compressed index member and prepends a separate gzip
member containing .SIGN.RSA[256].<key filename>. We verify those exact bytes.
The external tools are optional dependencies only for enabled verification.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
import re
import subprocess

from repowatch.processes import run
import tarfile
import tempfile
import zlib

from repowatch.gpgverify import SignatureError


def _member(raw: bytes, limit: int) -> tuple[bytes, bytes]:
    stream = zlib.decompressobj(31)
    try:
        decoded = stream.decompress(raw, limit + 1)
    except zlib.error as exc:
        raise SignatureError('APKINDEX: malformed gzip member') from exc
    if len(decoded) > limit or not stream.eof:
        raise SignatureError('APKINDEX: truncated or oversized gzip member')
    return decoded, stream.unused_data


def _run(args: list[str]) -> subprocess.CompletedProcess:
    try:
        result = run(args, text=True, timeout=30,
                                env={**os.environ, 'LC_ALL': 'C'})
    except (OSError, subprocess.SubprocessError) as exc:
        raise SignatureError(f'APKINDEX: cannot run {args[0]}: {exc}') from exc
    if result.returncode:
        raise SignatureError(f'APKINDEX: {args[0]} verification failed: {result.stderr[-2000:]}')
    return result


def verify_index(raw: bytes, keys_dir: str, backend: str = 'openssl') -> bytes:
    """Return only the verified compressed index, excluding its signature tar.

    Shared envelope validation prevents backend-dependent parsing of unsigned
    trailing members. Keys are explicitly operator-provided, never downloaded.
    """
    try:
        signature_tar, payload = _member(raw, 1024 * 1024)
        if not payload:
            raise SignatureError('APKINDEX: signature member missing')
        _, trailing = _member(payload, 256 * 1024 * 1024)
        if trailing:
            raise SignatureError('APKINDEX: unexpected trailing gzip member/data')
        with tarfile.open(fileobj=io.BytesIO(signature_tar), mode='r:') as archive:
            members = archive.getmembers()
            if len(members) != 1 or not members[0].isfile():
                raise SignatureError('APKINDEX: expected one signature')
            member = members[0]
            match = re.fullmatch(r'\.SIGN\.(RSA|RSA256)\.([A-Za-z0-9_@+.-]+)', member.name)
            if not match or match[2] in ('.', '..') or member.size > 16384:
                raise SignatureError('APKINDEX: unsupported signature or key name')
            signature = archive.extractfile(member).read()
        key = Path(keys_dir).resolve() / match[2]
        if not key.is_file():
            raise SignatureError(f'APKINDEX: trusted key missing: {key.name}')
        with tempfile.TemporaryDirectory(prefix='repowatch-apk-') as temporary:
            root = Path(temporary)
            # Give apk-tools exactly the matching configured key, not its
            # global trust store. Neither backend touches the host apk database.
            keys = root / 'keys'; keys.mkdir()
            trusted = keys / key.name
            trusted.write_bytes(key.read_bytes())
            if backend == 'openssl':
                data = root / 'index.gz'; data.write_bytes(payload)
                sig = root / 'signature'; sig.write_bytes(signature)
                _run(['openssl', 'dgst', '-sha1' if match[1] == 'RSA' else '-sha256',
                      '-verify', str(trusted), '-signature', str(sig), str(data)])
            elif backend == 'apk-tools':
                version = _run(['apk', '--version']).stdout
                number = re.search(r'apk-tools\s+(\d+)\.(\d+)', version)
                if not number or int(number[1]) < 3:
                    raise SignatureError('APKINDEX: apk-tools >= 3.0 required for index verification')
                data = root / 'APKINDEX.tar.gz'; data.write_bytes(raw)
                _run(['apk', '--keys-dir', str(keys), '--no-network', 'verify', str(data)])
            else:
                raise SignatureError('APKINDEX: unknown verification backend')
        return payload
    except (OSError, tarfile.TarError, ValueError) as exc:
        raise SignatureError(f'APKINDEX: invalid signature envelope: {exc}') from exc
