from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.desktop_security import DESKTOP_COOKIE_NAME, DesktopSessionMiddleware, desktop_bootstrap_url


def _desktop_client(token: str = "test-token", port: int = 8765) -> TestClient:
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

    desktop_app.add_middleware(DesktopSessionMiddleware, token=token, port=port)
    return TestClient(desktop_app, base_url=f"http://127.0.0.1:{port}")


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


def test_desktop_bootstrap_url_encodes_token() -> None:
    assert desktop_bootstrap_url(8765, "a token/+?") == (
        "http://127.0.0.1:8765/desktop/bootstrap?token=a+token%2F%2B%3F"
    )
