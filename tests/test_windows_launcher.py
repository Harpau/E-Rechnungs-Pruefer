from __future__ import annotations

import json
import os
import sys
import urllib.error
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import ANY, Mock

import pytest

from app import windows_launcher
from app.windows_launcher import RuntimeRecord


def test_desktop_data_directory_uses_local_app_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert windows_launcher._desktop_data_directory() == tmp_path / "E-Rechnungs-Pruefer"


def test_desktop_data_directory_falls_back_to_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(windows_launcher.Path, "home", lambda: tmp_path)

    assert windows_launcher._desktop_data_directory() == tmp_path / "AppData/Local/E-Rechnungs-Pruefer"


def test_runtime_record_roundtrip_and_validation(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    expected = RuntimeRecord(pid=123, port=8765, token="x" * 32)

    windows_launcher._write_runtime_record(path, expected)

    assert windows_launcher._read_runtime_record(path) == expected
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600

    path.write_text(json.dumps({"pid": 0, "port": 80, "token": "kurz"}), encoding="utf-8")
    assert windows_launcher._read_runtime_record(path) is None
    path.write_text("kein-json", encoding="utf-8")
    assert windows_launcher._read_runtime_record(path) is None


def test_startup_error_is_written_for_headless_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "startup-error.log"

    windows_launcher._write_startup_error(path, RuntimeError("Start fehlgeschlagen"))

    assert "RuntimeError: Start fehlgeschlagen" in path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_api_token_is_created_once_with_private_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "api-token.txt"
    monkeypatch.delenv(windows_launcher.API_TOKEN_ENV, raising=False)
    monkeypatch.setattr(windows_launcher.secrets, "token_urlsafe", lambda _length: "a" * 43)

    first = windows_launcher._load_or_create_api_token(path)
    monkeypatch.setattr(windows_launcher.secrets, "token_urlsafe", lambda _length: "b" * 43)
    second = windows_launcher._load_or_create_api_token(path)

    assert first == "a" * 43
    assert second == first
    assert path.read_text(encoding="ascii") == first + "\n"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_api_token_accepts_environment_override_and_rejects_invalid_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "api-token.txt"
    monkeypatch.setenv(windows_launcher.API_TOKEN_ENV, "x" * 32)
    assert windows_launcher._load_or_create_api_token(path) == "x" * 32
    assert not path.exists()

    monkeypatch.setenv(windows_launcher.API_TOKEN_ENV, "zu-kurz")
    with pytest.raises(RuntimeError, match="mindestens 32"):
        windows_launcher._load_or_create_api_token(path)

    monkeypatch.delenv(windows_launcher.API_TOKEN_ENV)
    path.write_text("ungueltig\n", encoding="ascii")
    with pytest.raises(RuntimeError, match="gespeicherte.*URL-sichere"):
        windows_launcher._load_or_create_api_token(path)


@pytest.mark.parametrize(
    "token",
    [
        "ä" * 32,
        "a" * 31 + "/",
        "a" * 31 + "+",
        "a" * 32 + " ",
    ],
)
def test_api_token_rejects_non_url_safe_ascii_environment_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    monkeypatch.setenv(windows_launcher.API_TOKEN_ENV, token)

    with pytest.raises(RuntimeError, match="URL-sichere ASCII-Zeichen"):
        windows_launcher._load_or_create_api_token(tmp_path / "api-token.txt")


@pytest.mark.parametrize("stored", [("a" * 31 + "%\n"), ("a" * 32 + " \n"), ("ä" * 32 + "\n")])
def test_api_token_rejects_invalid_stored_values(tmp_path: Path, stored: str) -> None:
    path = tmp_path / "api-token.txt"
    path.write_text(stored, encoding="utf-8")

    with pytest.raises(RuntimeError, match="API-Zugriffstoken"):
        windows_launcher._load_or_create_api_token(path)


def test_reserve_loopback_socket_uses_configured_local_port(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = Mock()
    listener.getsockname.return_value = ("127.0.0.1", 8080)
    socket_factory = Mock(return_value=listener)
    monkeypatch.setattr(windows_launcher.socket, "socket", socket_factory)

    actual_listener, port = windows_launcher._reserve_loopback_socket(8080)

    socket_factory.assert_called_once_with(windows_launcher.socket.AF_INET, windows_launcher.socket.SOCK_STREAM)
    listener.bind.assert_called_once_with(("127.0.0.1", 8080))
    listener.setblocking.assert_called_once_with(False)
    assert actual_listener is listener
    assert port == 8080


def test_reserve_loopback_socket_closes_after_bind_error(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = Mock()
    listener.bind.side_effect = OSError("belegt")
    monkeypatch.setattr(windows_launcher.socket, "socket", Mock(return_value=listener))

    with pytest.raises(OSError, match="belegt"):
        windows_launcher._reserve_loopback_socket(8080)

    listener.close.assert_called_once_with()


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_reserve_loopback_socket_rejects_non_fixed_or_invalid_port(port: int) -> None:
    with pytest.raises(ValueError, match="zwischen 1 und 65535"):
        windows_launcher._reserve_loopback_socket(port)


def test_health_check_handles_success_and_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    direct_open = Mock(return_value=response)
    monkeypatch.setattr(windows_launcher._DIRECT_HTTP_OPENER, "open", direct_open)

    assert windows_launcher._health_is_ready(8765, timeout=1.25) is True
    direct_open.assert_called_once_with("http://127.0.0.1:8765/api/health", timeout=1.25)

    direct_open.side_effect = urllib.error.URLError("nicht erreichbar")
    assert windows_launcher._health_is_ready(8765) is False


def test_health_check_opener_has_no_environment_proxy() -> None:
    proxy_handlers = [
        handler
        for handler in windows_launcher._DIRECT_HTTP_OPENER.handlers
        if isinstance(handler, urllib.request.ProxyHandler)
    ]

    assert proxy_handlers == []


def test_open_existing_instance_reuses_authenticated_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "runtime.json"
    record = RuntimeRecord(pid=123, port=8765, token="x" * 32)
    windows_launcher._write_runtime_record(path, record)
    browser_open = Mock(return_value=True)
    monkeypatch.setattr(windows_launcher, "_health_is_ready", lambda _port: True)
    monkeypatch.setattr(windows_launcher.webbrowser, "open", browser_open)

    assert windows_launcher._open_existing_instance(path) is True
    browser_open.assert_called_once_with(windows_launcher.desktop_bootstrap_url(record.port, record.token))


def test_open_existing_instance_times_out_for_stale_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "runtime.json"
    windows_launcher._write_runtime_record(path, RuntimeRecord(pid=123, port=8765, token="x" * 32))
    monkeypatch.setattr(windows_launcher, "_health_is_ready", lambda _port: False)

    assert windows_launcher._open_existing_instance(path, timeout=0.01) is False


def test_windows_mutex_creation_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
    create_mutex = Mock(return_value=123456)
    get_last_error = Mock(return_value=windows_launcher.ERROR_ALREADY_EXISTS)
    close_handle = Mock(return_value=True)
    kernel32 = SimpleNamespace(
        CreateMutexW=create_mutex,
        GetLastError=get_last_error,
        CloseHandle=close_handle,
    )
    monkeypatch.setattr(windows_launcher.ctypes, "CDLL", Mock(return_value=kernel32))

    mutex = windows_launcher._create_windows_mutex()

    assert mutex.handle == 123456
    assert mutex.already_exists is True
    create_mutex.assert_called_once_with(None, False, windows_launcher.WINDOWS_MUTEX_NAME)
    mutex.close()
    assert mutex.handle == 0
    close_handle.assert_called_once()


def test_windows_mutex_creation_reports_win32_error(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel32 = SimpleNamespace(CreateMutexW=Mock(return_value=0), GetLastError=Mock(return_value=5))
    monkeypatch.setattr(windows_launcher.ctypes, "CDLL", Mock(return_value=kernel32))

    with pytest.raises(OSError, match="CreateMutexW"):
        windows_launcher._create_windows_mutex()


def test_windows_shutdown_event_creation_wait_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
    create_event = Mock(return_value=654321)
    wait_for_single_object = Mock(side_effect=[windows_launcher.WAIT_TIMEOUT, windows_launcher.WAIT_OBJECT_0])
    close_handle = Mock(return_value=True)
    kernel32 = SimpleNamespace(
        CreateEventW=create_event,
        GetLastError=Mock(return_value=0),
        WaitForSingleObject=wait_for_single_object,
        CloseHandle=close_handle,
    )
    monkeypatch.setattr(windows_launcher.ctypes, "CDLL", Mock(return_value=kernel32))

    shutdown_event = windows_launcher._create_windows_shutdown_event()

    create_event.assert_called_once_with(None, True, False, windows_launcher.WINDOWS_SHUTDOWN_EVENT_NAME)
    assert shutdown_event.wait(250) is False
    assert shutdown_event.wait(250) is True
    shutdown_event.close()
    assert shutdown_event.handle == 0
    close_handle.assert_called_once()


def test_windows_shutdown_event_creation_reports_win32_error(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel32 = SimpleNamespace(CreateEventW=Mock(return_value=0), GetLastError=Mock(return_value=5))
    monkeypatch.setattr(windows_launcher.ctypes, "CDLL", Mock(return_value=kernel32))

    with pytest.raises(OSError, match="CreateEventW"):
        windows_launcher._create_windows_shutdown_event()


def test_windows_shutdown_event_wait_reports_win32_error(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel32 = SimpleNamespace(
        WaitForSingleObject=Mock(return_value=windows_launcher.WAIT_FAILED),
        GetLastError=Mock(return_value=6),
    )
    monkeypatch.setattr(windows_launcher.ctypes, "CDLL", Mock(return_value=kernel32))

    with pytest.raises(OSError, match="WaitForSingleObject") as error:
        windows_launcher.WindowsShutdownEvent(handle=123).wait(250)

    assert error.value.errno == 6


def test_shutdown_watcher_requests_orderly_server_stop() -> None:
    shutdown_event = Mock()
    shutdown_event.wait.side_effect = [False, True]
    watcher_stop = windows_launcher.Event()
    server = Mock()

    windows_launcher._watch_for_shutdown(shutdown_event, watcher_stop, server)

    assert shutdown_event.wait.call_count == 2
    server.request_stop.assert_called_once_with()


def test_show_windows_message_uses_error_icon(monkeypatch: pytest.MonkeyPatch) -> None:
    message_box = Mock(return_value=1)
    user32 = SimpleNamespace(MessageBoxW=message_box)
    monkeypatch.setattr(windows_launcher.ctypes, "CDLL", Mock(return_value=user32))

    windows_launcher._show_windows_message("Testfehler", error=True)

    message_box.assert_called_once_with(None, "Testfehler", "E-Rechnungs-Prüfer", 0x10)


def test_desktop_server_starts_opens_and_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    listener = Mock()
    fake_uvicorn_server = SimpleNamespace(run=Mock(), should_exit=False)
    config = object()
    config_factory = Mock(return_value=config)
    reserve_socket = Mock(return_value=(listener, 8765))
    monkeypatch.setattr(windows_launcher, "_reserve_loopback_socket", reserve_socket)
    monkeypatch.setattr(windows_launcher.secrets, "token_urlsafe", lambda _length: "x" * 32)
    monkeypatch.setattr(windows_launcher, "_health_is_ready", lambda _port: True)
    monkeypatch.setattr(windows_launcher.uvicorn, "Config", config_factory)
    monkeypatch.setattr(windows_launcher.uvicorn, "Server", Mock(return_value=fake_uvicorn_server))
    main_module = ModuleType("app.main")
    main_module.app = object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.main", main_module)
    configured_port = Mock(return_value=8080)
    monkeypatch.setattr(windows_launcher, "_configured_port", configured_port)
    browser_open = Mock(return_value=True)
    monkeypatch.setattr(windows_launcher.webbrowser, "open", browser_open)
    monkeypatch.delenv(windows_launcher.DESKTOP_TOKEN_ENV, raising=False)
    monkeypatch.delenv(windows_launcher.DESKTOP_PORT_ENV, raising=False)
    monkeypatch.delenv(windows_launcher.API_TOKEN_ENV, raising=False)
    runtime_file = tmp_path / "runtime.json"
    server = windows_launcher.DesktopServer(runtime_file, "a" * 32)

    server.start()

    configured_port.assert_called_once_with()
    reserve_socket.assert_called_once_with(8080)
    config_factory.assert_called_once_with(
        ANY,
        host="127.0.0.1",
        port=8765,
        access_log=False,
        log_config=None,
        log_level="warning",
    )
    assert windows_launcher.os.environ[windows_launcher.DESKTOP_TOKEN_ENV] == "x" * 32
    assert windows_launcher.os.environ[windows_launcher.DESKTOP_PORT_ENV] == "8765"
    assert windows_launcher.os.environ[windows_launcher.API_TOKEN_ENV] == "a" * 32
    assert windows_launcher._read_runtime_record(runtime_file) == RuntimeRecord(
        pid=windows_launcher.os.getpid(),
        port=8765,
        token="x" * 32,
    )
    assert server.open_browser() is True
    browser_open.assert_called_once_with(windows_launcher.desktop_bootstrap_url(8765, "x" * 32))
    server.wait()
    server.stop()

    assert fake_uvicorn_server.should_exit is True
    listener.close.assert_called_once_with()
    assert not runtime_file.exists()


def test_desktop_server_reports_early_server_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    server = windows_launcher.DesktopServer.__new__(windows_launcher.DesktopServer)
    server.port = 8765
    server.thread = Mock()
    server.thread.is_alive.return_value = False
    monkeypatch.setattr(windows_launcher, "_health_is_ready", lambda _port: False)

    with pytest.raises(RuntimeError, match="konnte nicht gestartet"):
        server._wait_until_ready()


def test_desktop_server_does_not_report_shutdown_while_thread_is_alive() -> None:
    server = windows_launcher.DesktopServer.__new__(windows_launcher.DesktopServer)
    server.server = SimpleNamespace(should_exit=False)
    server.thread = Mock()
    server.thread.is_alive.return_value = True
    server.listener = Mock()

    with pytest.raises(RuntimeError, match="nicht innerhalb von 10 Sekunden"):
        server.stop()

    assert server.server.should_exit is True
    server.thread.join.assert_called_once_with(timeout=10)
    server.listener.close.assert_not_called()


def test_tray_fallback_opens_browser_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    server = Mock()
    real_import = __import__

    def import_without_pystray(name, *args, **kwargs):
        if name == "pystray":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", import_without_pystray)

    windows_launcher._run_tray(server)

    server.open_browser.assert_called_once_with()
    server.wait.assert_called_once_with()


def test_tray_fallback_background_mode_waits_without_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    server = Mock()
    real_import = __import__

    def import_without_pystray(name, *args, **kwargs):
        if name == "pystray":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", import_without_pystray)

    windows_launcher._run_tray(server, open_browser_on_start=False)

    server.open_browser.assert_not_called()
    server.wait.assert_called_once_with()


def test_tray_shutdown_waits_until_pystray_is_running(monkeypatch: pytest.MonkeyPatch) -> None:
    server = Mock()
    callbacks: dict[str, object] = {}
    created_icons: list[object] = []

    class FakeMenuItem:
        def __init__(self, label, callback, **_kwargs) -> None:
            callbacks[label] = callback

    class FakeIcon:
        def __init__(self, *_args, **_kwargs) -> None:
            self.running = False
            self.visible = False
            self.stop_calls = 0
            created_icons.append(self)

        def stop(self) -> None:
            if self.running:
                self.stop_calls += 1
                self.running = False

        def run(self, *, setup) -> None:
            self.running = True
            setup(self)

    pystray_module = ModuleType("pystray")
    pystray_module.Icon = FakeIcon  # type: ignore[attr-defined]
    pystray_module.Menu = lambda *items: items  # type: ignore[attr-defined]
    pystray_module.MenuItem = FakeMenuItem  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pystray", pystray_module)
    monkeypatch.setattr(windows_launcher, "_tray_image", lambda: object())

    windows_launcher._run_tray(server, open_browser_on_start=False)

    assert len(created_icons) == 1
    icon = created_icons[0]
    assert icon.visible is True  # type: ignore[attr-defined]
    assert icon.stop_calls == 1  # type: ignore[attr-defined]
    server.wait.assert_called_once_with()
    server.open_browser.assert_not_called()

    callbacks["Beenden"](icon)  # type: ignore[operator]
    server.request_stop.assert_called_once_with()
    server.stop.assert_not_called()


def test_main_reopens_existing_windows_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mutex = SimpleNamespace(already_exists=True, close=Mock())
    runtime_file = tmp_path / "runtime.json"
    monkeypatch.setattr(windows_launcher.sys, "platform", "win32")
    monkeypatch.setattr(windows_launcher, "_create_windows_mutex", Mock(return_value=mutex))
    monkeypatch.setattr(windows_launcher, "_runtime_file", lambda: runtime_file)
    monkeypatch.setattr(windows_launcher, "_startup_error_file", lambda: tmp_path / "startup-error.log")
    open_existing = Mock(return_value=True)
    monkeypatch.setattr(windows_launcher, "_open_existing_instance", open_existing)

    windows_launcher.main([])

    open_existing.assert_called_once_with(runtime_file)
    mutex.close.assert_called_once_with()


def test_background_start_does_not_open_existing_windows_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutex = SimpleNamespace(already_exists=True, close=Mock())
    monkeypatch.setattr(windows_launcher.sys, "platform", "win32")
    monkeypatch.setattr(windows_launcher, "_create_windows_mutex", Mock(return_value=mutex))
    monkeypatch.setattr(windows_launcher, "_runtime_file", lambda: tmp_path / "runtime.json")
    monkeypatch.setattr(windows_launcher, "_startup_error_file", lambda: tmp_path / "startup-error.log")
    open_existing = Mock(return_value=True)
    monkeypatch.setattr(windows_launcher, "_open_existing_instance", open_existing)

    windows_launcher.main(["--background"])

    open_existing.assert_not_called()
    mutex.close.assert_called_once_with()


def test_main_starts_and_stops_first_windows_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mutex = SimpleNamespace(already_exists=False, close=Mock())
    backend_mutex = SimpleNamespace(already_exists=False, close=Mock())
    shutdown_event = Mock()
    shutdown_event.wait.return_value = True
    server = Mock()
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text("alt", encoding="utf-8")
    monkeypatch.setattr(windows_launcher.sys, "platform", "win32")
    monkeypatch.setenv(windows_launcher.SERVICE_MODE_ENV, "1")
    monkeypatch.setattr(windows_launcher, "_create_windows_mutex", Mock(return_value=mutex))
    monkeypatch.setattr(windows_launcher, "_create_backend_mutex", Mock(return_value=backend_mutex))
    monkeypatch.setattr(windows_launcher, "_create_windows_shutdown_event", Mock(return_value=shutdown_event))
    monkeypatch.setattr(windows_launcher, "_runtime_file", lambda: runtime_file)
    monkeypatch.setattr(windows_launcher, "_api_token_file", lambda: tmp_path / "api-token.txt")
    monkeypatch.setattr(windows_launcher, "_load_or_create_api_token", Mock(return_value="a" * 32))
    monkeypatch.setattr(windows_launcher, "_startup_error_file", lambda: tmp_path / "startup-error.log")
    server_factory = Mock(return_value=server)
    monkeypatch.setattr(windows_launcher, "DesktopServer", server_factory)
    run_tray = Mock()
    monkeypatch.setattr(windows_launcher, "_run_tray", run_tray)

    windows_launcher.main([])

    assert windows_launcher.SERVICE_MODE_ENV not in os.environ
    server_factory.assert_called_once_with(runtime_file, "a" * 32)
    server.start.assert_called_once_with()
    run_tray.assert_called_once_with(server, open_browser_on_start=True)
    server.stop.assert_called_once_with()
    shutdown_event.close.assert_called_once_with()
    backend_mutex.close.assert_called_once_with()
    mutex.close.assert_called_once_with()


def test_main_starts_first_windows_instance_in_background(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mutex = SimpleNamespace(already_exists=False, close=Mock())
    backend_mutex = SimpleNamespace(already_exists=False, close=Mock())
    shutdown_event = Mock()
    shutdown_event.wait.return_value = True
    server = Mock()
    runtime_file = tmp_path / "runtime.json"
    monkeypatch.setattr(windows_launcher.sys, "platform", "win32")
    monkeypatch.setattr(windows_launcher, "_create_windows_mutex", Mock(return_value=mutex))
    monkeypatch.setattr(windows_launcher, "_create_backend_mutex", Mock(return_value=backend_mutex))
    monkeypatch.setattr(windows_launcher, "_create_windows_shutdown_event", Mock(return_value=shutdown_event))
    monkeypatch.setattr(windows_launcher, "_runtime_file", lambda: runtime_file)
    monkeypatch.setattr(windows_launcher, "_api_token_file", lambda: tmp_path / "api-token.txt")
    monkeypatch.setattr(windows_launcher, "_load_or_create_api_token", Mock(return_value="a" * 32))
    monkeypatch.setattr(windows_launcher, "_startup_error_file", lambda: tmp_path / "startup-error.log")
    monkeypatch.setattr(windows_launcher, "DesktopServer", Mock(return_value=server))
    run_tray = Mock()
    monkeypatch.setattr(windows_launcher, "_run_tray", run_tray)

    windows_launcher.main(["--background"])

    server.start.assert_called_once_with()
    run_tray.assert_called_once_with(server, open_browser_on_start=False)
    server.stop.assert_called_once_with()
    shutdown_event.close.assert_called_once_with()
    backend_mutex.close.assert_called_once_with()
    mutex.close.assert_called_once_with()


def test_main_releases_all_handles_when_server_stop_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutex = SimpleNamespace(already_exists=False, close=Mock())
    backend_mutex = SimpleNamespace(already_exists=False, close=Mock())
    shutdown_event = Mock()
    shutdown_event.wait.return_value = True
    server = Mock()
    server.stop.side_effect = RuntimeError("Webserver hängt")
    startup_error_file = tmp_path / "startup-error.log"
    monkeypatch.setattr(windows_launcher.sys, "platform", "win32")
    monkeypatch.setenv(windows_launcher.NO_DIALOG_ENV, "1")
    monkeypatch.setattr(windows_launcher, "_create_windows_mutex", Mock(return_value=mutex))
    monkeypatch.setattr(windows_launcher, "_create_backend_mutex", Mock(return_value=backend_mutex))
    monkeypatch.setattr(windows_launcher, "_create_windows_shutdown_event", Mock(return_value=shutdown_event))
    monkeypatch.setattr(windows_launcher, "_runtime_file", lambda: tmp_path / "runtime.json")
    monkeypatch.setattr(windows_launcher, "_api_token_file", lambda: tmp_path / "api-token.txt")
    monkeypatch.setattr(windows_launcher, "_load_or_create_api_token", Mock(return_value="a" * 32))
    monkeypatch.setattr(windows_launcher, "_startup_error_file", lambda: startup_error_file)
    monkeypatch.setattr(windows_launcher, "_allow_kosit_processes", Mock())
    cancel_kosit = Mock(return_value=1)
    monkeypatch.setattr(windows_launcher, "_cancel_kosit_processes", cancel_kosit)
    monkeypatch.setattr(windows_launcher, "DesktopServer", Mock(return_value=server))
    monkeypatch.setattr(windows_launcher, "_run_tray", Mock())

    assert windows_launcher.main([]) == 1

    cancel_kosit.assert_called_once_with(windows_launcher.KOSIT_SHUTDOWN_TIMEOUT_SECONDS)
    server.stop.assert_called_once_with()
    shutdown_event.close.assert_called_once_with()
    backend_mutex.close.assert_called_once_with()
    mutex.close.assert_called_once_with()
    assert "RuntimeError: Webserver hängt" in startup_error_file.read_text(encoding="utf-8")


def test_main_stops_server_and_releases_handles_when_kosit_cancellation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutex = SimpleNamespace(already_exists=False, close=Mock())
    backend_mutex = SimpleNamespace(already_exists=False, close=Mock())
    shutdown_event = Mock()
    shutdown_event.wait.return_value = True
    server = Mock()
    monkeypatch.setattr(windows_launcher.sys, "platform", "win32")
    monkeypatch.setenv(windows_launcher.NO_DIALOG_ENV, "1")
    monkeypatch.setattr(windows_launcher, "_create_windows_mutex", Mock(return_value=mutex))
    monkeypatch.setattr(windows_launcher, "_create_backend_mutex", Mock(return_value=backend_mutex))
    monkeypatch.setattr(windows_launcher, "_create_windows_shutdown_event", Mock(return_value=shutdown_event))
    monkeypatch.setattr(windows_launcher, "_runtime_file", lambda: tmp_path / "runtime.json")
    monkeypatch.setattr(windows_launcher, "_api_token_file", lambda: tmp_path / "api-token.txt")
    monkeypatch.setattr(windows_launcher, "_load_or_create_api_token", Mock(return_value="a" * 32))
    monkeypatch.setattr(windows_launcher, "_startup_error_file", lambda: tmp_path / "startup-error.log")
    monkeypatch.setattr(windows_launcher, "_allow_kosit_processes", Mock())
    monkeypatch.setattr(
        windows_launcher,
        "_cancel_kosit_processes",
        Mock(side_effect=RuntimeError("KoSIT-Abbruch hängt")),
    )
    monkeypatch.setattr(windows_launcher, "DesktopServer", Mock(return_value=server))
    monkeypatch.setattr(windows_launcher, "_run_tray", Mock())

    assert windows_launcher.main([]) == 1

    server.stop.assert_called_once_with()
    shutdown_event.close.assert_called_once_with()
    backend_mutex.close.assert_called_once_with()
    mutex.close.assert_called_once_with()


def test_main_records_settings_load_error_after_windows_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutex = SimpleNamespace(already_exists=False, close=Mock())
    backend_mutex = SimpleNamespace(already_exists=False, close=Mock())
    shutdown_event = Mock()
    startup_error_file = tmp_path / "startup-error.log"
    monkeypatch.setattr(windows_launcher.sys, "platform", "win32")
    monkeypatch.setattr(windows_launcher, "_create_windows_mutex", Mock(return_value=mutex))
    monkeypatch.setattr(windows_launcher, "_create_backend_mutex", Mock(return_value=backend_mutex))
    monkeypatch.setattr(windows_launcher, "_create_windows_shutdown_event", Mock(return_value=shutdown_event))
    monkeypatch.setattr(windows_launcher, "_runtime_file", lambda: tmp_path / "runtime.json")
    monkeypatch.setattr(windows_launcher, "_api_token_file", lambda: tmp_path / "api-token.txt")
    monkeypatch.setattr(windows_launcher, "_load_or_create_api_token", Mock(return_value="a" * 32))
    monkeypatch.setattr(windows_launcher, "_startup_error_file", lambda: startup_error_file)
    monkeypatch.setattr(windows_launcher, "_configured_port", Mock(side_effect=RuntimeError("PORT ist ungültig")))
    show_message = Mock()
    monkeypatch.setattr(windows_launcher, "_show_windows_message", show_message)

    windows_launcher.main([])

    assert "RuntimeError: PORT ist ungültig" in startup_error_file.read_text(encoding="utf-8")
    show_message.assert_called_once_with(
        "Die Anwendung konnte nicht gestartet werden:\n\nPORT ist ungültig",
        error=True,
    )
    shutdown_event.close.assert_called_once_with()
    backend_mutex.close.assert_called_once_with()
    mutex.close.assert_called_once_with()


def test_main_reports_windows_start_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_launcher.sys, "platform", "win32")
    monkeypatch.setattr(windows_launcher, "_startup_error_file", lambda: tmp_path / "startup-error.log")
    monkeypatch.setattr(windows_launcher, "_create_windows_mutex", Mock(side_effect=RuntimeError("kaputt")))
    show_message = Mock()
    monkeypatch.setattr(windows_launcher, "_show_windows_message", show_message)

    result = windows_launcher.main([])

    assert result == 1
    show_message.assert_called_once_with(
        "Die Anwendung konnte nicht gestartet werden:\n\nkaputt",
        error=True,
    )
    assert "RuntimeError: kaputt" in (tmp_path / "startup-error.log").read_text(encoding="utf-8")


def test_main_returns_failure_when_service_owns_global_backend_mutex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutex = SimpleNamespace(already_exists=False, close=Mock())
    backend_mutex = SimpleNamespace(already_exists=True, close=Mock())
    monkeypatch.setattr(windows_launcher.sys, "platform", "win32")
    monkeypatch.setenv(windows_launcher.NO_DIALOG_ENV, "1")
    monkeypatch.setattr(windows_launcher, "_startup_error_file", lambda: tmp_path / "startup-error.log")
    monkeypatch.setattr(windows_launcher, "_runtime_file", lambda: tmp_path / "runtime.json")
    monkeypatch.setattr(windows_launcher, "_create_windows_mutex", Mock(return_value=mutex))
    monkeypatch.setattr(windows_launcher, "_create_backend_mutex", Mock(return_value=backend_mutex))

    assert windows_launcher.main(["--background"]) == 1
    assert "andere Betriebsart" in (tmp_path / "startup-error.log").read_text(encoding="utf-8")
    backend_mutex.close.assert_called_once_with()
    mutex.close.assert_called_once_with()


def test_main_rejects_non_windows_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_launcher.sys, "platform", "darwin")

    with pytest.raises(SystemExit, match="ausschließlich für Windows"):
        windows_launcher.main([])
