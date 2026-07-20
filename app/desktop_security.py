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
DESKTOP_COOKIE_NAME = "einvoice_desktop_session"
DESKTOP_BOOTSTRAP_PATH = "/desktop/bootstrap"


def desktop_bootstrap_url(port: int, token: str) -> str:
    query = urlencode({"token": token})
    return f"http://127.0.0.1:{port}{DESKTOP_BOOTSTRAP_PATH}?{query}"


class DesktopSessionMiddleware:
    """Restrict the packaged desktop server to the browser session it started.

    The middleware is deliberately dormant unless the desktop launcher supplies
    a token. Development, API and container deployments therefore retain their
    existing behavior.
    """

    def __init__(self, app: ASGIApp, *, token: str | None = None, port: int | None = None) -> None:
        self.app = app
        self.token = token
        self.port = port
        self.allowed_hosts = {"127.0.0.1", "localhost"}
        if port is not None:
            self.allowed_hosts.update({f"127.0.0.1:{port}", f"localhost:{port}"})

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not self.token:
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
        if token is None:
            return None
        if request.url.path == "/api/health":
            return None

        if not self._host_is_allowed(request):
            return self._forbidden(request, "Der lokale Hostname ist nicht zulässig.")

        if request.url.path == DESKTOP_BOOTSTRAP_PATH:
            return self._bootstrap(request)

        supplied = request.cookies.get(DESKTOP_COOKIE_NAME, "")
        if not secrets.compare_digest(supplied, token):
            return self._forbidden(request, "Diese lokale Sitzung ist nicht autorisiert.")

        if request.method not in {"GET", "HEAD", "OPTIONS"} and not self._origin_is_allowed(request):
            return self._forbidden(request, "Der Ursprung der Anfrage ist nicht zulässig.")
        return None

    def _host_is_allowed(self, request: Request) -> bool:
        host = request.headers.get("host", "").lower().rstrip(".")
        return host in self.allowed_hosts

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
        if not secrets.compare_digest(supplied, token):
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
