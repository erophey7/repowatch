import asyncio
import bz2
import gzip
import hashlib
import lzma
from pathlib import Path
import shutil
import struct
import subprocess

import httpx
import pytest

from repowatch.config.models import Config
from repowatch.errors import ConfigError
from repowatch.config.models import RepoConfig
from repowatch.config.models import StatusServerConfig
from repowatch.errors import SignatureError
from repowatch.parsers import AptRpmParser, PARSERS
from repowatch.parsers.apt_rpm import checksums, parse_pkglist, release_body
from repowatch.routing import warm_url
from repowatch.models import RepoSnapshot
from repowatch.runtime.context import ServiceState
from repowatch.runtime.syslog import match_repo_id
from repowatch.operations.check import check_repo
from test_gpgverify import gpg_env


def header(**overrides):
    fields = {1000:'demo', 1001:'1.2', 1002:'alt1', 1003:2, 1006:123,
              1022:'x86_64', 1155:'p11+123', 1000000:'demo-1.2-alt1.x86_64.rpm', 1000010:'RPMS.classic'}
    fields.update({int(k):v for k,v in overrides.items()})
    data=bytearray(); index=bytearray()
    for tag,value in sorted(fields.items()):
        if value is None:
            continue
        kind=4 if isinstance(value,int) else 6
        index.extend(struct.pack('>4I',tag,kind,len(data),1))
        data.extend(struct.pack('>I',value) if kind==4 else value.encode()+b'\0')
    return b'\x8e\xad\xe8\x01\0\0\0\0'+struct.pack('>II',len(index)//16,len(data))+index+data


def repo(**kwargs):
    return RepoConfig(**dict({'id':'alt', 'type':'apt-rpm', 'upstream':'https://example.test/p11/x86_64',
        'arch':'x86_64', 'component':'classic', 'prefetch':False}, **kwargs))


def release(raw, packed, suffix='.xz'):
    return ('Origin: synthetic ALT fixture\nBLAKE2b:\n'
        f' {hashlib.blake2b(raw).hexdigest()} {len(raw)} base/pkglist.classic\n'
        + (f' {hashlib.blake2b(packed).hexdigest()} {len(packed)} base/pkglist.classic{suffix}\n' if suffix else '')).encode()


def fetch(r=None, raw=None, packed=None, metadata=None, suffix='.xz'):
    raw = header() if raw is None else raw
    packed = lzma.compress(raw) if packed is None else packed
    metadata = release(raw,packed,suffix) if metadata is None else metadata
    requested=[]
    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200,content=metadata if request.url.path.endswith('/release') else packed)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await AptRpmParser(r or repo()).fetch(client)
    return asyncio.run(run()),requested


@pytest.mark.parametrize('suffix,compress',[('.xz',lzma.compress),('.bz2',bz2.compress),('.gz',gzip.compress),('',lambda b:b)])
def test_fetch_compressions_identity_paths_and_registry(suffix,compress):
    raw=header()+header(**{'1000':'docs','1022':'noarch','1000000':'docs.rpm'})+header(**{'1000':'other','1022':'aarch64'})
    packed=compress(raw)
    snapshot,requested=fetch(raw=raw,packed=packed,suffix=suffix)
    assert PARSERS['apt-rpm'] is AptRpmParser
    assert snapshot.packages == {'demo-2:1.2-alt1:p11+123@123.x86_64':'RPMS.classic/demo-1.2-alt1.x86_64.rpm',
                                'docs-2:1.2-alt1:p11+123@123.noarch':'RPMS.classic/docs.rpm'}
    assert set(snapshot.names.values())=={'demo','docs'}
    assert requested==['/p11/x86_64/base/release','/p11/x86_64/base/pkglist.classic'+suffix]


def test_head_uses_release():
    def handler(request):
        assert request.method=='HEAD' and request.url.path.endswith('/base/release')
        assert request.headers['if-none-match']=='v1'
        return httpx.Response(304)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert (await AptRpmParser(repo()).check_index_changed(client,'v1',None)).unchanged
    asyncio.run(run())


@pytest.mark.parametrize('bad', [b'', b'html', header()[:-1], b'\0'*16, header()+header(),
    header(**{'1000':None}), header(**{'1000000':'../escape.rpm'}), header(**{'1000010':'/etc'}),
    header(**{'1000000':'https://evil/a.rpm'}), header(**{'1001':42})])
def test_malformed_pkglist_rejected(bad):
    if not bad:
        # A legitimately empty, checksum-verified repository is allowed.
        assert parse_pkglist(bad,'','x86_64','classic')==[]
    else:
        with pytest.raises((ValueError,UnicodeError)):
            parse_pkglist(bad,'','x86_64','classic')


def test_checksum_and_expanded_checksum_mismatch():
    raw=header();packed=lzma.compress(raw)
    with pytest.raises(SignatureError,match='checksum'):
        fetch(raw=raw,packed=packed+b'x',metadata=release(raw,packed))
    with pytest.raises(SignatureError,match='expanded'):
        fetch(raw=raw,packed=packed,metadata=release(raw+b'x',packed))
    with pytest.raises(SignatureError,match='no SHA256'):
        fetch(metadata=b'Origin: bad\nMD5Sum:\n abc 1 base/pkglist.classic.xz\n')
    with pytest.raises(SignatureError):
        checksums(b'BLAKE2b:\n bad 1 base/pkglist.classic\n')


def test_signature_failure_precedes_index_download(monkeypatch):
    called=[]
    async def get(client,url):
        called.append(url)
        return b'Unsigned release\n'
    p=AptRpmParser(repo(verify_signature=True,keyring_path='/unused'))
    monkeypatch.setattr(p,'_http_get',get)
    with pytest.raises(SignatureError,match='no appended'):
        asyncio.run(p.fetch_packages(None))
    assert len(called)==1


@pytest.mark.skipif(not(shutil.which('gpg') and shutil.which('gpgv')),reason='gpg/gpgv required')
def test_real_appended_signature(gpg_env,tmp_path):
    env,keyring=gpg_env
    raw=header();packed=lzma.compress(raw);body=release(raw,packed)+b'\n'
    signature=subprocess.run(['gpg','--batch','--armor','--detach-sign'],input=body,env=env,
                             check=True,capture_output=True).stdout
    metadata=body+signature
    assert release_body(metadata,keyring)==body
    snapshot,_=fetch(repo(verify_signature=True,keyring_path=keyring),metadata=metadata)
    assert len(snapshot.packages)==1
    with pytest.raises(SignatureError):
        release_body(metadata.replace(b'synthetic',b'corrupted'),keyring)
    empty=tmp_path/'empty.gpg';empty.write_bytes(b'')
    with pytest.raises(SignatureError):
        release_body(metadata,str(empty))


def test_custom_alt_routes_warming_and_syslog():
    r=repo(url_template='/{distro}/{branch}/{arch}', url_variables={'distro':'altlinux','branch':'p11'})
    c=Config(Path('/tmp/unused'),300,'http://cache',StatusServerConfig(),repos=[r])
    url=warm_url(c,r,'RPMS.classic/demo.rpm')
    assert url=='http://cache/altlinux/p11/x86_64/RPMS.classic/demo.rpm'
    assert match_repo_id('/altlinux/p11/x86_64/RPMS.classic/demo.rpm',c.repos)=='alt'
    from repowatch.nginx.render import render
    text=render(c)
    assert 'location ~ ^/altlinux/p11/x86_64/base/' in text
    assert text.count('proxy_cache_valid 200 206 300s;')==2
    assert 'proxy_cache_revalidate on;' in text
    assert 'rewrite ^/altlinux/p11/x86_64/(.*)$ /p11/x86_64/$1 break;' in text


def test_watcher_preserves_snapshot_on_corruption_and_recovers(tmp_path,monkeypatch):
    r=repo();c=Config(tmp_path/'state',300,'http://cache',StatusServerConfig(),repos=[r])
    store=ServiceState(c.state_db);store.repositories.record_snapshot(RepoSnapshot('alt',{'old':'old.rpm'}))
    raw=header();packed=lzma.compress(raw);metadata=release(raw,packed);bad=True
    def handler(request):
        if request.method=='HEAD':return httpx.Response(200,headers={'ETag':'new'})
        return httpx.Response(200,content=metadata if request.url.path.endswith('/release') else packed+(b'x' if bad else b''))
    original=httpx.AsyncClient
    monkeypatch.setattr('repowatch.operations.check.httpx.AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    asyncio.run(check_repo(c,r,store))
    assert store.repositories.get_packages('alt')=={'old':'old.rpm'}
    with store.database.connect() as conn:
        assert conn.execute('SELECT consecutive_failures FROM failure_state').fetchone()[0]==1
    bad=False
    asyncio.run(check_repo(c,r,store))
    assert 'old' not in store.repositories.get_packages('alt')
    assert len(store.repositories.get_packages('alt'))==1
    with store.database.connect() as conn:
        assert conn.execute('SELECT count(*) FROM failure_state').fetchone()[0]==0


def test_persistent_binary_fixture():
    fixtures=Path(__file__).parent/'fixtures'/'apt-rpm'
    snapshot,_=fetch(packed=(fixtures/'pkglist.classic.xz').read_bytes(),metadata=(fixtures/'release').read_bytes())
    assert snapshot.packages['demo-2:1.2-alt1:p11+123@123.x86_64']=='RPMS.classic/demo-1.2-alt1.x86_64.rpm'


@pytest.mark.parametrize('field,value', [('component',None),('component','../classic'),('arch','x86/64')])
def test_alt_config_rejects_bad_component_and_arch(field,value):
    with pytest.raises(ConfigError):
        repo(**{field:value})


def test_parsed_alt_packages_warm_through_cache_and_respect_bans(tmp_path,monkeypatch):
    from repowatch.operations.warm import warm_cache
    r=repo(url_template='/altlinux/{arch}/',prefetch=True)
    c=Config(tmp_path/'state',300,'http://cache:8080',StatusServerConfig(),repos=[r])
    store=ServiceState(c.state_db)
    snapshot,_=fetch(r)
    store.repositories.record_snapshot(snapshot)
    requested=[]
    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200,content=b'rpm fixture')
    original=httpx.AsyncClient
    monkeypatch.setattr('repowatch.operations.warm.httpx.AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    asyncio.run(warm_cache(c,r,store,snapshot.packages))
    assert requested==['http://cache:8080/altlinux/x86_64/RPMS.classic/demo-1.2-alt1.x86_64.rpm']
    store.cache.ban_package(r.id,'demo')
    asyncio.run(warm_cache(c,r,store,snapshot.packages,force=True))
    assert len(requested)==1
