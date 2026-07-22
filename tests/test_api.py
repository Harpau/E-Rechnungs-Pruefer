from __future__ import annotations

import threading
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from lxml import html
from pypdf import PdfReader

import app.main as main_module
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
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["version"] == main_module.__version__
    assert set(payload) == {"status", "version", "kosit"}
    assert set(payload["kosit"]) == {"configured"}
    assert isinstance(payload["kosit"]["configured"], bool)


def test_health_endpoint_does_not_expose_kosit_configuration_details(monkeypatch):
    monkeypatch.setattr(
        main_module.KositValidator,
        "configuration_state",
        lambda _self: {
            "configured": False,
            "problems": ["Java fehlt."],
            "jar": "/vertraulich/validator.jar",
            "scenarios": ["/vertraulich/scenarios.xml"],
            "repositories": ["/vertraulich/repository"],
        },
    )

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": main_module.__version__,
        "kosit": {"configured": False},
    }


def test_analysis_capacity_returns_retryable_503_without_queueing(cii_path, monkeypatch):
    occupied_slots = threading.BoundedSemaphore(1)
    occupied_slots.acquire()
    monkeypatch.setattr(main_module, "_analysis_slots", occupied_slots)

    try:
        response = client.post(
            "/api/analyze",
            files={"file": (cii_path.name, cii_path.read_bytes(), "application/xml")},
            data={"official": "false"},
        )
    finally:
        occupied_slots.release()

    assert response.status_code == 503
    assert response.headers["retry-after"] == str(main_module._ANALYSIS_RETRY_AFTER_SECONDS)
    assert response.json() == {
        "detail": "Der Prüfdienst ist ausgelastet. Bitte versuchen Sie es später erneut.",
        "type": "analysis_capacity_error",
    }


def test_openapi_documents_report_status_headers():
    response = client.get("/openapi.json")

    assert response.status_code == 200
    document = response.json()
    headers = document["paths"]["/api/report"]["post"]["responses"]["200"]["headers"]
    assert headers["X-Einvoice-Syntax"]["schema"]["enum"] == ["CII", "UBL", "UNKNOWN"]
    assert headers["X-Einvoice-Validation-Status"]["schema"]["enum"] == ["ok", "warning", "invalid"]
    assert headers["X-Einvoice-Official-Status"]["schema"]["enum"] == [
        "accepted",
        "rejected",
        "not-requested",
        "unavailable",
        "indeterminate",
    ]
    pdf_response = document["paths"]["/api/report/pdf"]["post"]["responses"]["200"]
    assert pdf_response["headers"] == headers
    assert set(pdf_response["content"]) == {"application/pdf"}
    for path in ("/api/analyze", "/api/report", "/api/report/pdf"):
        busy = document["paths"][path]["post"]["responses"]["503"]
        assert busy["headers"]["Retry-After"]["schema"] == {
            "type": "integer",
            "maximum": 600.0,
            "minimum": 5.0,
        }


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
    assert report.headers["x-einvoice-syntax"] == "CII"
    assert report.headers["x-einvoice-validation-status"] == "warning"
    assert report.headers["x-einvoice-official-status"] == "not-requested"
    assert report.headers["content-disposition"] == 'inline; filename="E-Rechnungs-Pruefbericht.html"'
    report_headers = "\n".join(f"{name}: {value}" for name, value in report.headers.items())
    assert analysis["document"]["id"] not in report_headers
    assert cii_path.name not in report_headers

    xml_export = client.post(
        "/api/xml",
        files={"file": (cii_path.name, payload, "application/xml")},
    )
    assert xml_export.status_code == 200
    assert xml_export.content == payload
    assert "application/xml" in xml_export.headers["content-type"]


def test_pdf_report_endpoint_is_self_contained_and_keeps_status_contract(cii_path):
    payload = cii_path.read_bytes()
    html_report = client.post(
        "/api/report",
        files={"file": (cii_path.name, payload, "application/xml")},
        data={"official": "false"},
    )
    pdf_report = client.post(
        "/api/report/pdf",
        files={"file": (cii_path.name, payload, "application/xml")},
        data={"official": "false"},
    )

    assert pdf_report.status_code == 200
    assert pdf_report.headers["content-type"] == "application/pdf"
    assert pdf_report.headers["content-disposition"] == 'attachment; filename="E-Rechnungs-Pruefbericht.pdf"'
    assert pdf_report.content.startswith(b"%PDF-")
    assert pdf_report.content.rstrip().endswith(b"%%EOF")
    for name in (
        "x-einvoice-syntax",
        "x-einvoice-validation-status",
        "x-einvoice-official-status",
    ):
        assert pdf_report.headers[name] == html_report.headers[name]

    response_headers = "\n".join(f"{name}: {value}" for name, value in pdf_report.headers.items())
    assert "CII-DEMO-1" not in response_headers
    assert cii_path.name not in response_headers

    document = PdfReader(BytesIO(pdf_report.content))
    extracted = "\n".join(page.extract_text() or "" for page in document.pages)
    assert len(document.pages) > 1
    assert "CII-DEMO-1" in extracted
    assert "E-Rechnungs-Viewer & Prüfer" in extracted
    assert "Rechnungspositionen" in extracted
    assert "13.820,42 EUR" in extracted
    assert "Prüfbericht" in extracted
    assert "Technischer Anhang" in extracted
    assert "vollständige Original" in extracted
    assert "Seite 2" in extracted
    assert document.metadata is not None
    assert document.metadata.title == "E-Rechnungs-Prüfbericht"
    assert "CII-DEMO-1" not in "\n".join(str(value) for value in document.metadata.values())


def test_report_headers_identify_supported_ubl_and_unknown_xml(ubl_path):
    ubl = client.post(
        "/api/report",
        files={"file": (ubl_path.name, ubl_path.read_bytes(), "application/xml")},
        data={"official": "false"},
    )
    unknown = client.post(
        "/api/report",
        files={"file": ("anderes-dokument.xml", b"<OtherDocument />", "application/xml")},
        data={"official": "false"},
    )

    assert ubl.status_code == 200
    assert ubl.headers["x-einvoice-syntax"] == "UBL"
    assert ubl.headers["x-einvoice-validation-status"] == "ok"
    assert unknown.status_code == 200
    assert unknown.headers["x-einvoice-syntax"] == "UNKNOWN"
    assert unknown.headers["x-einvoice-validation-status"] == "invalid"


@pytest.mark.parametrize(
    ("official_result", "expected_status", "expected_validation"),
    [
        (
            {"configured": True, "executed": True, "accepted": True, "findings": []},
            "accepted",
            "warning",
        ),
        (
            {"configured": True, "executed": True, "accepted": False, "findings": []},
            "rejected",
            "invalid",
        ),
        (
            {"configured": False, "executed": False, "accepted": None, "findings": []},
            "unavailable",
            "warning",
        ),
        (
            {"configured": True, "executed": False, "accepted": None, "findings": []},
            "indeterminate",
            "warning",
        ),
    ],
)
def test_report_header_maps_official_status(
    monkeypatch,
    cii_path,
    official_result: dict[str, Any],
    expected_status: str,
    expected_validation: str,
):
    result = {
        "problems": [],
        "summary": "Synthetischer KoSIT-Status für den API-Test.",
        "raw_report": None,
        **official_result,
    }
    if not result["executed"]:
        result["findings"] = [
            {
                "id": "KOSIT-TEST",
                "severity": "warning",
                "title": "KoSIT-Prüfung wurde nicht ausgeführt",
                "message": "Synthetischer technischer Teststatus.",
                "location": None,
                "actual": None,
                "expected": None,
                "source": "KoSIT-Anbindung",
            }
        ]
    monkeypatch.setattr("app.analyzer.KositValidator.validate", lambda _self, _xml, _filename: result)

    response = client.post(
        "/api/report",
        files={"file": (cii_path.name, cii_path.read_bytes(), "application/xml")},
        data={"official": "true"},
    )

    assert response.status_code == 200
    assert response.headers["x-einvoice-official-status"] == expected_status
    assert response.headers["x-einvoice-validation-status"] == expected_validation


def test_pdf_xml_export_preserves_selected_attachment_bytes(ubl_path, pdf_bytes_factory):
    payload = ubl_path.read_bytes()
    pdf = pdf_bytes_factory(
        ("invoice.xml", b"<lower-priority-candidate />"),
        ("factur-x.xml", payload),
        ("notes.txt", b"Synthetic test attachment."),
    )

    response = client.post(
        "/api/xml",
        files={"file": ("hybrid-rechnung.pdf", pdf, "application/pdf")},
    )

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-disposition"] == 'attachment; filename="factur-x.xml"'


def test_pdf_xml_export_rejects_decoded_attachment_over_limit(monkeypatch, pdf_bytes_factory):
    payload = b"<Invoice>" + (b"x" * 200_000) + b"</Invoice>"
    pdf = pdf_bytes_factory(("invoice.xml", payload), compress_attachments=True)
    assert len(pdf) < 5_000 < len(payload)
    monkeypatch.setattr(main_module, "settings", replace(main_module.settings, max_upload_bytes=5_000))

    response = client.post(
        "/api/xml",
        files={"file": ("komprimierte-xml.pdf", pdf, "application/pdf")},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Eine eingebettete XML-Datei überschreitet die zulässige Größenbegrenzung.",
        "type": "invoice_input_error",
    }


def test_api_rejects_raw_upload_over_limit(monkeypatch):
    monkeypatch.setattr(main_module, "settings", replace(main_module.settings, max_upload_bytes=10))

    response = client.post(
        "/api/xml",
        files={"file": ("zu-gross.xml", b"<Invoice />", "application/xml")},
    )

    assert response.status_code == 422
    assert response.json()["type"] == "invoice_input_error"
    assert "größer als die zulässigen" in response.json()["detail"]


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
