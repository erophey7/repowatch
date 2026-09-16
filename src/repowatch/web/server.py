"""HTTP server construction, optional TLS, and serving lifecycle."""

from __future__ import annotations

import logging
import ssl
from http.server import ThreadingHTTPServer
from pathlib import Path
from repowatch.config.models import Config
from repowatch.runtime.context import ServiceState
from repowatch.web.handler import make_handler

logger = logging.getLogger(__name__)


class StatusHTTPServer(ThreadingHTTPServer):
    # HTTP/1.0 opens one connection per request. The stdlib default backlog
    # of 5 can cause TCP retransmits under load; raising it in a local test
    # with 32 clients cut page p95 from ~1.65s to ~0.78s. We don't add
    # per-IP or GET rate limits; actual request handling still runs in
    # stdlib threads.
    request_queue_size = 128


def create_server(config: Config, store: ServiceState, config_path: str | Path) -> StatusHTTPServer:
    """Bind HTTP and configure TLS synchronously; the caller owns the server."""
    store.bandwidth.bind(config_path)
    handler_cls = make_handler(config, store, config_path)
    addr = (config.status_server.bind, config.status_server.port)
    context = None
    if config.status_server.tls_cert_path is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            config.status_server.tls_cert_path, config.status_server.tls_key_path,
        )
    httpd = StatusHTTPServer(addr, handler_cls)
    try:
        if context is not None:
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        logger.info("status API listening on %s://%s:%d", "https" if context else "http", *addr)
        return httpd
    except BaseException:
        httpd.server_close()
        raise


def serve(config: Config, store: ServiceState, config_path: str | Path) -> None:
    """Run the standalone status server with deterministic socket cleanup."""
    with create_server(config, store, config_path) as httpd:
        httpd.serve_forever()
