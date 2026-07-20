from __future__ import annotations

import json
import os
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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


def test_reserve_loopback_socket_uses_an_available_local_port(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = Mock()
    listener.getsockname.return_value = ("127.0.0.1", 8765)
    socket_factory = Mock(return_value=listener)
    monkeypatch.setattr(windows_launcher.socket, "socket", socket_factory)

    actual_listener, port = windows_launcher._reserve_loopback_socket()

    socket_factory.assert_called_once_with(windows_launcher.socket.AF_INET, windows_launcher.socket.SOCK_STREAM)
    listener.bind.assert_called_once_with(("127.0.0.1", 0))
    listener.setblocking.assert_called_once_with(False)
    assert actual_listener is listener
    assert port == 8765


def test_reserve_loopback_socket_closes_after_bind_error(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = Mock()
    listener.bind.side_effect = OSError("belegt")
    monkeypatch.setattr(windows_launcher.socket, "socket", Mock(return_value=listener))

    with pytest.raises(OSError, match="belegt"):
        windows_launcher._reserve_loopback_socket()

    listener.close.assert_called_once_with()


def test_health_check_handles_success_and_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    urlopen = Mock(return_value=response)
    monkeypatch.setattr(windows_launcher.urllib.request, "urlopen", urlopen)

    assert windows_launcher._health_is_ready(8765, timeout=1.25) is True
    urlopen.assert_called_once_with("http://127.0.0.1:8765/api/health", timeout=1.25)

    urlopen.side_effect = urllib.error.URLError("nicht erreichbar")
    assert windows_launcher._health_is_ready(8765) is False


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
    monkeypatch.setattr(windows_launcher, "_reserve_loopback_socket", lambda: (listener, 8765))
    monkeypatch.setattr(windows_launcher.secrets, "token_urlsafe", lambda _length: "x" * 32)
    monkeypatch.setattr(windows_launcher, "_health_is_ready", lambda _port: True)
    monkeypatch.setattr(windows_launcher.uvicorn, "Config", Mock(return_value=config))
    monkeypatch.setattr(windows_launcher.uvicorn, "Server", Mock(return_value=fake_uvicorn_server))
    browser_open = Mock(return_value=True)
    monkeypatch.setattr(windows_launcher.webbrowser, "open", browser_open)
    monkeypatch.delenv(windows_launcher.DESKTOP_TOKEN_ENV, raising=False)
    monkeypatch.delenv(windows_launcher.DESKTOP_PORT_ENV, raising=False)
    runtime_file = tmp_path / "runtime.json"
    server = windows_launcher.DesktopServer(runtime_file)

    server.start()

    assert windows_launcher.os.environ[windows_launcher.DESKTOP_TOKEN_ENV] == "x" * 32
    assert windows_launcher.os.environ[windows_launcher.DESKTOP_PORT_ENV] == "8765"
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


def test_main_reopens_existing_windows_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mutex = SimpleNamespace(already_exists=True, close=Mock())
    runtime_file = tmp_path / "runtime.json"
    monkeypatch.setattr(windows_launcher.sys, "platform", "win32")
    monkeypatch.setattr(windows_launcher, "_create_windows_mutex", Mock(return_value=mutex))
    monkeypatch.setattr(windows_launcher, "_runtime_file", lambda: runtime_file)
    open_existing = Mock(return_value=True)
    monkeypatch.setattr(windows_launcher, "_open_existing_instance", open_existing)

    windows_launcher.main()

    open_existing.assert_called_once_with(runtime_file)
    mutex.close.assert_called_once_with()


def test_main_starts_and_stops_first_windows_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mutex = SimpleNamespace(already_exists=False, close=Mock())
    server = Mock()
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text("alt", encoding="utf-8")
    monkeypatch.setattr(windows_launcher.sys, "platform", "win32")
    monkeypatch.setattr(windows_launcher, "_create_windows_mutex", Mock(return_value=mutex))
    monkeypatch.setattr(windows_launcher, "_runtime_file", lambda: runtime_file)
    monkeypatch.setattr(windows_launcher, "DesktopServer", Mock(return_value=server))
    run_tray = Mock()
    monkeypatch.setattr(windows_launcher, "_run_tray", run_tray)

    windows_launcher.main()

    server.start.assert_called_once_with()
    run_tray.assert_called_once_with(server)
    server.stop.assert_called_once_with()
    mutex.close.assert_called_once_with()


def test_main_reports_windows_start_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_launcher.sys, "platform", "win32")
    monkeypatch.setattr(windows_launcher, "_create_windows_mutex", Mock(side_effect=RuntimeError("kaputt")))
    show_message = Mock()
    monkeypatch.setattr(windows_launcher, "_show_windows_message", show_message)

    windows_launcher.main()

    show_message.assert_called_once_with(
        "Die Anwendung konnte nicht gestartet werden:\n\nkaputt",
        error=True,
    )


def test_main_rejects_non_windows_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_launcher.sys, "platform", "darwin")

    with pytest.raises(SystemExit, match="ausschließlich für Windows"):
        windows_launcher.main()
