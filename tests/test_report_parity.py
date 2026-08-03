from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from app.analyzer import analyze_bytes
from app.report_presentation import build_report_presentation, load_presentation_contract


@pytest.fixture()
def analysis(ubl_path):
    return analyze_bytes(
        ubl_path.read_bytes(),
        ubl_path.name,
        "application/xml",
        run_official_validation=False,
    )


def test_presentation_contract_defines_the_same_30_header_facts_as_the_browser(analysis):
    contract = load_presentation_contract()
    presentation = build_report_presentation(analysis)

    assert contract["version"] == 1
    assert len(contract["header_facts"]) == 30
    assert [fact["key"] for fact in presentation["header_facts"]] == [
        "invoice_number",
        "invoice_date",
        "invoice_period",
        "delivery_period",
        "actual_delivery_date",
        "delivery_location_id",
        "delivery_location_address",
        "due_date",
        "document_currency",
        "vat_accounting_currency",
        "profile",
        "invoice_type",
        "document_type_status",
        "document_family",
        "base_polarity",
        "settlement_relevance",
        "self_billing",
        "buyer_reference",
        "syntax",
        "format",
        "document_type_recognition",
        "rendering_scope",
        "internal_checks_scope",
        "official_validation",
        "business_process",
        "tax_point_date",
        "tax_point_date_code",
        "profile_id",
        "ubl_root",
        "root_compatibility",
    ]
    assert len(presentation["header_facts"]) == 30
    assert all(set(fact) == {"key", "label", "value"} for fact in presentation["header_facts"])


@pytest.mark.parametrize(
    ("official", "internal", "processing", "expected_key", "expected_label"),
    [
        ("accepted", "clear", "complete", "ok", "Ausgewertet"),
        ("not-requested", "clear", "complete", "ok", "Ausgewertet"),
        ("unsupported", "clear", "complete", "warning", "Mit Hinweisen"),
        ("accepted", "attention", "complete", "warning", "Mit Hinweisen"),
        ("accepted", "clear", "limited", "warning", "Mit Hinweisen"),
        ("rejected", "attention", "limited", "invalid", "Handlungsbedarf"),
        ("accepted", "errors", "limited", "invalid", "Handlungsbedarf"),
        ("accepted", "attention", "incomplete", "invalid", "Handlungsbedarf"),
    ],
)
def test_overall_status_matches_browser_priority(
    analysis,
    official,
    internal,
    processing,
    expected_key,
    expected_label,
):
    candidate = deepcopy(analysis)
    candidate["assessment"]["official"]["status"] = official
    candidate["assessment"]["internal"]["status"] = internal
    candidate["assessment"]["processing"]["status"] = processing

    presentation = build_report_presentation(candidate)

    assert presentation["overall_status"] == {
        "key": expected_key,
        "label": expected_label,
        "css_class": expected_key,
    }
    assert [axis["label"] for axis in presentation["axes"]] == [
        {
            "accepted": "Akzeptiert",
            "rejected": "Abgelehnt",
            "not-requested": "Nicht angefordert",
            "unsupported": "Nicht unterstützt",
            "unavailable": "Nicht verfügbar",
            "indeterminate": "Unbestimmt",
        }[official],
        {
            "clear": "Unauffällig",
            "attention": "Hinweise",
            "errors": "Fehler",
            "not-run": "Nicht ausgeführt",
        }[internal],
        {
            "complete": "Vollständig",
            "limited": "Begrenzt",
            "incomplete": "Unvollständig",
        }[processing],
    ]


def test_presentation_compacts_payment_semantics_and_filters_technical_sections(analysis):
    candidate = deepcopy(analysis)
    candidate["roles"].update(
        {
            "issuer": "buyer",
            "document_recipient": "seller",
            "expected_payer": "buyer",
            "expected_recipient": "seller",
            "expected_payment_direction": "debtor-to-creditor",
            "derivation": "derived",
        }
    )
    candidate["assessment"]["official"].update(
        {
            "raw_report": "SYNTHETISCHER-ROHBERICHT",
            "technical_output": "SYNTHETISCHE-TECHNISCHE-AUSGABE",
        }
    )

    readable = build_report_presentation(candidate)
    complete = build_report_presentation(candidate, scope="complete")

    assert readable["scope"] == "readable"
    assert readable["include_technical"] is False
    assert complete["scope"] == "complete"
    assert complete["include_technical"] is True
    assert readable["payment_flow"] == {
        "document_flow": "Käufer → Verkäufer",
        "expected_payment_flow": "Käufer → Verkäufer",
        "note": (
            "Aus Dokumenttyp, Zahlbetrag und Parteienrollen abgeleitet. "
            "Dies ist kein Nachweis, dass eine Zahlung tatsächlich erfolgt ist oder erfolgen muss."
        ),
        "reference": candidate["payment"]["reference"],
    }
    assert [section["id"] for section in readable["sections"]] == [
        "document_overview",
        "parties",
        "allowances_charges",
        "lines",
        "tax_totals",
        "payment",
        "references_delivery",
        "notes_source",
        "assessment",
    ]
    assert [section["id"] for section in complete["sections"]] == [
        *[section["id"] for section in readable["sections"]],
        "kosit_raw",
        "technical_fields",
        "source_xml",
    ]
    assert readable["technical"] == {
        "official_raw_report": None,
        "official_technical_output": None,
        "fields": [],
        "source_xml": None,
    }
    assert complete["technical"]["official_raw_report"] == "SYNTHETISCHER-ROHBERICHT"
    assert complete["technical"]["official_technical_output"] == "SYNTHETISCHE-TECHNISCHE-AUSGABE"
    assert complete["technical"]["fields"] == candidate["technical"]["fields"]
    assert complete["technical"]["source_xml"] == candidate["technical"]["source_xml"]


def test_self_billing_type_389_keeps_document_and_payment_direction_distinct(analysis):
    candidate = deepcopy(analysis)
    candidate["document"]["type"].update(
        {
            "status": "known",
            "code": {"value": "389", "label": "Eigenabrechnung", "list_id": "UNCL1001"},
            "family": "invoice",
            "self_billing": True,
        }
    )
    candidate["roles"].update(
        {
            "issuer": "buyer",
            "document_recipient": "seller",
            "expected_payer": "buyer",
            "expected_recipient": "seller",
            "expected_payment_direction": "debtor-to-creditor",
            "derivation": "derived",
        }
    )

    presentation = build_report_presentation(candidate)

    facts = {fact["key"]: fact["value"] for fact in presentation["header_facts"]}
    assert presentation["header"]["document_type_summary"] == "Rechnungsart · 389 – Eigenabrechnung"
    assert facts["invoice_type"] == "389 – Eigenabrechnung"
    assert facts["self_billing"] == "Ja"
    assert presentation["payment_flow"]["document_flow"] == "Käufer → Verkäufer"
    assert presentation["payment_flow"]["expected_payment_flow"] == "Käufer → Verkäufer"


def test_no_raw_contract_enums_leak_into_presented_header_values(analysis):
    presentation = build_report_presentation(analysis)
    shown = {fact["value"] for fact in presentation["header_facts"]}

    assert not ({"known", "full", "debit", "relevant", "compatible"} & shown)


def test_line_tax_presentation_prioritizes_rates_and_explains_missing_values(analysis):
    candidate = deepcopy(analysis)
    candidate["lines"] = [
        {
            "tax_category": {"value": "S", "label": "Standardsteuersatz"},
            "tax_rate_percent": Decimal("19.00"),
        },
        {
            "tax_category": {"value": "O", "label": "Nicht der Umsatzsteuer unterliegend"},
            "tax_rate_percent": None,
        },
        {
            "tax_category": {"value": "E", "label": "Steuerbefreit"},
            "tax_rate_percent": None,
        },
        {"tax_category": None, "tax_rate_percent": Decimal("7")},
        {"tax_category": None, "tax_rate_percent": None},
    ]
    candidate["tax"]["breakdown"] = [
        {"category": {"value": "S"}, "rate_percent": Decimal("19.0")},
        {"category": {"value": "O"}, "rate_percent": None},
    ]

    presentation = build_report_presentation(candidate)

    assert presentation["line_taxes"] == [
        {
            "primary": "19 %",
            "primary_kind": "rate",
            "secondary": "S",
            "accessible_label": "Steuersatz 19 Prozent, Steuerkategorie S, Standardsteuersatz",
        },
        {
            "primary": "O",
            "primary_kind": "code",
            "secondary": "ohne Steuersatz",
            "accessible_label": ("Steuerkategorie O, Nicht der Umsatzsteuer unterliegend, ohne Steuersatz"),
        },
        {
            "primary": "E",
            "primary_kind": "code",
            "secondary": "Steuersatz nicht angegeben",
            "accessible_label": "Steuerkategorie E, Steuerbefreit, Steuersatz nicht angegeben",
        },
        {
            "primary": "7 %",
            "primary_kind": "rate",
            "secondary": "Kategorie nicht angegeben",
            "accessible_label": "Steuersatz 7 Prozent, Steuerkategorie nicht angegeben",
        },
        {
            "primary": "–",
            "primary_kind": "empty",
            "secondary": None,
            "accessible_label": "Steuerangaben nicht angegeben",
        },
    ]


def test_tax_breakdown_gaps_use_normalized_pairs_and_are_deduplicated(analysis):
    candidate = deepcopy(analysis)
    candidate["lines"] = [
        {
            "tax_category": {"value": "s", "label": "Standardsteuersatz"},
            "tax_rate_percent": Decimal("19.00"),
        },
        {
            "tax_category": {"value": "S", "label": "Standardsteuersatz"},
            "tax_rate_percent": Decimal("7.0"),
        },
        {
            "tax_category": {"value": "S", "label": "Standardsteuersatz"},
            "tax_rate_percent": Decimal("7.00"),
        },
        {
            "tax_category": {"value": "E", "label": "Steuerbefreit"},
            "tax_rate_percent": None,
        },
        {"tax_category": None, "tax_rate_percent": Decimal("5")},
        {"tax_category": None, "tax_rate_percent": None},
    ]
    candidate["tax"]["breakdown"] = [
        {"category": {"value": " S "}, "rate_percent": Decimal("19")},
    ]

    presentation = build_report_presentation(candidate)

    assert presentation["tax_breakdown_gaps"] == [
        "S – Standardsteuersatz · 7 %",
        "E – Steuerbefreit · Steuersatz nicht angegeben",
        "Kategorie nicht angegeben · 5 %",
    ]


def test_tax_breakdown_gap_is_absent_when_every_normalized_pair_matches(analysis):
    candidate = deepcopy(analysis)
    candidate["lines"] = [
        {
            "tax_category": {"value": "S", "label": "Standardsteuersatz"},
            "tax_rate_percent": Decimal("19.00"),
        },
        {
            "tax_category": {"value": "O", "label": "Nicht der Umsatzsteuer unterliegend"},
            "tax_rate_percent": None,
        },
    ]
    candidate["tax"]["breakdown"] = [
        {"category": {"value": "s"}, "rate_percent": Decimal("19")},
        {"category": {"value": "O"}, "rate_percent": None},
    ]

    presentation = build_report_presentation(candidate)

    assert presentation["tax_breakdown_gaps"] == []


def test_prefixed_tax_label_is_detected_case_insensitively_without_changing_raw_code(analysis):
    candidate = deepcopy(analysis)
    candidate["lines"] = [
        {
            "tax_category": {"value": "s", "label": "S – Standardsteuersatz"},
            "tax_rate_percent": Decimal("19"),
        }
    ]
    candidate["tax"]["breakdown"] = []

    presentation = build_report_presentation(candidate)

    assert presentation["line_taxes"] == [
        {
            "primary": "19 %",
            "primary_kind": "rate",
            "secondary": "s",
            "accessible_label": "Steuersatz 19 Prozent, Steuerkategorie s, Standardsteuersatz",
        }
    ]
    assert presentation["tax_breakdown_gaps"] == ["s – Standardsteuersatz · 19 %"]
