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


def test_ubl_credit_note_is_parsed_and_calculates(ubl_credit_note_path):
    result = analyze_bytes(
        ubl_credit_note_path.read_bytes(),
        ubl_credit_note_path.name,
        "application/xml",
        run_official_validation=False,
    )

    assert result["technical"]["root_element"] == "CreditNote"
    assert result["document"]["syntax"] == "UBL"
    assert result["document"]["format"] == "OASIS UBL 2.1 CreditNote"
    assert result["document"]["id"] == "UBL-CREDIT-DEMO-1"
    assert result["document"]["kind"] == "Gutschrift"
    assert result["document"]["type_code"] == "381"

    assert len(result["lines"]) == 1
    line = result["lines"][0]
    assert line["id"] == "1"
    assert line["name"] == "Synthetische Beratungsleistung"
    assert line["quantity"] == "2"
    assert line["unit_code"] == "H87"
    assert line["price"] == "50.00"
    assert line["line_total"] == "100.00"

    assert len(result["taxes"]) == 1
    tax = result["taxes"][0]
    assert tax["category_code"] == "S"
    assert tax["rate"] == "19"
    assert tax["basis_amount"] == "100.00"
    assert tax["tax_amount"] == "19.00"

    assert result["totals"]["line_total"] == "100.00"
    assert result["totals"]["tax_basis_total"] == "100.00"
    assert result["totals"]["tax_total"] == "19.00"
    assert result["totals"]["grand_total"] == "119.00"
    assert result["totals"]["due_payable_amount"] == "119.00"
    assert result["validation"]["status"] == "ok"
    assert result["validation"]["counts"]["error"] == 0
