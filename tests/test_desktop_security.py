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
    consume_api_token_environment,
    desktop_bootstrap_url,
    validate_api_token,
)
from app.ui_contract import UI_REVISION_HEADER


def _desktop_client(
    token: str | None = "test-token",
    port: int | None = 8765,
    api_token: str | None = None,
    ui_revision: str | None = None,
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

    @desktop_app.api_route("/api/analyze", methods=["GET", "POST"])
    async def analyze():
        return {"done": True}

    @desktop_app.api_route("/api/xml", methods=["GET", "POST"])
    async def export_xml():
        return {"done": True}

    @desktop_app.api_route("/api/report", methods=["GET", "POST"])
    async def report():
        return {"done": True}

    @desktop_app.api_route("/api/report/pdf", methods=["GET", "POST"])
    async def pdf_report():
        return {"done": True}

    @desktop_app.get("/api/examples/{name}")
    async def example(name: str):
        return {"name": name}

    desktop_app.add_middleware(
        DesktopSessionMiddleware,
        token=token,
        port=port,
        api_token=api_token,
        ui_revision=ui_revision,
    )
    return TestClient(desktop_app, base_url=f"http://127.0.0.1:{port or 8765}")


def test_desktop_middleware_is_dormant_without_token() -> None:
    app = FastAPI()

    @app.get("/")
    async def index():
        return {"ok": True}

    @app.post("/api/analyze")
    async def analyze():
        return {"ok": True}

    app.add_middleware(DesktopSessionMiddleware, ui_revision="a" * 64)

    assert TestClient(app).get("/").status_code == 200
    assert TestClient(app).post("/api/analyze").status_code == 200


def test_health_is_available_before_bootstrap() -> None:
    client = _desktop_client()

    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 403


def test_expired_browser_session_requires_reopening_the_application() -> None:
    client = _desktop_client(ui_revision="a" * 64)
    client.cookies.set(DESKTOP_COOKIE_NAME, "token-der-vorherigen-laufzeit")

    response = client.post(
        "/api/analyze",
        headers={UI_REVISION_HEADER: "b" * 64},
    )

    assert response.status_code == 403
    assert response.json()["type"] == "desktop_session_error"
    assert "schließen Sie dieses Fenster" in response.json()["detail"]
    assert "öffnen Sie den E-Rechnungs-Prüfer erneut" in response.json()["detail"]


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
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/").status_code == 200


def test_bootstrap_uses_revisioned_entry_url_when_configured() -> None:
    revision = "a" * 64
    client = _desktop_client(ui_revision=revision)

    response = client.get("/desktop/bootstrap?token=test-token", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/?ui={revision}"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/analyze"),
        ("POST", "/api/xml"),
        ("POST", "/api/report"),
        ("POST", "/api/report/pdf"),
        ("GET", "/api/examples/cii"),
    ],
)
def test_browser_ui_api_requires_the_current_revision(method: str, path: str) -> None:
    revision = "a" * 64
    client = _desktop_client(ui_revision=revision)
    client.get("/desktop/bootstrap?token=test-token")

    missing = client.request(method, path)
    outdated = client.request(method, path, headers={UI_REVISION_HEADER: "b" * 64})
    accepted = client.request(method, path, headers={UI_REVISION_HEADER: revision})

    expected = {
        "detail": (
            "Die geöffnete Oberfläche gehört zu einer anderen Anwendungsversion. "
            "Bitte schließen Sie dieses Fenster und öffnen Sie den E-Rechnungs-Prüfer erneut."
        ),
        "type": "ui_version_mismatch",
    }
    assert missing.status_code == 409
    assert missing.json() == expected
    assert missing.headers[UI_REVISION_HEADER] == revision
    assert missing.headers["cache-control"] == "no-store"
    assert outdated.status_code == 409
    assert outdated.json() == expected
    assert accepted.status_code == 200


def test_ui_revision_does_not_weaken_session_origin_or_bearer_checks() -> None:
    revision = "a" * 64
    api_token = "api-token-abcdefghijklmnopqrstuvwxyz"
    client = _desktop_client(api_token=api_token, ui_revision=revision)
    revision_header = {UI_REVISION_HEADER: revision}

    unauthenticated = client.post("/api/analyze", headers=revision_header)
    bearer = client.post(
        "/api/analyze",
        headers={"authorization": f"Bearer {api_token}", UI_REVISION_HEADER: "outdated"},
    )
    invalid_bearer = client.post(
        "/api/analyze",
        headers={"authorization": "Bearer falsch", **revision_header},
    )

    client.get("/desktop/bootstrap?token=test-token")
    cross_origin = client.post(
        "/api/analyze",
        headers={"origin": "https://example.test", **revision_header},
    )

    assert unauthenticated.status_code == 403
    assert bearer.status_code == 200
    assert invalid_bearer.status_code == 403
    assert cross_origin.status_code == 403
    assert cross_origin.json()["type"] == "desktop_session_error"


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


def test_default_http_origin_without_explicit_port_is_valid_on_port_80() -> None:
    client = _desktop_client(port=80)
    client.get("/desktop/bootstrap?token=test-token")

    response = client.post("/api/action", headers={"origin": "http://127.0.0.1"})

    assert response.status_code == 200


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


def test_api_token_is_consumed_even_when_validation_fails() -> None:
    valid_environment = {API_TOKEN_ENV: "a" * 43, "UNCHANGED": "value"}

    assert consume_api_token_environment(valid_environment) == "a" * 43
    assert valid_environment == {"UNCHANGED": "value"}

    invalid_environment = {API_TOKEN_ENV: "too-short"}
    with pytest.raises(ValueError, match="mindestens 32"):
        consume_api_token_environment(invalid_environment)
    assert API_TOKEN_ENV not in invalid_environment


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


def test_actual_asgi_app_consumes_valid_api_token_during_import() -> None:
    environment = os.environ.copy()
    environment[API_TOKEN_ENV] = "x" * 43

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; import app.main; assert 'EINVOICE_API_TOKEN' not in os.environ",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
