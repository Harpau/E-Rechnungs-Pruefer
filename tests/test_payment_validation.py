from __future__ import annotations

from copy import deepcopy

import pytest

from app.validators.builtin import validate_builtin

EN_PROFILE = "urn:cen.eu:en16931:2017"
XRECHNUNG_PROFILE = "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"


def _analysis() -> dict:
    return {
        "document": {
            "id": "SYNTHETISCH-1",
            "issue_date": "2026-07-19",
            "due_date": "2026-07-31",
            "delivery_date": "2026-07-18",
            "type_code": "380",
            "currency": "EUR",
            "profile_id": EN_PROFILE,
        },
        "seller": {"name": "Synthetischer Lieferant", "address": {"country_code": "DE"}},
        "buyer": {"name": "Synthetischer Kunde", "address": {"country_code": "DE"}},
        "lines": [
            {
                "id": "1",
                "name": "Synthetische Leistung",
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
        "payment": {"reference": None, "means": [], "terms": []},
        "header_allowances_charges": [],
        "technical": {"truncated": False},
    }


def _findings(analysis: dict) -> dict[str, dict]:
    result = validate_builtin(analysis)
    return {finding["id"]: finding for finding in result["findings"]}


@pytest.mark.parametrize("type_code", ["380", "381", "389", "999"])
def test_positive_payable_requires_due_date_or_payment_terms_for_every_document_type(type_code):
    analysis = _analysis()
    analysis["document"]["type_code"] = type_code
    analysis["document"]["due_date"] = None

    finding = _findings(analysis)["BR-CO-25"]

    assert finding["severity"] == "error"
    assert finding["source"] == "EN 16931"
    assert finding["reference"] == "BR-CO-25"
    assert finding["rule_class"] == "core_precheck"
    assert finding["semantic_reference"] == ["BT-115", "BT-9", "BT-20"]
    assert "Käufer" not in finding["message"]
    assert "Verkäufer" not in finding["message"]
    assert "Zahlungsschuldner" not in finding["message"]


def test_payment_terms_text_satisfies_br_co_25_without_due_date():
    analysis = _analysis()
    analysis["document"]["due_date"] = None
    analysis["payment"]["terms"] = [{"description": "Zahlbar innerhalb von 14 Tagen."}]

    assert "BR-CO-25" not in _findings(analysis)


@pytest.mark.parametrize("payable", ["0", "-1.00"])
def test_non_positive_payable_does_not_trigger_br_co_25(payable):
    analysis = _analysis()
    analysis["document"]["due_date"] = None
    analysis["totals"].update(
        {
            "grand_total": payable,
            "due_payable_amount": payable,
            "tax_basis_total": "-1.00" if payable.startswith("-") else "0",
            "tax_total": "0",
        }
    )
    analysis["taxes"] = []

    assert "BR-CO-25" not in _findings(analysis)


def test_existing_payment_instruction_requires_payment_means_type_code():
    analysis = _analysis()
    analysis["payment"]["means"] = [{"information": "Synthetische Zahlungsanweisung"}]

    finding = _findings(analysis)["BR-49"]

    assert finding["severity"] == "error"
    assert finding["reference"] == "BR-49"
    assert finding["rule_class"] == "core_precheck"
    assert finding["semantic_reference"] == ["BG-16", "BT-81"]
    assert finding["location"] == "Zahlungsanweisung 1: Zahlungsart (BT-81) / Zahlungsanweisungen (BG-16)"
    assert finding["location_label"] == finding["location"]


@pytest.mark.parametrize(
    ("type_code", "payable"),
    [
        ("380", "119.00"),
        ("381", "-119.00"),
        ("389", "0"),
    ],
)
def test_known_xrechnung_profile_requires_payment_instructions_independent_of_type_and_sign(
    type_code,
    payable,
):
    analysis = _analysis()
    analysis["document"].update({"profile_id": XRECHNUNG_PROFILE, "type_code": type_code})
    analysis["totals"]["due_payable_amount"] = payable

    finding = _findings(analysis)["XRECHNUNG-BR-DE-1"]

    assert finding["reference"] == "BR-DE-1"
    assert finding["rule_class"] == "profile_precheck"
    assert finding["profile"] == "XRechnung"
    assert finding["semantic_reference"] == ["BG-16"]
    assert finding["location"] == "Zahlungsanweisungen (BG-16)"
    assert finding["location_label"] == "Zahlungsanweisungen (BG-16)"
    assert "Käufer" not in finding["message"]
    assert "Verkäufer" not in finding["message"]


@pytest.mark.parametrize(
    "profile_id",
    [
        None,
        EN_PROFILE,
        "urn:example:xrechnung-like",
        "urn:example:custom-profile",
    ],
)
def test_br_de_1_is_not_applied_without_a_safely_recognized_xrechnung_profile(profile_id):
    analysis = _analysis()
    analysis["document"]["profile_id"] = profile_id

    assert "XRECHNUNG-BR-DE-1" not in _findings(analysis)


def test_complete_xrechnung_payment_instruction_satisfies_br_de_1_and_br_49():
    analysis = _analysis()
    analysis["document"]["profile_id"] = XRECHNUNG_PROFILE
    analysis["payment"]["means"] = [{"type_code": "58"}]

    findings = _findings(analysis)

    assert "XRECHNUNG-BR-DE-1" not in findings
    assert "BR-49" not in findings


@pytest.mark.parametrize("payment_code", ["10", "20", "48", "68", "97", "999"])
def test_non_bank_payment_types_do_not_run_bank_identifier_checks(payment_code):
    analysis = _analysis()
    analysis["payment"]["means"] = [
        {
            "type_code": payment_code,
            "iban": "KEINE-IBAN",
            "bic": "KEINE-BIC",
            "account_name": "Abweichender Name",
            "card_account": "1234",
        }
    ]

    findings = _findings(analysis)

    assert "PAY-002" not in findings
    assert "PAY-003" not in findings
    assert "PAY-004" not in findings


def test_missing_payment_type_reports_br_49_but_skips_bank_identifier_checks():
    analysis = _analysis()
    analysis["payment"]["means"] = [{"iban": "KEINE-IBAN", "bic": "KEINE-BIC"}]

    findings = _findings(analysis)

    assert "BR-49" in findings
    assert "PAY-002" not in findings
    assert "PAY-003" not in findings


def test_generic_local_account_identifier_is_not_validated_as_an_iban():
    analysis = _analysis()
    analysis["payment"]["means"] = [
        {
            "type_code": "58",
            "account_id": {"value": "KONTO-4711", "scheme": "LOCAL"},
            "service_provider_id": {"value": "INSTITUT-42", "scheme": "BANK"},
        }
    ]

    findings = _findings(analysis)

    assert "PAY-002" not in findings
    assert "PAY-003" not in findings


def test_declared_iban_and_bic_are_checked_for_bank_payment_types():
    analysis = _analysis()
    analysis["payment"]["means"] = [
        {
            "type_code": "58",
            "account_id": {"value": "DE001234", "scheme": "IBAN"},
            "service_provider_id": {"value": "KEINE-BIC", "scheme": "BIC"},
        }
    ]

    findings = _findings(analysis)

    assert "PAY-002" in findings
    assert "PAY-003" in findings


def test_account_name_plausibility_check_is_limited_to_bank_payment_types():
    bank_analysis = _analysis()
    bank_analysis["payment"]["means"] = [{"type_code": "58", "account_name": "Abweichender Name"}]
    card_analysis = deepcopy(bank_analysis)
    card_analysis["payment"]["means"][0]["type_code"] = "48"

    assert "PAY-004" in _findings(bank_analysis)
    assert "PAY-004" not in _findings(card_analysis)


@pytest.mark.parametrize(
    ("type_code", "payable", "account_holder"),
    [
        ("380", "119.00", "Synthetischer Lieferant"),
        ("380", "-119.00", "Synthetischer Kunde"),
        ("381", "119.00", "Synthetischer Kunde"),
        ("389", "119.00", "Synthetischer Lieferant"),
    ],
)
def test_account_holder_is_compared_with_semantically_expected_recipient(
    type_code,
    payable,
    account_holder,
):
    analysis = _analysis()
    analysis["document"]["type_code"] = type_code
    analysis["totals"]["due_payable_amount"] = payable
    analysis["payment"]["means"] = [{"type_code": "58", "account_name": account_holder}]

    assert "PAY-004" not in _findings(analysis)


def test_explicit_payee_is_used_for_debtor_to_creditor_account_holder_check():
    analysis = _analysis()
    analysis["payee"] = {"name": "Synthetischer Drittempfänger"}
    analysis["payment"]["means"] = [{"type_code": "58", "account_name": "Synthetischer Drittempfänger"}]

    assert "PAY-004" not in _findings(analysis)


def test_unknown_document_semantics_do_not_guess_expected_account_holder():
    analysis = _analysis()
    analysis["document"]["type_code"] = "999"
    analysis["payment"]["means"] = [{"type_code": "58", "account_name": "Beliebiger Empfänger"}]

    assert "PAY-004" not in _findings(analysis)
