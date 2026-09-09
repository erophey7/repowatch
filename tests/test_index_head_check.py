import asyncio

import httpx

from repowatch.config import RepoConfig
from repowatch.parsers.apk import ApkParser


def _repo() -> RepoConfig:
    return RepoConfig(
        id="alpine-test",
        type="apk",
        upstream="https://example.org/alpine/v3.20/main",
        arch="x86_64",
    )


class _FakeClient:
    """A minimal stand-in for httpx.AsyncClient — check_index_changed only
    calls client.head(...), no real client/socket is needed."""

    def __init__(self, response: httpx.Response):
        self._response = response

    async def head(self, url, headers=None, timeout=None):
        return self._response


def _response(status_code: int, headers: dict | None = None) -> httpx.Response:
    request = httpx.Request("HEAD", "https://example.org/index")
    return httpx.Response(status_code, headers=headers or {}, request=request)


def test_unchanged_on_304():
    parser = ApkParser(_repo())
    client = _FakeClient(_response(304))
    result = asyncio.run(
        parser.check_index_changed(client, prev_etag='"abc"', prev_last_modified=None)
    )

    assert result.unchanged is True
    assert result.etag == '"abc"'


def test_changed_when_etag_differs():
    parser = ApkParser(_repo())
    client = _FakeClient(_response(200, {"ETag": '"new"'}))
    result = asyncio.run(
        parser.check_index_changed(client, prev_etag='"old"', prev_last_modified=None)
    )

    assert result.unchanged is False
    assert result.etag == '"new"'


def test_unchanged_when_server_ignores_conditional_headers_but_etag_matches():
    """The server returned 200 (not 304), but the ETag in the response
    matches the previous one — we consider the index unchanged."""
    parser = ApkParser(_repo())
    client = _FakeClient(_response(200, {"ETag": '"same"'}))
    result = asyncio.run(
        parser.check_index_changed(client, prev_etag='"same"', prev_last_modified=None)
    )

    assert result.unchanged is True


def test_first_check_always_reports_changed():
    """No saved prev_etag/prev_last_modified — always download the index."""
    parser = ApkParser(_repo())
    client = _FakeClient(_response(200, {"ETag": '"anything"'}))
    result = asyncio.run(
        parser.check_index_changed(client, prev_etag=None, prev_last_modified=None)
    )

    assert result.unchanged is False


def test_non_304_http_error_propagates():
    """Any other HTTP error (not 304) must propagate — the watcher itself
    decides whether to fall back to a full download.

    Unlike urllib, httpx doesn't raise on a non-2xx status by itself — we
    have an explicit raise_for_status() for that (see
    IndexParser.check_index_changed)."""
    parser = ApkParser(_repo())
    client = _FakeClient(_response(500))

    try:
        asyncio.run(
            parser.check_index_changed(client, prev_etag='"abc"', prev_last_modified=None)
        )
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 500
    else:
        raise AssertionError("expected an HTTPStatusError exception")
