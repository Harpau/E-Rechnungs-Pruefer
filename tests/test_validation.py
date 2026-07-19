from __future__ import annotations

from copy import deepcopy

import pytest

from app.analyzer import analyze_bytes
from app.validators.builtin import validate_builtin
from app.xml_utils import InvoiceInputError


def _valid_analysis() -> dict:
    return {
        "document": {
            "id": "INV-1",
            "issue_date": "2026-07-19",
            "due_date": "2026-07-31",
            "delivery_date": "2026-07-18",
            "type_code": "380",
            "currency": "EUR",
            "profile_id": "urn:cen.eu:en16931:2017",
        },
        "seller": {"name": "Muster GmbH", "address": {"country_code": "DE"}},
        "buyer": {"name": "Beispiel AG", "address": {"country_code": "DE"}},
        "lines": [
            {
                "id": "1",
                "name": "Leistung",
                "description": None,
                "quantity": "1",
                "unit_code": "C62",
                "price": "100.00",
                "base_quantity": "1",
                "line_total": "100.00",
                "allowances_charges": [],
                "price_currency": "EUR",
                "line_currency": "EUR",
                "tax_category": "S",
                "tax_rate": "19",
            }
        ],
        "totals": {
            "line_total": "100.00",
            "tax_basis_total": "100.00",
            "tax_total": "19.00",
            "grand_total": "119.00",
            "due_payable_amount": "119.00",
        },
        "taxes": [
            {
                "category_code": "S",
                "basis_amount": "100.00",
                "rate": "19",
                "tax_amount": "19.00",
            }
        ],
        "payment": {
            "means": [
                {
                    "iban": "DE89370400440532013000",
                    "bic": "COBADEFFXXX",
                    "account_name": "Muster GmbH",
                }
            ]
        },
        "header_allowances_charges": [],
        "technical": {"truncated": False},
    }


def test_wrong_line_total_is_detected(cii_path):
    xml = (
        cii_path.read_text(encoding="utf-8")
        .replace(
            "<ram:LineTotalAmount>1098.80</ram:LineTotalAmount>",
            "<ram:LineTotalAmount>1198.80</ram:LineTotalAmount>",
            1,
        )
        .encode("utf-8")
    )
    result = analyze_bytes(xml, "wrong.xml", "application/xml", run_official_validation=False)
    ids = {item["id"] for item in result["validation"]["findings"]}
    assert "CALC-LINE-001" in ids
    assert "CALC-HDR-001" in ids
    assert result["validation"]["status"] == "invalid"


def test_invalid_iban_is_detected(cii_path):
    xml = (
        cii_path.read_text(encoding="utf-8")
        .replace(
            "DE89370400440532013000",
            "DE89370400440532013001",
        )
        .encode("utf-8")
    )
    result = analyze_bytes(xml, "bad-iban.xml", "application/xml", run_official_validation=False)
    assert any(item["id"] == "PAY-002" for item in result["validation"]["findings"])


def test_dtd_and_entities_are_rejected():
    payload = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "test">]><x>&a;</x>'
    with pytest.raises(InvoiceInputError, match="DTD"):
        analyze_bytes(payload, "unsafe.xml", "application/xml", run_official_validation=False)


def test_unknown_xml_is_shown_but_marked_unsupported():
    payload = b'<?xml version="1.0"?><root><value>123</value></root>'
    result = analyze_bytes(payload, "generic.xml", "application/xml", run_official_validation=False)
    assert result["document"]["syntax"] == "UNKNOWN"
    assert result["validation"]["status"] == "invalid"
    assert result["validation"]["findings"][0]["id"] == "SYNTAX-001"
    assert any(row["value"] == "123" for row in result["technical"]["rows"])


def test_utf16_xml_is_supported(ubl_path):
    text = ubl_path.read_text(encoding="utf-8")
    text = text.replace('encoding="UTF-8"', 'encoding="UTF-16"', 1)
    result = analyze_bytes(
        text.encode("utf-16"),
        "utf16.xml",
        "application/xml",
        run_official_validation=False,
    )
    assert result["document"]["id"] == "UBL-DEMO-1"


def test_utf16_dtd_is_rejected():
    payload = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "test">]><x>&a;</x>'.encode("utf-16")
    with pytest.raises(InvoiceInputError, match="DTD"):
        analyze_bytes(payload, "unsafe-utf16.xml", "application/xml", run_official_validation=False)


def test_builtin_validator_accepts_consistent_minimal_analysis():
    result = validate_builtin(_valid_analysis())

    assert result["status"] == "ok"
    assert result["counts"] == {"error": 0, "warning": 0, "info": 1}
    assert [finding["id"] for finding in result["findings"]] == ["CHECK-000"]


def test_builtin_validator_reports_missing_invoice_id_and_invalid_currency():
    analysis = _valid_analysis()
    analysis["document"]["id"] = ""
    analysis["document"]["currency"] = "eur"
    analysis["lines"][0]["price_currency"] = "eur"
    analysis["lines"][0]["line_currency"] = "eur"

    result = validate_builtin(analysis)
    errors = {finding["id"]: finding for finding in result["findings"] if finding["severity"] == "error"}

    assert result["status"] == "invalid"
    assert set(errors) == {"REQ-001", "CODE-001"}
    assert errors["REQ-001"]["location"] == "BT-1"
    assert errors["CODE-001"]["actual"] == "eur"


def test_builtin_validator_reports_duplicate_line_id():
    analysis = _valid_analysis()
    analysis["lines"].append(deepcopy(analysis["lines"][0]))
    analysis["totals"].update(
        {
            "line_total": "200.00",
            "tax_basis_total": "200.00",
            "tax_total": "38.00",
            "grand_total": "238.00",
            "due_payable_amount": "238.00",
        }
    )
    analysis["taxes"][0].update({"basis_amount": "200.00", "tax_amount": "38.00"})

    result = validate_builtin(analysis)
    errors = [finding for finding in result["findings"] if finding["severity"] == "error"]

    assert result["status"] == "invalid"
    assert [finding["id"] for finding in errors] == ["LINE-002"]
    assert errors[0]["actual"] == "1"


def test_builtin_validator_reports_amount_currency_and_bic_formats():
    analysis = _valid_analysis()
    analysis["lines"][0]["price_currency"] = "USD"
    analysis["lines"][0]["line_currency"] = "CHF"
    analysis["payment"]["means"][0]["bic"] = "BAD-BIC"

    result = validate_builtin(analysis)
    warnings = [finding for finding in result["findings"] if finding["severity"] == "warning"]

    assert result["status"] == "warning"
    assert [finding["id"] for finding in warnings] == ["CURR-001", "CURR-001", "PAY-003"]
    assert {finding["actual"] for finding in warnings} == {"USD", "CHF", "BAD-BIC"}
