from __future__ import annotations

from collections import Counter
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from pypdf import PdfReader, apply_configuration, get_configuration
from pypdf.errors import LimitReachedError
from pypdf.filters import decode_stream_data
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NullObject, PdfObject, StreamObject

from .xml_utils import InvoiceInputError, sha256_hex

PREFERRED_EMBEDDED_XML_NAMES = (
    "factur-x.xml",
    "zugferd-invoice.xml",
    "xrechnung.xml",
    "invoice.xml",
    "creditnote.xml",
)
MAX_PDF_ATTACHMENTS = 100


class ProcessingLimitError(InvoiceInputError):
    """A known resource budget was exceeded, not an invoice-conformity decision."""


@dataclass(frozen=True, slots=True)
class PdfResourceLimits:
    structure_stream_bytes: int = 25 * 1024 * 1024
    page_tree_entries: int = 10_000
    page_tree_depth: int = 64

    def __post_init__(self) -> None:
        for value in (self.structure_stream_bytes, self.page_tree_entries, self.page_tree_depth):
            if type(value) is not int or value <= 0:
                raise ValueError("PDF-Ressourcenlimits müssen positive ganze Zahlen sein.")


def _decoder_configuration(maximum: int) -> dict[str, Any]:
    if maximum <= 0:
        raise ProcessingLimitError("Das Größenbudget für eingebettete PDF-Dateien ist ausgeschöpft.")
    current = get_configuration()
    names = (
        "array_based_stream_maximum_output_length",
        "jbig2_maximum_output_length",
        "lzw_maximum_output_length",
        "run_length_maximum_output_length",
        "zlib_maximum_output_length",
        "image_maximum_buffer_size",
    )
    return {name: min(maximum, getattr(current, name)) for name in names}


def _resolved_filter_parameter(value: PdfObject, *, depth: int = 0) -> PdfObject:
    """Resolve filter metadata under the structural budget, never the remaining output budget."""
    if depth > 16:
        raise ProcessingLimitError("Die PDF-Filterparameter sind zu tief verschachtelt.")
    resolved = value.get_object()
    if resolved is None:
        raise InvoiceInputError("Die PDF enthält unvollständige Filterparameter.")
    value = resolved
    if isinstance(value, StreamObject):
        # External JBIG2 decoding is disabled; do not copy or decode a global image stream here.
        return value
    if isinstance(value, DictionaryObject):
        return DictionaryObject({key: _resolved_filter_parameter(item, depth=depth + 1) for key, item in value.items()})
    if isinstance(value, ArrayObject):
        return ArrayObject([_resolved_filter_parameter(item, depth=depth + 1) for item in value])
    return value


def _bounded_stream_data(stream: StreamObject, maximum: int) -> bytes:
    configuration = _decoder_configuration(maximum)  # Reject zero before any decoder invocation.
    # pypdf 6.19's get_data decodes a whole chain without checking every intermediate.
    # Its narrow StreamObject/decoder adapter lets us retain each upstream decoder
    # while checking each stage and resolving lazy metadata under the separate budget.
    filters = _resolved_filter_parameter(stream.get("/Filter", NullObject()))
    filter_items = (
        list(filters) if isinstance(filters, ArrayObject) else ([] if isinstance(filters, NullObject) else [filters])
    )
    parameters = _resolved_filter_parameter(stream.get("/DecodeParms", NullObject()))
    parameter_items = (
        list(parameters)
        if isinstance(parameters, ArrayObject)
        else ([NullObject()] * len(filter_items) if isinstance(parameters, NullObject) else [parameters])
    )
    if len(parameter_items) != len(filter_items):
        raise InvoiceInputError("Die PDF-Filterparameter passen nicht zur Filterkette.")
    height = _resolved_filter_parameter(stream.get("/Height", NullObject()))
    data = stream._data
    for filter_name, parameter in zip(filter_items, parameter_items, strict=True):
        stage = StreamObject()
        stage._data = data
        stage[NameObject("/Filter")] = filter_name
        stage[NameObject("/DecodeParms")] = parameter
        if not isinstance(height, NullObject):
            stage[NameObject("/Height")] = height
        with apply_configuration(**configuration):
            data = decode_stream_data(stage)
        if len(data) > maximum:
            raise ProcessingLimitError("Ein PDF-Filterschritt überschreitet das zulässige Größenbudget.")
    if len(data) > maximum:
        raise ProcessingLimitError("Die eingebettete PDF-Datei überschreitet das zulässige Größenbudget.")
    return data


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
    resource_limits: PdfResourceLimits | None = None,
) -> ExtractedSource:
    try:
        reader = PdfReader(BytesIO(data), strict=False)
    except LimitReachedError as exc:
        raise ProcessingLimitError("Die PDF überschreitet ein zulässiges Verarbeitungsbudget.") from exc
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
                error_type = ProcessingLimitError if resource_limits is not None else InvoiceInputError
                raise error_type(
                    f"Eine eingebettete {_embedded_file_kind(attachment_name)} "
                    "überschreitet die zulässige Größenbegrenzung."
                )

            if resource_limits is None:
                payload = embedded_file.content
            else:
                assert max_embedded_bytes is not None
                remaining = max_embedded_bytes - total_embedded_bytes
                # Resolve the stream under the fixed structural budget first. A reduced
                # attachment budget must not constrain its containing object/XRef stream.
                stream = embedded_file._embedded_file
                payload = _bounded_stream_data(stream, remaining)
            if max_embedded_bytes is not None and len(payload) > max_embedded_bytes:
                error_type = ProcessingLimitError if resource_limits is not None else InvoiceInputError
                raise error_type(
                    f"Eine eingebettete {_embedded_file_kind(attachment_name)} "
                    "überschreitet die zulässige Größenbegrenzung."
                )
            total_embedded_bytes += len(payload)
            if max_embedded_bytes is not None and total_embedded_bytes > max_embedded_bytes:
                error_type = ProcessingLimitError if resource_limits is not None else InvoiceInputError
                raise error_type("Die eingebetteten PDF-Dateien überschreiten zusammen die zulässige Größenbegrenzung.")

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
    except LimitReachedError as exc:
        raise ProcessingLimitError("Die PDF überschreitet ein zulässiges Verarbeitungsbudget.") from exc
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
    resource_limits: PdfResourceLimits | None = None,
) -> ExtractedSource:
    safe_name = Path(filename or "rechnung.xml").name
    detected_type = media_type or "application/octet-stream"

    if _looks_like_pdf(data):
        if resource_limits is not None:
            if type(max_embedded_bytes) is not int or max_embedded_bytes <= 0:
                raise ValueError("Begrenzte PDF-Verarbeitung benötigt ein positives Anhangsbudget.")
            configuration = {
                **_decoder_configuration(resource_limits.structure_stream_bytes),
                "maximum_declared_stream_length": resource_limits.structure_stream_bytes,
                "page_tree_maximum_entries": resource_limits.page_tree_entries,
                "page_tree_maximum_depth": resource_limits.page_tree_depth,
                "disable_legacy_handling": True,
                # Invoice extraction must never start an external image decoder.
                "jbig2dec_binary": None,
            }
            context: AbstractContextManager[Any] = apply_configuration(**configuration)
        else:
            context = nullcontext()
        with context:
            return _extract_pdf_xml(
                data,
                safe_name,
                detected_type,
                max_embedded_bytes=max_embedded_bytes,
                resource_limits=resource_limits,
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
