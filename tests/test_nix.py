"""Nix catalog, closure warming, signatures, and cache lifecycle regressions."""
import repowatch.cache.nix as cache_nix
import repowatch.cache.purge as cache_purge
import repowatch.nginx.render as nginx_render
import repowatch.operations.check as operations_check
import repowatch.operations.warm as operations_warm
import repowatch.routing as routing
from dataclasses import replace
import hashlib
import asyncio
from functools import wraps
import json
from pathlib import Path
import shutil
import subprocess
import sys

import httpx
import pytest

from repowatch.config.models import Config
from repowatch.errors import ConfigError
from repowatch.config.models import RepoConfig
from repowatch.config.models import NginxConfig
from repowatch.config.models import StatusServerConfig
from repowatch.runtime.context import ServiceState
from repowatch.models import RepoSnapshot
from repowatch.parsers.nix import NixParser, parse_catalog, run_nix
import repowatch.cache.nix as nix_cache
import repowatch.nginx.render as nginx
import repowatch.operations.warm as prefetch
import repowatch.operations.check as watcher
from repowatch.errors import SignatureError

def run_async(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return wrapper


A = '0' * 32
B = '1' * 32
FIXTURES = Path(__file__).parent / 'fixtures/nix'


def repo(**kwargs):
    return RepoConfig('nix-test', 'nix', 'https://cache.test', 'x86_64-linux',
                      nix_source='https://source.test/nixexprs.tar.xz', **kwargs)


def config(tmp_path, r=None, purge=True):
    return Config(tmp_path / 'state.sqlite', 300, 'http://nginx.test', StatusServerConfig(),
                  repos=[r or repo()], nginx=NginxConfig(enabled=True, enable_purge=purge))


def metadata(digest=A, references='', data=b'archive', url=None):
    return (f'StorePath: /nix/store/{digest}-package\nURL: {url or "nar/" + digest + ".nar"}\n'
            f'Compression: none\nFileHash: sha256:{hashlib.sha256(data).hexdigest()}\n'
            f'FileSize: {len(data)}\nNarHash: sha256:{"0" * 64}\nNarSize: 100\n'
            f'References: {references}\n').encode()


def catalog(digest=A):
    return json.dumps({'hello': {'name': 'hello-1.0', 'system': 'x86_64-linux',
                                'outputs': {'out': f'/nix/store/{digest}-hello-1.0'}}}).encode()


def test_cli_rejects_partial_evaluation_errors_after_long_diagnostics():
    with pytest.raises(ValueError, match='incomplete'):
        run_nix([sys.executable, '-c',
                 'import sys; print("warning" * 2000, file=sys.stderr); '
                 'print("error: ignored evaluation", file=sys.stderr); print("{}")'], 5)


def test_cli_timeout_terminates_invocation():
    with pytest.raises(subprocess.TimeoutExpired):
        run_nix([sys.executable, '-c', 'import time; time.sleep(20)'], 0.05)


def client_factory(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: original(*a, transport=httpx.MockTransport(handler), **kw))


@pytest.mark.parametrize('kwargs', [
    {'nix_source': None}, {'nix_source': 'file:///etc/passwd'},
    {'nix_source': 'https://user:password@source.test/x'},
    {'arch': 'amd64'}, {'nix_attributes': '--command'},
    {'nix_attributes': ['--option']}, {'nix_public_keys': ['invalid']},
    {'nix_timeout': True}, {'nix_max_paths': 0},
])
def test_nix_config_rejects_invalid_values(kwargs):
    values = dict(id='n', type='nix', upstream='https://cache.test', arch='x86_64-linux',
                  nix_source='https://source.test/src.tar.xz') | kwargs
    with pytest.raises(ConfigError):
        RepoConfig(**values)


def test_nix_signature_config_requires_public_keys():
    with pytest.raises(ConfigError, match='nix_public_keys'):
        repo(verify_signature=True)
    assert repo(verify_signature=True, nix_public_keys=[(FIXTURES/'trusted.pub').read_text().strip()])


def test_catalog_tracks_rebuilds_and_multiple_outputs():
    first = parse_catalog(catalog(), 'x86_64-linux', 10)
    second = parse_catalog(catalog(B), 'x86_64-linux', 10)
    assert first[0].key != second[0].key
    assert first[0].name == second[0].name == 'hello:out'
    assert first[0].filename == A+'.narinfo'
    data = json.loads(catalog())
    data['hello']['outputs']['dev'] = f'/nix/store/{B}-hello-dev'
    assert len(parse_catalog(json.dumps(data).encode(), 'x86_64-linux', 10)) == 2
    with pytest.raises(ValueError, match='nix_max_paths'):
        parse_catalog(json.dumps(data).encode(), 'x86_64-linux', 1)


@pytest.mark.parametrize('data', [b'[]', b'{"x":{}}', catalog().replace(b'x86_64-linux', b'aarch64-linux'),
                                  catalog().replace(b'/nix/store/', b'/tmp/store/')])
def test_catalog_rejects_partial_or_invalid_output(data):
    with pytest.raises(ValueError):
        parse_catalog(data, 'x86_64-linux', 10)


@pytest.mark.parametrize('url', ['nar/a.nar?hash=abc', 'https://cache.test/nar/a.nar?hash=abc'])
def test_narinfo_preserves_query_and_references(url):
    raw = metadata(references=f'{A}-package {B}-dep', url=url) + b'Sig: one\nSig: two\n'
    info = cache_nix.parse_narinfo(raw, A+'.narinfo', 'https://cache.test')
    assert info['filename'] == 'nar/a.nar?hash=abc'
    assert info['refs'] == [A+'.narinfo', B+'.narinfo']


@pytest.mark.parametrize('url', ['https://foreign.test/nar/a', '../nar/a', '/outside/nar/a',
                                 'nar/a#fragment', 'nar/%2e%2e/a', 'nar/a\n'])
def test_narinfo_rejects_unrepresentable_urls(url):
    with pytest.raises(ValueError):
        cache_nix.nar_filename('https://cache.test/cache/', url)


def test_narinfo_validates_identity_hash_and_duplicate_fields():
    for raw in [metadata(B), metadata()+b'StorePath: x\n',
                metadata().replace(b'FileSize: 7', b'FileSize: -1'),
                metadata().replace(b'FileHash: sha256:', b'FileHash: sha1:')]:
        with pytest.raises(ValueError):
            cache_nix.parse_narinfo(raw, A+'.narinfo', 'https://cache.test')


@run_async
async def test_discovery_handles_cycles_missing_references_and_limits():
    def handler(request):
        return httpx.Response(200, content=metadata(A, f'{A}-package {B}-dep') if request.url.path.endswith(A+'.narinfo')
                              else metadata(B, f'{A}-package'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await cache_nix.discover(client, repo(), A+'.narinfo')
        assert len(found) == 2
        with pytest.raises(ValueError, match='nix_max_paths'):
            await cache_nix.discover(client, repo(nix_max_paths=1), A+'.narinfo')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await cache_nix.discover(client, repo(), A+'.narinfo')


@run_async
async def test_warm_retries_partial_closure_and_preserves_bookkeeping(tmp_path, monkeypatch):
    c = config(tmp_path); r = c.repos[0]; store = ServiceState(c.state_db)
    store.repositories.record_snapshot(RepoSnapshot(r.id, {'root': A+'.narinfo'}, {'root':'hello'}))
    broken = True
    calls = []
    def handler(request):
        calls.append(str(request.url))
        if request.url.path.endswith(A+'.narinfo'):
            return httpx.Response(200, content=metadata(A, f'{B}-dependency'))
        if request.url.path.endswith(B+'.narinfo'):
            return httpx.Response(200, content=metadata(B))
        return httpx.Response(200, content=b'corrupt' if broken and request.url.path.endswith(B+'.nar') else b'archive')
    client_factory(monkeypatch, handler)
    result = await operations_warm.warm_cache(c, r, store, {'root': A+'.narinfo'})
    assert result == {'root': False}
    assert store.cache.get_warmed_packages(r.id)[0]['status'] == 'failed'
    assert len(store.cache.get_nix_artifacts(r.id, 'root')) == 4
    broken = False
    assert await operations_warm.warm_cache(c, r, store, {'root': A+'.narinfo'}) == {'root': True}
    assert all('nginx.test' in url for url in calls if '.nar?' in url or url.endswith('.nar'))
    assert store.cache.get_warmed_packages(r.id)[0]['status'] == 'ok'


@run_async
async def test_nix_disabled_and_banned_warming_do_not_fetch(tmp_path, monkeypatch):
    c = config(tmp_path, repo(prefetch=False)); r = c.repos[0]; store = ServiceState(c.state_db)
    store.repositories.record_snapshot(RepoSnapshot(r.id, {'root': A+'.narinfo'}, {'root':'hello'}))
    client_factory(monkeypatch, lambda request: pytest.fail('unexpected fetch'))
    assert await operations_warm.warm_cache(c,r,store,{'root':A+'.narinfo'}) == {}
    store.cache.ban_package(r.id, 'hello')
    assert await operations_warm.warm_cache(c,r,store,{'root':A+'.narinfo'},force=True) == {}


@run_async
async def test_purge_preserves_shared_artifacts_and_reports_errors(tmp_path, monkeypatch):
    c=config(tmp_path); r=c.repos[0]; store=ServiceState(c.state_db)
    store.repositories.record_snapshot(RepoSnapshot(r.id,{'other':B+'.narinfo'}))
    store.cache.record_nix_artifacts(r.id,'old',[{'filename': A+'.narinfo'},{'filename':'nar/shared.nar'}])
    store.cache.record_nix_artifacts(r.id,'other',[{'filename':'nar/shared.nar'}])
    selected=[]
    async def purge(c,r,items):
        selected.extend(items.values());return dict.fromkeys(items,'purged')
    monkeypatch.setattr(cache_purge,'purge_selected',purge)
    assert await cache_nix.purge(c,r,store,{'old':A+'.narinfo'}) == {'old':'retained_shared'}
    assert selected == [A+'.narinfo']
    async def error(c,r,items): return dict.fromkeys(items,'error (HTTP 503)')
    monkeypatch.setattr(cache_purge,'purge_selected',error)
    assert (await cache_nix.purge(c,r,store,{'old':A+'.narinfo'}))['old'].startswith('error')


def test_client_activity_does_not_claim_a_partial_closure_is_complete(tmp_path):
    store=ServiceState(tmp_path/'state.sqlite')
    for key,ok in [('complete',True),('partial',False)]:
        store.cache.record_nix_artifacts('n',key,[{'filename':'nar/shared.nar'}])
        store.cache.record_warmed_package('n',key,A+'.narinfo',ok,200)
    store.cache.touch_nix_artifact('n','nar/shared.nar')
    assert {x['package_key']:x['status'] for x in store.cache.get_warmed_packages('n')} == {'complete':'ok','partial':'failed'}


def test_trust_policy_change_invalidates_completed_closures(tmp_path):
    store = ServiceState(tmp_path / 'state.sqlite')
    store.cache.update_nix_trust('n', False, [])
    store.cache.record_warmed_package('n', 'root', A + '.narinfo', True, 200)
    store.cache.update_nix_trust('n', False, [])
    assert store.cache.get_warmed_packages('n')[0]['status'] == 'ok'
    store.cache.update_nix_trust('n', True, ['key-one'])
    assert store.cache.get_warmed_packages('n')[0]['status'] == 'failed'
    store.cache.record_warmed_package('n', 'root', A + '.narinfo', True, 200)
    store.cache.update_nix_trust('n', True, ['key-two'])
    assert store.cache.get_warmed_packages('n')[0]['status'] == 'failed'


@run_async
async def test_purge_retains_artifacts_when_other_closures_are_unknown(tmp_path, monkeypatch):
    c = config(tmp_path)
    r = c.repos[0]
    store = ServiceState(c.state_db)
    store.repositories.record_snapshot(RepoSnapshot(r.id, {'unknown': B + '.narinfo'}))
    store.cache.record_nix_artifacts(r.id, 'old', [{'filename': 'nar/shared.nar'}])
    async def purge(c, r, items):
        assert not items
        return {}
    monkeypatch.setattr(cache_purge, 'purge_selected', purge)
    assert await cache_nix.purge(c, r, store, {'old': A + '.narinfo'}) == {'old': 'retained_shared'}


def test_artifact_retention_keeps_current_and_warmed_owners(tmp_path):
    store = ServiceState(tmp_path / 'state.sqlite')
    store.repositories.record_snapshot(RepoSnapshot('n', {'current': A + '.narinfo'}))
    store.cache.record_warmed_package('n', 'warmed', B + '.narinfo', True, 200)
    for key in ('current', 'warmed', 'orphan'):
        store.cache.record_nix_artifacts('n', key, [{'filename': 'nar/shared.nar'}])
    store.cache.prune_warmed_packages(180)
    assert store.cache.get_nix_artifacts('n', 'current')
    assert store.cache.get_nix_artifacts('n', 'warmed')
    assert not store.cache.get_nix_artifacts('n', 'orphan')


@pytest.mark.parametrize('dedup',[False,True])
def test_nix_nginx_keys_preserve_nar_query(tmp_path,dedup):
    c=config(tmp_path);c=replace(c,nginx=replace(c.nginx,enable_dedup=dedup))
    text=nginx_render.render(c)
    assert 'nix-cache-info' in text and 'proxy_cache_valid 404 1s;' in text
    assert '$uri$is_args$args' in text if dedup else '$request_uri' in text
    assert '$1$is_args$args' in nginx_render.render_purge(c)
    key=routing.compute_cache_key(c,c.repos[0],'nar/a.nar?hash=one')
    assert key.endswith('nar/a.nar?hash=one')
    assert key != routing.compute_cache_key(c,c.repos[0],'nar/a.nar?hash=two')


def test_real_nix_cli_catalog_and_signature_verification():
    if not shutil.which('nix') or not shutil.which('nix-env'):
        pytest.skip('optional Nix CLI is not installed')
    raw=run_nix(['nix-env','--query','--available','--json','--out-path','--file',str(FIXTURES/'catalog.nix'),
                 '--argstr','system','x86_64-linux','--system-filter','x86_64-linux'],30)
    packages=parse_catalog(raw,'x86_64-linux',10)
    assert {p.name for p in packages} == {'hello:out','tools.multi:out','tools.multi:dev'}
    signed=(FIXTURES/'signed.narinfo').read_bytes()
    filename=signed.split(b'/nix/store/',1)[1][:32].decode()+'.narinfo'
    info=cache_nix.parse_narinfo(signed,filename,'https://cache.test')
    keys=[(FIXTURES/'trusted.pub').read_text().strip()]
    cache_nix.verify_metadata({filename:(signed,info)},keys,30)
    for damaged in [signed.replace(b'NarSize: ',b'NarSize: 1'),
                    b'\n'.join(line for line in signed.split(b'\n') if not line.startswith(b'Sig: '))]:
        with pytest.raises(SignatureError):
            cache_nix.verify_metadata({filename:(damaged,info)},keys,30)


@run_async
async def test_watcher_retries_missing_binary_without_catalog_change(tmp_path, monkeypatch):
    c=config(tmp_path);store=ServiceState(c.state_db)
    monkeypatch.setattr('repowatch.parsers.nix.run_nix', lambda args,timeout:
        json.dumps('/nix/store/'+A+'-source').encode() if args[0]=='nix-instantiate' else catalog())
    missing=True
    def handler(request):
        if missing and request.url.path.endswith('.narinfo'):
            return httpx.Response(404)
        if request.url.path.endswith('.narinfo'):
            return httpx.Response(200,content=metadata())
        return httpx.Response(200,content=b'archive')
    client_factory(monkeypatch,handler)
    await operations_check.check_repo(c,c.repos[0],store)
    assert store.cache.get_warmed_packages(c.repos[0].id)[0]['status']=='failed'
    first=store.repositories.get_status(c.repos[0].id)['changed_at']
    missing=False
    await operations_check.check_repo(c,c.repos[0],store)
    assert store.cache.get_warmed_packages(c.repos[0].id)[0]['status']=='ok'
    assert store.repositories.get_status(c.repos[0].id)['changed_at']==first
    assert len(store.repositories.get_history(c.repos[0].id))==1


def test_real_nix_client_through_generated_nginx(tmp_path):
    """Evaluate, warm, then consume a signed cache with its upstream offline."""
    if not all(shutil.which(tool) for tool in ('nix', 'nix-env', 'nginx')):
        pytest.skip('optional Nix CLI and nginx are required')
    import http.server
    import socket
    import tarfile
    import threading
    import time
    import urllib.request
    signed=(FIXTURES/'signed.narinfo').read_bytes()
    fields=dict(line.split(': ',1) for line in signed.decode().splitlines())
    path=fields['StorePath'];filename=path.split('/')[-1][:32]+'.narinfo'
    key=(FIXTURES/'trusted.pub').read_text().strip()
    source=tmp_path/'source';source.mkdir()
    nar_hash=fields['NarHash'].split(':',1)[1]
    (source/'default.nix').write_text('{ system ? builtins.currentSystem }: { fixture = builtins.derivation { '
        'name = "repowatch-nix-payload"; inherit system; builder = "/bin/false"; '
        f'outputHashMode = "recursive"; outputHashAlgo = "sha256"; outputHash = "{nar_hash}"; '
        '}; }')
    tarball=tmp_path/'source.tar.gz'
    with tarfile.open(tarball,'w:gz') as archive:
        archive.add(source,arcname='source')
    files={'/source.tar.gz':tarball.read_bytes(), '/cache/'+filename:signed,
           '/cache/nix-cache-info':b'StoreDir: /nix/store\nWantMassQuery: 0\nPriority: 40\n',
           '/cache/'+fields['URL']:(FIXTURES/'payload.nar.xz').read_bytes()}
    online=True
    class Backend(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            content=files.get(self.path)
            self.send_response(200 if content is not None and online else 503)
            body=content if content is not None and online else b'unavailable'
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args): pass
    backend=http.server.ThreadingHTTPServer(('127.0.0.1',0),Backend)
    threading.Thread(target=backend.serve_forever,daemon=True).start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    upstream=f'http://127.0.0.1:{backend.server_port}'
    r=RepoConfig('nix-test','nix',upstream+'/cache','x86_64-linux',nix_source=upstream+'/source.tar.gz',
                 verify_signature=True,nix_public_keys=[key])
    c=replace(config(tmp_path,r,purge=False),cache_base_url=f'http://127.0.0.1:{port}')
    c=replace(c,nginx=replace(c.nginx,listen=f'127.0.0.1:{port}'),syslog_listener=replace(c.syslog_listener,enabled=False))
    text=nginx_render.render(c,cache_dir=str(tmp_path/'cache'),access_log=str(tmp_path/'access.log'))
    conf=tmp_path/'nginx.conf';conf.write_text(f'pid {tmp_path}/nginx.pid; error_log {tmp_path}/error.log;\nevents {{}}\nhttp {{\n{text}\n}}')
    process=None
    try:
        command=['nginx','-p',str(tmp_path),'-c',str(conf)]
        subprocess.run(command+['-t'],check=True,capture_output=True)
        process=subprocess.Popen(command+['-g','daemon off;'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        for attempt in range(100):
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.1):break
            except OSError:time.sleep(.02)
        store=ServiceState(c.state_db)
        asyncio.run(operations_check.check_repo(c,r,store))
        warmed=store.cache.get_warmed_packages(r.id)
        assert len(warmed)==1 and warmed[0]['status']=='ok',warmed
        assert len(store.cache.get_nix_artifacts(r.id,warmed[0]['package_key']))==2
        online=False
        for name in ['nix-cache-info',filename,fields['URL']]:
            with urllib.request.urlopen(c.cache_base_url+'/nix/nix-test/'+name) as response:
                assert response.headers['X-Cache-Status']=='HIT'
                assert response.read()==files['/cache/'+name]
        destination=tmp_path/'client-cache'
        run_nix(['nix','--extra-experimental-features','nix-command','copy',
                 '--from',c.cache_base_url+'/nix/nix-test','--to',destination.as_uri(),
                 '--option','trusted-public-keys',key,path],30)
        assert (destination/filename).exists()
    finally:
        if process is not None:
            process.terminate();process.wait(timeout=10)
        backend.shutdown();backend.server_close()


@run_async
async def test_nix_warming_lists_filter_roots_without_breaking_closure(tmp_path, monkeypatch):
    r = repo(prefetch=False, prefetch_whitelist=['hello:*'], prefetch_blacklist=['*:dev', 'dependency:*'])
    c = config(tmp_path, r); store = ServiceState(c.state_db)
    files = {'root': A+'.narinfo', 'blocked': B+'.narinfo'}
    store.repositories.record_snapshot(RepoSnapshot(r.id, files, {'root': 'hello:out', 'blocked': 'hello:dev'}))
    calls = []
    def handler(request):
        calls.append(str(request.url))
        if request.url.path.endswith(A+'.narinfo'):
            return httpx.Response(200, content=metadata(A, f'{B}-dependency'))
        if request.url.path.endswith(B+'.narinfo'):
            return httpx.Response(200, content=metadata(B))
        return httpx.Response(200, content=b'archive')
    client_factory(monkeypatch, handler)
    assert await operations_warm.warm_cache(c, r, store, files, force=True) == {'root': True}
    assert any(B+'.nar' in url for url in calls)
    assert len(store.cache.get_nix_artifacts(r.id, 'root')) == 4
    assert not store.cache.get_nix_artifacts(r.id, 'blocked')
    calls.clear()
    blocked_repo = replace(r, prefetch_blacklist=['*'])
    assert await operations_warm.warm_cache(c, blocked_repo, store, files, force=True) == {}
    assert not calls
