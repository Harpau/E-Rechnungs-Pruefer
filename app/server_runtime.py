from __future__ import annotations

import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from threading import Thread
from typing import Any

import uvicorn

SERVER_READY_TIMEOUT_SECONDS = 20.0
DIRECT_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def reserve_loopback_socket(port: int) -> tuple[socket.socket, int]:
    if not 1 <= port <= 65535:
        raise ValueError("Der lokale API-Port muss zwischen 1 und 65535 liegen.")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", port))
        listener.setblocking(False)
        actual_port = int(listener.getsockname()[1])
        if actual_port != port:
            raise RuntimeError("Der lokale Webserver hat unerwartet einen anderen Port reserviert.")
        return listener, actual_port
    except Exception:
        listener.close()
        raise


def health_is_ready(port: int, timeout: float = 0.5) -> bool:
    try:
        with DIRECT_HTTP_OPENER.open(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def load_main_app() -> Any:
    from .main import app

    return app


class LoopbackServer:
    """UI-free lifecycle shared by the Windows desktop and SCM service."""

    def __init__(
        self,
        *,
        port: int,
        environment: Mapping[str, str] | None = None,
        app_loader: Callable[[], Any] = load_main_app,
        thread_name: str = "E-Rechnungs-Pruefer-Webserver",
        ready_timeout: float = SERVER_READY_TIMEOUT_SECONDS,
        daemon_thread: bool = False,
        socket_reserver: Callable[[int], tuple[socket.socket, int]] | None = None,
        health_probe: Callable[[int], bool] | None = None,
        config_factory: Callable[..., Any] | None = None,
        server_factory: Callable[[Any], uvicorn.Server] | None = None,
    ) -> None:
        self.environment = dict(environment or {})
        self.app_loader = app_loader
        self.thread_name = thread_name
        self.ready_timeout = ready_timeout
        self.daemon_thread = daemon_thread
        self.health_probe = health_probe or health_is_ready
        self.config_factory = config_factory or uvicorn.Config
        self.server_factory = server_factory or uvicorn.Server
        self.listener, self.port = (socket_reserver or reserve_loopback_socket)(port)
        self.server: uvicorn.Server | None = None
        self.thread: Thread | None = None
        self._closed = False

    def start(self) -> None:
        if self.server is not None or self.thread is not None:
            raise RuntimeError("Der lokale Webserver wurde bereits gestartet.")
        os.environ.update(self.environment)
        try:
            app = self.app_loader()
            config = self.config_factory(
                app,
                host="127.0.0.1",
                port=self.port,
                access_log=False,
                log_config=None,
                log_level="warning",
            )
            self.server = self.server_factory(config)
            self.thread = Thread(
                target=self.server.run,
                kwargs={"sockets": [self.listener]},
                name=self.thread_name,
                daemon=self.daemon_thread,
            )
            self.thread.start()
            self._wait_until_ready()
        except Exception:
            self.request_stop()
            if self.thread is not None and self.thread.is_alive():
                self.thread.join(timeout=2)
            if self.thread is None or not self.thread.is_alive():
                self._close_listener()
            raise

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if self.health_probe(self.port):
                return
            if self.thread is not None and not self.thread.is_alive():
                break
            time.sleep(0.1)
        raise RuntimeError("Der lokale Webserver konnte nicht gestartet werden.")

    def request_stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True

    def wait(self, timeout: float | None = None) -> bool:
        if self.thread is None:
            return True
        self.thread.join(timeout=timeout)
        return not self.thread.is_alive()

    def is_alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def stop(self, *, timeout: float) -> None:
        self.request_stop()
        if not self.wait(timeout):
            raise RuntimeError(f"Der lokale Webserver konnte nicht innerhalb von {timeout:g} Sekunden beendet werden.")
        self._close_listener()

    def _close_listener(self) -> None:
        if not self._closed:
            self.listener.close()
            self._closed = True
