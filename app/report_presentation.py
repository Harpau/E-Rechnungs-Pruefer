"""Shared, renderer-neutral presentation model for human-readable reports."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

ReportScope = Literal["readable", "complete"]
OverallStatusKey = Literal["ok", "warning", "invalid"]

_CONTRACT_PATH = Path(__file__).with_name("presentation_contract.json")
_EMPTY_VALUE = "–"


class OverallStatusPresentation(TypedDict):
    key: OverallStatusKey
    label: str
    css_class: str


class AxisPresentation(TypedDict):
    key: str
    title: str
    status: str
    label: str
    summary: str
    counts: dict[str, int]
    findings: list[dict[str, Any]]
    limitations: list[dict[str, Any]]


class HeaderPresentation(TypedDict):
    document_kind: str
    document_type_summary: str
    document_title: str
    subtitle: str
    payable_label: str
    payable: str
    due_date: str


class FactPresentation(TypedDict):
    key: str
    label: str
    value: str


class PaymentFlowPresentation(TypedDict):
    document_flow: str
    expected_payment_flow: str
    note: str
    reference: str | None


class LineTaxPresentation(TypedDict):
    primary: str
    primary_kind: Literal["rate", "code", "empty"]
    secondary: str | None
    accessible_label: str


class SectionPresentation(TypedDict):
    id: str
    label: str
    scope: ReportScope


class TechnicalPresentation(TypedDict):
    official_raw_report: str | None
    official_technical_output: str | None
    fields: list[dict[str, Any]]
    source_xml: str | None


class ReportPresentation(TypedDict):
    scope: ReportScope
    include_technical: bool
    overall_status: OverallStatusPresentation
    axes: list[AxisPresentation]
    header: HeaderPresentation
    header_facts: list[FactPresentation]
    payment_flow: PaymentFlowPresentation
    line_taxes: list[LineTaxPresentation]
    tax_breakdown_gaps: list[str]
    sections: list[SectionPresentation]
    technical: TechnicalPresentation


_DOCUMENT_FAMILY_LABELS = {
    "invoice": "Rechnung",
    "credit-note": "Gutschrift",
    "correction": "Korrekturrechnung",
    "debit-note": "Belastungsanzeige",
    "prepayment-invoice": "Vorauszahlungsrechnung",
    "payment-request": "Zahlungsaufforderung",
    "pro-forma": "Pro-forma-Rechnung",
    "information": "Informationsdokument",
    "claim": "Forderungsdokument",
    "other": "Sonstiges Rechnungsdokument",
    "unknown": "E‑Rechnung",
}
_ROLE_LABELS = {
    "seller": "Verkäufer",
    "buyer": "Käufer",
    "payee": "Zahlungsempfänger",
    "invoice-recipient": "Rechnungsempfänger",
    "delivery-recipient": "Lieferempfänger",
    "seller-tax-representative": "Steuervertreter des Verkäufers",
}
_DOCUMENT_TYPE_STATUS_LABELS = {
    "known": "Erkannt",
    "unknown": "Unbekannter Code",
    "missing": "Nicht angegeben",
}
_POLARITY_LABELS = {
    "debit": "Soll",
    "credit": "Haben",
    "neutral": "Neutral",
    "undetermined": "Nicht bestimmbar",
}
_SETTLEMENT_RELEVANCE_LABELS = {
    "relevant": "Zahlungsrelevant",
    "not-relevant": "Nicht zahlungsrelevant",
    "undetermined": "Nicht bestimmbar",
}
_ROOT_COMPATIBILITY_LABELS = {
    "compatible": "Kompatibel",
    "incompatible": "Nicht kompatibel",
    "not-applicable": "Nicht anwendbar",
    "undetermined": "Nicht bestimmbar",
}
_RECOGNITION_CAPABILITY_LABELS = {
    "recognized": "Erkannt",
    "unknown": "Unbekannt",
    "missing": "Fehlend",
}
_RENDERING_CAPABILITY_LABELS = {
    "full": "Vollständig",
    "partial": "Teilweise",
    "unsupported": "Nicht unterstützt",
}
_INTERNAL_CHECKS_CAPABILITY_LABELS = _RENDERING_CAPABILITY_LABELS
_OFFICIAL_VALIDATION_CAPABILITY_LABELS = {
    "bundled": "Enthalten",
    "not-bundled": "Nicht enthalten",
    "unknown": "Unbekannt",
    "unavailable": "Nicht verfügbar",
}


def _validate_contract(contract: dict[str, Any]) -> None:
    facts = contract.get("header_facts")
    sections = contract.get("sections")
    axes = contract.get("axes")
    if not isinstance(facts, list) or len(facts) != 30:
        raise RuntimeError("Der Präsentationsvertrag muss genau 30 Kopffelder definieren.")
    fact_keys = [item.get("key") for item in facts if isinstance(item, dict)]
    if len(fact_keys) != 30 or len(set(fact_keys)) != 30:
        raise RuntimeError("Die Schlüssel der Kopffelder müssen eindeutig sein.")
    if not isinstance(axes, list) or [item.get("key") for item in axes if isinstance(item, dict)] != [
        "official",
        "internal",
        "processing",
    ]:
        raise RuntimeError("Der Präsentationsvertrag muss die drei Bewertungsachsen definieren.")
    if not isinstance(sections, list) or any(
        not isinstance(item, dict) or item.get("scope") not in {"readable", "complete"} for item in sections
    ):
        raise RuntimeError("Der Präsentationsvertrag enthält einen ungültigen Berichtsabschnitt.")


@lru_cache(maxsize=1)
def load_presentation_contract() -> dict[str, Any]:
    """Load and minimally validate the declarative presentation contract."""

    contract = cast(dict[str, Any], json.loads(_CONTRACT_PATH.read_text(encoding="utf-8")))
    _validate_contract(contract)
    return contract


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _text(value: Any, fallback: str = _EMPTY_VALUE) -> str:
    return str(value) if _present(value) else fallback


def _format_date(value: Any) -> str:
    if not _present(value):
        return _EMPTY_VALUE
    if isinstance(value, (date, datetime)):
        return value.strftime("%d.%m.%Y")
    raw = str(value)
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return raw


def _format_number(value: Any, digits: int | None = None) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return _text(value)
    if digits is None:
        raw = format(number, "f")
        if "." in raw:
            raw = raw.rstrip("0").rstrip(".")
    else:
        number = number.quantize(Decimal(1).scaleb(-digits))
        raw = f"{number:.{digits}f}"
    integer, separator, fraction = raw.partition(".")
    sign = ""
    if integer.startswith("-"):
        sign, integer = "-", integer[1:]
    groups = []
    while integer:
        groups.append(integer[-3:])
        integer = integer[:-3]
    shown = sign + ".".join(reversed(groups))
    return f"{shown},{fraction}" if separator else shown


def _format_money(amount: Any) -> str:
    value = _mapping(amount)
    if not _present(value.get("value")):
        return _EMPTY_VALUE
    shown = _format_number(value["value"], 2)
    currency = _text(value.get("currency"), "")
    return f"{shown} {currency}".strip()


def _code_display(code: Any, fallback: str = _EMPTY_VALUE) -> str:
    value = _mapping(code)
    raw = value.get("value")
    if not _present(raw):
        return fallback
    shown = str(raw)
    label = value.get("label")
    if not _present(label):
        return shown
    label_text = str(label)
    if label_text == shown or label_text.startswith(f"{shown} –"):
        return label_text
    return f"{shown} – {label_text}"


def _identifier_display(identifier: Any, fallback: str = _EMPTY_VALUE) -> str:
    value = _mapping(identifier)
    raw = value.get("value")
    if not _present(raw):
        return fallback
    scheme = value.get("scheme_id")
    return f"{raw} ({scheme})" if _present(scheme) else str(raw)


def _format_period(period: Any) -> str:
    value = _mapping(period)
    parts = [
        f"von {_format_date(value['start_date'])}" if _present(value.get("start_date")) else None,
        f"bis {_format_date(value['end_date'])}" if _present(value.get("end_date")) else None,
        value.get("description"),
    ]
    return " ".join(str(part) for part in parts if _present(part)) or _EMPTY_VALUE


def _format_address(address: Any) -> str:
    value = _mapping(address)
    locality = " ".join(str(part) for part in (value.get("postcode"), value.get("city")) if _present(part))
    parts = [
        value.get("line1"),
        value.get("line2"),
        value.get("line3"),
        locality,
        value.get("subdivision"),
        _code_display(value.get("country"), ""),
    ]
    return ", ".join(str(part) for part in parts if _present(part)) or _EMPTY_VALUE


def _label(labels: Mapping[str, str], value: Any) -> str:
    return labels.get(str(value), _EMPTY_VALUE) if _present(value) else _EMPTY_VALUE


def _document_kind(document_type: Mapping[str, Any]) -> str:
    return _DOCUMENT_FAMILY_LABELS.get(str(document_type.get("family")), _DOCUMENT_FAMILY_LABELS["unknown"])


def _document_type_summary(document_type: Mapping[str, Any]) -> str:
    code = _mapping(document_type.get("code"))
    if document_type.get("status") == "missing" or not _present(code.get("value")):
        return "Rechnungsart · Nicht angegeben"
    if document_type.get("status") == "unknown":
        return f"Rechnungsart · {code['value']} – Unbekannter Dokumenttyp"
    return f"Rechnungsart · {_code_display(code, _document_kind(document_type))}"


def _overall_status(analysis: Mapping[str, Any], contract: Mapping[str, Any]) -> OverallStatusPresentation:
    assessment = _mapping(analysis.get("assessment"))
    official_status = _mapping(assessment.get("official")).get("status")
    internal_status = _mapping(assessment.get("internal")).get("status")
    processing_status = _mapping(assessment.get("processing")).get("status")
    has_errors = official_status == "rejected" or internal_status == "errors" or processing_status == "incomplete"
    has_warnings = (
        internal_status in {"attention", "not-run"}
        or processing_status == "limited"
        or official_status in {"unsupported", "unavailable", "indeterminate"}
    )
    key: OverallStatusKey = "invalid" if has_errors else "warning" if has_warnings else "ok"
    labels = cast(Mapping[str, str], contract["overall_status"])
    return {"key": key, "label": labels[key], "css_class": key}


def _axes(analysis: Mapping[str, Any], contract: Mapping[str, Any]) -> list[AxisPresentation]:
    assessment = _mapping(analysis.get("assessment"))
    result: list[AxisPresentation] = []
    for definition_value in cast(list[dict[str, Any]], contract["axes"]):
        key = str(definition_value["key"])
        axis = _mapping(assessment.get(key))
        status = _text(axis.get("status"), "unknown")
        counts = _mapping(axis.get("counts"))
        labels = cast(Mapping[str, str], definition_value["labels"])
        result.append(
            {
                "key": key,
                "title": str(definition_value["title"]),
                "status": status,
                "label": labels.get(status, "Unbekannt"),
                "summary": _text(axis.get("summary"), "Keine Zusammenfassung vorhanden."),
                "counts": {
                    "error": int(counts.get("error") or 0),
                    "warning": int(counts.get("warning") or 0),
                    "info": int(counts.get("info") or 0),
                },
                "findings": list(axis.get("findings") or []),
                "limitations": list(axis.get("limitations") or []),
            }
        )
    return result


def _fact_values(analysis: Mapping[str, Any]) -> dict[str, str]:
    document = _mapping(analysis.get("document"))
    document_type = _mapping(document.get("type"))
    profile = _mapping(analysis.get("profile"))
    capabilities = _mapping(analysis.get("capabilities"))
    periods = _mapping(analysis.get("periods"))
    delivery = _mapping(analysis.get("delivery"))
    location = _mapping(delivery.get("location"))
    payment = _mapping(analysis.get("payment"))
    self_billing = document_type.get("self_billing")
    syntax = " ".join(
        str(item) for item in (capabilities.get("syntax"), capabilities.get("syntax_version")) if _present(item)
    )
    return {
        "invoice_number": _text(document.get("id")),
        "invoice_date": _format_date(document.get("issue_date")),
        "invoice_period": _format_period(periods.get("invoice")),
        "delivery_period": _format_period(periods.get("delivery")),
        "actual_delivery_date": _format_date(delivery.get("actual_date")),
        "delivery_location_id": _identifier_display(location.get("id")),
        "delivery_location_address": _format_address(location.get("postal_address")),
        "due_date": _format_date(payment.get("due_date")),
        "document_currency": _code_display(document.get("document_currency")),
        "vat_accounting_currency": _code_display(document.get("vat_accounting_currency")),
        "profile": _text(profile.get("name")),
        "invoice_type": _code_display(document_type.get("code"), _document_kind(document_type)),
        "document_type_status": _label(_DOCUMENT_TYPE_STATUS_LABELS, document_type.get("status")),
        "document_family": _label(_DOCUMENT_FAMILY_LABELS, document_type.get("family")),
        "base_polarity": _label(_POLARITY_LABELS, document_type.get("base_polarity")),
        "settlement_relevance": _label(_SETTLEMENT_RELEVANCE_LABELS, document_type.get("settlement_relevance")),
        "self_billing": _EMPTY_VALUE if self_billing is None else "Ja" if self_billing else "Nein",
        "buyer_reference": _text(document.get("buyer_reference")),
        "syntax": syntax or _EMPTY_VALUE,
        "format": _text(capabilities.get("format_name")),
        "document_type_recognition": _label(
            _RECOGNITION_CAPABILITY_LABELS, capabilities.get("document_type_recognition")
        ),
        "rendering_scope": _label(_RENDERING_CAPABILITY_LABELS, capabilities.get("rendering")),
        "internal_checks_scope": _label(_INTERNAL_CHECKS_CAPABILITY_LABELS, capabilities.get("internal_checks")),
        "official_validation": _label(_OFFICIAL_VALIDATION_CAPABILITY_LABELS, capabilities.get("official_validation")),
        "business_process": _text(profile.get("business_process_id")),
        "tax_point_date": _format_date(document.get("tax_point_date")),
        "tax_point_date_code": _code_display(document.get("tax_point_date_code")),
        "profile_id": _text(profile.get("id")),
        "ubl_root": _text(document_type.get("ubl_root")),
        "root_compatibility": _label(_ROOT_COMPATIBILITY_LABELS, document_type.get("root_compatibility")),
    }


def _header(analysis: Mapping[str, Any]) -> HeaderPresentation:
    document = _mapping(analysis.get("document"))
    document_type = _mapping(document.get("type"))
    capabilities = _mapping(analysis.get("capabilities"))
    profile = _mapping(analysis.get("profile"))
    totals = _mapping(analysis.get("totals"))
    payment = _mapping(analysis.get("payment"))
    document_kind = _document_kind(document_type)
    subtitle = " · ".join(
        str(item)
        for item in (
            capabilities.get("format_name"),
            profile.get("name"),
            _format_date(document.get("issue_date")) if _present(document.get("issue_date")) else None,
        )
        if _present(item)
    )
    due_date = payment.get("due_date")
    return {
        "document_kind": document_kind,
        "document_type_summary": _document_type_summary(document_type),
        "document_title": " ".join(str(item) for item in (document_kind, document.get("id")) if _present(item)),
        "subtitle": subtitle,
        "payable_label": "Ausstehender Betrag (BT-115)",
        "payable": _format_money(totals.get("payable")),
        "due_date": (
            f"Fällig am {_format_date(due_date)}" if _present(due_date) else "Kein Fälligkeitsdatum angegeben"
        ),
    }


def _resolved_role_label(role: Any) -> str | None:
    if not _present(role) or role == "unknown":
        return None
    return _ROLE_LABELS.get(str(role))


def _role_flow(origin: Any, destination: Any) -> str:
    origin_label = _resolved_role_label(origin)
    destination_label = _resolved_role_label(destination)
    return (
        f"{origin_label} → {destination_label}" if origin_label and destination_label else "Nicht eindeutig ableitbar"
    )


def _payment_flow(analysis: Mapping[str, Any]) -> PaymentFlowPresentation:
    roles = _mapping(analysis.get("roles"))
    payment = _mapping(analysis.get("payment"))
    derivation = roles.get("derivation")
    explanation = {
        "explicit": "Aus den strukturierten Rechnungsangaben ermittelt.",
        "derived": "Aus Dokumenttyp, Zahlbetrag und Parteienrollen abgeleitet.",
        "ambiguous": "Wegen widersprüchlicher Angaben nicht eindeutig ableitbar.",
        "unknown": "Mangels hinreichender Angaben nicht eindeutig ableitbar.",
    }.get(str(derivation), "Mangels hinreichender Angaben nicht eindeutig ableitbar.")
    if roles.get("expected_payment_direction") == "none":
        expected_payment_flow = "Keine Zahlung erwartet"
    else:
        expected_payment_flow = _role_flow(roles.get("expected_payer"), roles.get("expected_recipient"))
    return {
        "document_flow": _role_flow(roles.get("issuer"), roles.get("document_recipient")),
        "expected_payment_flow": expected_payment_flow,
        "note": (
            f"{explanation} Dies ist kein Nachweis, dass eine Zahlung tatsächlich erfolgt ist oder erfolgen muss."
        ),
        "reference": str(payment["reference"]) if _present(payment.get("reference")) else None,
    }


def _tax_code(category: Any) -> str | None:
    raw = _mapping(category).get("value")
    if not _present(raw):
        return None
    shown = str(raw).strip()
    return shown or None


def _normalized_tax_code(category: Any) -> str | None:
    code = _tax_code(category)
    return code.upper() if code is not None else None


def _tax_category_label(category: Any, code: str | None) -> str | None:
    raw_label = _mapping(category).get("label")
    if not _present(raw_label):
        return None
    label = str(raw_label).strip()
    if not label or (code is not None and label.casefold() == code.casefold()):
        return None
    if code is not None:
        for separator in (" – ", " - "):
            prefix = f"{code}{separator}"
            if label.casefold().startswith(prefix.casefold()):
                return label[len(prefix) :]
    return label


def _tax_rate(value: Any) -> Decimal | None:
    if not _present(value):
        return None
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return rate if rate.is_finite() else None


def _line_tax_presentation(line: Mapping[str, Any]) -> LineTaxPresentation:
    category = line.get("tax_category")
    code = _tax_code(category)
    normalized_code = _normalized_tax_code(category)
    label = _tax_category_label(category, code)
    rate = _tax_rate(line.get("tax_rate_percent"))

    if rate is not None:
        shown_rate = _format_number(rate)
        accessible_parts = [f"Steuersatz {shown_rate} Prozent"]
        if code is None:
            secondary = "Kategorie nicht angegeben"
            accessible_parts.append("Steuerkategorie nicht angegeben")
        else:
            secondary = code
            accessible_parts.append(f"Steuerkategorie {code}")
            if label is not None:
                accessible_parts.append(label)
        return {
            "primary": f"{shown_rate} %",
            "primary_kind": "rate",
            "secondary": secondary,
            "accessible_label": ", ".join(accessible_parts),
        }

    if code is not None:
        missing_rate = "ohne Steuersatz" if normalized_code == "O" else "Steuersatz nicht angegeben"
        accessible_parts = [f"Steuerkategorie {code}"]
        if label is not None:
            accessible_parts.append(label)
        accessible_parts.append(missing_rate)
        return {
            "primary": code,
            "primary_kind": "code",
            "secondary": missing_rate,
            "accessible_label": ", ".join(accessible_parts),
        }

    return {
        "primary": _EMPTY_VALUE,
        "primary_kind": "empty",
        "secondary": None,
        "accessible_label": "Steuerangaben nicht angegeben",
    }


def _tax_pair(category: Any, rate: Any) -> tuple[str | None, Decimal | None]:
    return _normalized_tax_code(category), _tax_rate(rate)


def _tax_breakdown_gap_label(line: Mapping[str, Any]) -> str:
    category = line.get("tax_category")
    code = _tax_code(category)
    label = _tax_category_label(category, code)
    category_text = "Kategorie nicht angegeben"
    if code is not None:
        category_text = f"{code} – {label}" if label is not None else code
    rate = _tax_rate(line.get("tax_rate_percent"))
    if rate is not None:
        rate_text = f"{_format_number(rate)} %"
    elif _normalized_tax_code(category) == "O":
        rate_text = "ohne Steuersatz"
    else:
        rate_text = "Steuersatz nicht angegeben"
    return f"{category_text} · {rate_text}"


def _line_tax_presentations(analysis: Mapping[str, Any]) -> tuple[list[LineTaxPresentation], list[str]]:
    lines = [_mapping(line) for line in analysis.get("lines") or []]
    tax = _mapping(analysis.get("tax"))
    breakdown_pairs = {
        _tax_pair(item.get("category"), item.get("rate_percent"))
        for value in tax.get("breakdown") or []
        if (item := _mapping(value))
    }
    gaps: list[str] = []
    seen_gaps: set[tuple[str | None, Decimal | None]] = set()
    for line in lines:
        pair = _tax_pair(line.get("tax_category"), line.get("tax_rate_percent"))
        if pair == (None, None) or pair in breakdown_pairs or pair in seen_gaps:
            continue
        seen_gaps.add(pair)
        gaps.append(_tax_breakdown_gap_label(line))
    return [_line_tax_presentation(line) for line in lines], gaps


def _sections(contract: Mapping[str, Any], scope: ReportScope) -> list[SectionPresentation]:
    definitions = cast(list[dict[str, Any]], contract["sections"])
    return [
        {
            "id": str(section["id"]),
            "label": str(section["label"]),
            "scope": cast(ReportScope, section["scope"]),
        }
        for section in definitions
        if section["scope"] == "readable" or scope == "complete"
    ]


def build_report_presentation(
    analysis: Mapping[str, Any],
    *,
    scope: ReportScope = "readable",
) -> ReportPresentation:
    """Create the shared, localized report view without changing schema 2."""

    if scope not in {"readable", "complete"}:
        raise ValueError(f"Unbekannter Berichtsumfang: {scope!r}")
    contract = load_presentation_contract()
    fact_values = _fact_values(analysis)
    facts: list[FactPresentation] = [
        {
            "key": str(definition["key"]),
            "label": str(definition["label"]),
            "value": fact_values[str(definition["key"])],
        }
        for definition in cast(list[dict[str, Any]], contract["header_facts"])
    ]
    include_technical = scope == "complete"
    assessment = _mapping(analysis.get("assessment"))
    official = _mapping(assessment.get("official"))
    technical = _mapping(analysis.get("technical"))
    line_taxes, tax_breakdown_gaps = _line_tax_presentations(analysis)
    return {
        "scope": scope,
        "include_technical": include_technical,
        "overall_status": _overall_status(analysis, contract),
        "axes": _axes(analysis, contract),
        "header": _header(analysis),
        "header_facts": facts,
        "payment_flow": _payment_flow(analysis),
        "line_taxes": line_taxes,
        "tax_breakdown_gaps": tax_breakdown_gaps,
        "sections": _sections(contract, scope),
        "technical": {
            "official_raw_report": (
                str(official["raw_report"]) if include_technical and _present(official.get("raw_report")) else None
            ),
            "official_technical_output": (
                str(official["technical_output"])
                if include_technical and _present(official.get("technical_output"))
                else None
            ),
            "fields": list(technical.get("fields") or []) if include_technical else [],
            "source_xml": (
                str(technical.get("source_xml") or technical.get("pretty_xml"))
                if include_technical and _present(technical.get("source_xml") or technical.get("pretty_xml"))
                else None
            ),
        },
    }


__all__ = [
    "ReportPresentation",
    "ReportScope",
    "build_report_presentation",
    "load_presentation_contract",
]
