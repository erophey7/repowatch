import asyncio
from unittest.mock import AsyncMock, patch

from repowatch.config.models import Config
from repowatch.config.models import StatusServerConfig
from repowatch.notifications import (
    record_failure_and_maybe_notify,
    record_success_and_maybe_notify,
)
from repowatch.runtime.context import ServiceState


def _config(webhook_url: str | None = "https://hooks.example.org/x", after_failures: int = 3) -> Config:
    return Config(
        state_db="/tmp/unused.sqlite3",
        check_interval=300,
        cache_base_url="http://127.0.0.1:8080",
        status_server=StatusServerConfig(),
        notify_webhook_url=webhook_url,
        notify_after_failures=after_failures,
    )


def _store(tmp_path) -> ServiceState:
    return ServiceState(tmp_path / "state.sqlite3")


def test_no_notification_below_threshold(tmp_path):
    store = _store(tmp_path)
    config = _config(after_failures=3)

    with patch("repowatch.notifications._send", new_callable=AsyncMock) as mock_send:
        asyncio.run(record_failure_and_maybe_notify(config, store, "r", "gpg", "bad sig"))
        asyncio.run(record_failure_and_maybe_notify(config, store, "r", "gpg", "bad sig"))

    mock_send.assert_not_called()


def test_notifies_exactly_once_when_threshold_reached(tmp_path):
    store = _store(tmp_path)
    config = _config(after_failures=2)

    with patch("repowatch.notifications._send", new_callable=AsyncMock, return_value=True) as mock_send:
        asyncio.run(record_failure_and_maybe_notify(config, store, "r", "gpg", "bad sig"))
        asyncio.run(record_failure_and_maybe_notify(config, store, "r", "gpg", "bad sig"))
        # a third and later failure in the same streak must not resend
        asyncio.run(record_failure_and_maybe_notify(config, store, "r", "gpg", "bad sig"))

    assert mock_send.call_count == 1
    url, payload = mock_send.call_args.args
    assert url == "https://hooks.example.org/x"
    assert payload["repo_id"] == "r"
    assert payload["kind"] == "gpg"
    assert payload["status"] == "failing"
    assert payload["consecutive_failures"] == 2
    assert "text" in payload


def test_no_notification_without_webhook_configured(tmp_path):
    store = _store(tmp_path)
    config = _config(webhook_url=None, after_failures=1)

    with patch("repowatch.notifications._send", new_callable=AsyncMock) as mock_send:
        asyncio.run(record_failure_and_maybe_notify(config, store, "r", "prefetch", "boom"))

    mock_send.assert_not_called()
    # bookkeeping must still happen even without a webhook
    assert store.notifications.bump_failure("r", "prefetch", "boom") == (2, False)


def test_failed_send_does_not_mark_as_notified_so_it_retries_next_time(tmp_path):
    store = _store(tmp_path)
    config = _config(after_failures=1)

    with patch("repowatch.notifications._send", new_callable=AsyncMock, return_value=False) as mock_send:
        asyncio.run(record_failure_and_maybe_notify(config, store, "r", "gpg", "x"))
        asyncio.run(record_failure_and_maybe_notify(config, store, "r", "gpg", "x"))

    assert mock_send.call_count == 2


def test_recovery_notification_sent_only_after_a_notified_failure(tmp_path):
    store = _store(tmp_path)
    config = _config(after_failures=1)

    with patch("repowatch.notifications._send", new_callable=AsyncMock, return_value=True) as mock_send:
        asyncio.run(record_failure_and_maybe_notify(config, store, "r", "prefetch", "boom"))
        mock_send.reset_mock()

        asyncio.run(record_success_and_maybe_notify(config, store, "r", "prefetch"))

    assert mock_send.call_count == 1
    _url, payload = mock_send.call_args.args
    assert payload["status"] == "recovered"
    assert payload["repo_id"] == "r"
    assert payload["kind"] == "prefetch"


def test_no_recovery_notification_when_never_notified_of_failure(tmp_path):
    store = _store(tmp_path)
    config = _config(after_failures=3)

    with patch("repowatch.notifications._send", new_callable=AsyncMock) as mock_send:
        # one failure — below the threshold, no failure notification was sent
        asyncio.run(record_failure_and_maybe_notify(config, store, "r", "prefetch", "boom"))
        asyncio.run(record_success_and_maybe_notify(config, store, "r", "prefetch"))

    mock_send.assert_not_called()
