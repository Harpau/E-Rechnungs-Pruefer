from __future__ import annotations

from app.analyzer import analyze_bytes


def test_cii_invoice_is_fully_parsed(cii_path):
    result = analyze_bytes(
        cii_path.read_bytes(),
        cii_path.name,
        "application/xml",
        run_official_validation=False,
    )

    assert result["document"]["syntax"] == "CII"
    assert result["document"]["id"] == "CII-DEMO-1"
    assert result["document"]["issue_date"] == "2026-07-15"
    assert result["document"]["delivery_date"] == "2026-07-20"
    assert result["document"]["due_date"] == "2026-07-18"
    assert result["seller"]["name"].startswith("Beispiel Lieferant")
    assert result["buyer"]["name"] == "Beispiel Kunde AG"
    assert len(result["lines"]) == 6
    assert result["lines"][5]["base_quantity"] == "100"
    assert result["lines"][5]["line_total"] == "640.00"
    assert result["totals"]["due_payable_amount"] == "13820.42"


def test_cii_validation_finds_expected_date_and_account_notes(cii_path):
    result = analyze_bytes(
        cii_path.read_bytes(),
        cii_path.name,
        "application/xml",
        run_official_validation=False,
    )
    findings = {item["id"]: item for item in result["validation"]["findings"]}

    assert result["validation"]["status"] == "warning"
    assert "DATE-002" in findings
    assert "LINE-009" in findings
    assert "PAY-004" in findings
    assert result["validation"]["counts"]["error"] == 0


def test_technical_appendix_contains_values_attributes_and_namespaces(cii_path):
    source = cii_path.read_bytes()
    result = analyze_bytes(source, cii_path.name, "application/xml", run_official_validation=False)
    rows = result["technical"]["rows"]

    assert any(row["kind"] == "namespace" and row["name"] == "xmlns:rsm" for row in rows)
    assert any(row["kind"] == "attribute" and row["name"] == "unitCode" and row["value"] == "C62" for row in rows)
    assert any(
        row["kind"] == "element" and row["name"] == "GrandTotalAmount" and row["value"] == "13820.42" for row in rows
    )
    assert result["technical"]["original_xml"] == source.decode("utf-8")
