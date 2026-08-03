from __future__ import annotations

from app.analyzer import analyze_bytes


def test_cii_invoice_is_fully_parsed(cii_path):
    result = analyze_bytes(
        cii_path.read_bytes(),
        cii_path.name,
        "application/xml",
        run_official_validation=False,
    )

    assert result["schema_version"] == 2
    assert result["capabilities"]["syntax"] == "CII"
    assert result["document"]["id"] == "CII-DEMO-1"
    assert result["document"]["issue_date"] == "2026-07-15"
    assert result["periods"]["delivery"] is None
    assert result["delivery"]["actual_date"] == "2026-07-20"
    assert result["payment"]["due_date"] == "2026-07-18"
    assert result["parties"]["seller"]["legal_name"].startswith("Beispiel Lieferant")
    assert result["parties"]["buyer"]["legal_name"] == "Beispiel Kunde AG"
    assert len(result["lines"]) == 6
    assert result["lines"][5]["price"]["base_quantity"]["value"] == "100"
    assert result["lines"][5]["net_amount"]["value"] == "640.00"
    assert result["totals"]["payable"]["value"] == "13820.42"


def test_cii_validation_finds_expected_date_and_account_notes(cii_path):
    result = analyze_bytes(
        cii_path.read_bytes(),
        cii_path.name,
        "application/xml",
        run_official_validation=False,
    )
    internal = result["assessment"]["internal"]
    findings = {item["rule"]["id"]: item for item in internal["findings"]}

    assert internal["status"] == "attention"
    assert "DATE-002" in findings
    assert "LINE-009" in findings
    assert "PAY-004" in findings
    assert internal["counts"]["error"] == 0
    assert result["assessment"]["processing"]["status"] == "complete"
    assert result["assessment"]["official"]["status"] == "not-requested"


def test_technical_appendix_contains_values_attributes_and_namespaces(cii_path):
    source = cii_path.read_bytes()
    result = analyze_bytes(source, cii_path.name, "application/xml", run_official_validation=False)
    fields = result["technical"]["fields"]

    assert any(field["kind"] == "namespace" and field["name"] == "xmlns:rsm" for field in fields)
    assert any(
        field["kind"] == "attribute" and field["name"] == "unitCode" and field["value"] == "C62" for field in fields
    )
    assert any(
        field["kind"] == "element" and field["name"] == "GrandTotalAmount" and field["value"] == "13820.42"
        for field in fields
    )
    assert result["technical"]["source_xml"] == source.decode("utf-8")
