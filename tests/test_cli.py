from __future__ import annotations

import sys
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest

from app import cli
from app.desktop_security import API_TOKEN_ENV, DESKTOP_PORT_ENV, DESKTOP_TOKEN_ENV


def test_cli_uses_configured_defaults_without_opening_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock()
    timer = Mock()
    browser_open = Mock()
    monkeypatch.setattr(sys, "argv", ["e-rechnung-pruefer"])
    monkeypatch.setattr(cli, "settings", SimpleNamespace(host="127.0.0.2", port=8181))
    monkeypatch.setattr(cli.uvicorn, "run", run)
    monkeypatch.setattr(cli, "Timer", timer)
    monkeypatch.setattr(cli.webbrowser, "open", browser_open)

    cli.main()

    run.assert_called_once_with("app.main:app", host="127.0.0.2", port=8181, reload=False)
    timer.assert_not_called()
    browser_open.assert_not_called()


def test_cli_forwards_explicit_server_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock()
    timer = Mock()
    monkeypatch.setattr(
        sys,
        "argv",
        ["e-rechnung-pruefer", "--host", "192.0.2.10", "--port", "9090", "--reload"],
    )
    monkeypatch.setattr(cli.uvicorn, "run", run)
    monkeypatch.setattr(cli, "Timer", timer)

    cli.main()

    run.assert_called_once_with("app.main:app", host="192.0.2.10", port=9090, reload=True)
    timer.assert_not_called()


@pytest.mark.parametrize(
    ("bind_host", "browser_host"),
    [
        ("0.0.0.0", "127.0.0.1"),
        ("::", "127.0.0.1"),
        ("::1", "[::1]"),
        ("2001:db8::1", "[2001:db8::1]"),
        ("localhost", "localhost"),
    ],
)
def test_cli_open_uses_reachable_browser_host_without_starting_thread(
    bind_host: str,
    browser_host: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock()
    browser_open = Mock()
    scheduled: list[tuple[float, Callable[[], object]]] = []

    class SynchronousTimer:
        def __init__(self, interval: float, callback: Callable[[], object]) -> None:
            scheduled.append((interval, callback))

        def start(self) -> None:
            scheduled[-1][1]()

    monkeypatch.setattr(
        sys,
        "argv",
        ["e-rechnung-pruefer", "--host", bind_host, "--port", "8765", "--open"],
    )
    monkeypatch.setattr(cli.uvicorn, "run", run)
    monkeypatch.setattr(cli, "Timer", SynchronousTimer)
    monkeypatch.setattr(cli.webbrowser, "open", browser_open)

    cli.main()

    assert [interval for interval, _callback in scheduled] == [1.2]
    browser_open.assert_called_once_with(f"http://{browser_host}:8765")
    run.assert_called_once_with("app.main:app", host=bind_host, port=8765, reload=False)


def test_cli_open_with_api_token_creates_separate_browser_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock()
    browser_open = Mock()
    scheduled: list[tuple[float, Callable[[], object]]] = []
    api_token = "a" * 43
    desktop_token = "browser-session-token"

    class SynchronousTimer:
        def __init__(self, interval: float, callback: Callable[[], object]) -> None:
            scheduled.append((interval, callback))

        def start(self) -> None:
            scheduled[-1][1]()

    monkeypatch.setattr(sys, "argv", ["e-rechnung-pruefer", "--port", "8765", "--open"])
    monkeypatch.setenv(API_TOKEN_ENV, api_token)
    monkeypatch.delenv(DESKTOP_TOKEN_ENV, raising=False)
    monkeypatch.delenv(DESKTOP_PORT_ENV, raising=False)
    monkeypatch.setattr(cli.secrets, "token_urlsafe", lambda _length: desktop_token)
    monkeypatch.setattr(cli.uvicorn, "run", run)
    monkeypatch.setattr(cli, "Timer", SynchronousTimer)
    monkeypatch.setattr(cli.webbrowser, "open", browser_open)

    cli.main()

    opened_url = browser_open.call_args.args[0]
    parsed_url = urlparse(opened_url)
    assert parsed_url.scheme == "http"
    assert parsed_url.hostname == "127.0.0.1"
    assert parsed_url.port == 8765
    assert parsed_url.path == "/desktop/bootstrap"
    assert parse_qs(parsed_url.query) == {"token": [desktop_token]}
    assert api_token not in opened_url
    assert cli.os.environ[DESKTOP_TOKEN_ENV] == desktop_token
    assert cli.os.environ[DESKTOP_PORT_ENV] == "8765"
    run.assert_called_once_with("app.main:app", host="127.0.0.1", port=8765, reload=False)


def test_cli_without_open_does_not_publish_browser_session_for_api_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock()
    browser_open = Mock()
    timer = Mock()
    monkeypatch.setattr(sys, "argv", ["e-rechnung-pruefer"])
    monkeypatch.setenv(API_TOKEN_ENV, "a" * 43)
    monkeypatch.delenv(DESKTOP_TOKEN_ENV, raising=False)
    monkeypatch.delenv(DESKTOP_PORT_ENV, raising=False)
    monkeypatch.setattr(cli.uvicorn, "run", run)
    monkeypatch.setattr(cli, "Timer", timer)
    monkeypatch.setattr(cli.webbrowser, "open", browser_open)

    cli.main()

    assert DESKTOP_TOKEN_ENV not in cli.os.environ
    assert DESKTOP_PORT_ENV not in cli.os.environ
    timer.assert_not_called()
    browser_open.assert_not_called()
    run.assert_called_once_with("app.main:app", host="127.0.0.1", port=8080, reload=False)
