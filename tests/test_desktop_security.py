from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.desktop_security import (
    API_TOKEN_ENV,
    DESKTOP_COOKIE_NAME,
    DesktopSessionMiddleware,
    desktop_bootstrap_url,
    validate_api_token,
)


def _desktop_client(
    token: str | None = "test-token", port: int | None = 8765, api_token: str | None = None
) -> TestClient:
    desktop_app = FastAPI()

    @desktop_app.get("/")
    async def index():
        return {"ok": True}

    @desktop_app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @desktop_app.post("/api/action")
    async def action():
        return {"done": True}

    desktop_app.add_middleware(DesktopSessionMiddleware, token=token, port=port, api_token=api_token)
    return TestClient(desktop_app, base_url=f"http://127.0.0.1:{port or 8765}")


def test_desktop_middleware_is_dormant_without_token() -> None:
    app = FastAPI()

    @app.get("/")
    async def index():
        return {"ok": True}

    app.add_middleware(DesktopSessionMiddleware)

    assert TestClient(app).get("/").status_code == 200


def test_health_is_available_before_bootstrap() -> None:
    client = _desktop_client()

    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 403


def test_health_still_requires_an_allowed_host() -> None:
    client = _desktop_client()

    response = client.get("/api/health", headers={"host": "example.test"})
    wrong_port = client.get("/api/health", headers={"host": "127.0.0.1:9999"})

    assert response.status_code == 403
    assert response.json()["detail"] == "Der lokale Hostname ist nicht zulässig."
    assert wrong_port.status_code == 403


def test_bootstrap_sets_strict_session_cookie_and_redirects() -> None:
    client = _desktop_client()

    response = client.get("/desktop/bootstrap?token=test-token", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert f"{DESKTOP_COOKIE_NAME}=test-token" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert client.get("/").status_code == 200


def test_bootstrap_rejects_invalid_token_and_host() -> None:
    client = _desktop_client()

    assert client.get("/desktop/bootstrap?token=falsch").status_code == 403
    assert (
        client.get(
            "/desktop/bootstrap?token=test-token",
            headers={"host": "example.test"},
        ).status_code
        == 403
    )


def test_unsafe_request_rejects_cross_origin_but_accepts_matching_origin() -> None:
    client = _desktop_client()
    client.get("/desktop/bootstrap?token=test-token")

    rejected = client.post("/api/action", headers={"origin": "https://example.test"})
    accepted = client.post("/api/action", headers={"origin": "http://127.0.0.1:8765"})

    assert rejected.status_code == 403
    assert rejected.json()["type"] == "desktop_session_error"
    assert accepted.status_code == 200


def test_bearer_token_authorizes_only_api_requests() -> None:
    client = _desktop_client(api_token="api-token-abcdefghijklmnopqrstuvwxyz")

    accepted = client.post(
        "/api/action",
        headers={"authorization": "Bearer api-token-abcdefghijklmnopqrstuvwxyz"},
    )
    rejected = client.post("/api/action", headers={"authorization": "Bearer falsch"})
    browser_page = client.get("/", headers={"authorization": "Bearer api-token-abcdefghijklmnopqrstuvwxyz"})

    assert accepted.status_code == 200
    assert rejected.status_code == 403
    assert rejected.json()["type"] == "desktop_session_error"
    assert browser_page.status_code == 403


def test_bearer_authorization_is_unavailable_without_configured_api_token() -> None:
    client = _desktop_client()

    response = client.post("/api/action", headers={"authorization": "Bearer api-token-abcdefghijklmnopqrstuvwxyz"})

    assert response.status_code == 403


def test_api_token_is_enforced_without_desktop_token() -> None:
    client = _desktop_client(token=None, api_token="api-token-abcdefghijklmnopqrstuvwxyz")

    accepted = client.post(
        "/api/action",
        headers={"authorization": "Bearer api-token-abcdefghijklmnopqrstuvwxyz"},
    )
    missing = client.post("/api/action")
    rejected = client.post("/api/action", headers={"authorization": "Bearer falsch"})

    assert accepted.status_code == 200
    assert missing.status_code == 403
    assert missing.json()["detail"] == "Das API-Zugriffstoken fehlt."
    assert rejected.status_code == 403
    assert client.get("/").status_code == 200


def test_api_only_mode_accepts_the_actual_loopback_port_without_desktop_port() -> None:
    client = _desktop_client(token=None, port=None, api_token="api-token-abcdefghijklmnopqrstuvwxyz")

    response = client.post(
        "/api/action",
        headers={"authorization": "Bearer api-token-abcdefghijklmnopqrstuvwxyz"},
    )

    assert response.status_code == 200


def test_non_ascii_tokens_are_compared_without_server_error() -> None:
    client = _desktop_client(token="gültiges-token")

    accepted = client.get("/desktop/bootstrap?token=g%C3%BCltiges-token", follow_redirects=False)
    rejected = client.get("/desktop/bootstrap?token=ung%C3%BCltig", follow_redirects=False)

    assert accepted.status_code == 303
    assert rejected.status_code == 403
    assert client.get("/").status_code == 200


def test_desktop_bootstrap_url_encodes_token() -> None:
    assert desktop_bootstrap_url(8765, "a token/+?") == (
        "http://127.0.0.1:8765/desktop/bootstrap?token=a+token%2F%2B%3F"
    )


@pytest.mark.parametrize(
    "token",
    [
        "",
        "a" * 31,
        "a" * 31 + "/",
        "a" * 31 + "+",
        "ä" * 32,
        "a" * 32 + " ",
    ],
)
def test_api_token_contract_rejects_weak_or_non_url_safe_values(token: str) -> None:
    with pytest.raises(ValueError, match="mindestens 32 URL-sichere ASCII-Zeichen"):
        validate_api_token(token)


def test_actual_asgi_app_rejects_invalid_api_token_during_import() -> None:
    environment = os.environ.copy()
    environment[API_TOKEN_ENV] = "x"

    completed = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "EINVOICE_API_TOKEN muss mindestens 32 URL-sichere ASCII-Zeichen enthalten" in completed.stderr
