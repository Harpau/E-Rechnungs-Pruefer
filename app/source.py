from __future__ import annotations

from collections import Counter
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
MAX_PDF_ATTACHMENTS = 100


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


def _attachment_priority(name: str, is_xml: bool) -> int | None:
    lower_name = name.casefold()
    try:
        return PREFERRED_EMBEDDED_XML_NAMES.index(lower_name)
    except ValueError:
        if lower_name.endswith(".xml"):
            return len(PREFERRED_EMBEDDED_XML_NAMES)
        if is_xml:
            return len(PREFERRED_EMBEDDED_XML_NAMES) + 10
        return None


def _embedded_file_kind(name: str) -> str:
    return "XML-Datei" if name.casefold().endswith(".xml") else "PDF-Datei"


def _extract_pdf_xml(
    data: bytes,
    filename: str,
    media_type: str,
    *,
    max_embedded_bytes: int | None = None,
) -> ExtractedSource:
    try:
        reader = PdfReader(BytesIO(data), strict=False)
    except Exception as exc:
        raise InvoiceInputError(f"Die PDF-Datei konnte nicht gelesen werden: {exc}") from exc

    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                raise InvoiceInputError("Kennwortgeschützte PDF-Dateien werden nicht unterstützt.")
        except InvoiceInputError:
            raise
        except Exception as exc:
            raise InvoiceInputError("Kennwortgeschützte PDF-Dateien werden nicht unterstützt.") from exc

    attachment_rows: list[dict[str, Any]] = []
    invoice_candidates: list[tuple[int, str, bytes, bool]] = []
    total_embedded_bytes = 0

    try:
        embedded_files = []
        for embedded_file in reader.attachment_list:
            embedded_files.append(embedded_file)
            if len(embedded_files) > MAX_PDF_ATTACHMENTS:
                raise InvoiceInputError(f"Die PDF enthält mehr als {MAX_PDF_ATTACHMENTS} eingebettete Dateien.")
        attachment_names = [str(item.alternative_name or item.name) for item in embedded_files]
        attachment_name_counts = Counter(attachment_names)
        attachment_name_indexes: Counter[str] = Counter()

        for embedded_file, attachment_name in zip(embedded_files, attachment_names, strict=True):
            attachment_name_indexes[attachment_name] += 1
            shown_name = attachment_name
            if attachment_name_counts[attachment_name] > 1:
                shown_name = f"{attachment_name} ({attachment_name_indexes[attachment_name]})"

            declared_size = embedded_file.size
            if max_embedded_bytes is not None and declared_size is not None and declared_size > max_embedded_bytes:
                raise InvoiceInputError(
                    f"Eine eingebettete {_embedded_file_kind(attachment_name)} "
                    "überschreitet die zulässige Größenbegrenzung."
                )

            payload = embedded_file.content
            if max_embedded_bytes is not None and len(payload) > max_embedded_bytes:
                raise InvoiceInputError(
                    f"Eine eingebettete {_embedded_file_kind(attachment_name)} "
                    "überschreitet die zulässige Größenbegrenzung."
                )
            total_embedded_bytes += len(payload)
            if max_embedded_bytes is not None and total_embedded_bytes > max_embedded_bytes:
                raise InvoiceInputError(
                    "Die eingebetteten PDF-Dateien überschreiten zusammen die zulässige Größenbegrenzung."
                )

            is_xml = _looks_like_xml(payload)
            attachment_rows.append(
                {
                    "name": shown_name,
                    "size": len(payload),
                    "sha256": sha256_hex(payload),
                    "is_xml": is_xml,
                }
            )
            priority = _attachment_priority(attachment_name, is_xml)
            if priority is not None:
                invoice_candidates.append((priority, attachment_name, payload, is_xml))
        page_count = len(reader.pages)
    except InvoiceInputError:
        raise
    except Exception as exc:
        raise InvoiceInputError(f"Eingebettete PDF-Dateien konnten nicht ausgelesen werden: {exc}") from exc

    if not invoice_candidates:
        raise InvoiceInputError(
            "Die PDF enthält keine erkennbare eingebettete XML-Rechnung. "
            "Eine reine Sicht-PDF ist keine auswertbare strukturierte E-Rechnung."
        )

    invoice_candidates.sort(key=lambda item: (item[0], item[1].casefold()))
    selected_priority, xml_name, xml_bytes, selected_is_xml = invoice_candidates[0]
    same_name_candidates = [
        payload
        for priority, candidate_name, payload, _is_xml in invoice_candidates
        if priority == selected_priority and candidate_name.casefold() == xml_name.casefold()
    ]
    if any(payload != xml_bytes for payload in same_name_candidates):
        raise InvoiceInputError(
            "Die PDF enthält mehrere gleichnamige XML-Rechnungskandidaten mit unterschiedlichen Inhalten."
        )
    if not selected_is_xml:
        raise InvoiceInputError(
            f"Der bevorzugte eingebettete Rechnungskandidat {xml_name!r} enthält keine erkennbare XML-Datei."
        )
    return ExtractedSource(
        xml_bytes=xml_bytes,
        xml_filename=xml_name,
        original_filename=filename,
        original_media_type=media_type or "application/pdf",
        original_size=len(data),
        original_sha256=sha256_hex(data),
        container={
            "type": "PDF mit eingebetteter XML",
            "page_count": page_count,
            "selected_attachment": xml_name,
            "attachment_count": len(attachment_rows),
        },
        attachments=attachment_rows,
    )


def extract_source(
    data: bytes,
    filename: str,
    media_type: str | None = None,
    *,
    max_embedded_bytes: int | None = None,
) -> ExtractedSource:
    safe_name = Path(filename or "rechnung.xml").name
    detected_type = media_type or "application/octet-stream"

    if _looks_like_pdf(data):
        return _extract_pdf_xml(
            data,
            safe_name,
            detected_type,
            max_embedded_bytes=max_embedded_bytes,
        )

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
