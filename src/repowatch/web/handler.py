"""HTTP routing, authentication gates, and response serialization."""

from __future__ import annotations

import hmac
import json
import logging
import ssl
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from repowatch.auth import verify_password
from repowatch.config.load import load_config
from repowatch.config.models import Config
from repowatch.errors import ConfigError
from repowatch.reporting.metrics import metrics_payload
from repowatch.reporting.statistics import paged_payload, requests_summary_payload, prefetch_efficiency_payload, stats_payload
from repowatch.reporting.status import repos_list_payload, status_payload, healthz_payload
from repowatch.runtime.context import ServiceState
from repowatch.storage.access import AccessStore, SESSION_SECONDS
from repowatch.web.access import COOKIE_NAME, client_context, in_networks
from repowatch.web.packages import warm_packages_payload, purge_candidates_payload, purge_selected_payload, remove_warmed_package_payload, banned_packages_payload, ban_package_payload, unban_package_payload
from repowatch.web.repositories import add_repo_payload, update_repo_payload, delete_repo_payload
from repowatch.web.settings import safe_config_payload, update_safe_config_payload
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)


STATIC_DIR = Path(__file__).parents[1] / "static"
STATIC_ASSETS = {
    '/static/' + path.relative_to(STATIC_DIR).as_posix(): (path, content_type)
    for directory, suffix, content_type in (
        ('js', '*.js', 'text/javascript; charset=utf-8'),
        ('css', '*.css', 'text/css; charset=utf-8'),
    )
    for path in (STATIC_DIR / directory).glob(suffix)
}


# The versioned prefix covers only the client-facing status API — the one
# external host agents poll (`status.json`, `/healthz`, `/metrics`) — not the
# admin/dashboard API, which stays tied to the dashboard's own version and is
# never promised stable to outside consumers. `/api/v1/...` is currently a
# pure alias for the same unversioned routes below; a future breaking change
# to this specific contract would land in `/api/v2/` instead, leaving `v1`
# (and the unversioned routes, kept as a permanent alias of it) working.
_V1_PREFIX = "/api/v1"


def _normalize_v1_path(path: str) -> str:
    if not path.startswith(_V1_PREFIX + "/"):
        return path
    rest = path[len(_V1_PREFIX):]
    if rest in ("/healthz", "/metrics", "/status.json") or rest.startswith("/status/"):
        return rest
    return path


def make_handler(
    config: Config, store: ServiceState, config_path: str | Path
) -> type[BaseHTTPRequestHandler]:
    access = AccessStore(store.database)

    class Handler(BaseHTTPRequestHandler):
        server_version = "repowatch/0.1"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def end_headers(self) -> None:
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            for name, value in getattr(self, '_extra_headers', []):
                self.send_header(name, value)
            super().end_headers()

        def _context(self) -> bool:
            try:
                self.current = load_config(config_path)
                for name in ('Host', 'X-Forwarded-For', 'X-Forwarded-Proto', 'X-Forwarded-Host', 'Authorization', 'Cookie', 'Origin', 'X-CSRF-Token'):
                    if len(self.headers.get_all(name, [])) > 1:
                        raise ValueError('duplicate security header')
                self.client = client_context(self.client_address[0], self.headers,
                                             isinstance(self.connection, ssl.SSLSocket), self.current.status_server)
                cookies = SimpleCookie()
                cookies.load(self.headers.get('Cookie', ''))
                self.session_secret = cookies[COOKIE_NAME].value if COOKIE_NAME in cookies else ''
                self.admin = access.session(self.session_secret, self.current.admin_password_hash, self.client.secure)
                return True
            except (ConfigError, OSError):
                self._json({'error': 'configuration unavailable'}, status=503)
            except (ValueError, CookieError):
                self._json({'error': 'invalid request headers'}, status=400)
            return False

        def _admin_required(self, csrf: bool = False) -> bool:
            if not (self.client.credentials_allowed or self.current.status_server.allow_insecure_http):
                self._json({'error': 'HTTPS required'}, status=403)
                return False
            if not self.admin:
                self._json({'error': 'administrator login required'}, status=401)
                return False
            if csrf and not hmac.compare_digest(self.headers.get('X-CSRF-Token', '').encode(), self.admin.csrf.encode()):
                self._json({'error': 'CSRF token required'}, status=403)
                return False
            return True

        def _same_origin(self) -> bool:
            origin = self.headers.get('Origin')
            expected = ('https' if self.client.secure else 'http') + '://' + self.client.host
            return (not origin or origin.lower() == expected) and self.headers.get('Sec-Fetch-Site') != 'cross-site'

        def _json(self, payload: dict | list, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, body: bytes, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text(self, body: str, status: int = 200) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            parsed = parsed._replace(path=_normalize_v1_path(parsed.path))
            parts = [p for p in parsed.path.split("/") if p]

            if parsed.path == '/healthz':
                status, _ = healthz_payload(config_path, store)
                self._json({'healthy': status == 200}, status=status)
                return
            if parsed.path == '/login':
                self._html((STATIC_DIR / 'login.html').read_bytes())
                return
            if parsed.path.startswith('/static/'):
                asset = STATIC_ASSETS.get(parsed.path)
                if asset is None:
                    self._json({'error': 'not found'}, status=404)
                    return
                path, content_type = asset
                body = path.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not self._context():
                return
            if parsed.path == '/metrics':
                if not self.client.client_ip or not in_networks(self.client.client_ip, self.current.status_server.metrics_allowed_networks):
                    self._json({'error': 'metrics access denied'}, status=403)
                    return
                status, body = metrics_payload(config_path, store, current=self.current)
                self._text(body, status=status)
                return
            is_status = parsed.path == '/status.json' or (len(parts) == 2 and parts[0] == 'status' and parts[1].endswith('.json'))
            # Explicit read allowlist: future administrative GET routes stay closed.
            guest_route = (
                parsed.path in ('/', '/dashboard', '/api/auth/session', '/api/repos',
                                '/api/requests', '/api/requests/summary', '/api/config',
                                '/api/prefetch-efficiency')
                or is_status
                or (len(parts) == 3 and parts[0] == 'status' and parts[2] == 'history')
                or (len(parts) == 4 and parts[:2] == ['api', 'repos']
                    and parts[3] in ('packages', 'warmed', 'bans'))
            )
            guest = self.current.status_server.guest_read_only and not self.admin and guest_route
            allowed_repos = None
            if guest:
                pass
            elif is_status and not self.admin:
                authorization = self.headers.get('Authorization', '')
                grant = access.token_access(authorization[7:]) if authorization.startswith('Bearer ') else None
                if not (self.client.credentials_allowed or self.current.status_server.allow_insecure_http) or not authorization.startswith('Bearer ') or grant is None:
                    self._extra_headers = [('WWW-Authenticate', 'Bearer')]
                    self._json({'error': 'valid host token required'}, status=401)
                    return
                if self.current.status_server.token_repo_restrictions:
                    allowed_repos = grant['repo_ids']
                    if allowed_repos is not None and parsed.path != '/status.json' and parts[1][:-5] not in allowed_repos:
                        self._json({'error': 'repository access denied'}, status=403)
                        return
            else:
                if parsed.path in ('/', '/dashboard') and not self.admin:
                    self._extra_headers = [('Location', '/login')]
                    self._html(b'', status=303)
                    return
                if not self._admin_required():
                    return
            if parsed.path in ('/', '/dashboard'):
                self._html((STATIC_DIR / 'dashboard.html').read_bytes())
                return
            if parsed.path == '/api/auth/session':
                if guest:
                    self._json({'role': 'guest'})
                else:
                    self._json({'role': 'admin', 'csrf_token': self.admin.csrf, 'expires_at': self.admin.expires_at})
                return
            if parsed.path == '/api/tokens':
                self._json(access.tokens())
                return

            if parsed.path == "/status.json":
                status, payload = status_payload(config_path, store, current=self.current)
                if status == 200 and allowed_repos is not None:
                    payload = {k: v for k, v in payload.items() if k in allowed_repos}
                self._json(payload, status=status)
                return

            if len(parts) == 2 and parts[0] == "status" and parts[1].endswith(".json"):
                repo_id = parts[1][: -len(".json")]
                status, payload = status_payload(config_path, store, repo_id, current=self.current)
                self._json(payload, status=status)
                return

            if len(parts) == 3 and parts[0] == "status" and parts[2] == "history":
                repo_id = parts[1]
                qs = parse_qs(parsed.query)
                try:
                    limit = int(qs.get("limit", ["20"])[0])
                    if not 1 <= limit <= 200:
                        raise ValueError()
                except ValueError:
                    self._json({'error': 'limit must be 1..200'}, status=400)
                    return
                self._json(store.repositories.get_history(repo_id, limit=limit))
                return

            if parsed.path == "/api/repos":
                status, payload = repos_list_payload(config_path, store, current=self.current)
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "packages":
                status, payload = paged_payload(store, "packages", parts[2], parsed.query)
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "warmed":
                status, payload = paged_payload(store, "warmed", parts[2], parsed.query)
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "bans":
                status, payload = banned_packages_payload(config_path, store, parts[2], current=self.current)
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "purge-candidates":
                status, payload = purge_candidates_payload(config_path, store, parts[2], current=self.current)
                self._json(payload, status=status)
                return

            if parsed.path == "/api/config":
                status, payload = safe_config_payload(config_path, current=self.current)
                self._json(payload, status=status)
                return

            if parsed.path == "/api/stats":
                qs = parse_qs(parsed.query)
                status, payload = stats_payload(
                    config_path, store, include_cache_dir=qs.get("cache_dir", ["0"])[0] == "1", current=self.current)
                self._json(payload, status=status)
                return

            if parsed.path == "/api/requests/summary":
                qs = parse_qs(parsed.query)
                repo_id = qs.get("repo_id", [None])[0]
                try:
                    timeline_hours = int(qs.get("timeline_hours", ["24"])[0])
                    if not 1 <= timeline_hours <= 24 * 30:
                        raise ValueError()
                except ValueError:
                    self._json({'error': 'timeline_hours must be 1..720'}, status=400)
                    return
                status, payload = requests_summary_payload(store, repo_id, timeline_hours)
                self._json(payload, status=status)
                return

            if parsed.path == "/api/requests":
                qs = parse_qs(parsed.query)
                repo_id = qs.get("repo_id", [None])[0]
                status, payload = paged_payload(store, "requests", repo_id, parsed.query)
                self._json(payload, status=status)
                return

            if parsed.path == "/api/prefetch-efficiency":
                status, payload = prefetch_efficiency_payload(store)
                self._json(payload, status=status)
                return

            self._json({"error": "not found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._post()
            except (ConfigError, OSError) as exc:
                logger.error("API operation failed: %s", type(exc).__name__)
                self._json({'error': 'operation unavailable'}, status=503)

        def _post(self) -> None:
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]

            if not self._context():
                return
            if not self._same_origin():
                self._json({'error': 'cross-origin request denied'}, status=403)
                return
            if parsed.path != '/api/auth/login' and not self._admin_required(csrf=True):
                return
            if not (self.client.credentials_allowed or self.current.status_server.allow_insecure_http):
                self._json({'error': 'HTTPS required'}, status=403)
                return
            try:
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) > 1:
                    raise ValueError('invalid body framing')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 1024 * 1024:
                    self._json({'error': 'request body too large'}, status=413)
                    return
                if length and self.headers.get_content_type() != 'application/json':
                    self._json({'error': 'application/json required'}, status=415)
                    return
                self.connection.settimeout(15)
                raw_body = self.rfile.read(length) if length else b''
                if len(raw_body) != length:
                    raise ValueError('incomplete body')
                body = json.loads(raw_body) if raw_body else {}
                if not isinstance(body, dict):
                    raise ValueError('JSON object required')
            except (ValueError, TimeoutError):
                self._json({'error': 'invalid JSON request body'}, status=400)
                return
            if parsed.path == '/api/auth/login':
                password = body.get('password')
                if not self.current.admin_password_hash:
                    self._json({'error': 'set administrator password with repowatch set-password'}, status=503)
                    return
                if not isinstance(password, str) or len(password) > 1024 or not verify_password(password, self.current.admin_password_hash):
                    self._json({'error': 'invalid password'}, status=401)
                    return
                secret, session = access.create_session(self.current.admin_password_hash, self.client.secure)
                cookie = f'{COOKIE_NAME}={secret}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}'
                if self.client.secure:
                    cookie += '; Secure'
                self._extra_headers = [('Set-Cookie', cookie)]
                self._json({'csrf_token': session.csrf, 'expires_at': session.expires_at})
                return
            if parsed.path == '/api/auth/logout':
                access.logout(self.session_secret)
                cookie = f'{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'
                if self.client.secure:
                    cookie += '; Secure'
                self._extra_headers = [('Set-Cookie', cookie)]
                self._json({'ok': True})
                return
            if parsed.path == '/api/tokens':
                try:
                    if set(body) - {'name', 'expires_at', 'repo_ids'}:
                        raise ValueError('only name, expires_at and repo_ids are supported')
                    repo_ids = body.get('repo_ids')
                    if repo_ids is not None:
                        if not self.current.status_server.token_repo_restrictions:
                            raise ValueError('enable status_server.token_repo_restrictions before issuing scoped tokens')
                        if not isinstance(repo_ids, list) or any(not isinstance(r, str) or self.current.repo_by_id(r) is None for r in repo_ids):
                            raise ValueError('repo_ids must contain existing repository ids')
                    token = access.create_token(body.get('name'), body.get('expires_at'), repo_ids)
                except ValueError as exc:
                    self._json({'error': str(exc)}, status=400)
                    return
                self._json(token, status=201)
                return
            if len(parts) == 4 and parts[:2] == ['api', 'tokens'] and parts[3] == 'revoke':
                found = access.revoke_token(parts[2])
                self._json({'ok': found}, status=200 if found else 404)
                return
            password = self.admin  # internal identity, never taken from a request header

            if parsed.path == "/api/repos":
                status, payload = add_repo_payload(config_path, password, body)
                self._json(payload, status=status)
                return

            if parsed.path == "/api/config":
                status, payload = update_safe_config_payload(config_path, password, body)
                self._json(payload, status=status)
                return

            if len(parts) == 3 and parts[0] == "api" and parts[1] == "repos":
                status, payload = update_repo_payload(config_path, password, parts[2], body)
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "delete":
                status, payload = delete_repo_payload(config_path, password, parts[2])
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "warm":
                status, payload = warm_packages_payload(config_path, store, parts[2], password, body)
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "purge":
                status, payload = purge_selected_payload(config_path, store, parts[2], password, body)
                self._json(payload, status=status)
                return

            if (
                len(parts) == 5
                and parts[0] == "api" and parts[1] == "repos" and parts[3] == "warmed"
                and parts[4] == "remove"
            ):
                status, payload = remove_warmed_package_payload(
                    config_path, store, parts[2], password, body
                )
                self._json(payload, status=status)
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "repos" and parts[3] == "bans":
                status, payload = ban_package_payload(config_path, store, parts[2], password, body)
                self._json(payload, status=status)
                return

            if (
                len(parts) == 5
                and parts[0] == "api" and parts[1] == "repos" and parts[3] == "bans"
                and parts[4] == "remove"
            ):
                status, payload = unban_package_payload(config_path, store, parts[2], password, body)
                self._json(payload, status=status)
                return

            self._json({"error": "not found"}, status=404)

    return Handler
