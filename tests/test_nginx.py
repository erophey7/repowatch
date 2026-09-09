from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest

from repowatch.config import Config, ConfigError, NginxConfig, RepoConfig, StatusServerConfig, load_config
from repowatch import nginx


def config(repos):
    return Config(Path('/tmp/unused.sqlite'), 300, 'http://localhost:8080', StatusServerConfig(),
                  repos=repos, nginx=NginxConfig(enabled=True))


def apt(id='debian', upstream='http://deb.debian.org/debian', **kwargs):
    return RepoConfig(id, 'apt', upstream, 'amd64', distribution='bookworm', component='main', **kwargs)


def test_routes_match_parsers_and_warming():
    c = config([
        RepoConfig('core', 'pacman', 'https://mirror.test/core/os/x86_64', 'x86_64', repo_name='core'),
        apt(), apt('security', 'http://security.ubuntu.com/ubuntu'),
        apt('ppa', 'https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu'),
        RepoConfig('apk', 'apk', 'https://alpine.test/alpine/v3.20/main', 'x86_64'),
        RepoConfig('rpm', 'dnf', 'https://rpm.test/9/BaseOS/x86_64/os', 'x86_64'),
    ])
    text = nginx.render(c)
    assert 'rewrite ^/arch/core/os/x86_64/(.*)$ /core/os/x86_64/$1 break;' in text
    assert '/core/os/x86_64/core/os/' not in text
    assert 'location /alpine/v3.20/main/x86_64/' in text
    assert 'rewrite ^/ubuntu\\-security/(.*)$ /ubuntu/$1 break;' in text
    assert 'rewrite ^/rpm/rpm/(.*)$ /9/BaseOS/x86_64/os/$1 break;' in text
    assert 'arch-index-v2:$scheme$proxy_host$request_uri' in text
    assert 'location ~ ^/debian/dists/' in text
    assert nginx.render(replace(c, repos=list(reversed(c.repos)))) == text


def test_shared_apt_prefix_minimum_interval_and_conflicts():
    text = nginx.render(config([apt(), replace(apt('updates'), distribution='bookworm-updates', check_interval=60)]))
    assert text.count('location /debian/') == 1
    assert 'proxy_cache_valid 200 206 60s;' in text
    with pytest.raises(ConfigError, match='conflicting'):
        nginx.render(config([apt(), apt('other', 'http://other.test/debian')]))
    with pytest.raises(ConfigError, match='overlapping'):
        nginx.render(config([apt(), apt('other', 'http://other.test/debian/extra')]))


@pytest.mark.parametrize('upstream', ['http://evil/;include', 'http://evil/$arg_url',
    'http://user:pass@evil/debian', 'file:///etc/passwd', 'http://evil/debian?x=1',
    'http://evil/debian/../root', 'http://evil:99999/debian'])
def test_reject_upstream_injection(upstream):
    with pytest.raises(ConfigError):
        nginx.render(config([apt(upstream=upstream)]))


@pytest.mark.parametrize('settings', [dict(listen='8080;include'), dict(listen='99999'),
    dict(server_name='a b'), dict(resolvers=['127.0.0.1;']), dict(enabled='yes'),
    dict(cache_max_size='100g;'), dict(index_ttl=0), dict(package_ttl=True)])
def test_reject_nginx_parameter_injection(settings):
    with pytest.raises(ConfigError):
        NginxConfig(**settings)


def setup_apply(tmp_path, monkeypatch):
    monkeypatch.setattr(nginx, '_check_policy_owner', lambda p: None)
    c = config([apt()])
    monkeypatch.setattr(nginx, 'load_config', lambda p: c)
    policy = tmp_path / 'policy.json'
    (tmp_path / 'site.conf').symlink_to(tmp_path / 'active.conf')
    policy.write_text(json.dumps(dict(site_link=str(tmp_path / 'site.conf'), cache_dir='/var/cache/nginx/repo', access_log='/var/log/nginx/repo.log',
                                     nginx_conf='/etc/nginx/nginx.conf', nginx_binary='/usr/sbin/nginx')))
    calls = []
    monkeypatch.setattr(nginx.subprocess, 'run', lambda args, **kw: calls.append(args))
    return policy, calls


def test_apply_noop_and_previous(tmp_path, monkeypatch):
    policy, calls = setup_apply(tmp_path, monkeypatch)
    active = tmp_path / 'active.conf'
    active.write_text('# previous working\n')
    assert nginx.apply('config', str(policy))
    assert len(calls) == 2
    assert (tmp_path / 'previous.conf').read_text() == '# previous working\n'
    assert not nginx.apply('config', str(policy))
    assert len(calls) == 2
    assert json.loads((tmp_path / 'status.json').read_text())['ok']


@pytest.mark.parametrize('fail_at', [1, 2])
def test_apply_failure_restores_old_and_reloads(tmp_path, monkeypatch, fail_at):
    policy, calls = setup_apply(tmp_path, monkeypatch)
    active = tmp_path / 'active.conf'
    active.write_text('# previous working\n')
    def run(args, **kw):
        calls.append(args)
        if len(calls) == fail_at:
            raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(nginx.subprocess, 'run', run)
    with pytest.raises(ConfigError, match='restored'):
        nginx.apply('config', str(policy))
    assert active.read_text() == '# previous working\n'
    assert calls[-1] == ['systemctl', 'reload', 'nginx.service']
    assert not (tmp_path / 'previous.conf').exists()
    assert not json.loads((tmp_path / 'status.json').read_text())['ok']


def test_apply_use_systemctl_false_reloads_via_nginx_binary(tmp_path, monkeypatch):
    policy, calls = setup_apply(tmp_path, monkeypatch)
    assert nginx.apply('config', str(policy), use_systemctl=False)
    assert calls[-1] == ['/usr/sbin/nginx', '-s', 'reload', '-c', '/etc/nginx/nginx.conf']


def test_policy_rejects_unprivileged_path(tmp_path):
    path = tmp_path / 'policy.json'
    path.write_text('{}')
    with pytest.raises(ConfigError, match='root-owned'):
        nginx._check_policy_owner(path)


def test_example_renders_and_cli_without_database(tmp_path, capsys):
    from repowatch.cli import main
    assert main(['-c', 'config/config.example.yaml', 'nginx-render']) == 0
    assert 'server {' in capsys.readouterr().out
    c = load_config('config/config.example.yaml')
    assert 'location /debian/' in nginx.render(c)


def test_real_nginx_proxy_routes_and_cache(tmp_path):
    import http.server
    import shutil
    import socket
    import threading
    import time
    import urllib.request
    binary = shutil.which('nginx')
    if not binary:
        pytest.skip('nginx is an optional system dependency')
    seen = []
    class Backend(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = self.path.encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    backend = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Backend)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    host = f'http://127.0.0.1:{backend.server_port}'
    c = config([
        apt(upstream=host + '/debian'),
        RepoConfig('core', 'pacman', host + '/core/os/x86_64', 'x86_64', repo_name='core',
                   url_template='/archlinux/{arch}/{repo_name}/'),
        RepoConfig('alt', 'apt-rpm', host + '/p11/branch/x86_64', 'x86_64', component='classic',
                   url_template='/altlinux/p11/{arch}/'),
        RepoConfig('apk', 'apk', host + '/alpine/v3/main', 'x86_64'),
        RepoConfig('rpm', 'dnf', host + '/9/BaseOS/os', 'x86_64'),
    ])
    c = replace(c, nginx=replace(c.nginx, listen=f'127.0.0.1:{port}'))
    text = nginx.render(c, cache_dir=str(tmp_path / 'cache'), access_log=str(tmp_path / 'access.log'))
    conf = tmp_path / 'nginx.conf'
    conf.write_text(f'pid {tmp_path}/nginx.pid; error_log {tmp_path}/error.log;\n'
                    'events {}\nhttp {\n' + text + '\n}\n')
    command = [binary, '-p', str(tmp_path), '-c', str(conf)]
    process = None
    try:
        subprocess.run(command + ['-t'], check=True, capture_output=True)
        process = subprocess.Popen(command + ['-g', 'daemon off;'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1):
                    break
            except OSError:
                time.sleep(.02)
        pairs = [('/archlinux/x86_64/core/core.db', '/core/os/x86_64/core.db'),
                 ('/archlinux/x86_64/core/pkg.pkg.tar.zst', '/core/os/x86_64/pkg.pkg.tar.zst'),
                 ('/debian/dists/bookworm/InRelease', '/debian/dists/bookworm/InRelease'),
                 ('/debian/pool/main/a/a.deb', '/debian/pool/main/a/a.deb'),
                 ('/alpine/v3/main/x86_64/APKINDEX.tar.gz', '/alpine/v3/main/x86_64/APKINDEX.tar.gz'),
                 ('/rpm/rpm/repodata/repomd.xml', '/9/BaseOS/os/repodata/repomd.xml'),
                 ('/altlinux/p11/x86_64/base/pkglist.classic.xz', '/p11/branch/x86_64/base/pkglist.classic.xz'),
                 ('/altlinux/p11/x86_64/RPMS.classic/package.rpm', '/p11/branch/x86_64/RPMS.classic/package.rpm')]
        for local, remote in pairs:
            for attempt in range(2):
                with urllib.request.urlopen(f'http://127.0.0.1:{port}{local}', timeout=5) as response:
                    assert response.read().decode() == remote
                    assert response.headers['X-Cache-Status'] == ('MISS' if attempt == 0 else 'HIT')
        assert seen == [remote for _, remote in pairs]
    finally:
        if process:
            process.terminate()
            process.communicate(timeout=10)
        backend.shutdown()
        backend.server_close()


def test_cache_version_and_resolver_types():
    c = config([apt()])
    assert 'proxy_cache_key "migration-2:$scheme$proxy_host$request_uri";' in nginx.render(
        replace(c, nginx=replace(c.nginx, cache_key_version='migration-2')))
    with pytest.raises(ConfigError):
        NginxConfig(resolvers=[0])
    with pytest.raises(ConfigError):
        NginxConfig(cache_key_version='$arg_url')


def test_nginx_config_cache_dir_defaults_to_none_and_validates_when_set():
    assert NginxConfig().cache_dir is None
    assert NginxConfig(cache_dir='/var/cache/nginx/repowatch').cache_dir == '/var/cache/nginx/repowatch'
    with pytest.raises(ConfigError, match='cache_dir'):
        NginxConfig(cache_dir='relative/path')
    with pytest.raises(ConfigError, match='cache_dir'):
        NginxConfig(cache_dir='/var/cache/../etc')
    with pytest.raises(ConfigError, match='cache_dir'):
        NginxConfig(cache_dir=123)


def test_apply_accepts_matching_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(nginx, '_check_policy_owner', lambda p: None)
    c = replace(config([apt()]), nginx=NginxConfig(enabled=True, cache_dir='/var/cache/nginx/repo'))
    monkeypatch.setattr(nginx, 'load_config', lambda p: c)
    policy = tmp_path / 'policy.json'
    (tmp_path / 'site.conf').symlink_to(tmp_path / 'active.conf')
    policy.write_text(json.dumps(dict(
        site_link=str(tmp_path / 'site.conf'), cache_dir='/var/cache/nginx/repo',
        access_log='/var/log/nginx/repo.log', nginx_conf='/etc/nginx/nginx.conf',
        nginx_binary='/usr/sbin/nginx',
    )))
    monkeypatch.setattr(nginx.subprocess, 'run', lambda args, **kw: None)

    assert nginx.apply('config', str(policy))


def test_apply_rejects_cache_dir_mismatch_without_touching_anything(tmp_path, monkeypatch):
    """config.yaml's declared nginx.cache_dir must match what the installed
    policy actually enforces — a mismatch must refuse before writing
    active.conf or running nginx -t/reload at all, not apply the real
    (policy) path silently while the declared one lies."""
    monkeypatch.setattr(nginx, '_check_policy_owner', lambda p: None)
    c = replace(config([apt()]), nginx=NginxConfig(enabled=True, cache_dir='/var/cache/nginx/wrong-path'))
    monkeypatch.setattr(nginx, 'load_config', lambda p: c)
    policy = tmp_path / 'policy.json'
    (tmp_path / 'site.conf').symlink_to(tmp_path / 'active.conf')
    policy.write_text(json.dumps(dict(
        site_link=str(tmp_path / 'site.conf'), cache_dir='/var/cache/nginx/repo',
        access_log='/var/log/nginx/repo.log', nginx_conf='/etc/nginx/nginx.conf',
        nginx_binary='/usr/sbin/nginx',
    )))
    calls = []
    monkeypatch.setattr(nginx.subprocess, 'run', lambda args, **kw: calls.append(args))

    with pytest.raises(ConfigError, match='cache_dir'):
        nginx.apply('config', str(policy))

    assert calls == []
    assert not (tmp_path / 'active.conf').exists()
