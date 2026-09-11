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
        RepoConfig('void', 'xbps', 'https://xbps.test/current', 'x86_64'),
    ])
    text = nginx.render(c)
    assert 'rewrite ^/arch/core/os/x86_64/(.*)$ /core/os/x86_64/$1 break;' in text
    assert '/core/os/x86_64/core/os/' not in text
    assert 'location /alpine/v3.20/main/x86_64/' in text
    assert 'rewrite ^/ubuntu\\-security/(.*)$ /ubuntu/$1 break;' in text
    assert 'rewrite ^/rpm/rpm/(.*)$ /9/BaseOS/x86_64/os/$1 break;' in text
    assert 'rewrite ^/xbps/void/(.*)$ /current/$1 break;' in text
    assert 'location ~ ^/xbps/void/[^/]+-repodata$' in text
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


def test_apply_writes_purge_conf_and_includes_it_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(nginx, '_check_policy_owner', lambda p: None)
    c = config([apt()])
    c = replace(c, nginx=replace(c.nginx, enable_purge=True))
    monkeypatch.setattr(nginx, 'load_config', lambda p: c)
    policy = tmp_path / 'policy.json'
    (tmp_path / 'site.conf').symlink_to(tmp_path / 'active.conf')
    policy.write_text(json.dumps(dict(site_link=str(tmp_path / 'site.conf'), cache_dir='/var/cache/nginx/repo', access_log='/var/log/nginx/repo.log',
                                     nginx_conf='/etc/nginx/nginx.conf', nginx_binary='/usr/sbin/nginx')))
    monkeypatch.setattr(nginx.subprocess, 'run', lambda args, **kw: None)

    assert nginx.apply('config', str(policy))
    purge_path = tmp_path / 'purge.conf'
    active = tmp_path / 'active.conf'
    assert f'include {purge_path};' in active.read_text()
    assert 'proxy_cache_purge' in purge_path.read_text()
    # A real no-op on the next run — both files, not just active.conf, are compared.
    assert not nginx.apply('config', str(policy))


def test_apply_rolls_back_purge_conf_together_with_active_conf(tmp_path, monkeypatch):
    monkeypatch.setattr(nginx, '_check_policy_owner', lambda p: None)
    c = config([apt()])
    c = replace(c, nginx=replace(c.nginx, enable_purge=True))
    monkeypatch.setattr(nginx, 'load_config', lambda p: c)
    policy = tmp_path / 'policy.json'
    (tmp_path / 'site.conf').symlink_to(tmp_path / 'active.conf')
    policy.write_text(json.dumps(dict(site_link=str(tmp_path / 'site.conf'), cache_dir='/var/cache/nginx/repo', access_log='/var/log/nginx/repo.log',
                                     nginx_conf='/etc/nginx/nginx.conf', nginx_binary='/usr/sbin/nginx')))
    active = tmp_path / 'active.conf'
    purge_path = tmp_path / 'purge.conf'
    active.write_text('# previous working\n')
    purge_path.write_text('# previous purge\n')
    calls = []
    def run(args, **kw):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(nginx.subprocess, 'run', run)

    with pytest.raises(ConfigError, match='restored'):
        nginx.apply('config', str(policy))
    assert active.read_text() == '# previous working\n'
    assert purge_path.read_text() == '# previous purge\n'


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
        RepoConfig('void', 'xbps', host + '/current', 'x86_64'),
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
                 ('/altlinux/p11/x86_64/RPMS.classic/package.rpm', '/p11/branch/x86_64/RPMS.classic/package.rpm'),
                 ('/xbps/void/x86_64-repodata', '/current/x86_64-repodata'),
                 ('/xbps/void/bash-5.3_2.x86_64.xbps', '/current/bash-5.3_2.x86_64.xbps')]
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


def test_real_nginx_dedup_shares_the_canonical_cache_entry(tmp_path):
    """docs_dev/ROADMAP.md item 29, end-to-end: a request for the duplicate
    repo's copy of a byte-identical file must HIT the CANONICAL repo's own
    cache entry (same bytes, same key) instead of creating a second one —
    real nginx, two distinct real backends, no mocks. This also guards
    against the two real bugs hand-testing caught before this shipped: an
    nginx-detected internal rewrite cycle (map keyed on $request_uri instead
    of $uri, which never updates across a rewrite) and a stale cached
    map-variable value surviving the rewrite even after switching to $uri
    (fixed by explicitly resetting it — see nginx.py's render())."""
    import http.server
    import shutil
    import socket
    import threading
    import time
    import urllib.request
    binary = shutil.which('nginx')
    if not binary:
        pytest.skip('nginx is an optional system dependency')

    def make_backend(label):
        class Backend(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = f'{label}:{self.path}'.encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    debian_backend = make_backend('debian-origin')
    ubuntu_backend = make_backend('ubuntu-origin')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    c = config([
        apt('debian', f'http://127.0.0.1:{debian_backend.server_port}/debian'),
        apt('ubuntu', f'http://127.0.0.1:{ubuntu_backend.server_port}/ubuntu'),
    ])
    c = replace(c, nginx=replace(c.nginx, enable_dedup=True, listen=f'127.0.0.1:{port}'))
    dedup_conf = tmp_path / 'dedup.map'
    text = nginx.render(c, cache_dir=str(tmp_path / 'cache'), access_log=str(tmp_path / 'access.log'),
                         dedup_conf=str(dedup_conf))
    pairs = nginx.resolve_dedup_pairs(c, [('ubuntu', 'debian', 'pool/main/a/a.deb')])
    dedup_conf.write_text(nginx.render_dedup(c, pairs))
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

        def get(path):
            request = urllib.request.Request(f'http://127.0.0.1:{port}{path}')
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.read().decode(), response.headers['X-Cache-Status']

        body, status = get('/debian/pool/main/a/a.deb')
        assert (body, status) == ('debian-origin:/debian/pool/main/a/a.deb', 'MISS')
        body, status = get('/debian/pool/main/a/a.deb')
        assert (body, status) == ('debian-origin:/debian/pool/main/a/a.deb', 'HIT')
        # The whole point: ubuntu's own path for the SAME file HITS debian's
        # already-warm entry — body is debian's, ubuntu's backend never sees
        # this request at all.
        body, status = get('/ubuntu/pool/main/a/a.deb')
        assert (body, status) == ('debian-origin:/debian/pool/main/a/a.deb', 'HIT')
        # A different, non-deduped ubuntu file is completely unaffected.
        body, status = get('/ubuntu/pool/main/b/b.deb')
        assert (body, status) == ('ubuntu-origin:/ubuntu/pool/main/b/b.deb', 'MISS')
        # Index files are never subject to dedup, even under /ubuntu/.
        body, status = get('/ubuntu/dists/noble/InRelease')
        assert body == 'ubuntu-origin:/ubuntu/dists/noble/InRelease'
    finally:
        if process:
            process.terminate()
            process.communicate(timeout=10)
        debian_backend.shutdown()
        debian_backend.server_close()
        ubuntu_backend.shutdown()
        ubuntu_backend.server_close()


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


def test_nginx_config_enable_purge_defaults_to_false_and_validates_type():
    assert NginxConfig().enable_purge is False
    assert NginxConfig(enable_purge=True).enable_purge is True
    with pytest.raises(ConfigError, match='enable_purge'):
        NginxConfig(enable_purge='yes')


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


# --- active cache purge on package removal (docs_dev/ROADMAP.md item 24) ---

def test_purge_disabled_by_default_renders_no_purge_locations():
    text = nginx.render(config([apt()]))
    assert 'proxy_cache_purge' not in text
    assert '/purge' not in text


def test_purge_enabled_adds_an_include_line_pointing_at_the_default_purge_conf():
    c = config([apt()])
    c = replace(c, nginx=replace(c.nginx, enable_purge=True))
    text = nginx.render(c)
    assert 'include /etc/nginx/repowatch/purge.conf;' in text
    assert 'proxy_cache_purge' not in text


def test_purge_include_path_is_configurable_and_validated():
    c = config([apt()])
    c = replace(c, nginx=replace(c.nginx, enable_purge=True))
    text = nginx.render(c, purge_conf='/etc/nginx/repowatch/custom-purge.conf')
    assert 'include /etc/nginx/repowatch/custom-purge.conf;' in text
    with pytest.raises(ConfigError):
        nginx.render(c, purge_conf='relative/purge.conf')


def test_purge_disabled_renders_no_include_line():
    text = nginx.render(config([apt()]))
    assert 'include' not in text
    assert '/purge' not in text


def test_render_purge_empty_comment_only_when_disabled():
    text = nginx.render_purge(config([apt()]))
    assert text == '# Generated by repowatch; edit YAML parameters, not this file.\n'


def test_render_purge_renders_a_loopback_only_location_per_repo():
    c = config([
        RepoConfig('core', 'pacman', 'https://mirror.test/core/os/x86_64', 'x86_64', repo_name='core'),
        apt(),
    ])
    c = replace(c, nginx=replace(c.nginx, enable_purge=True))
    text = nginx.render_purge(c)

    assert 'location ~ ^/purge/arch/core/os/x86_64/(.*)$ {' in text
    assert 'location ~ ^/purge/debian/(.*)$ {' in text
    purge_block = text[text.index('location ~ ^/purge/arch'):]
    purge_block = purge_block[:purge_block.index('\n    }') + 6]
    assert 'allow 127.0.0.1;' in purge_block
    assert 'deny all;' in purge_block
    # ${scheme} braced — bare $scheme would glue onto the following
    # hostname text and nginx would parse a nonexistent "$schememirror..."
    # variable (a real mistake caught by hand-testing before wiring this in).
    assert 'proxy_cache_purge repo_cache "${scheme}mirror.test/arch/core/os/x86_64/$1";' in purge_block


def test_render_purge_key_matches_the_real_content_locations_key_format():
    """The whole point: a purge request must reconstruct EXACTLY the same
    cache key a real package request under this prefix would produce
    (server-level "{key_prefix}$scheme$proxy_host$request_uri"), or it
    purges a key nothing ever used and silently does nothing."""
    c = config([apt()])
    c = replace(c, nginx=replace(c.nginx, enable_purge=True, cache_key_version='v7'))
    text = nginx.render(c)
    assert 'proxy_cache_key "v7:$scheme$proxy_host$request_uri";' in text
    assert 'proxy_cache_purge repo_cache "v7:${scheme}deb.debian.org/debian/$1";' in nginx.render_purge(c)


def test_render_purge_key_matches_dedup_uri_basis_when_dedup_is_also_enabled():
    """Real bug found on production (2026-09-12, rocky-9-baseos-x86_64):
    with nginx.enable_dedup on, the real content location's proxy_cache_key
    switches from $request_uri (the local path) to $uri, which — after
    that location's own internal `rewrite local -> remote` — holds the
    POST-rewrite upstream-relative path instead. render_purge() must match
    THAT basis, not the local path, or purge silently misses every real
    cache entry (confirmed by hand: curl showed a real X-Cache-Status: HIT
    immediately followed by 404 from the purge location for the identical
    file)."""
    c = config([apt()])
    c = replace(c, nginx=replace(c.nginx, enable_purge=True, enable_dedup=True, cache_key_version='v7'))
    text = nginx.render(c)
    assert 'proxy_cache_key "v7:$scheme$proxy_host$uri";' in text
    # NOT .../debian/$1 (the local prefix) — /debian/pool/main is the local
    # prefix, but $uri after the location's own rewrite is the upstream
    # path (apt() defaults upstream to deb.debian.org/debian, so local ==
    # remote here — pick a repo whose local/remote genuinely differ to make
    # the distinction unambiguous).
    pacman_repo = RepoConfig('core', 'pacman', 'https://mirror.test/core/os/x86_64', 'x86_64', repo_name='core')
    c2 = config([pacman_repo])
    c2 = replace(c2, nginx=replace(c2.nginx, enable_purge=True, enable_dedup=True))
    rendered = nginx.render(c2)
    assert 'rewrite ^/arch/core/os/x86_64/(.*)$ /core/os/x86_64/$1 break;' in rendered
    assert 'proxy_cache_key "$scheme$proxy_host$uri";' in rendered
    purge_text = nginx.render_purge(c2)
    # Local prefix (/arch/core/os/x86_64) must NOT appear as the key basis —
    # that was the bug. The upstream-relative path (/core/os/x86_64, what
    # $uri actually holds post-rewrite) must.
    assert 'proxy_cache_purge repo_cache "${scheme}mirror.test/core/os/x86_64/$1";' in purge_text
    assert '"${scheme}mirror.test/arch/core/os/x86_64/$1"' not in purge_text


def test_purge_location_omitted_entirely_when_disabled_even_with_cache_key_version():
    c = config([apt()])
    c = replace(c, nginx=replace(c.nginx, cache_key_version='v7'))
    assert 'proxy_cache_purge' not in nginx.render(c)
    assert 'proxy_cache_purge' not in nginx.render_purge(c)


# --- cross-repository dedup of byte-identical files (docs_dev/ROADMAP.md item 29) ---

def test_nginx_config_enable_dedup_defaults_to_false_and_validates_type():
    assert NginxConfig().enable_dedup is False
    assert NginxConfig(enable_dedup=True).enable_dedup is True
    with pytest.raises(ConfigError, match='enable_dedup'):
        NginxConfig(enable_dedup='yes')


def test_dedup_disabled_renders_no_map_block_or_rewrite_check():
    text = nginx.render(config([apt()]))
    assert 'map $request_uri' not in text
    assert 'repowatch_canonical_uri' not in text


def test_dedup_enabled_adds_a_map_block_and_a_rewrite_check_in_content_locations_only():
    c = config([apt()])
    c = replace(c, nginx=replace(c.nginx, enable_dedup=True))
    text = nginx.render(c)
    # Keyed on $uri, not $request_uri: $request_uri never changes across an
    # internal rewrite, which caused a real "rewrite or internal redirection
    # cycle" (nginx re-matches on $uri, so the map must be too — see nginx.py).
    assert 'map $uri $repowatch_canonical_uri {' in text
    assert 'include /etc/nginx/repowatch/dedup.map;' in text
    # map{} must be OUTSIDE server{} (http-context directive) — before it, not after.
    assert text.index('map $uri') < text.index('server {')

    index_block = text[text.index('location ~ ^/debian/dists/'):text.index('location /debian/')]
    content_block = text[text.index('location /debian/'):]
    assert 'repowatch_canonical_uri' not in index_block
    assert 'if ($repowatch_canonical_uri) {' in content_block
    # The map-derived variable is captured into a plain `set` variable and
    # explicitly cleared BEFORE the rewrite, not read again directly —
    # nginx caches a map variable's value for the rest of the request even
    # across an internal `rewrite ... last` that changes $uri, so without
    # this reset the canonical repo's own location (which runs this same
    # check) sees the stale non-empty value and rewrites again forever
    # (nginx's "rewrite or internal redirection cycle" 500 — a real bug
    # caught by hand-testing before this shipped).
    assert 'set $repowatch_dedup_target $repowatch_canonical_uri;' in content_block
    assert 'set $repowatch_canonical_uri "";' in content_block
    assert 'rewrite ^ $repowatch_dedup_target last;' in content_block
    # The cache key override is what actually makes a rewritten request land
    # on the canonical repo's own entry — $request_uri (the default) is the
    # ORIGINAL pre-rewrite URI and would keep the duplicate's own path baked
    # into the key, silently creating a second cache entry instead of
    # sharing one (a real bug caught by hand-testing before this shipped).
    assert 'proxy_cache_key "$scheme$proxy_host$uri";' in content_block


def test_dedup_include_path_is_configurable_and_validated():
    c = config([apt()])
    c = replace(c, nginx=replace(c.nginx, enable_dedup=True))
    text = nginx.render(c, dedup_conf='/etc/nginx/repowatch/custom-dedup.map')
    assert 'include /etc/nginx/repowatch/custom-dedup.map;' in text
    with pytest.raises(ConfigError):
        nginx.render(c, dedup_conf='relative/dedup.map')


def test_render_dedup_empty_by_default():
    assert nginx.render_dedup(config([apt()]), []) == '# Generated by repowatch; edit YAML parameters, not this file.\n'


def test_render_dedup_renders_quoted_key_value_pairs():
    text = nginx.render_dedup(config([apt()]), [('/ubuntu/pool/main/a/a.deb', '/debian/pool/main/a/a.deb')])
    assert '"/ubuntu/pool/main/a/a.deb" "/debian/pool/main/a/a.deb";' in text


def test_render_dedup_escapes_quotes_and_backslashes():
    text = nginx.render_dedup(config([apt()]), [('/a"b\\c', '/x')])
    assert r'"/a\"b\\c" "/x";' in text


def test_resolve_dedup_pairs_uses_the_same_local_path_as_warm_and_purge_urls():
    c = config([
        apt('debian', 'http://deb.debian.org/debian'),
        apt('ubuntu', 'http://archive.ubuntu.com/ubuntu'),
    ])
    rows = [('ubuntu', 'debian', 'pool/main/a/a.deb')]
    pairs = nginx.resolve_dedup_pairs(c, rows)
    assert pairs == [('/ubuntu/pool/main/a/a.deb', '/debian/pool/main/a/a.deb')]


def test_resolve_dedup_pairs_skips_rows_for_repos_no_longer_in_config():
    c = config([apt('debian')])
    rows = [('removed-repo', 'debian', 'a.deb'), ('debian', 'removed-repo', 'a.deb')]
    assert nginx.resolve_dedup_pairs(c, rows) == []


def test_apply_writes_dedup_map_and_includes_it_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(nginx, '_check_policy_owner', lambda p: None)
    c = config([apt()])
    c = replace(c, nginx=replace(c.nginx, enable_dedup=True))
    monkeypatch.setattr(nginx, 'load_config', lambda p: c)
    policy = tmp_path / 'policy.json'
    (tmp_path / 'site.conf').symlink_to(tmp_path / 'active.conf')
    policy.write_text(json.dumps(dict(site_link=str(tmp_path / 'site.conf'), cache_dir='/var/cache/nginx/repo', access_log='/var/log/nginx/repo.log',
                                     nginx_conf='/etc/nginx/nginx.conf', nginx_binary='/usr/sbin/nginx')))
    monkeypatch.setattr(nginx.subprocess, 'run', lambda args, **kw: None)

    assert nginx.apply('config', str(policy))
    dedup_path = tmp_path / 'dedup.map'
    active = tmp_path / 'active.conf'
    assert f'include {dedup_path};' in active.read_text()
    assert dedup_path.exists()
    # A real no-op on the next run — all three files, not just active.conf, are compared.
    assert not nginx.apply('config', str(policy))


def test_apply_rolls_back_dedup_map_together_with_active_conf(tmp_path, monkeypatch):
    monkeypatch.setattr(nginx, '_check_policy_owner', lambda p: None)
    c = config([apt()])
    c = replace(c, nginx=replace(c.nginx, enable_dedup=True))
    monkeypatch.setattr(nginx, 'load_config', lambda p: c)
    policy = tmp_path / 'policy.json'
    (tmp_path / 'site.conf').symlink_to(tmp_path / 'active.conf')
    policy.write_text(json.dumps(dict(site_link=str(tmp_path / 'site.conf'), cache_dir='/var/cache/nginx/repo', access_log='/var/log/nginx/repo.log',
                                     nginx_conf='/etc/nginx/nginx.conf', nginx_binary='/usr/sbin/nginx')))
    active = tmp_path / 'active.conf'
    dedup_path = tmp_path / 'dedup.map'
    active.write_text('# previous working\n')
    dedup_path.write_text('# previous dedup\n')
    calls = []
    def run(args, **kw):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(nginx.subprocess, 'run', run)

    with pytest.raises(ConfigError, match='restored'):
        nginx.apply('config', str(policy))
    assert active.read_text() == '# previous working\n'
    assert dedup_path.read_text() == '# previous dedup\n'


def test_apply_finds_real_duplicates_via_state_store_when_enabled(tmp_path, monkeypatch):
    """apply() must query the real repo_packages table for duplicates when
    enable_dedup is on — not just render an empty map, which would make the
    whole feature a no-op in production."""
    from repowatch.state import RepoSnapshot, StateStore

    monkeypatch.setattr(nginx, '_check_policy_owner', lambda p: None)
    db_path = tmp_path / 'state.sqlite3'
    c = config([
        apt('debian', 'http://deb.debian.org/debian'),
        apt('ubuntu', 'http://archive.ubuntu.com/ubuntu'),
    ])
    c = replace(c, state_db=db_path, nginx=replace(c.nginx, enable_dedup=True))
    store = StateStore(db_path)
    store.record_snapshot(RepoSnapshot('debian', packages={'a-1': 'pool/main/a/a.deb'},
                                        content_hashes={'a-1': 'f' * 64}))
    store.record_snapshot(RepoSnapshot('ubuntu', packages={'a-1': 'pool/main/a/a.deb'},
                                        content_hashes={'a-1': 'f' * 64}))
    monkeypatch.setattr(nginx, 'load_config', lambda p: c)
    policy = tmp_path / 'policy.json'
    (tmp_path / 'site.conf').symlink_to(tmp_path / 'active.conf')
    policy.write_text(json.dumps(dict(site_link=str(tmp_path / 'site.conf'), cache_dir='/var/cache/nginx/repo', access_log='/var/log/nginx/repo.log',
                                     nginx_conf='/etc/nginx/nginx.conf', nginx_binary='/usr/sbin/nginx')))
    monkeypatch.setattr(nginx.subprocess, 'run', lambda args, **kw: None)

    assert nginx.apply('config', str(policy))
    dedup_text = (tmp_path / 'dedup.map').read_text()
    assert '"/ubuntu/pool/main/a/a.deb" "/debian/pool/main/a/a.deb";' in dedup_text


def test_apply_never_queries_state_store_when_dedup_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(nginx, '_check_policy_owner', lambda p: None)
    c = config([apt()])
    monkeypatch.setattr(nginx, 'load_config', lambda p: c)

    def boom(*a, **kw):
        raise AssertionError('StateStore must not be constructed when enable_dedup is off')
    monkeypatch.setattr(nginx, 'StateStore', boom)

    policy = tmp_path / 'policy.json'
    (tmp_path / 'site.conf').symlink_to(tmp_path / 'active.conf')
    policy.write_text(json.dumps(dict(site_link=str(tmp_path / 'site.conf'), cache_dir='/var/cache/nginx/repo', access_log='/var/log/nginx/repo.log',
                                     nginx_conf='/etc/nginx/nginx.conf', nginx_binary='/usr/sbin/nginx')))
    monkeypatch.setattr(nginx.subprocess, 'run', lambda args, **kw: None)

    assert nginx.apply('config', str(policy))


# --- distinguishing repowatch's own traffic in the syslog access_log ---

def test_prefetch_marker_omitted_when_syslog_listener_disabled():
    text = nginx.render(config([apt()]))
    assert 'repowatch_is_prefetch' not in text
    assert 'log_format repowatch_requests' not in text


def test_prefetch_marker_map_and_log_format_present_when_syslog_listener_enabled():
    c = config([apt()])
    c = replace(c, syslog_listener=replace(c.syslog_listener, enabled=True))
    text = nginx.render(c)
    assert 'map $http_user_agent $repowatch_is_prefetch { default 0; "~*^repowatch/" 1; }' in text
    assert ("log_format repowatch_requests '$remote_addr $request_method $request_uri $status "
            "$upstream_cache_status $repowatch_is_prefetch';") in text
    # map{} is http-context — must come before server{}, same rule as dedup's map.
    assert text.index('map $http_user_agent') < text.index('server {')
