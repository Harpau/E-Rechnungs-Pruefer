from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .xml_utils import InvoiceInputError, sha256_hex

PREFERRED_EMBEDDED_XML_NAMES = (
    "factur-x.xml",
    "zugferd-invoice.xml",
    "xrechnung.xml",
    "invoice.xml",
    "creditnote.xml",
)


@dataclass(slots=True)
class ExtractedSource:
    xml_bytes: bytes
    xml_filename: str
    original_filename: str
    original_media_type: str
    original_size: int
    original_sha256: str
    container: dict[str, Any] = field(default_factory=dict)
    attachments: list[dict[str, Any]] = field(default_factory=list)


def _looks_like_pdf(data: bytes) -> bool:
    return b"%PDF-" in data[:1024]


def _looks_like_xml(data: bytes) -> bool:
    if data.startswith((b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")):
        try:
            return data.decode("utf-32").lstrip().startswith("<")
        except UnicodeDecodeError:
            return False
    if data.startswith((b"\xfe\xff", b"\xff\xfe")):
        try:
            return data.decode("utf-16").lstrip().startswith("<")
        except UnicodeDecodeError:
            return False
    stripped = data.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    return stripped.startswith(b"<")


def _normalise_attachment_values(value: Any) -> list[bytes]:
    if isinstance(value, (bytes, bytearray)):
        return [bytes(value)]
    if isinstance(value, list):
        return [bytes(item) for item in value if isinstance(item, (bytes, bytearray))]
    try:
        return [bytes(value)]
    except Exception:
        return []


def _extract_pdf_xml(data: bytes, filename: str, media_type: str) -> ExtractedSource:
    try:
        reader = PdfReader(BytesIO(data), strict=False)
    except Exception as exc:
        raise InvoiceInputError(f"Die PDF-Datei konnte nicht gelesen werden: {exc}") from exc

    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                raise InvoiceInputError("Verschlüsselte PDF-Dateien werden nicht unterstützt.")
        except InvoiceInputError:
            raise
        except Exception as exc:
            raise InvoiceInputError("Verschlüsselte PDF-Dateien werden nicht unterstützt.") from exc

    attachment_rows: list[dict[str, Any]] = []
    xml_candidates: list[tuple[int, str, bytes]] = []

    try:
        attachments = reader.attachments
        items = attachments.items() if hasattr(attachments, "items") else []
        for attachment_name, value in items:
            values = _normalise_attachment_values(value)
            for index, payload in enumerate(values, start=1):
                shown_name = str(attachment_name)
                if len(values) > 1:
                    shown_name = f"{shown_name} ({index})"
                attachment_rows.append(
                    {
                        "name": shown_name,
                        "size": len(payload),
                        "sha256": sha256_hex(payload),
                        "is_xml": _looks_like_xml(payload),
                    }
                )
                if _looks_like_xml(payload):
                    lower_name = str(attachment_name).lower()
                    try:
                        priority = PREFERRED_EMBEDDED_XML_NAMES.index(lower_name)
                    except ValueError:
                        priority = len(PREFERRED_EMBEDDED_XML_NAMES) + (0 if lower_name.endswith(".xml") else 10)
                    xml_candidates.append((priority, str(attachment_name), payload))
    except Exception as exc:
        raise InvoiceInputError(f"Eingebettete PDF-Dateien konnten nicht ausgelesen werden: {exc}") from exc

    if not xml_candidates:
        raise InvoiceInputError(
            "Die PDF enthält keine erkennbare eingebettete XML-Rechnung. "
            "Eine reine Sicht-PDF ist keine auswertbare strukturierte E-Rechnung."
        )

    xml_candidates.sort(key=lambda item: (item[0], item[1].lower()))
    _, xml_name, xml_bytes = xml_candidates[0]
    return ExtractedSource(
        xml_bytes=xml_bytes,
        xml_filename=xml_name,
        original_filename=filename,
        original_media_type=media_type or "application/pdf",
        original_size=len(data),
        original_sha256=sha256_hex(data),
        container={
            "type": "PDF mit eingebetteter XML",
            "page_count": len(reader.pages),
            "selected_attachment": xml_name,
            "attachment_count": len(attachment_rows),
        },
        attachments=attachment_rows,
    )


def extract_source(data: bytes, filename: str, media_type: str | None = None) -> ExtractedSource:
    safe_name = Path(filename or "rechnung.xml").name
    detected_type = media_type or "application/octet-stream"

    if _looks_like_pdf(data):
        return _extract_pdf_xml(data, safe_name, detected_type)

    if _looks_like_xml(data):
        return ExtractedSource(
            xml_bytes=data,
            xml_filename=safe_name,
            original_filename=safe_name,
            original_media_type=detected_type if detected_type != "application/octet-stream" else "application/xml",
            original_size=len(data),
            original_sha256=sha256_hex(data),
            container={"type": "XML-Datei"},
            attachments=[],
        )

    raise InvoiceInputError("Unterstützt werden XML-Rechnungen sowie PDF-Dateien mit eingebetteter XML-Rechnung.")
