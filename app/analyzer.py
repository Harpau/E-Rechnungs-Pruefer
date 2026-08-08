from __future__ import annotations

import time
from copy import deepcopy
from decimal import Decimal
from typing import Any

from lxml import etree

from . import __version__
from .analysis_builder import build_analysis_response
from .parsers.cii import parse_cii
from .parsers.common import empty_party
from .parsers.namespaces import CII_ROOT_NAMESPACE, UBL_ROOT_NAMESPACES
from .parsers.ubl import parse_ubl
from .profiles import OfficialValidationCapability, resolve_profile
from .settings import Settings, settings
from .source import ExtractedSource, extract_source
from .validators.builtin import validate_builtin
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
    if root_name == "CrossIndustryInvoice" and root_namespace == CII_ROOT_NAMESPACE:
        return parse_cii(root), None
    if UBL_ROOT_NAMESPACES.get(root_name) == root_namespace:
        return parse_ubl(root), None
    return _unknown_document(root), (
        f"Das Wurzelelement {root_name!r} wird nicht als CII CrossIndustryInvoice, UBL Invoice oder UBL CreditNote erkannt."
    )


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

    root = safe_parse_xml(
        source.xml_bytes,
        max_structure_items=app_settings.max_xml_structure_items,
    )
    parsed, syntax_error = _detect_and_parse(root)

    technical = technical_rows(
        root,
        app_settings.max_technical_rows,
        include_namespaces=True,
        max_seconds=app_settings.max_technical_seconds,
    )
    raw_xml = pretty_xml(root)

    working: dict[str, Any] = deepcopy(parsed)
    working["source"] = {
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
    working["technical"] = {
        "root_element": local_name(root),
        "root_namespace": namespace_uri(root),
        "field_count": len(technical.rows),
        "truncated": technical.truncated,
        "limit_reason": technical.limit_reason,
        "rows": technical.rows,
        "raw_xml": raw_xml,
        "original_xml": decode_xml_bytes(source.xml_bytes),
    }

    if syntax_error:
        builtin = None
    else:
        builtin = validate_builtin(working)

    kosit = KositValidator(app_settings)
    profile_value = parsed.get("profile")
    document_value = parsed.get("document")
    profile_data: dict[str, Any] = profile_value if isinstance(profile_value, dict) else {}
    document_data: dict[str, Any] = document_value if isinstance(document_value, dict) else {}
    profile_id = profile_data.get("id") or document_data.get("profile_id")
    profile_capability = resolve_profile(profile_id).capabilities.official_validation
    if (
        run_official_validation
        and app_settings.kosit_enabled
        and profile_capability is OfficialValidationCapability.BUNDLED
    ):
        official = kosit.validate(source.xml_bytes, source.xml_filename)
    else:
        state = kosit.configuration_state()
        if not run_official_validation:
            summary = "Offizielle KoSIT-Prüfung wurde für diesen Aufruf nicht ausgeführt."
        elif profile_capability is OfficialValidationCapability.NOT_BUNDLED:
            summary = "Für das erkannte Profil ist keine offizielle Prüfung im gebündelten KoSIT-Regelwerk enthalten."
        elif profile_capability is OfficialValidationCapability.UNKNOWN:
            summary = "Für das Profil konnte kein unterstütztes offizielles KoSIT-Regelwerk sicher bestimmt werden."
        else:
            summary = "Die angeforderte offizielle KoSIT-Prüfung ist nicht verfügbar."
        official = {
            **state,
            "executed": False,
            "accepted": None,
            "exit_code": None,
            "summary": summary,
            "findings": [],
            "raw_report": None,
        }

    duration_ms = Decimal(str(round((time.perf_counter() - started) * 1000, 2)))
    return build_analysis_response(
        working,
        builtin=builtin,
        official=official,
        official_requested=run_official_validation,
        syntax_error=syntax_error,
        duration_ms=duration_ms,
        application_version=__version__,
    ).model_dump(mode="json")
