from __future__ import annotations

from app.analyzer import analyze_bytes


def test_ubl_invoice_is_parsed_and_calculates(ubl_path):
    result = analyze_bytes(
        ubl_path.read_bytes(),
        ubl_path.name,
        "application/xml",
        run_official_validation=False,
    )

    assert result["schema_version"] == 2
    assert result["capabilities"]["syntax"] == "UBL"
    assert result["document"]["id"] == "UBL-DEMO-1"
    assert result["parties"]["seller"]["legal_name"] == "Beispiel Lieferant GmbH"
    assert result["parties"]["buyer"]["legal_name"] == "Beispiel Kunde AG"
    assert result["lines"][0]["quantity"]["unit"]["value"] == "H87"
    assert result["tax"]["totals"]["document_currency"]["value"] == "19.00"
    assert result["assessment"]["internal"]["status"] == "clear"
    assert result["assessment"]["internal"]["counts"]["error"] == 0
    assert result["assessment"]["processing"]["status"] == "complete"
    assert result["assessment"]["official"]["status"] == "not-requested"


def test_ubl_credit_note_is_parsed_and_calculates(ubl_credit_note_path):
    result = analyze_bytes(
        ubl_credit_note_path.read_bytes(),
        ubl_credit_note_path.name,
        "application/xml",
        run_official_validation=False,
    )

    assert result["technical"]["root_element"] == "CreditNote"
    assert result["capabilities"]["syntax"] == "UBL"
    assert result["capabilities"]["format_name"] == "OASIS UBL 2.1 CreditNote"
    assert result["document"]["id"] == "UBL-CREDIT-DEMO-1"
    assert result["document"]["type"]["code"]["label"] == "Gutschrift"
    assert result["document"]["type"]["code"]["value"] == "381"
    assert result["document"]["type"]["family"] == "credit-note"

    assert len(result["lines"]) == 1
    line = result["lines"][0]
    assert line["id"] == "1"
    assert line["item"]["name"] == "Synthetische Beratungsleistung"
    assert line["quantity"]["value"] == "2"
    assert line["quantity"]["unit"]["value"] == "H87"
    assert line["price"]["net"]["value"] == "50.00"
    assert line["net_amount"]["value"] == "100.00"

    assert len(result["tax"]["breakdown"]) == 1
    tax = result["tax"]["breakdown"][0]
    assert tax["category"]["value"] == "S"
    assert tax["rate_percent"] == "19"
    assert tax["taxable_amount"]["value"] == "100.00"
    assert tax["tax_amount"]["value"] == "19.00"

    assert result["totals"]["line_net_total"]["value"] == "100.00"
    assert result["totals"]["tax_exclusive_total"]["value"] == "100.00"
    assert result["tax"]["totals"]["document_currency"]["value"] == "19.00"
    assert result["totals"]["tax_inclusive_total"]["value"] == "119.00"
    assert result["totals"]["payable"]["value"] == "119.00"
    assert result["assessment"]["internal"]["status"] == "clear"
    assert result["assessment"]["internal"]["counts"]["error"] == 0
    assert result["assessment"]["processing"]["status"] == "complete"
