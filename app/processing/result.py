"""Small parent-safe result contract; no invoice parsers, renderers or application imports."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any


@dataclass(frozen=True, slots=True)
class OperationLimits:
    json_bytes: int = 128 * 1024 * 1024
    html_bytes: int = 128 * 1024 * 1024
    pdf_bytes: int = 64 * 1024 * 1024
    xml_bytes: int = 25 * 1024 * 1024

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 0 < value <= 2**31 - 1:
                raise ValueError("Ungültige Ergebnisgrößenbegrenzung.")


OPERATION_MEDIA_TYPES = {
    "analyze": "application/json",
    "export_xml": "application/xml",
    "report_html": "text/html",
    "report_pdf": "application/pdf",
}
_LIMIT_FIELDS = {
    "analyze": "json_bytes",
    "export_xml": "xml_bytes",
    "report_html": "html_bytes",
    "report_pdf": "pdf_bytes",
}
_REPORT_HEADERS = {
    "X-Einvoice-Analysis-Schema": {"2"},
    "X-Einvoice-Syntax": {"CII", "UBL", "UNKNOWN"},
    "X-Einvoice-Conformity-Status": {
        "accepted",
        "rejected",
        "not-requested",
        "unsupported",
        "unavailable",
        "indeterminate",
    },
    "X-Einvoice-Internal-Status": {"clear", "attention", "errors", "not-run"},
    "X-Einvoice-Processing-Status": {"complete", "limited", "incomplete"},
    "X-Einvoice-Report-Scope": {"readable", "complete"},
}
DEFAULT_OPERATION_LIMITS = OperationLimits()


def result_limit(operation: str, limits: OperationLimits) -> int:
    if operation not in _LIMIT_FIELDS:
        raise ValueError("Unbekannte Verarbeitungsoperation.")
    return int(getattr(limits, _LIMIT_FIELDS[operation]))


def validate_result_metadata(
    metadata: object,
    *,
    operation: str,
    limits: OperationLimits = DEFAULT_OPERATION_LIMITS,
    expected_scope: str | None = None,
) -> dict[str, Any]:
    """Validate only a bounded envelope; invoice JSON remains opaque to the parent."""
    maximum = result_limit(operation, limits)
    if not isinstance(metadata, Mapping) or set(metadata) != {"status_code", "media_type", "headers", "body_size"}:
        raise ValueError("Ungültige Ergebnismetadaten.")
    if type(metadata["status_code"]) is not int or metadata["status_code"] != 200:
        raise ValueError("Ungültiger Erfolgsstatus.")
    if metadata["media_type"] != OPERATION_MEDIA_TYPES[operation]:
        raise ValueError("Ungültiger Ergebnismedientyp.")
    if type(metadata["body_size"]) is not int or not 0 < metadata["body_size"] <= maximum:
        raise ValueError("Ungültige Ergebnisgröße.")
    headers = metadata["headers"]
    if not isinstance(headers, dict) or any(not isinstance(v, str) or len(v) > 256 for v in headers.values()):
        raise ValueError("Ungültige Ergebnisheader.")
    expected = (
        set(_REPORT_HEADERS) | {"Content-Disposition"}
        if operation.startswith("report_")
        else ({"Content-Disposition"} if operation == "export_xml" else set())
    )
    if set(headers) != expected:
        raise ValueError("Unbekannte oder fehlende Ergebnisheader.")
    if operation.startswith("report_"):
        if any(headers[key] not in choices for key, choices in _REPORT_HEADERS.items()):
            raise ValueError("Ungültiger Schema-2-Berichtsstatus.")
        if expected_scope is not None and headers["X-Einvoice-Report-Scope"] != expected_scope:
            raise ValueError("Berichtsumfang passt nicht zum Auftrag.")
        disposition = (
            'inline; filename="E-Rechnungs-Pruefbericht.html"'
            if operation == "report_html"
            else 'attachment; filename="E-Rechnungs-Pruefbericht.pdf"'
        )
        if headers["Content-Disposition"] != disposition:
            raise ValueError("Ungültiger Berichtsdownloadname.")
    elif operation == "export_xml" and not re.fullmatch(
        r'attachment; filename="[A-Za-z0-9_-][A-Za-z0-9._-]{0,119}\.xml"', headers["Content-Disposition"]
    ):
        raise ValueError("Ungültiger XML-Downloadname.")
    return {**metadata, "headers": dict(headers)}


@dataclass(frozen=True, slots=True)
class OperationResult:
    body: bytes
    media_type: str
    headers: dict[str, str]

    def metadata(self) -> dict[str, Any]:
        return {
            "status_code": 200,
            "media_type": self.media_type,
            "headers": dict(self.headers),
            "body_size": len(self.body),
        }
