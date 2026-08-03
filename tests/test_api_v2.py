from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_analyze_returns_only_closed_schema_two_contract(cii_path) -> None:
    response = client.post(
        "/api/analyze",
        files={"file": (cii_path.name, cii_path.read_bytes(), "application/xml")},
        data={"official": "false"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "schema_version",
        "document",
        "profile",
        "capabilities",
        "parties",
        "roles",
        "periods",
        "delivery",
        "references",
        "lines",
        "allowances_charges",
        "tax",
        "totals",
        "payment",
        "assessment",
        "source",
        "technical",
        "runtime",
    }
    assert payload["schema_version"] == 2
    assert payload["document"]["id"] == "CII-DEMO-1"
    assert payload["document"]["type"]["code"]["value"] == "380"
    assert payload["capabilities"]["syntax"] == "CII"
    assert payload["parties"]["seller"]["legal_name"].startswith("Beispiel Lieferant")
    assert payload["assessment"]["official"]["status"] == "not-requested"
    assert "validation" not in payload
    assert "seller" not in payload
    assert "payment" not in payload["document"]


def test_openapi_exposes_schema_two_response_and_only_new_report_headers() -> None:
    document = client.get("/openapi.json").json()
    analyze_response = document["paths"]["/api/analyze"]["post"]["responses"]["200"]

    assert analyze_response["content"]["application/json"]["schema"]["$ref"].endswith("/AnalysisResponse")

    expected_headers = {
        "X-Einvoice-Analysis-Schema",
        "X-Einvoice-Syntax",
        "X-Einvoice-Conformity-Status",
        "X-Einvoice-Internal-Status",
        "X-Einvoice-Processing-Status",
        "X-Einvoice-Report-Scope",
    }
    for path in ("/api/report", "/api/report/pdf"):
        headers = document["paths"][path]["post"]["responses"]["200"]["headers"]
        assert set(headers) == expected_headers
        assert "X-Einvoice-Validation-Status" not in headers
        assert "X-Einvoice-Official-Status" not in headers


def test_report_endpoints_publish_separate_status_axes(cii_path) -> None:
    request = {
        "files": {"file": (cii_path.name, cii_path.read_bytes(), "application/xml")},
        "data": {"official": "false"},
    }

    html = client.post("/api/report", **request)
    pdf = client.post("/api/report/pdf", **request)

    assert html.status_code == 200
    assert pdf.status_code == 200
    for response in (html, pdf):
        assert response.headers["x-einvoice-analysis-schema"] == "2"
        assert response.headers["x-einvoice-syntax"] == "CII"
        assert response.headers["x-einvoice-conformity-status"] == "not-requested"
        assert response.headers["x-einvoice-internal-status"] in {"clear", "attention", "errors"}
        assert response.headers["x-einvoice-processing-status"] in {"complete", "limited"}
        assert response.headers["x-einvoice-report-scope"] == "readable"
        assert "x-einvoice-validation-status" not in response.headers
        assert "x-einvoice-official-status" not in response.headers


def test_health_exposes_public_component_versions_without_local_paths() -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["analysis_schema_version"] == 2
    assert payload["kosit"]["components"]["validator"] == "1.6.2"
    assert payload["kosit"]["components"]["xrechnung"] == "3.0.2"
    assert payload["kosit"]["components"]["xrechnung_configuration"] == "2026-01-31"
    assert payload["kosit"]["components"]["cen_en16931"] == "1.3.15"
    serialized = response.text
    assert "/Users/" not in serialized
    assert "\\Users\\" not in serialized
