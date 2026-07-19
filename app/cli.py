from __future__ import annotations

import argparse
import webbrowser
from threading import Timer

import uvicorn

from .settings import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="E-Rechnungs-Viewer & Prüfer starten")
    parser.add_argument("--host", default=settings.host, help="Bind-Adresse (Standard: %(default)s)")
    parser.add_argument("--port", type=int, default=settings.port, help="Port (Standard: %(default)s)")
    parser.add_argument("--open", action="store_true", help="Browser nach dem Start öffnen")
    parser.add_argument("--reload", action="store_true", help="Entwicklungsmodus mit automatischem Reload")
    args = parser.parse_args()

    if args.open:
        host_for_browser = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
        Timer(1.2, lambda: webbrowser.open(f"http://{host_for_browser}:{args.port}")).start()

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
