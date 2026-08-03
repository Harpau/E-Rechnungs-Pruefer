from __future__ import annotations

import pytest

from app.document_types import DOCUMENT_TYPE_REGISTRY
from app.validators.builtin import validate_builtin

XRECHNUNG_PROFILE = "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
XRECHNUNG_TYPE_CODES = {"326", "380", "381", "384", "389", "875", "876", "877"}


def _analysis(
    type_code: str,
    *,
    profile_id: str = "urn:cen.eu:en16931:2017",
    syntax: str = "CII",
    root_element: str = "CrossIndustryInvoice",
) -> dict:
    return {
        "document": {
            "id": "SYNTHETISCH-TYP",
            "issue_date": "2026-07-31",
            "due_date": "2026-08-31",
            "type_code": type_code,
            "currency": "EUR",
            "profile_id": profile_id,
            "syntax": syntax,
        },
        "profile": {"id": profile_id},
        "seller": {"name": "Synthetischer Verkäufer", "address": {"country_code": "DE"}},
        "buyer": {"name": "Synthetischer Käufer", "address": {"country_code": "DE"}},
        "lines": [],
        "taxes": [],
        "totals": {"due_payable_amount": "0"},
        "payment": {"means": [], "terms": []},
        "header_allowances_charges": [],
        "technical": {"root_element": root_element, "truncated": False},
    }


def _finding_ids(analysis: dict) -> set[str]:
    return {item["id"] for item in validate_builtin(analysis)["findings"]}


@pytest.mark.parametrize("type_code", sorted(DOCUMENT_TYPE_REGISTRY))
def test_every_pinned_cen_document_type_passes_core_code_list_precheck(type_code: str) -> None:
    assert "BR-CL-01" not in _finding_ids(_analysis(type_code))


def test_unknown_document_type_is_reported_without_invoice_default() -> None:
    finding = next(item for item in validate_builtin(_analysis("999"))["findings"] if item["id"] == "BR-CL-01")

    assert finding["rule_class"] == "core_precheck"
    assert finding["reference"] == "BR-CL-01"
    assert finding["semantic_reference"] == ["BT-3"]
    assert finding["actual"] == "999"


def test_ubl_document_type_must_be_compatible_with_the_actual_root() -> None:
    findings = validate_builtin(_analysis("381", syntax="UBL", root_element="Invoice"))["findings"]
    finding = next(item for item in findings if item["id"] == "BR-CL-01")

    assert "UBL Invoice" in finding["message"]
    assert finding["actual"] == "381"


@pytest.mark.parametrize("type_code", sorted(XRECHNUNG_TYPE_CODES))
def test_xrechnung_recommended_document_types_pass_br_de_17_precheck(type_code: str) -> None:
    assert "XRECHNUNG-BR-DE-17" not in _finding_ids(_analysis(type_code, profile_id=XRECHNUNG_PROFILE))


def test_xrechnung_other_cen_type_reports_profile_precheck() -> None:
    finding = next(
        item
        for item in validate_builtin(_analysis("325", profile_id=XRECHNUNG_PROFILE))["findings"]
        if item["id"] == "XRECHNUNG-BR-DE-17"
    )

    assert finding["rule_class"] == "profile_precheck"
    assert finding["reference"] == "BR-DE-17"
    assert finding["profile"] == "XRechnung"
    assert finding["semantic_reference"] == ["BT-3"]
