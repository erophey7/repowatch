#!/usr/bin/env python3
"""Stage a reviewed release and compatible offline wheels, then upgrade over SSH.

Explicitly invoked only; never a dependency of test/install. SSH must already
work and the operator needs sudo. No credentials are stored in this script.
"""
import argparse
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--wheelhouse', type=Path, required=True)
    parser.add_argument('--manifest', default='/usr/local/share/repowatch/install.json')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', args.target):
        parser.error('target must be an SSH host alias or user@host')
    if not re.fullmatch(r'/[A-Za-z0-9_./+-]+', args.manifest) or '..' in Path(args.manifest).parts:
        parser.error('manifest must be a safe absolute path')
    wheels = list(args.wheelhouse.glob('*.whl'))
    if len(list(args.wheelhouse.glob('repowatch-*.whl'))) != 1:
        parser.error('wheelhouse must contain exactly one app wheel and its target-compatible dependencies')
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='repowatch-release-') as temporary:
        archive = Path(temporary) / 'release.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for name in ('scripts', 'systemd', 'config', 'src', 'pyproject.toml', 'README.md'):
                def exclude(info):
                    return None if any(p in ('__pycache__', 'config.yaml') or p.endswith('.egg-info') for p in Path(info.name).parts) else info
                tar.add(root / name, arcname=name, filter=exclude)
            for wheel in wheels: tar.add(wheel, arcname='wheels/' + wheel.name)
        stage = run(['ssh', '--', args.target, 'mktemp -d /tmp/repowatch-release.XXXXXXXX'],
                    capture_output=True, text=True).stdout.strip()
        if not re.fullmatch(r'/tmp/repowatch-release\.[A-Za-z0-9]+', stage):
            raise ValueError('unexpected staging path from SSH')
        run(['scp', str(archive), args.target + ':' + stage + '/release.tar.gz'])
        command = ('tar -xzf ' + shlex.quote(stage + '/release.tar.gz') + ' -C ' + shlex.quote(stage)
                   + ' && sudo python3 ' + shlex.quote(stage + '/scripts/upgrade-system.py')
                   + ' --manifest ' + shlex.quote(args.manifest) + ' --source ' + shlex.quote(stage)
                   + ' --wheelhouse ' + shlex.quote(stage + '/wheels') + ' --apply')
        run(['ssh', '--', args.target, command])
        print('Release staging (retained for audit):', stage)


if __name__ == '__main__':
    main()
