"""Synchronous invoice operations for the bounded worker; never start the web application."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from .. import __version__
from ..analyzer import analyze_bytes
from ..api_models import AnalysisResponse
from ..configuration import Settings, settings_to_snapshot
from ..pdf_report import render_pdf_report
from ..report_presentation import ReportScope, build_report_presentation
from ..report_templates import report_environment
from ..source import PdfResourceLimits, ProcessingLimitError, extract_source
from .result import DEFAULT_OPERATION_LIMITS, result_limit, validate_result_metadata
from .result import OperationLimits as OperationLimits
from .result import OperationResult as OperationResult

DEFAULT_PDF_LIMITS = PdfResourceLimits()


def _collect(chunks: Iterable[str], maximum: int) -> bytes:
    result = bytearray()
    for chunk in chunks:
        encoded = chunk.encode("utf-8")
        if len(encoded) > maximum - len(result):
            raise ProcessingLimitError("Die Ausgabe überschreitet das zulässige Verarbeitungsbudget.")
        result.extend(encoded)
    return bytes(result)


def _no_direct_validation(data: bytes, filename: str) -> dict[str, Any]:
    del data, filename
    raise RuntimeError("Die kontrollierte KoSIT-Schnittstelle ist nicht verfügbar.")


def execute_operation(
    operation: str,
    data: bytes,
    filename: str,
    media_type: str | None = None,
    *,
    app_settings: Settings,
    official: bool = True,
    scope: str = "readable",
    official_validator: Callable[[bytes, str], dict[str, Any]] | None = None,
    official_state: Mapping[str, Any] | None = None,
    limits: OperationLimits = DEFAULT_OPERATION_LIMITS,
    pdf_limits: PdfResourceLimits = DEFAULT_PDF_LIMITS,
) -> OperationResult:
    """Return complete serialized output; callers own process limits and transport."""
    maximum = result_limit(operation, limits)
    if scope not in {"readable", "complete"} or type(official) is not bool:
        raise ValueError("Ungültige Verarbeitungsoptionen.")
    settings_to_snapshot(app_settings)
    if not isinstance(data, bytes) or len(data) > app_settings.max_upload_bytes:
        raise ProcessingLimitError("Die Eingabe überschreitet das zulässige Verarbeitungsbudget.")
    headers: dict[str, str] = {}
    if operation == "export_xml":
        source = extract_source(
            data,
            filename,
            media_type,
            max_embedded_bytes=app_settings.max_upload_bytes,
            resource_limits=pdf_limits,
        )
        name = Path(source.xml_filename).name
        stem = Path(name).stem if Path(name).suffix else name
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")[:120] or "rechnung"
        headers["Content-Disposition"] = f'attachment; filename="{safe_stem}.xml"'
        result = OperationResult(source.xml_bytes, "application/xml", headers)
    else:
        state = (
            official_state
            if official_state is not None
            else {
                "configured": False,
                "problems": ["KoSIT-Konfigurationsstatus wurde nicht übergeben."],
                "jar": None,
                "jar_main_class": None,
                "scenarios": [],
                "repositories": [],
            }
        )
        analysis = analyze_bytes(
            data,
            filename,
            media_type,
            run_official_validation=official,
            app_settings=app_settings,
            official_validator=official_validator or _no_direct_validation,
            official_state=state,
            resource_limits=pdf_limits,
        )
        # The parent must never need to import or instantiate a full invoice model.
        analysis = AnalysisResponse.model_validate(analysis).model_dump(mode="json")
        if operation == "analyze":
            encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            result = OperationResult(_collect(encoder.iterencode(analysis), maximum), "application/json", headers)
        else:
            report_scope = cast(ReportScope, scope)
            presentation = build_report_presentation(analysis, scope=report_scope)
            assessment = analysis["assessment"]
            headers.update(
                {
                    "X-Einvoice-Analysis-Schema": str(analysis["schema_version"]),
                    "X-Einvoice-Syntax": str(analysis["capabilities"]["syntax"]),
                    "X-Einvoice-Conformity-Status": str(assessment["official"]["status"]),
                    "X-Einvoice-Internal-Status": str(assessment["internal"]["status"]),
                    "X-Einvoice-Processing-Status": str(assessment["processing"]["status"]),
                    "X-Einvoice-Report-Scope": report_scope,
                }
            )
            generated_at = datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S %Z")
            if operation == "report_html":
                chunks = (
                    report_environment()
                    .get_template("report.html")
                    .generate(
                        analysis=analysis,
                        presentation=presentation,
                        report_scope=report_scope,
                        generated_at=generated_at,
                        version=__version__,
                    )
                )
                headers["Content-Disposition"] = 'inline; filename="E-Rechnungs-Pruefbericht.html"'
                result = OperationResult(_collect(chunks, maximum), "text/html", headers)
            else:
                body = render_pdf_report(
                    analysis,
                    generated_at=generated_at,
                    version=__version__,
                    scope=report_scope,
                    presentation=presentation,
                )
                headers["Content-Disposition"] = 'attachment; filename="E-Rechnungs-Pruefbericht.pdf"'
                result = OperationResult(body, "application/pdf", headers)
    if len(result.body) > maximum:
        raise ProcessingLimitError("Die Ausgabe überschreitet das zulässige Verarbeitungsbudget.")
    validate_result_metadata(result.metadata(), operation=operation, limits=limits, expected_scope=scope)
    return result
