from __future__ import annotations

import secrets
import time
from collections.abc import Callable, MutableMapping
from http import HTTPStatus
from threading import Lock
from urllib.parse import urlencode

from starlette.datastructures import URL
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp

DESKTOP_TOKEN_ENV = "EINVOICE_DESKTOP_TOKEN"
DESKTOP_PORT_ENV = "EINVOICE_DESKTOP_PORT"
API_TOKEN_ENV = "EINVOICE_API_TOKEN"
DESKTOP_COOKIE_NAME = "einvoice_desktop_session"
DESKTOP_BOOTSTRAP_PATH = "/desktop/bootstrap"
MINIMUM_API_TOKEN_LENGTH = 32
URL_SAFE_API_TOKEN_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
SERVICE_MODE_ENV = "EINVOICE_SERVICE_MODE"
SERVICE_BOOTSTRAP_TTL_SECONDS = 60
SERVICE_BROWSER_SESSION_TTL_SECONDS = 30 * 60
MAX_PENDING_SERVICE_BOOTSTRAPS = 32
MAX_SERVICE_BROWSER_SESSIONS = 128


class OneTimeBrowserSessions:
    """Issue one-time browser grants and validate separate expiring cookies."""

    def __init__(
        self,
        *,
        bootstrap_ttl_seconds: int = SERVICE_BOOTSTRAP_TTL_SECONDS,
        session_ttl_seconds: int = SERVICE_BROWSER_SESSION_TTL_SECONDS,
        max_pending_bootstraps: int = MAX_PENDING_SERVICE_BOOTSTRAPS,
        max_sessions: int = MAX_SERVICE_BROWSER_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if bootstrap_ttl_seconds <= 0 or session_ttl_seconds <= 0:
            raise ValueError("Die Gültigkeitsdauer lokaler Browsersitzungen muss positiv sein.")
        if max_pending_bootstraps <= 0 or max_sessions <= 0:
            raise ValueError("Die Kapazität lokaler Browsersitzungen muss positiv sein.")
        self.bootstrap_ttl_seconds = bootstrap_ttl_seconds
        self.session_ttl_seconds = session_ttl_seconds
        self.max_pending_bootstraps = max_pending_bootstraps
        self.max_sessions = max_sessions
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._bootstrap_tokens: dict[str, float] = {}
        self._sessions: dict[str, float] = {}
        self._lock = Lock()

    def _prune(self, now: float) -> None:
        self._bootstrap_tokens = {token: expires for token, expires in self._bootstrap_tokens.items() if expires >= now}
        self._sessions = {token: expires for token, expires in self._sessions.items() if expires >= now}

    def _new_token(self) -> str:
        for _attempt in range(10):
            token = self._token_factory()
            if token and token not in self._bootstrap_tokens and token not in self._sessions:
                return token
        raise RuntimeError("Ein eindeutiges lokales Sitzungstoken konnte nicht erzeugt werden.")

    @staticmethod
    def _discard_earliest(tokens: dict[str, float]) -> None:
        if tokens:
            tokens.pop(min(tokens, key=tokens.__getitem__))

    def issue_bootstrap(self) -> str:
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._bootstrap_tokens) >= self.max_pending_bootstraps:
                self._discard_earliest(self._bootstrap_tokens)
            token = self._new_token()
            self._bootstrap_tokens[token] = now + self.bootstrap_ttl_seconds
            return token

    def consume_bootstrap(self, supplied: str) -> str | None:
        with self._lock:
            now = self._clock()
            self._prune(now)
            expires = self._bootstrap_tokens.pop(supplied, None)
            if expires is None or expires < now:
                return None
            if len(self._sessions) >= self.max_sessions:
                self._discard_earliest(self._sessions)
            session = self._new_token()
            self._sessions[session] = now + self.session_ttl_seconds
            return session

    def session_is_valid(self, supplied: str) -> bool:
        with self._lock:
            now = self._clock()
            self._prune(now)
            expires = self._sessions.get(supplied)
            return expires is not None and expires >= now

    def clear(self) -> None:
        with self._lock:
            self._bootstrap_tokens.clear()
            self._sessions.clear()


_SERVICE_BROWSER_SESSIONS = OneTimeBrowserSessions()


def get_service_browser_sessions() -> OneTimeBrowserSessions:
    return _SERVICE_BROWSER_SESSIONS


def validate_api_token(token: str, *, description: str = API_TOKEN_ENV) -> str:
    """Validate the shared bearer-token contract before the ASGI app starts."""

    if len(token) < MINIMUM_API_TOKEN_LENGTH or any(
        character not in URL_SAFE_API_TOKEN_CHARACTERS for character in token
    ):
        raise ValueError(
            f"{description} muss mindestens {MINIMUM_API_TOKEN_LENGTH} URL-sichere ASCII-Zeichen enthalten."
        )
    return token


def consume_api_token_environment(environ: MutableMapping[str, str]) -> str | None:
    """Validate and remove the bearer token from a process environment."""

    token = environ.pop(API_TOKEN_ENV, None)
    return validate_api_token(token) if token is not None else None


def desktop_bootstrap_url(port: int, token: str) -> str:
    query = urlencode({"token": token})
    return f"http://127.0.0.1:{port}{DESKTOP_BOOTSTRAP_PATH}?{query}"


def _tokens_match(supplied: str, expected: str) -> bool:
    """Compare untrusted text without rejecting non-ASCII input."""

    try:
        return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    except UnicodeEncodeError:
        return False


class DesktopSessionMiddleware:
    """Restrict the packaged desktop server to the browser session it started.

    The middleware is deliberately dormant unless a desktop or API token is
    configured. Development and container deployments without either token
    therefore retain their existing behavior.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str | None = None,
        port: int | None = None,
        api_token: str | None = None,
        browser_sessions: OneTimeBrowserSessions | None = None,
    ) -> None:
        self.app = app
        self.token = token
        self.port = port
        self.api_token = api_token
        self.browser_sessions = browser_sessions

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not (self.token or self.api_token or self.browser_sessions):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        response = self._authorize(request)
        if response is not None:
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _authorize(self, request: Request) -> Response | None:
        token = self.token
        if not self._host_is_allowed(request):
            return self._forbidden(request, "Der lokale Hostname ist nicht zulässig.")
        if request.url.path == "/api/health":
            return None

        authorization = request.headers.get("authorization")
        if request.url.path.startswith("/api/") and authorization:
            if self._bearer_is_allowed(authorization):
                return None
            return self._forbidden(request, "Das API-Zugriffstoken ist ungültig.")

        if request.url.path == DESKTOP_BOOTSTRAP_PATH and self.browser_sessions is not None:
            return self._service_bootstrap(request)

        if self.browser_sessions is not None:
            supplied = request.cookies.get(DESKTOP_COOKIE_NAME, "")
            if not self.browser_sessions.session_is_valid(supplied):
                return self._forbidden(request, "Diese lokale Sitzung ist nicht autorisiert.")
        elif not token:
            if request.url.path.startswith("/api/") and self.api_token:
                return self._forbidden(request, "Das API-Zugriffstoken fehlt.")
            return None
        elif request.url.path == DESKTOP_BOOTSTRAP_PATH:
            return self._bootstrap(request)
        else:
            supplied = request.cookies.get(DESKTOP_COOKIE_NAME, "")
            if not _tokens_match(supplied, token):
                return self._forbidden(request, "Diese lokale Sitzung ist nicht autorisiert.")

        if request.method not in {"GET", "HEAD", "OPTIONS"} and not self._origin_is_allowed(request):
            return self._forbidden(request, "Der Ursprung der Anfrage ist nicht zulässig.")
        return None

    def _bearer_is_allowed(self, authorization: str) -> bool:
        scheme, separator, supplied = authorization.partition(" ")
        token = self.api_token
        return bool(token and separator and scheme.lower() == "bearer" and supplied and _tokens_match(supplied, token))

    def _host_is_allowed(self, request: Request) -> bool:
        try:
            hostname = (request.url.hostname or "").lower().rstrip(".")
            request_port = request.url.port
        except ValueError:
            return False
        if hostname not in {"127.0.0.1", "localhost"}:
            return False
        return self.port is None or request_port in {None, self.port}

    def _origin_is_allowed(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        if not origin:
            return True
        try:
            url = URL(origin)
            origin_port = url.port if url.port is not None else 80
            return url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost"} and origin_port == self.port
        except ValueError:
            return False

    def _bootstrap(self, request: Request) -> Response:
        token = self.token or ""
        supplied = request.query_params.get("token", "")
        if not _tokens_match(supplied, token):
            return self._forbidden(request, "Der Startlink ist ungültig oder abgelaufen.")

        response = RedirectResponse(url="/", status_code=HTTPStatus.SEE_OTHER)
        response.set_cookie(
            DESKTOP_COOKIE_NAME,
            token,
            httponly=True,
            secure=False,
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def _service_bootstrap(self, request: Request) -> Response:
        broker = self.browser_sessions
        supplied = request.query_params.get("token", "")
        session = broker.consume_bootstrap(supplied) if broker is not None else None
        if session is None:
            return self._forbidden(request, "Der Startlink ist ungültig oder abgelaufen.")
        assert broker is not None

        response = RedirectResponse(url="/", status_code=HTTPStatus.SEE_OTHER)
        response.set_cookie(
            DESKTOP_COOKIE_NAME,
            session,
            max_age=broker.session_ttl_seconds,
            httponly=True,
            secure=False,
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @staticmethod
    def _forbidden(request: Request, message: str) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=HTTPStatus.FORBIDDEN,
                content={"detail": message, "type": "desktop_session_error"},
                headers={"Cache-Control": "no-store"},
            )
        return PlainTextResponse(
            message,
            status_code=HTTPStatus.FORBIDDEN,
            headers={"Cache-Control": "no-store"},
        )
