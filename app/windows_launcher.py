from __future__ import annotations

import ctypes
import json
import os
import secrets
import socket
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Thread
from typing import Any

import uvicorn

from .desktop_security import DESKTOP_PORT_ENV, DESKTOP_TOKEN_ENV, desktop_bootstrap_url

APP_DIRECTORY_NAME = "E-Rechnungs-Pruefer"
RUNTIME_FILE_NAME = "runtime.json"
WINDOWS_MUTEX_NAME = "Local\\E-Rechnungs-Pruefer-Desktop"
ERROR_ALREADY_EXISTS = 183
SERVER_READY_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class RuntimeRecord:
    pid: int
    port: int
    token: str


@dataclass(slots=True)
class WindowsMutex:
    handle: int
    already_exists: bool

    def close(self) -> None:
        if self.handle:
            close_handle = ctypes.CDLL("kernel32").CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_bool
            close_handle(ctypes.c_void_p(self.handle))
            self.handle = 0


def _desktop_data_directory() -> Path:
    base = os.getenv("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_DIRECTORY_NAME


def _runtime_file() -> Path:
    return _desktop_data_directory() / RUNTIME_FILE_NAME


def _write_runtime_record(path: Path, record: RuntimeRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(record), ensure_ascii=True), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _read_runtime_record(path: Path) -> RuntimeRecord | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        record = RuntimeRecord(pid=int(payload["pid"]), port=int(payload["port"]), token=str(payload["token"]))
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if record.pid <= 0 or not 1 <= record.port <= 65535 or len(record.token) < 20:
        return None
    return record


def _reserve_loopback_socket() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.setblocking(False)
        port = int(listener.getsockname()[1])
        return listener, port
    except Exception:
        listener.close()
        raise


def _health_is_ready(port: int, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _open_existing_instance(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = _read_runtime_record(path)
        if record is not None and _health_is_ready(record.port):
            return webbrowser.open(desktop_bootstrap_url(record.port, record.token))
        time.sleep(0.1)
    return False


def _create_windows_mutex() -> WindowsMutex:
    kernel32 = ctypes.CDLL("kernel32")
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.GetLastError.restype = ctypes.c_ulong
    handle = kernel32.CreateMutexW(None, False, WINDOWS_MUTEX_NAME)
    last_error = int(kernel32.GetLastError())
    if not handle:
        raise OSError(last_error, "CreateMutexW ist fehlgeschlagen.")
    return WindowsMutex(
        handle=int(handle),
        already_exists=last_error == ERROR_ALREADY_EXISTS,
    )


def _show_windows_message(message: str, *, error: bool = False) -> None:
    flags = 0x10 if error else 0x40
    message_box = ctypes.CDLL("user32").MessageBoxW
    message_box.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    message_box.restype = ctypes.c_int
    message_box(None, message, "E-Rechnungs-Prüfer", flags)


class DesktopServer:
    def __init__(self, runtime_file: Path) -> None:
        self.runtime_file = runtime_file
        self.listener, self.port = _reserve_loopback_socket()
        self.token = secrets.token_urlsafe(32)
        self.server: uvicorn.Server | None = None
        self.thread: Thread | None = None

    def start(self) -> None:
        os.environ[DESKTOP_TOKEN_ENV] = self.token
        os.environ[DESKTOP_PORT_ENV] = str(self.port)

        # Import only after the environment has activated the desktop middleware.
        from .main import app

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            access_log=False,
            log_level="warning",
        )
        self.server = uvicorn.Server(config)
        self.thread = Thread(
            target=self.server.run,
            kwargs={"sockets": [self.listener]},
            name="E-Rechnungs-Pruefer-Webserver",
        )
        self.thread.start()
        self._wait_until_ready()
        _write_runtime_record(
            self.runtime_file,
            RuntimeRecord(pid=os.getpid(), port=self.port, token=self.token),
        )

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + SERVER_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if _health_is_ready(self.port):
                return
            if self.thread is not None and not self.thread.is_alive():
                break
            time.sleep(0.1)
        raise RuntimeError("Der lokale Webserver konnte nicht gestartet werden.")

    def open_browser(self) -> bool:
        return webbrowser.open(desktop_bootstrap_url(self.port, self.token))

    def wait(self) -> None:
        if self.thread is not None:
            self.thread.join()

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=10)
        self.listener.close()
        record = _read_runtime_record(self.runtime_file)
        if record is not None and record.token == self.token:
            self.runtime_file.unlink(missing_ok=True)


def _tray_image() -> Any:
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (20, 78, 86, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((13, 7, 51, 57), radius=5, fill=(255, 255, 255, 255))
    draw.line((21, 21, 43, 21), fill=(20, 78, 86, 255), width=4)
    draw.line((21, 31, 43, 31), fill=(20, 78, 86, 255), width=4)
    draw.line((21, 41, 36, 41), fill=(20, 78, 86, 255), width=4)
    return image


def _run_tray(server: DesktopServer) -> None:
    try:
        import pystray
    except ImportError:
        server.open_browser()
        server.wait()
        return

    def open_browser(_icon=None, _item=None) -> None:
        server.open_browser()

    def stop(icon, _item=None) -> None:
        server.stop()
        icon.stop()

    icon = pystray.Icon(
        "e-rechnungs-pruefer",
        _tray_image(),
        "E-Rechnungs-Prüfer",
        menu=pystray.Menu(
            pystray.MenuItem("Öffnen", open_browser, default=True),
            pystray.MenuItem("Beenden", stop),
        ),
    )

    def stop_tray_after_server() -> None:
        server.wait()
        icon.stop()

    Thread(target=stop_tray_after_server, name="E-Rechnungs-Pruefer-Waechter", daemon=True).start()
    server.open_browser()
    icon.run()


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Der Desktop-Launcher ist ausschließlich für Windows vorgesehen.")

    mutex: WindowsMutex | None = None
    server: DesktopServer | None = None
    try:
        mutex = _create_windows_mutex()
        runtime_file = _runtime_file()
        if mutex.already_exists:
            if not _open_existing_instance(runtime_file):
                _show_windows_message("Die Anwendung läuft bereits, konnte aber nicht geöffnet werden.", error=True)
            return

        runtime_file.unlink(missing_ok=True)
        server = DesktopServer(runtime_file)
        server.start()
        _run_tray(server)
    except Exception as exc:
        _show_windows_message(f"Die Anwendung konnte nicht gestartet werden:\n\n{exc}", error=True)
    finally:
        if server is not None:
            server.stop()
        if mutex is not None:
            mutex.close()


if __name__ == "__main__":
    main()
