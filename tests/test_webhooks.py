import repowatch.operations.check as operations_check
import repowatch.operations.warm as operations_warm
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from repowatch.config.models import Config
from repowatch.errors import ConfigError
from repowatch.config.models import RepoConfig
from repowatch.config.models import StatusServerConfig
from repowatch.config.load import load_config
from repowatch import notifications
import repowatch.operations.warm as prefetch
import repowatch.operations.check as watcher
from repowatch.parsers.base import IndexHeadResult
from repowatch.models import RepoSnapshot
from repowatch.runtime.context import ServiceState


def config(**kwargs):
    return Config(state_db='/tmp/unused.db', check_interval=300,
                  cache_base_url='http://127.0.0.1:8080', status_server=StatusServerConfig(),
                  notify_webhook_url='https://example.org/secret', **kwargs)


@pytest.mark.parametrize('events', ['warm.started', None, ['unknown'], [1],
                                     ['warm.started', 'warm.started'], [{}]])
def test_invalid_event_selection(events):
    with pytest.raises(ConfigError):
        config(notify_events=events)


def test_yaml_event_selection(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('state_db: /tmp/unused.db\ncache_base_url: http://127.0.0.1:8080\nrepos: [{id: r, type: apk, upstream: https://example.org, arch: x86_64}]\nnotify_events: [repository.changed, warm.completed]\n')
    assert load_config(path).notify_events == ['repository.changed', 'warm.completed']
    path.write_text('state_db: /tmp/unused.db\ncache_base_url: http://127.0.0.1:8080\nrepos: [{id: r, type: apk, upstream: https://example.org, arch: x86_64}]\nnotify_events: invalid\n')
    with pytest.raises(ConfigError):
        load_config(path)


def delivery(monkeypatch, replies):
    requests = []
    def handler(request):
        requests.append(request)
        reply = replies[len(requests) - 1]
        if isinstance(reply, BaseException):
            raise reply
        code, headers = reply if isinstance(reply, tuple) else (reply, {})
        return httpx.Response(code, headers=headers)
    client = httpx.AsyncClient
    monkeypatch.setattr(notifications.httpx, 'AsyncClient',
                        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    sleep = AsyncMock()
    monkeypatch.setattr(notifications.asyncio, 'sleep', sleep)
    return requests, sleep


@pytest.mark.parametrize('first', [500, 503, 429, httpx.ReadTimeout('secret'), httpx.DecodingError('secret')])
def test_retry_reuses_payload_and_idempotency_key(monkeypatch, first):
    requests, sleep = delivery(monkeypatch, [first, 204])
    assert asyncio.run(notifications.emit(config(), 'repository.failing', 'r', {'status': 'failing'}))
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert requests[0].headers['Idempotency-Key'] == requests[1].headers['Idempotency-Key']
    sleep.assert_awaited_once_with(1.0)


@pytest.mark.parametrize('code', [301, 302, 400, 401, 403, 404])
def test_terminal_responses_do_not_retry_or_follow_redirect(monkeypatch, code, caplog):
    requests, sleep = delivery(monkeypatch, [(code, {'Location': 'https://other.example/token'})])
    assert not asyncio.run(notifications.emit(config(), 'repository.failing', 'r', {}))
    assert len(requests) == 1
    sleep.assert_not_awaited()
    assert 'secret' not in caplog.text and 'other.example' not in caplog.text


def test_exhausted_delivery_is_bounded_and_redacts_errors(monkeypatch, caplog):
    requests, sleep = delivery(monkeypatch, [httpx.ConnectError('secret')] * 3)
    assert not asyncio.run(notifications.emit(config(), 'repository.failing', 'r', {}))
    assert len(requests) == 3
    assert [call.args[0] for call in sleep.await_args_list] == [1, 2]
    assert 'secret' not in caplog.text


@pytest.mark.parametrize('delay,attempts', [('5', 2), ('31', 1), ('invalid', 2)])
def test_retry_after(monkeypatch, delay, attempts):
    requests, sleep = delivery(monkeypatch, [(429, {'Retry-After': delay}), 200])
    result = asyncio.run(notifications.emit(config(), 'repository.failing', 'r', {}))
    assert len(requests) == attempts
    assert result == (attempts == 2)
    if delay == '5':
        sleep.assert_awaited_once_with(5)


def test_retry_after_http_date():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
    assert 18 < notifications._retry_delay(future, 0) <= 20


def test_cancellation_is_not_swallowed(monkeypatch):
    delivery(monkeypatch, [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(notifications.emit(config(), 'repository.failing', 'r', {}))


def test_new_events_opt_in_and_unique_envelopes(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notifications, '_send', send)
    asyncio.run(notifications.emit(config(), 'warm.started', 'r', {}))
    send.assert_not_called()
    c = config(notify_events=['warm.started'])
    for _ in range(2):
        asyncio.run(notifications.emit(c, 'warm.started', 'r', {}))
    a, b = [call.args[1] for call in send.call_args_list]
    assert a['event_id'] != b['event_id']
    assert a['schema_version'] == 1 and a['occurred_at']
    assert a['text'] and a['repo_id'] == 'r'


@pytest.mark.parametrize('nix', [False, True])
def test_warm_lifecycle_counts_filters_and_nix_roots(tmp_path, monkeypatch, nix):
    repo = RepoConfig('r', 'nix' if nix else 'apk', 'https://example.org', 'x86_64-linux' if nix else 'x86_64',
                      nix_source='https://example.org/source.tar.xz' if nix else None,
                      prefetch_blacklist=['blocked'])
    store = ServiceState(tmp_path / 'state.db')
    packages = {'a': 'a', 'b': 'b', 'c': 'c'}
    store.repositories.record_snapshot(RepoSnapshot('r', packages, {'a': 'allowed', 'b': 'other', 'c': 'blocked'}))
    if nix:
        monkeypatch.setattr('repowatch.cache.nix.warm', AsyncMock(return_value={'a': True, 'b': False}))
    else:
        monkeypatch.setattr(operations_warm, 'download_package', AsyncMock(side_effect=[(True, 200), (False, 503)]))
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notifications, '_send', send)
    c = config(repos=[repo], notify_events=['warm.started', 'warm.completed'])
    outcomes = asyncio.run(operations_warm.warm_cache(c, repo, store, packages, force=True))
    assert outcomes == {'a': True, 'b': False}
    start, end = [call.args[1] for call in send.call_args_list]
    assert start['event'] == 'warm.started' and end['event'] == 'warm.completed'
    assert start['operation_id'] == end['operation_id']
    assert start['event_id'] != end['event_id']
    assert end['succeeded'] == end['failed'] == end['skipped'] == 1
    assert end['status'] == 'partial' and end['manual']


def test_no_lifecycle_for_disabled_or_fully_excluded_warm(tmp_path, monkeypatch):
    repo = RepoConfig('r', 'apk', 'https://example.org', 'x86_64', prefetch=False,
                      prefetch_blacklist=['blocked'])
    store = ServiceState(tmp_path / 'state.db')
    store.repositories.record_snapshot(RepoSnapshot('r', {'a': 'a'}, {'a': 'blocked'}))
    send = AsyncMock()
    monkeypatch.setattr(notifications, '_send', send)
    c = config(notify_events=['warm.started', 'warm.completed'])
    for force in (True, False):
        assert asyncio.run(operations_warm.warm_cache(c, repo, store, {'a': 'a'}, force)) == {}
    send.assert_not_called()


def test_repository_changed_only_after_new_snapshot(tmp_path, monkeypatch):
    repo = RepoConfig('r', 'apk', 'https://example.org', 'x86_64', prefetch=False)
    store = ServiceState(tmp_path / 'state.db')
    parser = operations_check.PARSERS['apk']
    monkeypatch.setattr(parser, 'check_index_changed', AsyncMock(return_value=IndexHeadResult(False, None, None)))
    monkeypatch.setattr(parser, 'fetch', AsyncMock(return_value=RepoSnapshot('r', {'a': 'a'})))
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notifications, '_send', send)
    c = config(repos=[repo], notify_events=['repository.changed'])
    for _ in range(2):
        asyncio.run(operations_check.check_repo(c, repo, store))
    assert send.await_count == 1
    event = send.call_args.args[1]
    assert event['added'] == 1 and event['removed'] == event['modified'] == 0


def test_warm_error_is_reported_and_reraised(tmp_path, monkeypatch):
    repo = RepoConfig('r', 'apk', 'https://example.org', 'x86_64')
    store = ServiceState(tmp_path / 'state.db')
    monkeypatch.setattr(operations_warm, '_warm_cache', AsyncMock(side_effect=RuntimeError('broken')))
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notifications, '_send', send)
    with pytest.raises(RuntimeError, match='broken'):
        asyncio.run(operations_warm.warm_cache(config(notify_events=['warm.completed']),
                                       repo, store, {'a': 'a'}))
    assert send.await_count == 1
    assert send.call_args.args[1]['status'] == 'error'
    assert 'broken' not in str(send.call_args.args[1])


def test_disabling_events_preserves_failure_bookkeeping(tmp_path, monkeypatch):
    store = ServiceState(tmp_path / 'state.db')
    send = AsyncMock()
    monkeypatch.setattr(notifications, '_send', send)
    c = config(notify_events=[], notify_after_failures=1)
    asyncio.run(notifications.record_failure_and_maybe_notify(c, store, 'r', 'gpg', 'bad'))
    assert store.notifications.bump_failure('r', 'gpg', 'bad') == (2, False)
    asyncio.run(notifications.record_success_and_maybe_notify(c, store, 'r', 'gpg'))
    assert store.notifications.bump_failure('r', 'gpg', 'bad') == (1, False)
    send.assert_not_called()


def test_delivery_failure_does_not_break_warm(tmp_path, monkeypatch):
    repo = RepoConfig('r', 'apk', 'https://example.org', 'x86_64')
    store = ServiceState(tmp_path / 'state.db')
    monkeypatch.setattr(operations_warm, 'download_package', AsyncMock(return_value=(True, 200)))
    requests, sleep = delivery(monkeypatch, [503] * 6)
    c = config(notify_events=['warm.started', 'warm.completed'])
    assert asyncio.run(operations_warm.warm_cache(c, repo, store, {'a': 'a'})) == {'a': True}
    assert len(requests) == 6
    assert store.cache.get_warmed_packages('r')[0]['status'] == 'ok'


def test_real_http_receiver_gets_identical_retries(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import json
    import threading
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            received.append((body, self.headers['Idempotency-Key']))
            self.send_response(503 if len(received) == 1 else 204)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(notifications.asyncio, 'sleep', AsyncMock())
    from dataclasses import replace
    c = replace(config(), notify_webhook_url=f'http://127.0.0.1:{server.server_port}/hook')
    try:
        assert asyncio.run(notifications.emit(c, 'repository.failing', 'r', {}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert len(received) == 2 and received[0] == received[1]
    assert json.loads(received[0][0])['event_id'] == received[0][1]
