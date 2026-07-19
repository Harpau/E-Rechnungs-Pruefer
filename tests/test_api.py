from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_analyze_and_report_endpoints(cii_path):
    payload = cii_path.read_bytes()
    response = client.post(
        "/api/analyze",
        files={"file": (cii_path.name, payload, "application/xml")},
        data={"official": "false"},
    )
    assert response.status_code == 200
    assert response.json()["document"]["id"] == "CII-DEMO-1"

    report = client.post(
        "/api/report",
        files={"file": (cii_path.name, payload, "application/xml")},
        data={"official": "false"},
    )
    assert report.status_code == 200
    assert "Rechnungspositionen" in report.text
    assert "Alle XML-Elemente" in report.text
    assert "13820.42" in report.text

    xml_export = client.post(
        "/api/xml",
        files={"file": (cii_path.name, payload, "application/xml")},
    )
    assert xml_export.status_code == 200
    assert xml_export.content == payload
    assert "application/xml" in xml_export.headers["content-type"]


def test_index_and_examples_are_available():
    index = client.get("/")
    assert index.status_code == 200
    assert "E‑Rechnungs‑Viewer" in index.text

    example = client.get("/api/examples/ubl")
    assert example.status_code == 200
    assert b"<Invoice" in example.content
