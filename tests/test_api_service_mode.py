from __future__ import annotations

from fastapi.testclient import TestClient

from app.desktop_security import DesktopSessionMiddleware, OneTimeBrowserSessions
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
    assert accepted.json()["validation"]["official"]["executed"] is False


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
    assert pdf.headers["x-einvoice-official-status"] == "not-requested"
    assert exported.status_code == 200
    assert exported.content == payload
