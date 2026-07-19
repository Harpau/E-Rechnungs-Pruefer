from __future__ import annotations

from lxml import etree

from ..code_lists import (
    country_label,
    currency_label,
    document_type_label,
    payment_means_label,
    tax_basis_label,
    tax_category_display,
    tax_category_label,
    unit_label,
)
from ..xml_utils import attr_value, clean_text, first_node, first_text, parse_date_value


def profile_name(profile_id: str | None) -> str:
    value = (profile_id or "").lower()
    if "xrechnung" in value:
        return "XRechnung"
    if "peppol" in value or "poacc" in value:
        return "Peppol BIS Billing 3.0"
    if "factur-x" in value:
        return "Factur-X"
    if "zugferd" in value:
        return "ZUGFeRD"
    if "en16931" in value or "en:16931" in value:
        return "EN 16931"
    if profile_id:
        return "Unbekanntes/individuelles Profil"
    return "Nicht angegeben"


def document_kind(type_code: str | None, root_kind: str | None = None) -> str:
    if root_kind == "CreditNote" or type_code in {"381", "396", "532"}:
        return "Gutschrift"
    if type_code in {"384"}:
        return "Korrekturrechnung"
    return "Rechnung"


def document_meta(
    *,
    syntax: str,
    format_name: str,
    profile_id: str | None,
    document_id: str | None,
    type_code: str | None,
    issue_date: str | None,
    due_date: str | None,
    tax_point_date: str | None,
    delivery_date: str | None,
    currency: str | None,
    buyer_reference: str | None,
    notes: list[str],
    root_kind: str | None = None,
) -> dict:
    return {
        "syntax": syntax,
        "format": format_name,
        "profile_id": profile_id,
        "profile_name": profile_name(profile_id),
        "id": document_id,
        "type_code": type_code,
        "type_label": document_type_label(type_code),
        "kind": document_kind(type_code, root_kind),
        "issue_date": issue_date,
        "due_date": due_date,
        "tax_point_date": tax_point_date,
        "delivery_date": delivery_date,
        "currency": currency,
        "currency_label": currency_label(currency),
        "buyer_reference": buyer_reference,
        "notes": notes,
    }


def empty_party() -> dict:
    return {
        "name": None,
        "trading_name": None,
        "description": None,
        "ids": [],
        "tax_ids": [],
        "endpoint": None,
        "contact": {
            "name": None,
            "department": None,
            "phone": None,
            "email": None,
        },
        "address": {
            "line1": None,
            "line2": None,
            "line3": None,
            "postcode": None,
            "city": None,
            "subdivision": None,
            "country_code": None,
            "country": None,
        },
    }


def id_entry(node: etree._Element | None, *, scheme_attribute: str = "schemeID") -> dict | None:
    if node is None:
        return None
    value = clean_text(node)
    if not value:
        return None
    return {"value": value, "scheme": attr_value(node, scheme_attribute)}


def make_amount(node: etree._Element | None) -> dict | None:
    if node is None:
        return None
    value = clean_text(node)
    if value is None:
        return None
    return {
        "value": value,
        "currency": attr_value(node, "currencyID") or attr_value(node, "currency"),
    }


def parse_period(
    node: etree._Element | None,
    start_expr: str,
    end_expr: str,
    description_expr: str | None = None,
) -> dict | None:
    if node is None:
        return None
    start_node = first_node(node, start_expr)
    end_node = first_node(node, end_expr)
    start = (
        parse_date_value(clean_text(start_node), attr_value(start_node, "format")) if start_node is not None else None
    )
    end = parse_date_value(clean_text(end_node), attr_value(end_node, "format")) if end_node is not None else None
    description = first_text(node, description_expr) if description_expr else None
    if not any((start, end, description)):
        return None
    return {"start": start, "end": end, "description": description}


def readable_unit(code: str | None) -> str | None:
    return unit_label(code)


def readable_tax_category(code: str | None) -> str | None:
    return tax_category_label(code)


def readable_tax_category_display(code: str | None) -> str | None:
    return tax_category_display(code)


def readable_tax_basis_label(code: str | None) -> str:
    return tax_basis_label(code)


def readable_payment_means(code: str | None) -> str | None:
    return payment_means_label(code)


def readable_country(code: str | None) -> str | None:
    return country_label(code)
