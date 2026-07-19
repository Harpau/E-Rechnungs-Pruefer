from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from lxml import html

from app.main import app

client = TestClient(app)


def _official_checkbox(page: str):
    document = html.fromstring(page)
    matches = document.xpath("//input[@id='official-checkbox']")
    assert len(matches) == 1
    return matches[0]


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
    analysis = response.json()
    assert analysis["document"]["id"] == "CII-DEMO-1"
    assert analysis["validation"]["official"]["executed"] is False
    assert analysis["validation"]["official"]["findings"] == []

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


def test_index_disables_official_validation_when_kosit_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        "app.main.KositValidator.configuration_state",
        lambda _self: {"configured": False, "problems": ["KoSIT-Testkonfiguration fehlt."]},
    )

    response = client.get("/")
    checkbox = _official_checkbox(response.text)

    assert response.status_code == 200
    assert "disabled" in checkbox.attrib
    assert "checked" not in checkbox.attrib
    assert "KoSIT-Testkonfiguration fehlt." in response.text


def test_index_enables_official_validation_when_kosit_is_configured(monkeypatch):
    monkeypatch.setattr(
        "app.main.KositValidator.configuration_state",
        lambda _self: {"configured": True, "problems": []},
    )

    response = client.get("/")
    checkbox = _official_checkbox(response.text)

    assert response.status_code == 200
    assert "disabled" not in checkbox.attrib
    assert "checked" in checkbox.attrib


def test_interactive_requests_ignore_disabled_official_checkbox():
    script = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(encoding="utf-8")

    assert "return checkbox.checked && !checkbox.disabled;" in script
    assert script.count("officialValidationRequested() ? 'true' : 'false'") == 2
    assert "$('#official-checkbox').checked ? 'true' : 'false'" not in script
