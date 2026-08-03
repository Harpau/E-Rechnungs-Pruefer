from __future__ import annotations

import argparse
import os
import secrets
import webbrowser
from threading import Timer

import uvicorn

from .desktop_security import (
    API_TOKEN_ENV,
    DESKTOP_PORT_ENV,
    DESKTOP_TOKEN_ENV,
    desktop_bootstrap_url,
    validate_api_token,
)
from .settings import settings


def _browser_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return f"[{host}]"
    return host


def _interactive_browser_url(host: str, port: int) -> str:
    """Create a separate browser session when the local API uses a bearer token."""

    api_token = os.getenv(API_TOKEN_ENV)
    desktop_token = os.getenv(DESKTOP_TOKEN_ENV)
    if api_token is None and not desktop_token:
        return f"http://{_browser_host(host)}:{port}"

    if api_token is not None:
        validate_api_token(api_token)
    if not desktop_token:
        desktop_token = secrets.token_urlsafe(32)
        os.environ[DESKTOP_TOKEN_ENV] = desktop_token
    os.environ[DESKTOP_PORT_ENV] = str(port)
    return desktop_bootstrap_url(port, desktop_token)


def main() -> None:
    parser = argparse.ArgumentParser(description="E-Rechnungs-Viewer & Prüfer starten")
    parser.add_argument("--host", default=settings.host, help="Bind-Adresse (Standard: %(default)s)")
    parser.add_argument("--port", type=int, default=settings.port, help="Port (Standard: %(default)s)")
    parser.add_argument("--open", action="store_true", help="Browser nach dem Start öffnen")
    parser.add_argument("--reload", action="store_true", help="Entwicklungsmodus mit automatischem Reload")
    args = parser.parse_args()

    if args.open:
        browser_url = _interactive_browser_url(args.host, args.port)
        Timer(1.2, lambda: webbrowser.open(browser_url)).start()

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
