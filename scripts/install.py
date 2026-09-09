#!/usr/bin/env python3
"""System installer and read-only preflight. Only Python stdlib is needed to check.

Make exports settings instead of interpolating them into shell commands. Runtime
paths never contain DESTDIR. nginx configuration is read, never executed by check
(`nginx -T` also opens logs and is unsuitable for a read-only diagnostic).
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import fnmatch
import glob
import grp
import importlib.util
import json
import logging
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterator

logger = logging.getLogger(__name__)


def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(args, check=True, **kwargs)


def absolute(value: str) -> Path:
    # These paths enter shell wrappers, nginx and systemd as well as the FS.
    # Refuse syntax-bearing names instead of interpreting them differently.
    if not re.fullmatch(r'/[A-Za-z0-9_./+\-]*', value) or '..' in Path(value).parts:
        raise ValueError(f"expected absolute path without whitespace or special characters: {value!r}")
    return Path(value)


@dataclass
class Layout:
    prefix: str = '/usr/local'
    sysconfdir: str = '/etc'
    localstatedir: str = '/var'
    destdir: str = ''
    cache_dir: str = '/var/cache/nginx/repowatch'
    systemd_unit_dir: str = '/etc/systemd/system'
    nginx_conf: str = ''
    nginx_enabled_dir: str = ''
    with_nginx: bool = True
    with_systemd: bool = True

    @classmethod
    def from_env(cls) -> Layout:
        values: dict[str, Any] = {}
        for key in cls.__dataclass_fields__:
            value = os.environ.get(key.upper())
            if value is not None:
                if key.startswith('with_'):
                    if value not in ('0', '1'):
                        raise ValueError(f'{key.upper()} must be 0 or 1')
                    values[key] = value == '1'
                else:
                    values[key] = value
        if 'cache_dir' not in values:
            values['cache_dir'] = values.get('localstatedir', '/var') + '/cache/nginx/repowatch'
        layout = cls(**values)
        for key, value in asdict(layout).items():
            if isinstance(value, str) and value:
                setattr(layout, key, str(absolute(value)))
        if layout.destdir == '/':
            raise ValueError('DESTDIR=/ is not staging; leave DESTDIR empty')
        return layout

    @property
    def config(self) -> Path:
        return Path(self.sysconfdir) / 'repowatch/config.yaml'

    @property
    def state(self) -> Path:
        return Path(self.localstatedir) / 'lib/repowatch'

    @property
    def venv(self) -> Path:
        return Path(self.prefix) / 'lib/repowatch/venv'

    @property
    def share(self) -> Path:
        return Path(self.prefix) / 'share/repowatch'

    @property
    def libexec(self) -> Path:
        return Path(self.prefix) / 'libexec/repowatch'

    @property
    def nginx_policy(self) -> Path:
        return Path(self.nginx_conf or '/etc/nginx/nginx.conf').parent / 'repowatch/policy.json'

    @property
    def cli(self) -> Path:
        return Path(self.prefix) / 'bin/repowatch'

    def disk(self, path: Path | str) -> Path:
        path = absolute(str(path))
        return Path(self.destdir) / path.relative_to('/') if self.destdir else path


# Tokenize rather than grep: comments and quoted semicolons/braces must not
# masquerade as directives. This scanner discovers includes, not nginx validity.
_TOKEN = re.compile(r'''\s+|\#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[;{}]|(?:\\.|[^\s;{}"'\#])+''')


def tokens(text: str) -> Iterator[tuple[str, bool]]:
    end = 0
    for match in _TOKEN.finditer(text):
        if match.start() != end:
            raise ValueError('unsupported or unterminated nginx token')
        end = match.end()
        token = match.group()
        if token.isspace() or token.startswith('#'):
            continue
        delimiter = token in (';', '{', '}')
        if token.startswith(('"', "'")):
            token = shlex.split(token)[0]
        yield token, delimiter
    if end != len(text):
        raise ValueError('unterminated nginx token')


def nginx_includes(config: Path) -> set[str]:
    """Return wildcard includes directly in http, including nested include files.

    Relative includes use nginx's configuration prefix (main config parent),
    not the parent of each included fragment. No nginx -t/-T or file writes.
    """
    patterns: set[str] = set()
    base = config.parent

    def scan(path: Path, context: tuple[str, ...], ancestors: tuple[Path, ...]) -> None:
        resolved = path.resolve()
        if resolved in ancestors or len(ancestors) > 32:
            raise ValueError(f'cyclic/deep nginx include: {path}')
        stack = list(context)
        words: list[str] = []
        for token, delimiter in tokens(path.read_text()):
            if delimiter and token == '{':
                if not words:
                    raise ValueError(f'unnamed nginx block: {path}')
                stack.append(words[0])
                words = []
            elif delimiter and token == '}':
                if words or len(stack) <= len(context):
                    raise ValueError(f'unbalanced nginx block: {path}')
                stack.pop()
            elif delimiter and token == ';':
                if words and words[0] == 'include':
                    if len(words) != 2 or '$' in words[1]:
                        raise ValueError(f'unsupported nginx include: {path}')
                    pattern = Path(words[1])
                    if not pattern.is_absolute():
                        pattern = base / pattern
                    if tuple(stack) == ('http',) and glob.has_magic(pattern.name):
                        if not glob.has_magic(str(pattern.parent)):
                            patterns.add(str(pattern))
                    matches = sorted(glob.glob(str(pattern)))
                    if not matches and not glob.has_magic(str(pattern)):
                        raise ValueError(f'missing nginx include: {pattern}')
                    for included in matches:
                        scan(Path(included), tuple(stack), ancestors + (resolved,))
                words = []
            else:
                words.append(token)
        if words or len(stack) != len(context):
            raise ValueError(f'incomplete nginx configuration: {path}')

    scan(config, (), ())
    return patterns


def select_nginx_dir(config: Path, explicit: str = '') -> Path:
    patterns = nginx_includes(config)
    candidates = {Path(p).parent for p in patterns
                  if fnmatch.fnmatchcase('repowatch.conf', Path(p).name)}
    if explicit:
        chosen = absolute(explicit)
        if chosen not in candidates:
            raise ValueError(f'NGINX_ENABLED_DIR={chosen} is not included in http for repowatch.conf')
        return chosen
    if len(candidates) != 1:
        found = ', '.join(str(p) for p in sorted(candidates)) or 'none'
        raise ValueError(f'nginx enabled directory is ambiguous/missing ({found}); set NGINX_CONF and NGINX_ENABLED_DIR to an included directory')
    return candidates.pop()


def default_nginx_conf() -> Path:
    result = run(['nginx', '-V'], capture_output=True, text=True, timeout=10)
    args = shlex.split(result.stderr + result.stdout)
    for arg in args:
        if arg.startswith('--conf-path='):
            return absolute(arg.split('=', 1)[1])
    raise ValueError('cannot discover nginx main configuration; set NGINX_CONF')


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def emit(self, level: str, name: str, detail: str) -> None:
        self.failures += level == 'FAIL'
        logger.info('%-4s %-22s %s', level, name, detail)

    def tool(self, name: str, required: bool = True) -> None:
        path = shutil.which(name)
        self.emit('OK' if path else 'FAIL' if required else 'WARN', name,
                  path or 'not found' + ('' if required else ' (optional)'))

    def path(self, name: str, path: Path, directory: bool = True,
             expected_link: Path | None = None, preserve: bool = False) -> None:
        # Reject symlink parents: particularly important for staging as root.
        for parent in path.parents:
            if parent.is_symlink():
                self.emit('FAIL', name, f'symlink parent: {parent}')
                return
        if path.is_symlink():
            if expected_link is None or os.readlink(path) != str(expected_link):
                self.emit('FAIL', name, f'conflicting symlink: {path}')
                return
        elif path.exists() and (path.is_dir() != directory):
            self.emit('FAIL', name, f'wrong file type: {path}')
            return
        parent = path if path.exists() and directory else path.parent
        while not parent.exists():
            parent = parent.parent
        if not parent.is_dir():
            self.emit('FAIL', name, f'parent is not a directory: {parent}')
            return
        info = parent.stat()
        writable = os.access(parent, os.W_OK | os.X_OK)
        # Regular existing files are atomically replaced; parent permissions matter.
        self.emit('OK' if writable else 'FAIL', name,
                  f'{path}; parent={parent} uid:gid={info.st_uid}:{info.st_gid} '
                  f'mode={stat.S_IMODE(info.st_mode):04o}; '
                  + ('preserve existing' if preserve and path.exists() else 'writable' if writable else 'not writable'))


def python_dependencies(report: Report, executable: Path, required: bool = False) -> None:
    if not executable.exists():
        report.emit('WARN', 'target venv', f'{executable}: will be created during install')
        return
    result = subprocess.run([str(executable), '-B', '-c',
        'import importlib.metadata as m; print("PyYAML="+m.version("PyYAML")); print("httpx="+m.version("httpx"))'],
        capture_output=True, text=True, timeout=10)
    report.emit('OK' if result.returncode == 0 else 'FAIL' if required else 'WARN',
                'Python dependencies', result.stdout.strip().replace('\n', ', ') if result.returncode == 0
                else f'{executable}: dependencies not installed; wheel install will supply them')


def check(layout: Layout, for_install: bool = True) -> Report:
    report = Report()
    report.emit('OK' if sys.version_info >= (3, 11) else 'FAIL', 'Python', sys.version.split()[0])
    for module in ('venv', 'ensurepip', 'sqlite3', 'ssl', 'pip'):
        report.emit('OK' if importlib.util.find_spec(module) else 'FAIL', module, 'host stdlib')
    report.tool('make')
    report.tool('bash')
    for tool in ('sqlite3', 'gzip', 'find'):
        report.tool(tool, required=layout.with_systemd and not layout.destdir)
    for tool in ('gpgv', 'openssl', 'apk'):
        report.tool(tool, required=False)
    report.emit('WARN', 'APK signatures', 'optional unless enabled in repository config; checked below per backend')
    python_dependencies(report, Path(sys.executable))
    python_dependencies(report, layout.disk(layout.venv / 'bin/python'))
    if layout.with_systemd:
        report.tool('systemctl', required=not bool(layout.destdir))
        for path in (layout.prefix, layout.sysconfdir, layout.localstatedir):
            if Path(path).parts[1:2] in (('home',), ('root',)):
                report.emit('FAIL', 'systemd ProtectHome', f'{path}: choose paths outside /home and /root, or WITH_SYSTEMD=0')
        report.emit('OK' if Path('/run/systemd/system').is_dir() else 'WARN', 'systemd running',
                    'needed only for activation, not file installation')
        if for_install and not layout.destdir and shutil.which('systemctl') and Path('/run/systemd/system').is_dir():
            active = subprocess.run(['systemctl', 'is-active', '--quiet', 'repowatch.service',
                                     'repowatch-status.service', 'repowatch-check.service', 'repowatch-check.timer',
                                     'repowatch-backup.service', 'repowatch-backup.timer',
                'repowatch-nginx.service', 'repowatch-nginx.timer'],
                                    capture_output=True, timeout=10)
            if active.returncode == 0:
                report.emit('FAIL', 'running application', 'stop repowatch services/timers before updating the environment')
    if layout.with_nginx:
        report.tool('nginx', required=not bool(layout.destdir))
        try:
            if layout.destdir:
                if not layout.nginx_enabled_dir:
                    raise ValueError('DESTDIR requires explicit target NGINX_ENABLED_DIR; host nginx is not the target')
                report.emit('WARN', 'nginx target include', 'staging: validate include on target before activation')
            else:
                config = absolute(layout.nginx_conf) if layout.nginx_conf else default_nginx_conf()
                chosen = select_nginx_dir(config, layout.nginx_enabled_dir)
                source = 'explicit, verified' if layout.nginx_enabled_dir else 'detected'
                layout.nginx_conf = str(config)
                layout.nginx_enabled_dir = str(chosen)
                report.emit('OK', 'nginx enabled_sites', f'{chosen} ({source}, from {config})')
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            report.emit('FAIL', 'nginx enabled_sites', str(exc))
    destinations = [
        ('venv', layout.venv), ('config directory', layout.config.parent),
        ('state', layout.state), ('backups', layout.state / 'backups'),
        ('libexec', layout.libexec), ('share', layout.share),
    ]
    if layout.with_nginx:
        destinations.append(('package cache', Path(layout.cache_dir)))
        if layout.nginx_enabled_dir:
            destinations.append(('nginx include dir', Path(layout.nginx_enabled_dir)))
    if layout.with_systemd:
        destinations.append(('systemd units', Path(layout.systemd_unit_dir)))
    for name, runtime in destinations:
        report.emit('OK', name + ' runtime', str(runtime))
        report.path(name, layout.disk(runtime))
    report.path('config file', layout.disk(layout.config), directory=False, preserve=True)
    report.path('CLI', layout.disk(layout.cli), directory=False, expected_link=layout.venv / 'bin/repowatch')
    if layout.disk(layout.cli).exists() and not layout.disk(layout.cli).is_symlink():
        report.emit('FAIL', 'CLI conflict', f'{layout.cli} is an existing regular file')
    if layout.with_nginx and layout.nginx_enabled_dir:
        report.path('nginx site', layout.disk(Path(layout.nginx_enabled_dir) / 'repowatch.conf'),
                    directory=False, expected_link=layout.nginx_policy.parent / 'active.conf')
    # Fail before pip writes if any generated artifact conflicts with a symlink/directory.
    artifacts = [layout.share / 'install.json', layout.share / 'config.example.yaml',
                 layout.libexec / 'backup-state.sh', layout.libexec / 'backup-config.py',
                 layout.libexec / 'install.py']
    if layout.with_nginx:
        artifacts.extend([layout.nginx_policy, layout.nginx_policy.parent / 'active.conf'])
    if layout.with_systemd:
        names = ('repowatch.service', 'repowatch-status.service', 'repowatch-check.service',
                 'repowatch-check.timer', 'repowatch-backup.service', 'repowatch-backup.timer',
                 'repowatch-nginx.service', 'repowatch-nginx.timer')
        artifacts.extend(layout.share / 'systemd' / name for name in names)
        artifacts.extend(Path(layout.systemd_unit_dir) / name for name in names)
    for artifact in artifacts:
        disk = layout.disk(artifact)
        if disk.is_symlink() or disk.is_dir() or any(p.is_symlink() for p in disk.parents):
            report.emit('FAIL', 'file conflict', str(disk))
    manifest = layout.disk(layout.share / 'install.json')
    if layout.disk(layout.venv).exists() and not manifest.is_file():
        report.emit('FAIL', 'existing installation', 'venv without install.json; refuse to overwrite unmanaged environment')
    if manifest.is_file():
        try:
            previous = json.loads(manifest.read_text())
            for field in ('prefix', 'sysconfdir', 'localstatedir', 'cache_dir'):
                if previous[field] != getattr(layout, field):
                    report.emit('FAIL', 'layout changed', f'{field}: migrate data/config explicitly before changing paths')
        except (ValueError, KeyError) as exc:
            report.emit('FAIL', 'install manifest', str(exc))
    # Inspect existing configuration using an available environment, without DB access.
    config = layout.disk(layout.config)
    if config.is_file() and not config.is_symlink():
        interpreter = layout.disk(layout.venv / 'bin/python')
        if not interpreter.exists():
            interpreter = Path(sys.executable)
        probe = subprocess.run([str(interpreter), '-B', '-c',
            'import sys, json; from repowatch.config import load_config; '
            'c=load_config(sys.argv[1]); print(json.dumps({"signed": '
            '[[r.id,r.type,r.keyring_path,getattr(r,"apk_keys_dir",None),getattr(r,"apk_signature_backend","openssl")] for r in c.repos if r.verify_signature], "db": str(c.state_db), '
            '"tls": [c.status_server.tls_cert_path, c.status_server.tls_key_path]}))', str(config)],
            capture_output=True, text=True, timeout=15)
        if probe.returncode:
            missing = 'ModuleNotFoundError' in probe.stderr
            report.emit('WARN' if missing else 'FAIL', 'existing YAML',
                        'cannot validate without installed application' if missing else 'invalid config; run repowatch check-config for details')
        else:
            data = json.loads(probe.stdout)
            report.emit('OK', 'configured state_db', data['db'] + ' (existing YAML preserved)')
            for pem in filter(None, data['tls']):
                pem_file = layout.disk(absolute(pem))
                report.emit('OK' if pem_file.is_file() and os.access(pem_file, os.R_OK) else 'FAIL',
                            'TLS PEM', str(pem_file))
            report.path('configured DB', layout.disk(absolute(data['db'])), directory=False, preserve=True)
            for repo, kind, key, keys_dir, backend in data['signed']:
                tool = ('apk' if backend == 'apk-tools' else 'openssl') if kind == 'apk' else 'gpgv'
                report.tool(tool, required=not bool(layout.destdir))
                key_file = layout.disk(absolute(keys_dir if kind == 'apk' else key))
                valid = key_file.is_dir() and any(key_file.glob('*.pub')) if kind == 'apk' else key_file.is_file()
                report.emit('OK' if valid and os.access(key_file, os.R_OK) else 'FAIL',
                            f'trusted keys {repo}', str(key_file))
                if kind == 'apk' and backend == 'apk-tools' and not layout.destdir and shutil.which('apk'):
                    version = subprocess.run(['apk', '--version'], capture_output=True, text=True, timeout=10)
                    number = re.search(r'apk-tools\s+(\d+)\.', version.stdout)
                    report.emit('OK' if version.returncode == 0 and number and int(number[1]) >= 3 else 'FAIL',
                                'apk-tools version', 'APKINDEX verification requires >= 3.0')
    logger.info('%-4s %-22s %s', 'FAIL' if report.failures else 'OK', 'summary',
                f'{report.failures} blocking problem(s); no system changes made')
    return report


def write(path: Path, content: str, mode: int = 0o644, preserve: bool = False) -> None:
    if path.is_symlink():
        raise ValueError(f'refuse to overwrite symlink: {path}')
    if preserve and path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def build_wheels(root: Path, wheelhouse: Path) -> None:
    wheelhouse.mkdir(parents=True, exist_ok=True)
    # Build from an isolated source copy: setuptools must not leave root-owned
    # build/egg-info in the user's source tree during sudo make install.
    with tempfile.TemporaryDirectory(prefix='repowatch-build-') as temporary:
        source = Path(temporary)
        shutil.copy2(root / 'pyproject.toml', source)
        shutil.copytree(root / 'src', source / 'src', ignore=shutil.ignore_patterns('__pycache__', '*.egg-info'))
        run([sys.executable, '-m', 'pip', 'wheel', '--wheel-dir', str(wheelhouse.resolve()), str(source)])


def install(layout: Layout, root: Path, wheelhouse: Path) -> None:
    if check(layout).failures:
        raise ValueError('installation blocked by preflight')
    wheels = list(wheelhouse.glob('repowatch-*.whl'))
    if len(wheels) != 1:
        raise ValueError('wheelhouse must contain exactly one repowatch wheel; use a fresh WHEELHOUSE')
    for directory in (layout.state, layout.state / 'backups', layout.config.parent):
        path = layout.disk(directory)
        if not path.exists():
            path.mkdir(parents=True, mode=0o750)
    venv = layout.disk(layout.venv)
    run([sys.executable, '-m', 'venv', str(venv)])
    python = venv / 'bin/python'
    run([str(python), '-m', 'pip', 'install', '--no-index', '--find-links', str(wheelhouse.resolve()),
         '--upgrade', '--force-reinstall', str(wheels[0].resolve())])
    purelib = Path(run([str(python), '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'],
                       capture_output=True, text=True).stdout.strip())
    write(purelib / 'repowatch/_paths.py', f'DEFAULT_CONFIG_PATH = {str(layout.config)!r}\n')
    # A staged venv targets the same OS/architecture/Python installation.
    # Rewrite generated activation scripts/shebangs; DESTDIR is never runtime.
    if layout.destdir:
        for path in (venv / 'bin').iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            try:
                content = path.read_text()
            except UnicodeDecodeError:
                continue
            write(path, content.replace(str(venv), str(layout.venv)), stat.S_IMODE(path.stat().st_mode))
        cfg = venv / 'pyvenv.cfg'
        write(cfg, cfg.read_text().replace(str(venv), str(layout.venv)))
    config_text = (root / 'config/config.example.yaml').read_text().replace(
        '/var/lib/repowatch', str(layout.state)).replace('/etc/repowatch', str(layout.config.parent))
    write(layout.disk(layout.share / 'config.example.yaml'), config_text)
    write(layout.disk(layout.config), config_text, mode=0o640, preserve=True)
    backup = (root / 'scripts/backup-state.sh').read_text().replace('/var/lib/repowatch', str(layout.state))
    write(layout.disk(layout.libexec / 'backup-state.sh'), backup, 0o755)
    write(layout.disk(layout.libexec / 'backup-config.py'), (root / 'scripts/backup-config.py').read_text())
    write(layout.disk(layout.libexec / 'install.py'), (root / 'scripts/install.py').read_text())
    if layout.with_systemd:
        db_parent = absolute(run([str(python), '-B', '-c',
            'from repowatch.config import load_config; import sys; print(load_config(sys.argv[1]).state_db.parent)',
            str(layout.disk(layout.config))], capture_output=True, text=True).stdout.strip())
        for source in sorted((root / 'systemd').glob('repowatch*')):
            if source.name.startswith('repowatch-nginx') and not layout.with_nginx:
                continue
            content = source.read_text().replace('/usr/local/bin/repowatch', str(layout.cli))
            content = content.replace('/etc/repowatch', str(layout.config.parent))
            content = content.replace('/etc/nginx/repowatch/policy.json', str(layout.nginx_policy))
            content = content.replace('/var/lib/repowatch', str(layout.state))
            content = content.replace('/opt/repowatch/src/scripts/backup-state.sh',
                f'{layout.venv}/bin/python {layout.libexec}/backup-config.py {layout.config} {layout.state}/backups')
            if source.name == 'repowatch.service' and db_parent != layout.state:
                content = content.replace('[Install]', f'ReadWritePaths={db_parent}\n\n[Install]')
            write(layout.disk(layout.share / 'systemd' / source.name), content)
    cli = layout.disk(layout.cli)
    cli.parent.mkdir(parents=True, exist_ok=True)
    if not cli.is_symlink():
        if cli.exists():
            raise ValueError(f'CLI already exists and is not our link: {cli}')
        cli.symlink_to(layout.venv / 'bin/repowatch')
    # Publish the manifest last; it also records integration choices for activate.
    settings = asdict(layout)
    settings['destdir'] = ''
    write(layout.disk(layout.share / 'install.json'), json.dumps(settings, indent=2) + '\n')
    logger.info('Installed files. Existing config/data preserved. Services not started; use make activate explicitly.')


def activate(layout: Layout) -> None:
    if layout.destdir:
        raise ValueError('activation is forbidden with DESTDIR')
    if os.geteuid() != 0:
        raise ValueError('activation requires root')
    manifest = layout.share / 'install.json'
    saved = json.loads(manifest.read_text())
    saved['destdir'] = ''
    installed = Layout(**saved)
    # Activate the installed layout, not a second set of make defaults.
    if installed.prefix != layout.prefix:
        raise ValueError('installation prefix mismatch')
    if check(installed, for_install=False).failures:
        raise ValueError('activation blocked by preflight')
    python = str(installed.venv / 'bin/python')
    run([python, '-c', 'from repowatch.cli import main; import sys; sys.exit(main())',
         '-c', str(installed.config), 'check-config'])
    try:
        group = grp.getgrnam('repowatch')
    except KeyError:
        run(['groupadd', '--system', 'repowatch'])
        group = grp.getgrnam('repowatch')
    try:
        user = pwd.getpwnam('repowatch')
    except KeyError:
        run(['useradd', '--system', '--gid', 'repowatch', '--no-create-home', '--shell', '/usr/sbin/nologin', 'repowatch'])
        user = pwd.getpwnam('repowatch')
    # No recursive chown over existing data or an NFS cache.
    for path in (installed.config.parent, installed.config, installed.state, installed.state / 'backups'):
        os.chown(path, user.pw_uid, group.gr_gid)
    if installed.with_nginx:
        cache = Path(installed.cache_dir)
        cache.mkdir(parents=True, exist_ok=True)
        Path(installed.localstatedir, 'log/nginx').mkdir(parents=True, exist_ok=True)
        # The YAML remains service-writable; privileged file destinations do not.
        policy = installed.nginx_policy
        policy.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        write(policy, json.dumps({
            'cache_dir': installed.cache_dir,
            'access_log': installed.localstatedir + '/log/nginx/repo-cache.access.log',
            'nginx_conf': installed.nginx_conf,
            'nginx_binary': shutil.which('nginx'),
            'site_link': str(Path(installed.nginx_enabled_dir) / 'repowatch.conf'),
        }, indent=2) + '\n', preserve=True)
        candidate = run([python, '-B', '-c',
            'import sys; from repowatch.config import load_config; from repowatch.nginx import render; '
            'print(render(load_config(sys.argv[1]), cache_dir=sys.argv[2], access_log=sys.argv[3]), end="")',
            str(installed.config), installed.cache_dir,
            installed.localstatedir + '/log/nginx/repo-cache.access.log'], capture_output=True, text=True).stdout
        active = policy.parent / 'active.conf'
        had_active = active.exists()
        write(active, candidate, preserve=True)
        link = Path(installed.nginx_enabled_dir) / 'repowatch.conf'
        created = not link.is_symlink()
        if created:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(active)
        try:
            run(['nginx', '-t', '-c', installed.nginx_conf])
        except subprocess.CalledProcessError:
            if created:
                link.unlink()
            if not had_active:
                active.unlink()
            raise
    if installed.with_systemd:
        units = Path(installed.systemd_unit_dir)
        units.mkdir(parents=True, exist_ok=True)
        for source in (installed.share / 'systemd').iterdir():
            write(units / source.name, source.read_text())
        run(['systemctl', 'link', *[str(units / source.name) for source in (installed.share / 'systemd').iterdir()]])
        run(['systemctl', 'daemon-reload'])
        if installed.with_nginx:
            run(['systemctl', 'enable', '--now', 'nginx'])
            run(['systemctl', 'reload', 'nginx'])
            run(['systemctl', 'enable', '--now', 'repowatch-nginx.timer'])
        run(['systemctl', 'enable', '--now', 'repowatch.service', 'repowatch-backup.timer'])
        run(['systemctl', 'restart', 'repowatch.service'])
    else:
        if installed.with_nginx:
            logger.info('nginx configuration linked and validated.')
        logger.info(
            'WITH_SYSTEMD=0: no systemd units installed. Run `repowatch supervise` '
            'in place of repowatch.service/repowatch-nginx.timer/repowatch-backup.timer '
            '(see docs/deployment.md, "Running without systemd"), or start/reload '
            '`repowatch run` and nginx using your own supervisor.'
        )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'wheel', 'install', 'activate'))
    args = parser.parse_args()
    try:
        layout = Layout.from_env()
        root = Path(__file__).resolve().parent.parent
        wheelhouse = Path(os.environ.get('WHEELHOUSE', 'build/wheels'))
        if args.command == 'check':
            return int(check(layout).failures > 0)
        if args.command == 'wheel':
            build_wheels(root, wheelhouse)
        elif args.command == 'install':
            install(layout, root, wheelhouse)
        else:
            activate(layout)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.error('FAIL %s', exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
