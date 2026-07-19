from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.analyzer import analyze_bytes
from app.main import app

client = TestClient(app)


def _analyze(path: Path) -> dict:
    return analyze_bytes(
        path.read_bytes(),
        path.name,
        "application/xml",
        run_official_validation=False,
    )


def test_cii_category_o_has_no_rate_and_keeps_reason(cii_category_o_path):
    result = _analyze(cii_category_o_path)
    tax = result["taxes"][0]
    line = result["lines"][0]

    assert tax["category_display"] == "O – Nicht der Umsatzsteuer unterliegend"
    assert tax["rate"] is None
    assert tax["basis_label"] == "Nettobetrag dieser Steuerkategorie"
    assert tax["basis_amount"] == "495.00"
    assert tax["exemption_reason"] == "Leistung nicht im Inland steuerbar gemäß § 3a Abs. 2 UStG"
    assert line["tax_category_display"] == "O – Nicht der Umsatzsteuer unterliegend"
    assert line["tax_rate"] is None
    assert result["validation"]["status"] == "ok"


def test_ubl_category_o_has_same_normalized_display(ubl_category_o_path):
    result = _analyze(ubl_category_o_path)
    tax = result["taxes"][0]

    assert result["document"]["syntax"] == "UBL"
    assert tax["category_display"] == "O – Nicht der Umsatzsteuer unterliegend"
    assert tax["rate"] is None
    assert tax["basis_label"] == "Nettobetrag dieser Steuerkategorie"
    assert tax["exemption_reason"].startswith("Leistung nicht im Inland steuerbar")
    assert result["validation"]["counts"]["error"] == 0


def test_category_o_with_rate_is_rejected_by_internal_rules(cii_category_o_path):
    xml = cii_category_o_path.read_text(encoding="utf-8")
    xml = xml.replace(
        "<ram:CategoryCode>O</ram:CategoryCode>",
        "<ram:CategoryCode>O</ram:CategoryCode><ram:RateApplicablePercent>0</ram:RateApplicablePercent>",
    ).encode("utf-8")

    result = analyze_bytes(xml, "cii-o-with-rate.xml", "application/xml", run_official_validation=False)
    finding_ids = {finding["id"] for finding in result["validation"]["findings"]}

    assert "TAX-LINE-O-001" in finding_ids
    assert "TAX-HDR-O-001" in finding_ids
    assert result["validation"]["status"] == "invalid"


def test_category_g_with_non_taxable_reason_gets_semantic_warning(cii_category_g_mismatch_path):
    result = _analyze(cii_category_g_mismatch_path)
    findings = {finding["id"]: finding for finding in result["validation"]["findings"]}

    assert result["taxes"][0]["category_display"] == "G – Steuerfreie Ausfuhr außerhalb der EU"
    assert "TAX-SEM-001" in findings
    assert findings["TAX-SEM-001"]["severity"] == "warning"
    assert result["validation"]["counts"]["error"] == 0


def test_html_report_shows_basis_and_reason_together(cii_category_o_path):
    payload = cii_category_o_path.read_bytes()
    response = client.post(
        "/api/report",
        files={"file": (cii_category_o_path.name, payload, "application/xml")},
        data={"official": "false"},
    )

    assert response.status_code == 200
    assert "O – Nicht der Umsatzsteuer unterliegend" in response.text
    assert "Nettobetrag dieser Steuerkategorie" in response.text
    assert "Begründung: Leistung nicht im Inland steuerbar gemäß § 3a Abs. 2 UStG" in response.text
    assert "O – Nicht der Umsatzsteuer unterliegend · 0 %" not in response.text


def test_interactive_renderer_does_not_choose_between_basis_and_reason():
    script = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(encoding="utf-8")

    assert "tax.basis_amount ?" not in script
    assert "if (present(tax.exemption_reason))" in script
    assert "if (present(tax.exemption_reason_code))" in script
