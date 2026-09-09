#!/usr/bin/env python3
"""CPU/RSS for a single local RPM primary file; no network is used.

Run the stream/whole modes as separate processes: ru_maxrss is the peak
over the process's whole lifetime. whole reproduces the old full-gzip
decompression only for comparison; the production parser uses stream.
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
import platform
import resource
import time

from repowatch.parsers.dnf import _checked_primary, _parse_primary, _parse_repomd, _verify_bytes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repomd', type=Path, required=True)
    parser.add_argument('--primary', type=Path, required=True)
    parser.add_argument('--arch', default='x86_64')
    parser.add_argument('--mode', choices=['stream', 'whole'], default='stream')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    metadata = _parse_repomd(args.repomd.read_bytes())
    raw = args.primary.read_bytes()
    started = time.perf_counter()
    if args.mode == 'whole':
        if not metadata.href.endswith('.gz'):
            parser.error('whole comparison requires gzip primary')
        _verify_bytes(raw, metadata.checksum_type, metadata.checksum, metadata.size)
        opened = gzip.decompress(raw)
        _verify_bytes(opened, metadata.open_checksum_type, metadata.open_checksum, metadata.open_size)
        packages = _parse_primary(opened, args.arch)
    else:
        packages = _checked_primary(raw, metadata, args.arch)
    report = {'mode': args.mode, 'python': platform.python_version(),
              'checksum': metadata.checksum, 'compressed_bytes': len(raw), 'open_bytes': metadata.open_size,
              'packages': len(packages), 'elapsed_s': round(time.perf_counter() - started, 3),
              'max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    args.output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
