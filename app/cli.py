from __future__ import annotations

import argparse
import webbrowser
from threading import Timer

import uvicorn

from .settings import settings


def _browser_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return f"[{host}]"
    return host


def main() -> None:
    parser = argparse.ArgumentParser(description="E-Rechnungs-Viewer & Prüfer starten")
    parser.add_argument("--host", default=settings.host, help="Bind-Adresse (Standard: %(default)s)")
    parser.add_argument("--port", type=int, default=settings.port, help="Port (Standard: %(default)s)")
    parser.add_argument("--open", action="store_true", help="Browser nach dem Start öffnen")
    parser.add_argument("--reload", action="store_true", help="Entwicklungsmodus mit automatischem Reload")
    args = parser.parse_args()

    if args.open:
        host_for_browser = _browser_host(args.host)
        Timer(1.2, lambda: webbrowser.open(f"http://{host_for_browser}:{args.port}")).start()

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
