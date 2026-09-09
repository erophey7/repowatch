from __future__ import annotations

from email.message import Message
from pathlib import Path
import threading
import time

import httpx
import pytest
import yaml

from repowatch.access import AccessStore, client_context
from repowatch.api import StatusHTTPServer, make_handler
from repowatch.auth import hash_password, verify_password
from repowatch.cli import main
from repowatch.config import ConfigError, StatusServerConfig, load_config
from repowatch.config_edit import config_lock, set_password_hash
from repowatch.state import StateStore


@pytest.fixture
def api(tmp_path):
    path = tmp_path / 'config.yaml'
    raw = {'state_db': str(tmp_path / 'state'), 'cache_base_url': 'http://localhost',
           'admin_password_hash': hash_password('secret', iterations=1000),
           'repos': [{'id': 'r', 'type': 'apk', 'arch': 'x86_64', 'upstream': 'https://example.org'}]}
    path.write_text(yaml.safe_dump(raw))
    config = load_config(path)
    store = StateStore(config.state_db)
    server = StatusHTTPServer(('127.0.0.1', 0), make_handler(config, store, path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False) as client:
            yield client, path, store
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def login(client):
    response = client.post('/api/auth/login', json={'password': 'secret'})
    assert response.status_code == 200, response.text
    return response.json()['csrf_token']


@pytest.mark.parametrize('path', ['/', '/dashboard', '/api/repos', '/api/config', '/api/requests',
    '/api/requests/summary', '/status/r/history', '/api/repos/r/packages', '/api/repos/r/warmed',
    '/api/repos/r/bans', '/api/tokens', '/api/auth/session', '/status.json', '/status/r.json'])
def test_anonymous_routes_closed(api, path):
    client, _, _ = api
    response = client.get(path)
    assert response.status_code in (303, 401)
    assert 'cache_base_url' not in response.text
    assert response.headers['cache-control'] == 'no-store'


def test_login_session_csrf_and_logout(api):
    client, _, store = api
    assert client.get('/login').status_code == 200
    assert client.post('/api/auth/login', json={'password': 'wrong'}).status_code == 401
    response = client.post('/api/auth/login', json={'password': 'secret'})
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=Strict' in cookie and 'Max-Age=43200' in cookie
    csrf = response.json()['csrf_token']
    assert 43190 < response.json()['expires_at'] - time.time() <= 43200
    assert client.get('/').status_code == 200
    assert client.get('/api/auth/session').json()['csrf_token'] == csrf
    assert client.post('/api/config', json={'check_interval': 60}).status_code == 403
    assert client.post('/api/config', json={'check_interval': 60}, headers={'X-CSRF-Token': csrf}).status_code == 200
    assert client.post('/api/auth/logout', headers={'X-CSRF-Token': csrf}).status_code == 200
    assert client.get('/api/repos').status_code == 401
    with store._connect() as conn:
        assert conn.execute('SELECT count(*) FROM admin_sessions').fetchone()[0] == 0


def test_token_permissions_expiry_and_revocation(api):
    client, _, store = api
    csrf = login(client)
    response = client.post('/api/tokens', json={'name': 'host-a'}, headers={'X-CSRF-Token': csrf})
    assert response.status_code == 201
    token = response.json()
    listed = client.get('/api/tokens').json()
    assert 'token' not in listed[0] and 'secret_hash' not in listed[0]
    with store._connect() as conn:
        secret_hash = conn.execute('SELECT secret_hash FROM host_tokens').fetchone()[0]
        assert token['token'] != secret_hash and len(secret_hash) == 64
    cookies = httpx.Cookies(client.cookies)
    client.cookies.clear()
    header = {'Authorization': 'Bearer ' + token['token']}
    assert client.get('/status.json', headers=header).status_code == 200
    assert client.get('/status/r.json', headers=header).status_code == 200
    for route in ('/api/repos', '/api/tokens', '/status/r/history', '/api/repos/r/packages'):
        assert client.get(route, headers=header).status_code == 401
    assert client.post('/api/config', json={}, headers=header).status_code == 401
    assert AccessStore(store).tokens()[0]['last_used_at'] is not None
    client.cookies = cookies
    assert client.post(f"/api/tokens/{token['id']}/revoke", headers={'X-CSRF-Token': csrf}).status_code == 200
    client.cookies.clear()
    assert client.get('/status.json', headers=header).status_code == 401
    expired = AccessStore(store).create_token('expire', time.time() + 100)
    with store._connect() as conn:
        conn.execute('UPDATE host_tokens SET expires_at=0 WHERE id=?', (expired['id'],))
    assert AccessStore(store).token_access(expired['token']) is None


def test_password_change_revokes_sessions_preserves_tokens(api):
    client, path, store = api
    login(client)
    token = AccessStore(store).create_token('host')['token']
    set_password_hash(path, hash_password('new', iterations=1000))
    assert client.get('/api/repos').status_code == 401
    client.cookies.clear()
    assert client.get('/status.json', headers={'Authorization': 'Bearer ' + token}).status_code == 200
    assert client.post('/api/auth/login', json={'password': 'new'}).status_code == 200


def test_sessions_expire_and_secure_sessions_cannot_downgrade(api):
    client, _, store = api
    login(client)
    with store._connect() as conn:
        conn.execute('UPDATE admin_sessions SET expires_at=0')
    assert client.get('/api/repos').status_code == 401
    login(client)
    with store._connect() as conn:
        conn.execute('UPDATE admin_sessions SET secure=1')
    assert client.get('/api/repos').status_code == 401


def test_missing_password_and_invalid_config_fail_closed(api):
    client, path, _ = api
    raw = yaml.safe_load(path.read_text())
    raw.pop('admin_password_hash')
    path.write_text(yaml.safe_dump(raw))
    assert client.post('/api/auth/login', json={'password': 'secret'}).status_code == 503
    assert client.get('/api/repos').status_code == 401
    assert client.get('/').status_code == 303
    path.write_text('bad: [')
    assert client.get('/status.json').status_code == 503
    assert client.get('/api/repos').status_code == 503


def test_health_minimal_metrics_local_and_proxy_cannot_bypass(api):
    client, path, _ = api
    assert set(client.get('/healthz').json()) == {'healthy'}
    assert client.get('/metrics').status_code == 200
    assert client.get('/metrics', headers={'X-Forwarded-For': '203.0.113.1'}).status_code == 403
    raw = yaml.safe_load(path.read_text())
    raw['status_server'] = {'trusted_proxies': ['127.0.0.1']}
    path.write_text(yaml.safe_dump(raw))
    assert client.get('/metrics').status_code == 403
    assert client.get('/metrics', headers={'X-Forwarded-For': '203.0.113.1'}).status_code == 403
    raw['status_server']['metrics_allowed_networks'] = ['192.0.2.0/24']
    path.write_text(yaml.safe_dump(raw))
    assert client.get('/metrics', headers={'X-Forwarded-For': '192.0.2.9'}).status_code == 200
    assert client.get('/metrics', headers={'X-Forwarded-For': '192.0.2.9, 203.0.113.1'}).status_code == 403


def test_trusted_https_login_sets_secure_cookie_and_checks_origin(api):
    client, path, _ = api
    raw = yaml.safe_load(path.read_text())
    raw['status_server'] = {'trusted_proxies': ['127.0.0.1']}
    path.write_text(yaml.safe_dump(raw))
    headers = {'X-Forwarded-For': '192.0.2.9', 'X-Forwarded-Proto': 'https',
               'X-Forwarded-Host': 'repo.example', 'Origin': 'https://repo.example'}
    response = client.post('/api/auth/login', json={'password': 'secret'}, headers=headers)
    assert response.status_code == 200
    assert '; Secure' in response.headers['set-cookie']
    headers['Origin'] = 'https://evil.example'
    assert client.post('/api/auth/login', json={'password': 'secret'}, headers=headers).status_code == 403


@pytest.mark.parametrize('body', [[], {'password': None}, {'password': 1}, {'password': 'x' * 1025}])
def test_invalid_login_payload(api, body):
    assert api[0].post('/api/auth/login', json=body).status_code in (400, 401)


def test_old_password_header_no_longer_authorizes_http(api):
    client, _, _ = api
    assert client.post('/api/repos', json={}, headers={'X-Repowatch-Password': 'secret'}).status_code == 401
    assert client.post('/api/auth/check', headers={'X-Repowatch-Password': 'secret'}).status_code == 401


@pytest.mark.parametrize('setting', [{'trusted_proxies': ['bad']}, {'trusted_proxies': '127.0.0.1'},
    {'metrics_allowed_networks': [1]}, {'metrics_allowed_networks': None}])
def test_invalid_network_config(setting):
    with pytest.raises(ConfigError):
        StatusServerConfig(**setting)


def test_untrusted_proxy_metadata_ignored_and_chain_walked_right_to_left():
    headers = Message()
    headers['Host'] = 'local'
    headers['X-Forwarded-For'] = '127.0.0.1, 198.51.100.1, 10.1.0.3'
    headers['X-Forwarded-Proto'] = 'https'
    headers['X-Forwarded-Host'] = 'external'
    ctx = client_context('10.0.0.1', headers, False, StatusServerConfig())
    assert ctx.client_ip == '10.0.0.1' and not ctx.secure and ctx.host == 'local'
    ctx = client_context('10.0.0.1', headers, False, StatusServerConfig(trusted_proxies=['10.0.0.0/8']))
    assert ctx.client_ip == '198.51.100.1' and ctx.secure and ctx.host == 'external'


def test_cli_password_preserves_comments_mode_and_does_not_open_database(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'config'
    text = '# settings\nstate_db: /nonexistent/never-created\ncache_base_url: http://localhost\nrepos:\n  - {id: r, type: apk, arch: x86_64, upstream: https://example.org}\n'
    path.write_text(text)
    path.chmod(0o640)
    answers = iter(['new password', 'new password'])
    monkeypatch.setattr('getpass.getpass', lambda _: next(answers))
    assert main(['-c', str(path), 'set-password']) == 0
    first = load_config(path).admin_password_hash
    assert verify_password('new password', first)
    assert path.read_text().startswith(text)
    assert path.stat().st_mode & 0o777 == 0o640
    assert not Path('/nonexistent/never-created').exists()
    assert 'new password' not in capsys.readouterr().out
    before = path.read_text()
    set_password_hash(path, hash_password('next', iterations=1000))
    assert path.read_text().split('admin_password_hash:')[0] == before.split('admin_password_hash:')[0]
    assert verify_password('next', load_config(path).admin_password_hash)


def test_cli_password_mismatch_preserves_file(tmp_path, monkeypatch):
    path = tmp_path / 'config'
    path.write_text('state_db: /unused\ncache_base_url: http://localhost\nrepos:\n  - {id: r, type: apk, arch: x, upstream: https://example.org}\n')
    before = path.read_bytes()
    answers = iter(['first', 'second'])
    monkeypatch.setattr('getpass.getpass', lambda _: next(answers))
    assert main(['-c', str(path), 'set-password']) == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize('field', ['admin_password_hash:\n', 'admin_password_hash: null\n',
    'unused: &old secret\nadmin_password_hash: *old\n',
    'admin_password_hash: &old secret\nunused: *old\n',
    'admin_password_hash: |\n  secret\n'])
def test_cli_updates_yaml_scalar_without_changing_other_fields(tmp_path, field):
    path = tmp_path / 'config'
    path.write_text('state_db: /unused\ncache_base_url: http://localhost\nrepos:\n  - {id: r, type: apk, arch: x, upstream: https://example.org}\n' + field)
    original = yaml.safe_load(path.read_text())
    new_hash = hash_password('new', iterations=1000)
    set_password_hash(path, new_hash)
    expected = {**original, 'admin_password_hash': new_hash}
    assert yaml.safe_load(path.read_text()) == expected


def test_config_edits_wait_for_shared_directory_lock(api):
    _, path, _ = api
    entered = threading.Event()
    completed = threading.Event()
    def edit():
        entered.set()
        set_password_hash(path, hash_password('changed', iterations=1000))
        completed.set()
    with config_lock(path):
        thread = threading.Thread(target=edit)
        thread.start()
        assert entered.wait(1)
        assert not completed.wait(0.05)
    thread.join(timeout=2)
    assert completed.is_set()
    assert not Path(str(path) + '.lock').exists()


def test_explicit_insecure_http_allows_login_but_not_metrics_or_csrf_bypass(api):
    client, path, _ = api
    headers = {'X-Forwarded-For': '192.0.2.8', 'X-Forwarded-Proto': 'http'}
    raw = yaml.safe_load(path.read_text())
    raw['status_server'] = {'trusted_proxies': ['127.0.0.1']}
    path.write_text(yaml.safe_dump(raw))
    assert client.post('/api/auth/login', json={'password': 'secret'}, headers=headers).status_code == 403
    raw['status_server']['allow_insecure_http'] = True
    path.write_text(yaml.safe_dump(raw))
    response = client.post('/api/auth/login', json={'password': 'secret'}, headers=headers)
    assert response.status_code == 200
    assert '; Secure' not in response.headers['set-cookie']
    assert client.get('/api/repos', headers=headers).status_code == 200
    assert client.get('/metrics', headers=headers).status_code == 403
    assert client.post('/api/tokens', json={'name': 'host'}, headers=headers).status_code == 403
    response = client.post('/api/tokens', json={'name': 'host'}, headers={**headers, 'X-CSRF-Token': response.json()['csrf_token']})
    assert response.status_code == 201
    token = response.json()['token']
    client.cookies.clear()
    assert client.get('/status.json', headers={**headers, 'Authorization': 'Bearer ' + token}).status_code == 200
    assert client.get('/api/repos', headers=headers).status_code == 401


@pytest.mark.parametrize('setting', ['true', 1, None, []])
def test_guest_setting_requires_boolean(setting):
    with pytest.raises(ConfigError):
        StatusServerConfig(guest_read_only=setting)


def test_guest_reads_and_live_disable_without_password(api):
    client, path, store = api
    from repowatch.state import RepoSnapshot
    store.record_snapshot(RepoSnapshot("r", {"pkg-1": "pkg-1.apk"}))
    raw = yaml.safe_load(path.read_text())
    raw.pop('admin_password_hash')
    raw['status_server'] = {'guest_read_only': True}
    path.write_text(yaml.safe_dump(raw))
    # Guest reads also work over external HTTP without allowing credentials.
    headers = {'X-Forwarded-For': '192.0.2.5'}
    for route in ['/', '/dashboard', '/status.json', '/status/r.json', '/api/repos',
                  '/status/r/history', '/api/repos/r/packages', '/api/repos/r/warmed',
                  '/api/repos/r/bans', '/api/requests', '/api/requests/summary', '/api/config']:
        response = client.get(route, headers=headers)
        assert response.status_code == 200, (route, response.text)
        assert response.headers['cache-control'] == 'no-store'
    assert client.get('/api/auth/session', headers=headers).json() == {'role': 'guest'}
    assert client.get('/metrics', headers=headers).status_code == 403
    assert client.get('/metrics').status_code == 200
    for route in ['/api/tokens']:
        assert client.get(route).status_code == 401
    with store._connect() as conn:
        assert conn.execute('SELECT count(*) FROM admin_sessions').fetchone()[0] == 0
    raw['status_server']['guest_read_only'] = False
    path.write_text(yaml.safe_dump(raw))
    assert client.get('/').status_code == 303
    assert client.get('/api/repos').status_code == 401
    assert client.get('/status.json').status_code == 401
    path.write_text('bad: [')
    assert client.get('/api/repos').status_code == 503


@pytest.mark.parametrize('route', ['/api/auth/logout', '/api/config', '/api/tokens',
    '/api/tokens/test/revoke', '/api/repos', '/api/repos/r', '/api/repos/r/delete',
    '/api/repos/r/warm', '/api/repos/r/warmed/remove', '/api/repos/r/bans', '/api/repos/r/bans/remove'])
def test_guest_cannot_mutate_even_with_forged_csrf(api, route):
    client, path, _ = api
    raw = yaml.safe_load(path.read_text())
    raw['status_server'] = {'guest_read_only': True}
    path.write_text(yaml.safe_dump(raw))
    before = path.read_bytes()
    assert client.post(route, json={}, headers={'X-CSRF-Token': 'forged'}).status_code == 401
    assert path.read_bytes() == before


def test_guest_mode_preserves_admin_login_and_csrf(api):
    client, path, _ = api
    raw = yaml.safe_load(path.read_text())
    raw['status_server'] = {'guest_read_only': True}
    path.write_text(yaml.safe_dump(raw))
    csrf = login(client)
    assert client.get('/api/auth/session').json()['role'] == 'admin'
    assert client.get('/api/tokens').status_code == 200
    assert client.get('/api/config').status_code == 200
    assert client.post('/api/tokens', json={'name': 'host'}).status_code == 403
    assert client.post('/api/tokens', json={'name': 'host'}, headers={'X-CSRF-Token': csrf}).status_code == 201
    assert client.post('/api/auth/logout', headers={'X-CSRF-Token': csrf}).status_code == 200
    assert client.get('/api/auth/session').json() == {'role': 'guest'}


def test_guest_safe_config_excludes_secrets_and_security_settings(api):
    client, path, _ = api
    raw = yaml.safe_load(path.read_text())
    raw['status_server'] = {'guest_read_only': True}
    raw['notify_webhook_url'] = 'https://example.test/private-webhook-secret'
    path.write_text(yaml.safe_dump(raw))
    before = path.read_bytes()
    response = client.get('/api/config')
    assert response.status_code == 200
    from repowatch.api import SAFE_CONFIG_FIELDS
    assert set(response.json()) == set(SAFE_CONFIG_FIELDS)
    assert response.json()['cache_base_url'] == raw['cache_base_url']
    for secret in (raw['admin_password_hash'], raw['notify_webhook_url'], 'trusted_proxies', 'guest_read_only'):
        assert secret not in response.text
    assert client.post('/api/config', json={'check_interval': 1}).status_code == 401
    assert path.read_bytes() == before
    raw['status_server']['guest_read_only'] = False
    path.write_text(yaml.safe_dump(raw))
    assert client.get('/api/config').status_code == 401


def test_scoped_tokens_filter_and_live_policy(api):
    client, path, store = api
    raw = yaml.safe_load(path.read_text())
    raw['repos'].append({**raw['repos'][0], 'id': 'private'})
    raw['status_server'] = {'token_repo_restrictions': True}
    path.write_text(yaml.safe_dump(raw))
    csrf = login(client)
    for invalid in (['missing'], 'r', [12]):
        assert client.post('/api/tokens', json={'name': 'bad', 'repo_ids': invalid}, headers={'X-CSRF-Token': csrf}).status_code == 400
    response = client.post('/api/tokens', json={'name': 'limited', 'repo_ids': ['r']}, headers={'X-CSRF-Token': csrf})
    assert response.status_code == 201
    assert client.get('/api/tokens').json()[0]['repo_ids'] == ['r']
    client.cookies.clear()
    headers = {'Authorization': 'Bearer ' + response.json()['token']}
    assert set(client.get('/status.json', headers=headers).json()) == {'r'}
    assert client.get('/status/r.json', headers=headers).status_code == 200
    assert client.get('/status/private.json', headers=headers).status_code == 403
    assert client.get('/status/missing.json', headers=headers).status_code == 403
    assert client.get('/status/private/history', headers=headers).status_code == 401
    empty = AccessStore(store).create_token('none', repo_ids=[])['token']
    assert client.get('/status.json', headers={'Authorization': 'Bearer ' + empty}).json() == {}
    all_token = AccessStore(store).create_token('all')['token']
    assert set(client.get('/status.json', headers={'Authorization': 'Bearer ' + all_token}).json()) == {'r', 'private'}
    raw['status_server']['token_repo_restrictions'] = False
    path.write_text(yaml.safe_dump(raw))
    assert set(client.get('/status.json', headers=headers).json()) == {'r', 'private'}
    raw['status_server'] = {'token_repo_restrictions': True, 'guest_read_only': True}
    path.write_text(yaml.safe_dump(raw))
    # Guest mode explicitly publishes status independently of token scopes.
    assert set(client.get('/status.json').json()) == {'r', 'private'}


def test_scoped_issuance_requires_policy(api):
    client, _, _ = api
    csrf = login(client)
    assert client.post('/api/tokens', json={'name': 'limited', 'repo_ids': ['r']}, headers={'X-CSRF-Token': csrf}).status_code == 400


def test_legacy_tokens_migrate_to_all(tmp_path):
    import sqlite3
    from repowatch.access import digest
    db = tmp_path / 'old.sqlite'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE host_tokens(id TEXT PRIMARY KEY, name TEXT, secret_hash TEXT UNIQUE, created_at REAL, expires_at REAL, last_used_at REAL, revoked_at REAL)')
        conn.execute('INSERT INTO host_tokens VALUES (?, ?, ?, ?, NULL, NULL, NULL)', ('id', 'legacy', digest('rw_legacy'), time.time()))
    access = AccessStore(StateStore(db))
    assert access.token_access('rw_legacy') == {'repo_ids': None}
    assert access.tokens()[0]['repo_ids'] is None
