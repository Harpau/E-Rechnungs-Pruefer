from __future__ import annotations

from copy import deepcopy

import pytest

from app.analyzer import analyze_bytes
from app.validators.builtin import (
    MAX_DECIMAL_CONTEXT_PRECISION,
    MAX_DECIMAL_DIGITS,
    _decimal_work_precision,
    validate_builtin,
)
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
                    "type_code": "58",
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
    internal = result["assessment"]["internal"]
    ids = {item["rule"]["id"] for item in internal["findings"]}
    assert "CALC-LINE-001" in ids
    assert "CALC-HDR-001" in ids
    assert internal["status"] == "errors"
    assert result["assessment"]["processing"]["status"] == "complete"


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
    assert any(item["rule"]["id"] == "PAY-002" for item in result["assessment"]["internal"]["findings"])


def test_dtd_and_entities_are_rejected():
    payload = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "test">]><x>&a;</x>'
    with pytest.raises(InvoiceInputError, match="DTD"):
        analyze_bytes(payload, "unsafe.xml", "application/xml", run_official_validation=False)


def test_unknown_xml_is_shown_but_marked_unsupported():
    payload = b'<?xml version="1.0"?><root><value>123</value></root>'
    result = analyze_bytes(payload, "generic.xml", "application/xml", run_official_validation=False)
    processing = result["assessment"]["processing"]

    assert result["capabilities"]["syntax"] == "UNKNOWN"
    assert result["assessment"]["internal"]["status"] == "not-run"
    assert processing["status"] == "incomplete"
    assert processing["findings"][0]["rule"]["id"] == "SYNTAX-001"
    assert any(field["value"] == "123" for field in result["technical"]["fields"])


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
    assert errors[0]["occurrence"] == {
        "scope": "line",
        "index": 1,
        "identifier": "1",
        "json_pointer": "/lines/1",
    }


@pytest.mark.parametrize(
    ("container", "field", "value", "missing_rule", "format_rule"),
    [
        ("document", "issue_date", "2026-99-99", "REQ-002", "FORMAT-DATE-001"),
        ("totals", "due_payable_amount", "NaN", "REQ-008", "FORMAT-DECIMAL-001"),
    ],
)
def test_builtin_validator_distinguishes_invalid_required_typed_values_from_missing_values(
    container,
    field,
    value,
    missing_rule,
    format_rule,
):
    analysis = _valid_analysis()
    analysis[container][field] = value

    result = validate_builtin(analysis)
    ids = {finding["id"] for finding in result["findings"]}

    assert result["status"] == "invalid"
    assert format_rule in ids
    assert missing_rule not in ids
    assert "CHECK-000" not in ids


@pytest.mark.parametrize(
    ("container", "field", "value"),
    [
        ("document", "due_date", "kein Datum"),
        ("document", "delivery_date", "2026-02-30"),
        ("totals", "allowance_total", "1e2"),
        ("totals", "tax_total", "Infinity"),
    ],
)
def test_present_invalid_optional_typed_values_never_disappear_as_clear(container, field, value):
    analysis = _valid_analysis()
    analysis[container][field] = value

    result = validate_builtin(analysis)
    ids = {finding["id"] for finding in result["findings"]}

    assert result["status"] == "invalid"
    assert {"FORMAT-DATE-001", "FORMAT-DECIMAL-001"} & ids
    assert "CHECK-000" not in ids


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


def test_analyzer_handles_non_finite_xml_quantity_without_an_unhandled_exception(cii_path):
    xml = (
        cii_path.read_text(encoding="utf-8")
        .replace(
            '<ram:BilledQuantity unitCode="C62">10.00</ram:BilledQuantity>',
            '<ram:BilledQuantity unitCode="C62">NaN</ram:BilledQuantity>',
            1,
        )
        .encode("utf-8")
    )

    result = analyze_bytes(xml, "nan.xml", "application/xml", run_official_validation=False)
    ids = {finding["rule"]["id"] for finding in result["assessment"]["internal"]["findings"]}

    assert "LINE-004" in ids
    assert "CALC-LINE-001" not in ids


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1,5", "1e2", "kein Betrag"])
@pytest.mark.parametrize(
    ("field", "expected_rule"),
    [
        ("quantity", "LINE-004"),
        ("price", "LINE-006"),
        ("line_total", "LINE-007"),
    ],
)
def test_builtin_validator_rejects_non_xml_decimal_line_values_without_follow_up_calculations(
    field,
    expected_rule,
    value,
):
    analysis = _valid_analysis()
    analysis["lines"][0][field] = value

    result = validate_builtin(analysis)
    ids = {finding["id"] for finding in result["findings"]}

    assert expected_rule in ids
    assert "CALC-LINE-001" not in ids
    assert "CALC-HDR-001" not in ids


def test_builtin_validator_reports_zero_price_base_without_claiming_it_was_used():
    analysis = _valid_analysis()
    analysis["lines"][0]["base_quantity"] = "0"

    result = validate_builtin(analysis)
    ids = {finding["id"] for finding in result["findings"]}

    assert "LINE-008" in ids
    assert "LINE-009" not in ids
    assert "CALC-LINE-001" not in ids


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1,5", "1e2", "kein Betrag"])
def test_builtin_validator_reports_invalid_price_base_without_follow_up_calculations(value):
    analysis = _valid_analysis()
    analysis["lines"][0]["base_quantity"] = value

    result = validate_builtin(analysis)
    ids = {finding["id"] for finding in result["findings"]}

    assert "LINE-010" in ids
    assert "LINE-009" not in ids
    assert "CALC-LINE-001" not in ids


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1,5", "1e2", "kein Betrag"])
def test_invalid_line_allowance_does_not_cause_a_misleading_line_total_error(value):
    analysis = _valid_analysis()
    analysis["lines"][0]["line_total"] = "90.00"
    analysis["lines"][0]["allowances_charges"] = [{"type": "allowance", "amount": value}]
    analysis["totals"].update(
        {
            "line_total": "90.00",
            "tax_basis_total": "90.00",
            "tax_total": "17.10",
            "grand_total": "107.10",
            "due_payable_amount": "107.10",
        }
    )
    analysis["taxes"][0].update({"basis_amount": "90.00", "tax_amount": "17.10"})

    result = validate_builtin(analysis)
    ids = {finding["id"] for finding in result["findings"]}

    assert "FORMAT-DECIMAL-001" in ids
    assert "CALC-LINE-001" not in ids


@pytest.mark.parametrize(
    ("field", "dependent_rule"),
    [
        ("allowance_total", "CALC-HDR-002"),
        ("charge_total", "CALC-HDR-002"),
        ("prepaid_amount", "CALC-HDR-006"),
        ("rounding_amount", "CALC-HDR-006"),
    ],
)
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1,5", "1e2", "kein Betrag"])
def test_invalid_optional_total_does_not_cause_a_misleading_dependent_error(
    field,
    dependent_rule,
    value,
):
    analysis = _valid_analysis()
    analysis["totals"][field] = value
    if field == "allowance_total":
        analysis["totals"]["tax_basis_total"] = "90.00"
    elif field == "charge_total":
        analysis["totals"]["tax_basis_total"] = "110.00"
    else:
        analysis["totals"]["due_payable_amount"] = "100.00"

    result = validate_builtin(analysis)
    ids = {finding["id"] for finding in result["findings"]}

    assert "FORMAT-DECIMAL-001" in ids
    assert dependent_rule not in ids


def test_invalid_listed_header_allowance_does_not_cause_a_misleading_sum_error():
    analysis = _valid_analysis()
    analysis["header_allowances_charges"] = [{"type": "allowance", "amount": "NaN"}]
    analysis["totals"].update(
        {
            "allowance_total": "10.00",
            "tax_basis_total": "90.00",
            "tax_total": "17.10",
            "grand_total": "107.10",
            "due_payable_amount": "107.10",
        }
    )
    analysis["taxes"][0].update({"basis_amount": "90.00", "tax_amount": "17.10"})

    result = validate_builtin(analysis)
    ids = {finding["id"] for finding in result["findings"]}

    assert "FORMAT-DECIMAL-001" in ids
    assert "CALC-HDR-003" not in ids


def test_builtin_calculations_keep_large_finite_decimal_values_exact() -> None:
    analysis = _valid_analysis()
    amount = f"{'9' * 128}.00"
    analysis["lines"][0].update({"quantity": "1", "price": amount, "line_total": amount})
    analysis["totals"].update(
        {
            "line_total": amount,
            "tax_basis_total": amount,
            "tax_total": "0.00",
            "grand_total": amount,
            "due_payable_amount": amount,
        }
    )
    analysis["taxes"] = []

    result = validate_builtin(analysis)

    assert result["status"] == "ok"
    assert [finding["id"] for finding in result["findings"]] == ["CHECK-000"]


def test_decimal_context_is_hard_bounded_and_rejects_only_oversized_operands() -> None:
    analysis = _valid_analysis()
    repeated_line = deepcopy(analysis["lines"][0])
    repeated_line["price"] = "9" * 128
    analysis["lines"] = [repeated_line] * 10_000

    assert _decimal_work_precision(analysis) < 512

    analysis["lines"] = [repeated_line]
    analysis["lines"][0]["price"] = "9" * MAX_DECIMAL_DIGITS

    assert _decimal_work_precision(analysis) <= MAX_DECIMAL_CONTEXT_PRECISION

    analysis["lines"][0]["price"] = "9" * (MAX_DECIMAL_DIGITS + 1)
    with pytest.raises(InvoiceInputError, match=rf"{MAX_DECIMAL_DIGITS} Dezimalziffern"):
        _decimal_work_precision(analysis)


def test_three_wide_decimal_operands_stay_inside_the_bounded_context() -> None:
    analysis = _valid_analysis()
    quantity = "9" * 100
    price = "9" * 100
    base_quantity = f".{('0' * 99)}1"
    analysis["lines"][0].update(
        {
            "quantity": quantity,
            "price": price,
            "base_quantity": base_quantity,
            "line_total": "0.00",
        }
    )
    analysis["totals"].update(
        {
            "line_total": "0.00",
            "tax_basis_total": "0.00",
            "tax_total": "0.00",
            "grand_total": "0.00",
            "due_payable_amount": "0.00",
        }
    )
    analysis["taxes"] = []

    result = validate_builtin(analysis)

    finding = next(item for item in result["findings"] if item["id"] == "CALC-LINE-001")
    assert finding["expected"].endswith(".00")
    assert len(finding["expected"].partition(".")[0]) == 300
    assert _decimal_work_precision(analysis) <= MAX_DECIMAL_CONTEXT_PRECISION


@pytest.mark.parametrize(
    ("limit_reason", "expected", "unexpected"),
    [
        ("rows", "Zeilengrenze", "Zeitbudget"),
        ("time", "Zeitbudget", "Zeilengrenze"),
    ],
)
def test_technical_finding_describes_the_actual_limit_reason(limit_reason, expected, unexpected) -> None:
    analysis = _valid_analysis()
    analysis["technical"] = {"truncated": True, "limit_reason": limit_reason}

    result = validate_builtin(analysis)
    finding = next(item for item in result["findings"] if item["id"] == "TECH-001")
    shown = f"{finding['title']} {finding['message']}"

    assert expected in shown
    assert unexpected not in shown
