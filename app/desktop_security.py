from __future__ import annotations

import secrets
from http import HTTPStatus
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


def validate_api_token(token: str, *, description: str = API_TOKEN_ENV) -> str:
    """Validate the shared bearer-token contract before the ASGI app starts."""

    if len(token) < MINIMUM_API_TOKEN_LENGTH or any(
        character not in URL_SAFE_API_TOKEN_CHARACTERS for character in token
    ):
        raise ValueError(
            f"{description} muss mindestens {MINIMUM_API_TOKEN_LENGTH} URL-sichere ASCII-Zeichen enthalten."
        )
    return token


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
    ) -> None:
        self.app = app
        self.token = token
        self.port = port
        self.api_token = api_token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not (self.token or self.api_token):
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

        if not token:
            if request.url.path.startswith("/api/") and self.api_token:
                return self._forbidden(request, "Das API-Zugriffstoken fehlt.")
            return None

        if request.url.path == DESKTOP_BOOTSTRAP_PATH:
            return self._bootstrap(request)

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
            return url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost"} and url.port == self.port
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
