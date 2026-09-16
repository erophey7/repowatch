"""Installation preflight must be read-only and must not guess nginx layout."""
from __future__ import annotations
import repowatch.cli.main as cli_main

import importlib.util
import logging
from pathlib import Path
import subprocess
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location('repowatch_installer', Path(__file__).parents[1] / 'scripts/install.py')
installer = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = installer
_SPEC.loader.exec_module(installer)


def layout_at(tmp_path, **kwargs):
    return installer.Layout(prefix=str(tmp_path / 'usr'), sysconfdir=str(tmp_path / 'etc'),
                            localstatedir=str(tmp_path / 'var'), cache_dir=str(tmp_path / 'var/cache'),
                            with_systemd=False, with_nginx=False, **kwargs)


def test_nginx_nested_includes_relative_to_main_config(tmp_path):
    (tmp_path / 'parts').mkdir()
    (tmp_path / 'parts/http.conf').write_text('include sites-enabled/*;')
    config = tmp_path / 'nginx.conf'
    config.write_text('events {} http { include parts/http.conf; }')
    assert installer.select_nginx_dir(config) == tmp_path / 'sites-enabled'
    assert not (tmp_path / 'sites-enabled').exists()


def test_nginx_ignores_comments_quoted_directives_and_wrong_context(tmp_path):
    config = tmp_path / 'nginx.conf'
    config.write_text('''
# include fake/*.conf;
stream { include streams/*.conf; }
http {
  log_format custom "include fake/*.conf; } {";
  include "conf.d/*.conf";
  server { include snippets/*.conf; }
}
''')
    assert installer.select_nginx_dir(config) == tmp_path / 'conf.d'


def test_nginx_ambiguous_requires_explicit_included_path(tmp_path):
    config = tmp_path / 'nginx.conf'
    config.write_text('http { include sites-enabled/*; include conf.d/*.conf; }')
    with pytest.raises(ValueError, match='ambiguous'):
        installer.select_nginx_dir(config)
    assert installer.select_nginx_dir(config, str(tmp_path / 'conf.d')) == tmp_path / 'conf.d'
    with pytest.raises(ValueError, match='not included'):
        installer.select_nginx_dir(config, str(tmp_path / 'exists'))


@pytest.mark.parametrize('body', [
    'http { include conf.d/*.vhost; }',
    '# include sites-enabled/*;\nhttp { server {} }',
    'http { server { include sites-enabled/*; } }',
])
def test_nginx_directory_must_include_our_filename_in_http(tmp_path, body):
    (tmp_path / 'sites-enabled').mkdir()
    config = tmp_path / 'nginx.conf'
    config.write_text(body)
    with pytest.raises(ValueError):
        installer.select_nginx_dir(config)


def test_nginx_cycle_is_reported(tmp_path):
    config = tmp_path / 'nginx.conf'
    config.write_text('http { include nginx.conf; }')
    with pytest.raises(ValueError, match='cyclic'):
        installer.select_nginx_dir(config)


def test_stage_does_not_inspect_host_nginx(tmp_path, monkeypatch):
    layout = layout_at(tmp_path, destdir=str(tmp_path / 'stage'))
    layout.with_nginx = True
    layout.nginx_enabled_dir = '/etc/nginx/conf.d'
    monkeypatch.setattr(installer, 'default_nginx_conf', lambda: pytest.fail('read host nginx'))
    assert installer.check(layout).failures == 0
    assert not (tmp_path / 'stage').exists()
    layout.nginx_enabled_dir = ''
    assert installer.check(layout).failures > 0


def test_check_read_only_paths_and_dependencies(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    layout = layout_at(tmp_path)
    before = list(tmp_path.rglob('*'))
    assert installer.check(layout).failures == 0
    assert list(tmp_path.rglob('*')) == before
    assert 'summary' in caplog.text
    assert 'uid:gid=' in caplog.text
    assert 'optional unless enabled in repository config' in caplog.text


def test_check_blocks_parent_file_and_cli_conflict(tmp_path):
    layout = layout_at(tmp_path)
    Path(layout.prefix).write_text('not a directory')
    assert installer.check(layout).failures > 0
    Path(layout.prefix).unlink()
    layout.cli.parent.mkdir(parents=True)
    layout.cli.write_text('foreign executable')
    assert installer.check(layout).failures > 0
    assert layout.cli.read_text() == 'foreign executable'


def test_check_blocks_symlink_parent_and_config(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    layout = layout_at(tmp_path)
    Path(layout.sysconfdir).symlink_to(outside, target_is_directory=True)
    assert installer.check(layout).failures > 0
    assert not list(outside.iterdir())


def test_install_blocked_before_subprocess_mutations(tmp_path, monkeypatch):
    layout = layout_at(tmp_path)
    layout.cli.parent.mkdir(parents=True)
    layout.cli.write_text('foreign')
    monkeypatch.setattr(installer, 'run', lambda *a, **kw: pytest.fail('mutation after failed check'))
    with pytest.raises(ValueError, match='preflight'):
        installer.install(layout, Path(__file__).parents[1], tmp_path / 'wheels')
    assert not layout.config.parent.exists()


def test_destdir_only_affects_disk_paths(tmp_path):
    layout = installer.Layout(destdir=str(tmp_path))
    assert str(layout.config) == '/etc/repowatch/config.yaml'
    assert layout.disk(layout.config) == tmp_path / 'etc/repowatch/config.yaml'


def test_activation_refuses_staging(tmp_path):
    with pytest.raises(ValueError, match='DESTDIR'):
        installer.activate(installer.Layout(destdir=str(tmp_path)))


@pytest.mark.parametrize('value', ['relative', '/tmp/../etc', '/tmp/a\nExecStart=bad', '/tmp/a;touch'])
def test_reject_syntax_bearing_paths(value):
    with pytest.raises(ValueError):
        installer.absolute(value)


def test_write_preserves_config_and_refuses_symlinks(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text('# operator config\nsecret: retained\n')
    original = config.read_bytes()
    installer.write(config, 'replacement', preserve=True)
    assert config.read_bytes() == original
    link = tmp_path / 'link'
    link.symlink_to(config)
    with pytest.raises(ValueError, match='symlink'):
        installer.write(link, 'replacement')
    assert config.read_bytes() == original


def test_cli_uses_install_default_and_explicit_override(tmp_path, monkeypatch, capsys):
    import repowatch.cli.main as cli
    config = tmp_path / 'config.yaml'
    config.write_text('state_db: /unused/state\ncache_base_url: http://localhost\nrepos:\n'
                      '  - {id: test, type: apk, upstream: https://example.org/alpine, arch: x86_64}\n')
    monkeypatch.setattr('repowatch.cli.parser.DEFAULT_CONFIG_PATH', str(config))
    assert cli_main.main(['check-config']) == 0
    assert str(config) in capsys.readouterr().out
    assert cli_main.main(['-c', str(tmp_path / 'missing'), 'check-config']) == 1


def test_make_defaults_are_read_only_help():
    root = Path(__file__).parents[1]
    for makefile in ('Makefile', 'Makefile.dev'):
        result = subprocess.run(['make', '-f', makefile], cwd=root, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert 'make check' in result.stdout
        assert 'ssh ' not in result.stdout


def test_activation_nginx_failure_removes_new_link_and_never_starts_services(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    layout = layout_at(tmp_path)
    layout.with_nginx = True
    layout.nginx_enabled_dir = str(tmp_path / 'enabled')
    layout.nginx_conf = str(tmp_path / 'nginx.conf')
    layout.share.mkdir(parents=True)
    (layout.share / 'install.json').write_text(json.dumps(installer.asdict(layout)))
    monkeypatch.setattr(installer.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(installer, 'check', lambda *a, **kw: installer.Report())
    monkeypatch.setattr(installer.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=1000, pw_gid=1000))
    monkeypatch.setattr(installer.grp, 'getgrnam', lambda _: SimpleNamespace(gr_gid=1000))
    monkeypatch.setattr(installer.os, 'chown', lambda *a: None)
    commands = []

    def fake_run(args, **kwargs):
        commands.append(args)
        if args[0] == 'nginx':
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({name: "# generated\n" for name in ("active.conf", "purge.conf", "dedup.map", "probe.conf", "probe.js")} ))

    monkeypatch.setattr(installer, 'run', fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        installer.activate(layout)
    assert not (Path(layout.nginx_enabled_dir) / 'repowatch.conf').is_symlink()
    assert not any(command[0] == 'systemctl' for command in commands)


def test_home_prefix_rejected_with_hardened_systemd(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    layout = layout_at(tmp_path)
    layout.prefix = '/home/example/repowatch'
    layout.with_systemd = True
    layout.destdir = str(tmp_path / 'stage')
    installer.check(layout)
    assert 'systemd ProtectHome' in caplog.text


def test_generated_file_symlink_blocks_before_install(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    layout = layout_at(tmp_path)
    layout.share.mkdir(parents=True)
    (layout.share / 'config.example.yaml').symlink_to(tmp_path / 'foreign')
    assert installer.check(layout).failures > 0
    assert 'file conflict' in caplog.text


def test_activation_executes_packaged_cli_before_host_mutations(tmp_path, monkeypatch):
    import json
    layout = layout_at(tmp_path)
    layout.share.mkdir(parents=True)
    (layout.share / 'install.json').write_text(json.dumps(installer.asdict(layout)))
    layout.config.parent.mkdir(parents=True)
    layout.config.write_text(
        f'state_db: {tmp_path / "unused.db"}\ncache_base_url: http://127.0.0.1:8080\n'
        'repos:\n  - id: r\n    type: apk\n    upstream: https://example.org\n    arch: x86_64\n')
    monkeypatch.setattr(installer.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(installer, 'check', lambda *a, **kw: installer.Report())
    class CheckedCommand(Exception):
        pass
    def execute(args, **kwargs):
        result = subprocess.run([sys.executable, *args[1:]], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / 'unused.db').exists()
        raise CheckedCommand()
    monkeypatch.setattr(installer, 'run', execute)
    with pytest.raises(CheckedCommand):
        installer.activate(layout)


@pytest.mark.parametrize('failure', [None, 'write', 'nginx'])
def test_first_activation_bootstraps_all_features_without_creating_database(tmp_path, monkeypatch, failure):
    import json
    import os
    from types import SimpleNamespace
    layout = layout_at(tmp_path)
    layout.with_nginx = True
    layout.nginx_conf = str(tmp_path / 'nginx/nginx.conf')
    layout.nginx_enabled_dir = str(tmp_path / 'nginx/conf.d')
    layout.share.mkdir(parents=True)
    (layout.share / 'install.json').write_text(json.dumps(installer.asdict(layout)))
    layout.config.parent.mkdir(parents=True)
    state_db = layout.state / 'state.sqlite3'
    source = Path(__file__).parents[1] / 'config/config.example.yaml'
    layout.config.write_text(source.read_text().replace('/var/lib/repowatch', str(layout.state)))
    monkeypatch.setattr(installer.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(installer.os, 'chown', lambda *a: None)
    monkeypatch.setattr(installer.grp, 'getgrnam', lambda *a: SimpleNamespace(gr_gid=os.getgid()))
    monkeypatch.setattr(installer.pwd, 'getpwnam', lambda *a: SimpleNamespace(pw_uid=os.getuid()))
    monkeypatch.setattr(installer, 'check', lambda *a, **kw: installer.Report())
    names = ('active.conf', 'purge.conf', 'dedup.map', 'probe.conf', 'probe.js')
    checked = []
    def run(args, **kwargs):
        if args[0] != 'nginx':
            return subprocess.run([sys.executable, *args[1:]], check=True, capture_output=True, text=True)
        directory = layout.nginx_policy.parent
        assert all((directory / name).is_file() for name in names)
        active = (directory / 'active.conf').read_text()
        assert str(directory / 'probe.js') in active
        assert str(directory / 'probe.conf') in active
        assert layout.cache_dir in (directory / 'probe.js').read_text()
        assert not state_db.exists()
        checked.append(args)
        if failure == 'nginx':
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(installer, 'run', run)
    write = installer.write
    def fail_write(path, content, *args, **kwargs):
        if failure == 'write' and path.name == 'probe.js':
            raise OSError('simulated disk failure')
        return write(path, content, *args, **kwargs)
    monkeypatch.setattr(installer, 'write', fail_write)
    if failure:
        with pytest.raises((OSError, subprocess.CalledProcessError)):
            installer.activate(layout)
        assert all(not (layout.nginx_policy.parent / name).exists() for name in names)
        assert not (Path(layout.nginx_enabled_dir) / 'repowatch.conf').exists()
    else:
        installer.activate(layout)
        assert checked
    assert not state_db.exists()
