from __future__ import annotations

from fastapi.testclient import TestClient

from app.desktop_security import DESKTOP_COOKIE_NAME, DesktopSessionMiddleware, OneTimeBrowserSessions
from app.main import app


def _service_api_client(token: str) -> TestClient:
    protected = DesktopSessionMiddleware(
        app,
        port=8080,
        api_token=token,
        browser_sessions=OneTimeBrowserSessions(),
    )
    return TestClient(protected, base_url="http://127.0.0.1:8080")


def test_service_api_requires_correct_bearer_and_keeps_health_loopback_only(cii_path, monkeypatch) -> None:
    token = "s" * 43
    client = _service_api_client(token)
    payload = cii_path.read_bytes()
    request = {
        "files": {"file": (cii_path.name, payload, "application/xml")},
        "data": {"official": "false"},
    }
    monkeypatch.setattr(
        "app.analyzer.KositValidator.validate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("KoSIT darf nicht aufgerufen werden")),
    )

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health", headers={"host": "example.test"}).status_code == 403
    assert client.post("/api/analyze", **request).status_code == 403
    assert client.post("/api/analyze", headers={"authorization": "Bearer falsch"}, **request).status_code == 403

    accepted = client.post(
        "/api/analyze",
        headers={"authorization": f"Bearer {token}"},
        **request,
    )
    assert accepted.status_code == 200
    official = accepted.json()["assessment"]["official"]
    assert official["status"] == "not-requested"
    assert official["requested"] is False
    assert official["executed"] is False


def test_service_api_pdf_and_xml_contract_with_bearer(cii_path) -> None:
    token = "s" * 43
    client = _service_api_client(token)
    headers = {"authorization": f"Bearer {token}"}
    payload = cii_path.read_bytes()

    pdf = client.post(
        "/api/report/pdf",
        headers=headers,
        files={"file": (cii_path.name, payload, "application/xml")},
        data={"official": "false"},
    )
    exported = client.post(
        "/api/xml",
        headers=headers,
        files={"file": (cii_path.name, payload, "application/xml")},
    )

    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF-")
    assert pdf.headers["x-einvoice-conformity-status"] == "not-requested"
    assert exported.status_code == 200
    assert exported.content == payload


def test_interactive_source_browser_and_bearer_api_work_together(cii_path) -> None:
    api_token = "a" * 43
    desktop_token = "separate-browser-session"
    protected = DesktopSessionMiddleware(
        app,
        token=desktop_token,
        port=8080,
        api_token=api_token,
    )
    browser = TestClient(protected, base_url="http://127.0.0.1:8080")
    payload = cii_path.read_bytes()
    analyze_request = {
        "files": {"file": (cii_path.name, payload, "application/xml")},
        "data": {"official": "false"},
    }

    assert browser.get("/api/examples/cii").status_code == 403
    assert browser.post("/api/analyze", **analyze_request).status_code == 403

    bootstrap = browser.get(
        f"/desktop/bootstrap?token={desktop_token}",
        follow_redirects=False,
    )
    assert bootstrap.status_code == 303
    assert "HttpOnly" in bootstrap.headers["set-cookie"]
    assert "SameSite=strict" in bootstrap.headers["set-cookie"]
    assert browser.cookies.get(DESKTOP_COOKIE_NAME) == desktop_token
    assert browser.get("/api/examples/cii").status_code == 200
    assert browser.post("/api/analyze", **analyze_request).status_code == 200

    automation = TestClient(protected, base_url="http://127.0.0.1:8080")
    bearer = {"authorization": f"Bearer {api_token}"}
    assert automation.get("/api/examples/cii", headers=bearer).status_code == 200
    assert automation.post("/api/analyze", headers=bearer, **analyze_request).status_code == 200
    assert automation.get("/", headers=bearer).status_code == 403
