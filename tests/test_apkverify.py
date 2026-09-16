import repowatch.verification.apk as verification_apk
import gzip
import io
import shutil
import subprocess
import tarfile

import pytest

from repowatch.verification.apk import verify_index
from repowatch.config.models import RepoConfig
from repowatch.errors import SignatureError
import httpx
import asyncio


def tar_bytes(name, data, cut=False):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as tar:
        item = tarfile.TarInfo(name); item.size = len(data)
        tar.addfile(item, io.BytesIO(data))
    raw = buffer.getvalue()
    return raw[:512 + ((len(data)+511)//512)*512] if cut else raw


@pytest.fixture
def signed(tmp_path):
    if not shutil.which('openssl'): pytest.skip('openssl required')
    key = tmp_path / 'private'
    subprocess.run(['openssl', 'genrsa', '-out', str(key), '2048'], check=True, capture_output=True)
    keys = tmp_path / 'keys'; keys.mkdir()
    subprocess.run(['openssl', 'rsa', '-in', str(key), '-pubout', '-out', str(keys/'test.rsa.pub')], check=True, capture_output=True)
    payload = gzip.compress(tar_bytes('APKINDEX', b'P:hello\nV:1.0\n\n'))
    def sign(kind='RSA256', data=payload):
        signature = subprocess.run(['openssl', 'dgst', '-sha256' if kind=='RSA256' else '-sha1', '-sign', str(key)], input=data, check=True, capture_output=True).stdout
        return gzip.compress(tar_bytes('.SIGN.' + kind + '.test.rsa.pub', signature, cut=True)) + data
    return keys, payload, sign


@pytest.mark.parametrize('kind', ['RSA', 'RSA256'])
@pytest.mark.parametrize('backend', ['openssl', 'apk-tools'])
def test_verified_and_tampered(signed, kind, backend):
    if backend == 'apk-tools' and not shutil.which('apk'): pytest.skip('apk-tools >=3 required')
    keys, payload, sign = signed
    raw = sign(kind)
    assert verify_index(raw, str(keys), backend) == payload
    # Structurally valid recompression still differs from the signed bytes.
    corrupt = raw[:-len(payload)] + gzip.compress(gzip.decompress(payload), mtime=1)
    with pytest.raises(SignatureError): verify_index(corrupt, str(keys), backend)
    with pytest.raises(SignatureError): verify_index(raw + gzip.compress(b'unsigned'), str(keys), backend)
    with pytest.raises(SignatureError): verify_index(payload, str(keys), backend)
    (keys/'test.rsa.pub').write_text('not a key')
    with pytest.raises(SignatureError): verify_index(raw, str(keys), backend)


def test_missing_key_tool_and_old_apk(signed, monkeypatch):
    keys, _, sign = signed
    raw = sign()
    monkeypatch.setenv('PATH', '/nonexistent')
    with pytest.raises(SignatureError, match='cannot run'): verify_index(raw, str(keys))


def test_apk_watcher_preserves_state_and_recovers(signed, tmp_path, monkeypatch):
    from repowatch.config.models import Config
    from repowatch.config.models import StatusServerConfig
    from repowatch.runtime.context import ServiceState
    from repowatch.models import RepoSnapshot
    from repowatch.operations.check import check_repo
    keys, payload, sign = signed
    raw = sign()
    repo = RepoConfig(id='apk', type='apk', upstream='https://example.org', arch='x86_64',
                      prefetch=False, verify_signature=True, apk_keys_dir=str(keys))
    config = Config(tmp_path/'state', 300, 'http://cache', StatusServerConfig(), repos=[repo])
    store = ServiceState(config.state_db)
    store.repositories.record_snapshot(RepoSnapshot('apk', {'old':'old.apk'}))
    corrupt = True
    def handler(request):
        if request.method=='HEAD': return httpx.Response(200, headers={'ETag':'new'})
        return httpx.Response(200, content=raw[:-1] if corrupt else raw)
    original = httpx.AsyncClient
    monkeypatch.setattr('repowatch.operations.check.httpx.AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    asyncio.run(check_repo(config, repo, store))
    assert store.repositories.get_packages('apk')=={'old':'old.apk'}
    with store.database.connect() as conn:
        assert conn.execute('SELECT consecutive_failures FROM failure_state').fetchone()[0]==1
    corrupt=False
    asyncio.run(check_repo(config, repo, store))
    assert store.repositories.get_packages('apk')=={'hello-1.0':'hello-1.0.apk'}
    with store.database.connect() as conn:
        assert conn.execute('SELECT count(*) FROM failure_state').fetchone()[0]==0
    # Same ETag must not hide loss of a trusted key after successful verification.
    (keys/'test.rsa.pub').unlink()
    asyncio.run(check_repo(config, repo, store))
    assert store.repositories.get_packages('apk')=={'hello-1.0':'hello-1.0.apk'}
    with store.database.connect() as conn:
        assert conn.execute('SELECT consecutive_failures FROM failure_state').fetchone()[0]==1


def test_old_apk_rejected(signed, monkeypatch):
    import repowatch.verification.apk as module
    keys, _, sign = signed
    raw = sign()
    monkeypatch.setattr(verification_apk, '_run', lambda args: subprocess.CompletedProcess(args, 0, 'apk-tools 2.14.0, compiled for x86_64', ''))
    with pytest.raises(SignatureError, match='>= 3.0'):
        verify_index(raw, str(keys), 'apk-tools')


def test_missing_trusted_key_and_bad_envelope(signed):
    keys, _, sign = signed
    raw = sign()
    (keys/'test.rsa.pub').unlink()
    with pytest.raises(SignatureError, match='trusted key missing'): verify_index(raw, str(keys))
    for malformed in (b'', b'not gzip', gzip.compress(b'not tar')+gzip.compress(b'data')):
        with pytest.raises(SignatureError): verify_index(malformed, str(keys))


@pytest.mark.parametrize('backend', ['openssl', 'apk-tools'])
def test_different_valid_public_key_is_rejected(signed, tmp_path, backend):
    if backend=='apk-tools' and not shutil.which('apk'): pytest.skip('apk-tools required')
    keys, _, sign=signed
    raw=sign()
    other=tmp_path/'other-private'
    subprocess.run(['openssl','genrsa','-out',str(other),'2048'],check=True,capture_output=True)
    subprocess.run(['openssl','rsa','-in',str(other),'-pubout','-out',str(keys/'test.rsa.pub')],check=True,capture_output=True)
    with pytest.raises(SignatureError): verify_index(raw,str(keys),backend)
