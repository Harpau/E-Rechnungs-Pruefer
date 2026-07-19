from __future__ import annotations

import time
from copy import deepcopy
from typing import Any

from lxml import etree

from . import __version__
from .parsers.cii import parse_cii
from .parsers.common import empty_party
from .parsers.ubl import parse_ubl
from .settings import Settings, settings
from .source import ExtractedSource, extract_source
from .validators.builtin import SEVERITY_ORDER, validate_builtin
from .validators.kosit import KositValidator
from .xml_utils import (
    InvoiceInputError,
    decode_xml_bytes,
    local_name,
    namespace_uri,
    pretty_xml,
    safe_parse_xml,
    sha256_hex,
    technical_rows,
)


def _unknown_document(root: etree._Element) -> dict[str, Any]:
    return {
        "document": {
            "syntax": "UNKNOWN",
            "format": f"Nicht unterstützte XML-Syntax ({local_name(root)})",
            "profile_id": None,
            "profile_name": "Nicht erkannt",
            "id": None,
            "type_code": None,
            "type_label": None,
            "kind": "Unbekanntes Dokument",
            "issue_date": None,
            "due_date": None,
            "tax_point_date": None,
            "delivery_date": None,
            "currency": None,
            "currency_label": None,
            "buyer_reference": None,
            "notes": [],
        },
        "seller": empty_party(),
        "buyer": empty_party(),
        "payee": empty_party(),
        "invoicee": empty_party(),
        "ship_to": empty_party(),
        "lines": [],
        "taxes": [],
        "totals": {},
        "payment": {"reference": None, "means": [], "terms": []},
        "references": {
            "buyer_order": None,
            "seller_order": None,
            "contract": None,
            "project": None,
            "preceding_invoices": [],
            "additional_documents": [],
        },
        "header_allowances_charges": [],
        "delivery": {},
        "profile": {"id": None, "name": "Nicht erkannt", "business_process_id": None},
    }


def _detect_and_parse(root: etree._Element) -> tuple[dict[str, Any], str | None]:
    root_name = local_name(root)
    root_namespace = namespace_uri(root) or ""
    if root_name == "CrossIndustryInvoice":
        return parse_cii(root), None
    if root_name in {"Invoice", "CreditNote"} and (
        "oasis:names:specification:ubl" in root_namespace.lower()
        or root.xpath("./*[local-name()='AccountingSupplierParty']")
    ):
        return parse_ubl(root), None
    return _unknown_document(root), (
        f"Das Wurzelelement {root_name!r} wird nicht als CII CrossIndustryInvoice, UBL Invoice oder UBL CreditNote erkannt."
    )


def _namespace_rows(root: etree._Element) -> list[dict[str, str | None]]:
    rows: list[dict[str, str | None]] = []
    root_path = f"/{local_name(root)}[1]"
    for prefix, uri in sorted(root.nsmap.items(), key=lambda item: item[0] or ""):
        shown = "xmlns" if prefix is None else f"xmlns:{prefix}"
        rows.append(
            {
                "kind": "namespace",
                "path": f"{root_path}/@{shown}",
                "name": shown,
                "namespace": None,
                "value": uri,
            }
        )
    return rows


def _count_findings(findings: list[dict[str, Any]]) -> dict[str, int]:
    return {
        severity: sum(1 for item in findings if item.get("severity") == severity)
        for severity in ("error", "warning", "info")
    }


def analyze_bytes(
    data: bytes,
    filename: str,
    media_type: str | None = None,
    *,
    run_official_validation: bool = True,
    app_settings: Settings = settings,
) -> dict[str, Any]:
    started = time.perf_counter()
    if len(data) > app_settings.max_upload_bytes:
        limit_mb = app_settings.max_upload_bytes / (1024 * 1024)
        raise InvoiceInputError(f"Die Datei ist größer als die zulässigen {limit_mb:g} MB.")

    source: ExtractedSource = extract_source(
        data,
        filename,
        media_type,
        max_embedded_bytes=app_settings.max_upload_bytes,
    )
    if len(source.xml_bytes) > app_settings.max_upload_bytes:
        raise InvoiceInputError("Die eingebettete XML-Datei überschreitet die zulässige Größenbegrenzung.")

    root = safe_parse_xml(source.xml_bytes)
    parsed, syntax_error = _detect_and_parse(root)

    rows, truncated = technical_rows(root, app_settings.max_technical_rows)
    rows = _namespace_rows(root) + rows
    raw_xml = pretty_xml(root)

    analysis: dict[str, Any] = deepcopy(parsed)
    analysis["source"] = {
        "filename": source.original_filename,
        "media_type": source.original_media_type,
        "size": source.original_size,
        "sha256": source.original_sha256,
        "xml_filename": source.xml_filename,
        "xml_size": len(source.xml_bytes),
        "xml_sha256": sha256_hex(source.xml_bytes),
        "container": source.container,
        "attachments": source.attachments,
    }
    analysis["technical"] = {
        "root_element": local_name(root),
        "root_namespace": namespace_uri(root),
        "field_count": len(rows),
        "truncated": truncated,
        "rows": rows,
        "raw_xml": raw_xml,
        "original_xml": decode_xml_bytes(source.xml_bytes),
    }

    if syntax_error:
        builtin = {
            "status": "invalid",
            "counts": {"error": 1, "warning": 0, "info": 0},
            "findings": [
                {
                    "id": "SYNTAX-001",
                    "severity": "error",
                    "title": "Nicht unterstützte E-Rechnungssyntax",
                    "message": syntax_error,
                    "location": f"/{local_name(root)}",
                    "actual": namespace_uri(root),
                    "expected": "CII CrossIndustryInvoice oder UBL Invoice/CreditNote",
                    "source": "Interne Prüfung",
                }
            ],
            "scope": "Nur die technische XML-Darstellung konnte erzeugt werden.",
        }
    else:
        builtin = validate_builtin(analysis)

    kosit = KositValidator(app_settings)
    if run_official_validation and app_settings.kosit_enabled:
        official = kosit.validate(source.xml_bytes, source.xml_filename)
    else:
        state = kosit.configuration_state()
        official = {
            **state,
            "executed": False,
            "accepted": None,
            "exit_code": None,
            "summary": "Offizielle KoSIT-Prüfung wurde für diesen Aufruf nicht ausgeführt.",
            "findings": [],
            "raw_report": None,
        }

    combined = list(builtin.get("findings", [])) + list(official.get("findings", []))
    combined.sort(
        key=lambda item: (
            SEVERITY_ORDER.get(item.get("severity", "info"), 99),
            item.get("source") or "",
            item.get("id") or "",
        )
    )
    counts = _count_findings(combined)
    if official.get("executed") and official.get("accepted") is False:
        status = "invalid"
    elif counts["error"]:
        status = "invalid"
    elif counts["warning"]:
        status = "warning"
    else:
        status = "ok"

    analysis["validation"] = {
        "status": status,
        "counts": counts,
        "findings": combined,
        "builtin": builtin,
        "official": official,
        "assessment": (
            "Offizielle KoSIT-Konformitätsprüfung ausgeführt."
            if official.get("executed")
            else "Interne Prüfung; KoSIT-Konformitätsprüfung nicht ausgeführt."
        ),
    }
    analysis["processing"] = {
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "application_version": __version__,
    }
    return analysis
