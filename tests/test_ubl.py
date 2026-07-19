from __future__ import annotations

from app.analyzer import analyze_bytes


def test_ubl_invoice_is_parsed_and_calculates(ubl_path):
    result = analyze_bytes(
        ubl_path.read_bytes(),
        ubl_path.name,
        "application/xml",
        run_official_validation=False,
    )

    assert result["document"]["syntax"] == "UBL"
    assert result["document"]["id"] == "UBL-DEMO-1"
    assert result["seller"]["name"] == "Beispiel Lieferant GmbH"
    assert result["buyer"]["name"] == "Beispiel Kunde AG"
    assert result["lines"][0]["unit_code"] == "H87"
    assert result["totals"]["tax_total"] == "19.00"
    assert result["validation"]["status"] == "ok"
    assert result["validation"]["counts"]["error"] == 0
