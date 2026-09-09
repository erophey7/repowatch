"""Exercise upgrade rollback against real temporary files/SQLite, fake services."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
try:
    spec = importlib.util.spec_from_file_location('upgrade_system', Path(__file__).parents[1] / 'scripts/upgrade-system.py')
    upgrade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upgrade)
finally:
    sys.path.pop(0)


@pytest.mark.parametrize('nginx', [False, True])
@pytest.mark.parametrize('failure', ['install', 'activate', 'smoke', None])
def test_upgrade_restores_exact_files_and_database(tmp_path, monkeypatch, failure, nginx):
    layout = upgrade.Layout(prefix=str(tmp_path/'usr'), sysconfdir=str(tmp_path/'etc'),
        localstatedir=str(tmp_path/'var'), systemd_unit_dir=str(tmp_path/'units'), with_nginx=nginx,
        nginx_conf=str(tmp_path/'nginx/nginx.conf'), nginx_enabled_dir=str(tmp_path/'nginx/conf.d'))
    for directory in (layout.venv, layout.libexec, layout.share/'systemd', layout.cli.parent,
                      layout.config.parent, layout.state/'backups', Path(layout.systemd_unit_dir)):
        directory.mkdir(parents=True, exist_ok=True)
    layout.config.write_bytes(b'original config\n')
    layout.config.chmod(0o640)
    binary = layout.venv/'version'; binary.write_text('old')
    layout.cli.symlink_to(binary)
    (layout.share/'install.json').write_text('original manifest')
    unit = Path(layout.systemd_unit_dir)/'repowatch.service'; unit.write_text('old unit')
    (layout.share/'systemd/repowatch.service').write_text('old unit')
    db = layout.state/'custom.sqlite'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE data(value TEXT)'); conn.execute("INSERT INTO data VALUES ('old')")
    source = tmp_path/'source'; (source/'systemd').mkdir(parents=True)
    (source/'systemd/repowatch.service').write_text('new unit')
    wheels = tmp_path/'wheels'; wheels.mkdir(); (wheels/'repowatch-1.whl').touch()
    info = {'db':str(db), 'bind':'127.0.0.1', 'port':8085, 'tls':False}
    states = {'repowatch.service': {'active':True, 'enabled':'enabled'}, 'nginx.service': {'active':True, 'enabled':'disabled'}}
    events=[]
    commands=[]
    monkeypatch.setattr(upgrade.os, 'geteuid', lambda:0)
    monkeypatch.setattr(upgrade, 'check', lambda *a, **k:SimpleNamespace(failures=0))
    monkeypatch.setattr(upgrade, 'service_state', lambda:states)
    monkeypatch.setattr(upgrade, 'probe', lambda *a:info)
    monkeypatch.setattr(upgrade, 'run', lambda args, **kw:(commands.append(list(map(str,args))), events.append(str(args[0]))))
    monkeypatch.setattr(upgrade, 'stop', lambda:events.append('stop'))
    monkeypatch.setattr(upgrade, 'resume', lambda s:events.append(('resume', s)))
    monkeypatch.setattr(upgrade.os, 'chown', lambda *a:None)
    def install(*a):
        binary.write_text('new')
        with sqlite3.connect(db) as conn: conn.execute("UPDATE data SET value='new'")
        if failure=='install': raise RuntimeError('injected install')
    def activate(*a):
        unit.write_text('new unit')
        if failure=='activate': raise RuntimeError('injected activate')
    def smoke(*a):
        if failure=='smoke': raise RuntimeError('injected smoke')
    monkeypatch.setattr(upgrade, 'install', install)
    monkeypatch.setattr(upgrade, 'activate', activate)
    monkeypatch.setattr(upgrade, 'smoke', smoke)
    if failure:
        with pytest.raises(RuntimeError, match='injected'): upgrade.upgrade(layout, source, wheels)
    else: upgrade.upgrade(layout, source, wheels)
    assert binary.read_text()==('old' if failure else 'new')
    assert unit.read_text()==('old unit' if failure else 'new unit')
    assert layout.cli.is_symlink() and layout.cli.resolve()==binary
    assert layout.config.read_bytes()==b'original config\n'
    assert layout.config.stat().st_mode & 0o777 == 0o640
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT value FROM data').fetchone()[0]==('old' if failure else 'new')
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert events[-1]==('resume',states)
    if nginx and failure in ('smoke', None):
        assert [str(layout.cli), '-c', str(layout.config), 'nginx-apply', '--policy', str(layout.nginx_policy)] in commands
    backup=next((layout.state/'backups').iterdir())
    assert (backup/'installation.tar.gz').is_file()
    assert json.loads((backup/'metadata.json').read_text())['database']==str(db)


def test_failure_preparing_candidate_does_not_stop_services(tmp_path, monkeypatch):
    layout=upgrade.Layout(prefix=str(tmp_path/'usr'),sysconfdir=str(tmp_path/'etc'),with_nginx=False)
    layout.config.parent.mkdir(parents=True); layout.config.write_text('config')
    (layout.share/'systemd').mkdir(parents=True)
    source=tmp_path/'src'; (source/'systemd').mkdir(parents=True)
    monkeypatch.setattr(upgrade.os,'geteuid',lambda:0)
    monkeypatch.setattr(upgrade,'check',lambda *a,**k:SimpleNamespace(failures=0))
    monkeypatch.setattr(upgrade,'service_state',lambda:{'repowatch.service':{'active':True}})
    monkeypatch.setattr(upgrade,'probe',lambda *a:{'db':str(tmp_path/'db')})
    def fail(*a,**k): raise RuntimeError('candidate pip failed')
    monkeypatch.setattr(upgrade,'run',fail)
    monkeypatch.setattr(upgrade,'stop',lambda:pytest.fail('must not stop before successful preparation'))
    with pytest.raises(RuntimeError): upgrade.upgrade(layout,source,tmp_path/'wheels')


def test_system_python_without_pip_uses_installed_venv(tmp_path, monkeypatch):
    layout=upgrade.Layout(prefix=str(tmp_path/'prefix'))
    python=layout.venv/'bin/python'; python.parent.mkdir(parents=True); python.touch()
    monkeypatch.setattr(upgrade.importlib.util,'find_spec',lambda name:None)
    monkeypatch.setattr(upgrade.sys,'executable','/usr/bin/python3')
    monkeypatch.setattr(upgrade.sys,'argv',['upgrade-system.py','--apply'])
    calls=[]
    monkeypatch.setattr(upgrade.os,'execv',lambda binary,args:calls.append((binary,args)))
    upgrade.ensure_interpreter(layout)
    assert calls==[(str(python),[str(python),'upgrade-system.py','--apply'])]
    monkeypatch.setattr(upgrade.sys,'executable',str(python))
    with pytest.raises(ValueError,match='pip unavailable'): upgrade.ensure_interpreter(layout)
