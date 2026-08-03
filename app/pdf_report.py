from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any
from unicodedata import category

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.doctemplate import LayoutError

from app.report_presentation import ReportScope, build_report_presentation

PDF_TECHNICAL_ROW_LIMIT = 2_000
PDF_TECHNICAL_CHARACTER_LIMIT = 250_000
PDF_RAW_XML_CHARACTER_LIMIT = 50_000
PDF_OFFICIAL_REPORT_CHARACTER_LIMIT = 100_000
PDF_LINE_LIMIT = 250
PDF_FINDING_LIMIT = 250
PDF_INVOICE_NOTE_LIMIT = 50
PDF_GENERIC_LIST_LIMIT = 1_000
PDF_SCALAR_CHARACTER_LIMIT = 4_000
PDF_TOTAL_CHARACTER_LIMIT = 600_000
PDF_CORE_CHARACTER_RESERVE = 100_000
PDF_SCALAR_NEWLINE_LIMIT = 100
PDF_GENERAL_NEWLINE_LIMIT = 6_000
PDF_CORE_NEWLINE_RESERVE = 500
PDF_TECHNICAL_NEWLINE_LIMIT = 2_000
PDF_OFFICIAL_REPORT_NEWLINE_LIMIT = 2_000
PDF_PAGE_LIMIT = 200

_FONT_REGISTRATION_LOCK = threading.Lock()
_FONTS_REGISTERED = False
_FONT_REGULAR = "EInvoiceNotoSans"
_FONT_BOLD = "EInvoiceNotoSansBold"
_FONT_ITALIC = "EInvoiceNotoSansItalic"
_FONT_BOLD_ITALIC = "EInvoiceNotoSansBoldItalic"
_FONT_CJK = "EInvoiceNotoSansSC"
_PRIMARY_GLYPHS: frozenset[int] = frozenset()
_CJK_GLYPHS: frozenset[int] = frozenset()


@dataclass
class _PdfPreparation:
    lines_total: int = 0
    lines_rendered: int = 0
    findings_total: int = 0
    findings_rendered: int = 0
    notes_total: int = 0
    notes_rendered: int = 0
    scalar_truncated: bool = False
    total_truncated: bool = False
    newlines_limited: bool = False
    generic_lists_truncated: bool = False
    technical_rows_total: int = 0
    technical_rows_rendered: int = 0
    technical_characters: int = 0
    technical_limited: bool = False
    original_xml_length: int = 0
    original_xml_limited: bool = False
    official_report_length: int = 0
    official_report_limited: bool = False

    @property
    def content_limited(self) -> bool:
        return any(
            (
                self.lines_rendered < self.lines_total,
                self.findings_rendered < self.findings_total,
                self.notes_rendered < self.notes_total,
                self.scalar_truncated,
                self.total_truncated,
                self.newlines_limited,
                self.generic_lists_truncated,
                self.technical_limited,
                self.original_xml_limited,
                self.official_report_limited,
            )
        )


class _PdfPageLimitExceeded(RuntimeError):
    pass


class _TextBudget:
    def __init__(self, character_limit: int, newline_limit: int) -> None:
        self.remaining_characters = character_limit
        self.remaining_general_newlines = newline_limit
        self.truncated_by_total = False

    def general_text(self, value: Any, preparation: _PdfPreparation) -> str:
        character_limit = min(PDF_SCALAR_CHARACTER_LIMIT, self.remaining_characters)
        newline_limit = min(PDF_SCALAR_NEWLINE_LIMIT, self.remaining_general_newlines)
        text, characters_limited, newlines_limited, _ = _bounded_plain_text(
            value,
            character_limit=character_limit,
            newline_limit=newline_limit,
        )
        if characters_limited:
            preparation.scalar_truncated = True
            if character_limit < PDF_SCALAR_CHARACTER_LIMIT:
                preparation.total_truncated = True
        preparation.newlines_limited |= newlines_limited
        self.remaining_characters -= len(text)
        self.remaining_general_newlines -= text.count("\n")
        return text

    def special_text(
        self,
        value: Any,
        *,
        character_limit: int,
        newline_limit: int,
    ) -> tuple[str, bool, int, int]:
        available_characters = min(character_limit, self.remaining_characters)
        text, characters_limited, newlines_limited, original_length = _bounded_plain_text(
            value,
            character_limit=available_characters,
            newline_limit=newline_limit,
        )
        if characters_limited and available_characters < character_limit:
            self.truncated_by_total = True
        self.remaining_characters -= len(text)
        return text, characters_limited or newlines_limited, original_length, text.count("\n")


def _register_fonts() -> None:
    global _CJK_GLYPHS, _FONTS_REGISTERED, _PRIMARY_GLYPHS
    if _FONTS_REGISTERED:
        return
    with _FONT_REGISTRATION_LOCK:
        if _FONTS_REGISTERED:
            return
        font_directory = Path(__file__).resolve().parent / "assets" / "fonts"
        fonts = {
            _FONT_REGULAR: font_directory / "NotoSans-Regular.ttf",
            _FONT_BOLD: font_directory / "NotoSans-Bold.ttf",
            _FONT_ITALIC: font_directory / "NotoSans-Italic.ttf",
            _FONT_BOLD_ITALIC: font_directory / "NotoSans-BoldItalic.ttf",
            _FONT_CJK: font_directory / "NotoSansSC-Variable.ttf",
        }
        registered: dict[str, TTFont] = {}
        for name, path in fonts.items():
            if not path.is_file():
                raise RuntimeError(f"Die für PDF-Berichte erforderliche Schriftdatei fehlt: {path.name}")
            font = TTFont(name, str(path))
            pdfmetrics.registerFont(font)
            registered[name] = font
        pdfmetrics.registerFontFamily(
            _FONT_REGULAR,
            normal=_FONT_REGULAR,
            bold=_FONT_BOLD,
            italic=_FONT_ITALIC,
            boldItalic=_FONT_BOLD_ITALIC,
        )
        _PRIMARY_GLYPHS = frozenset(
            codepoint for codepoint, glyph in registered[_FONT_REGULAR].face.charToGlyph.items() if glyph != 0
        )
        _CJK_GLYPHS = frozenset(
            codepoint for codepoint, glyph in registered[_FONT_CJK].face.charToGlyph.items() if glyph != 0
        )
        _FONTS_REGISTERED = True


def _safe_plain_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    rendered: list[str] = []
    for character in text:
        if character == "\n":
            rendered.append(character)
        elif category(character) in {"Cc", "Cs"} or character in {"\ufffe", "\uffff"}:
            rendered.append(f"[U+{ord(character):04X}]")
        else:
            rendered.append(character)
    return "".join(rendered)


def _bounded_plain_text(
    value: Any,
    *,
    character_limit: int,
    newline_limit: int,
) -> tuple[str, bool, bool, int]:
    if value is None:
        return "", False, False, 0
    raw = str(value)
    original_length = len(raw)
    rendered: list[str] = []
    rendered_length = 0
    kept_newlines = 0
    characters_limited = False
    newlines_limited = False
    index = 0
    while index < original_length:
        character = raw[index]
        if character == "\r":
            if index + 1 < original_length and raw[index + 1] == "\n":
                index += 1
            character = "\n"
        if character == "\n":
            if kept_newlines < newline_limit:
                piece = "\n"
                kept_newlines += 1
            else:
                piece = " "
                newlines_limited = True
        elif category(character) in {"Cc", "Cs"} or character in {"\ufffe", "\uffff"}:
            piece = f"[U+{ord(character):04X}]"
        else:
            piece = character
        if rendered_length + len(piece) > character_limit:
            characters_limited = True
            break
        rendered.append(piece)
        rendered_length += len(piece)
        index += 1

    if index < original_length:
        characters_limited = True
    text = "".join(rendered)
    if characters_limited and character_limit > 0:
        marker = "[...]"
        if character_limit >= len(marker):
            text = text[: character_limit - len(marker)] + marker
        else:
            text = text[:character_limit]
    return text, characters_limited, newlines_limited, original_length


def _text(value: Any, fallback: str = "-") -> str:
    if value is None or value == "":
        return fallback
    if isinstance(value, bool):
        return "Ja" if value else "Nein"
    return _safe_plain_text(value)


def _markup(value: Any, fallback: str = "-") -> str:
    text = _text(value, fallback)
    parts: list[str] = []
    run: list[str] = []
    run_font: str | None = None

    def flush() -> None:
        if not run:
            return
        rendered = escape("".join(run), quote=False)
        parts.append(f'<font name="{_FONT_CJK}">{rendered}</font>' if run_font == _FONT_CJK else rendered)
        run.clear()

    for character in text:
        if character == "\n":
            flush()
            parts.append("<br/>")
            run_font = None
            continue
        codepoint = ord(character)
        if codepoint in _PRIMARY_GLYPHS:
            font = _FONT_REGULAR
            rendered_character = character
        elif codepoint in _CJK_GLYPHS:
            font = _FONT_CJK
            rendered_character = character
        else:
            font = _FONT_REGULAR
            rendered_character = f"[U+{codepoint:04X}]"
        if run and font != run_font:
            flush()
        run_font = font
        run.append(rendered_character)
    flush()
    return "".join(parts)


def _format_number(value: Any, digits: int | None = None) -> str:
    if value is None or value == "":
        return "-"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return _text(value)
    if digits is not None:
        number = number.quantize(Decimal(1).scaleb(-digits))
        raw = f"{number:.{digits}f}"
    else:
        raw = format(number, "f")
        if "." in raw:
            raw = raw.rstrip("0").rstrip(".")
    integer, dot, fraction = raw.partition(".")
    sign = ""
    if integer.startswith("-"):
        sign, integer = "-", integer[1:]
    groups = [
        integer[max(0, len(integer) - offset - 3) : len(integer) - offset] for offset in range(0, len(integer), 3)
    ]
    grouped = ".".join(reversed(groups))
    return f"{sign}{grouped}{',' + fraction if dot else ''}"


def _format_money(value: Any, currency: Any) -> str:
    formatted = _format_number(value, 2)
    return f"{formatted} {_text(currency, '')}".strip() if formatted != "-" else formatted


def _format_date(value: Any) -> str:
    if not value:
        return "-"
    raw = str(value)
    parts = raw.split("-")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        return f"{parts[2]}.{parts[1]}.{parts[0]}"
    return _text(value)


def _format_bytes(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "-"
    units = ["B", "KB", "MB", "GB"]
    unit = 0
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    return f"{size:.0f} {units[unit]}" if unit == 0 else f"{size:.1f} {units[unit]}"


def _present(value: Any) -> bool:
    return value not in (None, "", [], {})


def _bounded_value(
    value: Any,
    budget: _TextBudget,
    preparation: _PdfPreparation,
    path: tuple[str, ...],
) -> Any:
    if isinstance(value, dict):
        return {key: _bounded_value(item, budget, preparation, (*path, str(key))) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        values = list(value)
        limit = PDF_GENERIC_LIST_LIMIT
        if path == ("lines",):
            preparation.lines_total = len(values)
            limit = PDF_LINE_LIMIT
        elif path == ("document", "notes"):
            preparation.notes_total = len(values)
            limit = PDF_INVOICE_NOTE_LIMIT
        elif len(values) > limit:
            preparation.generic_lists_truncated = True

        selected: list[Any] = []
        for item in values[:limit]:
            if budget.remaining_characters <= 0:
                preparation.total_truncated = True
                break
            selected.append(_bounded_value(item, budget, preparation, (*path, "[]")))
        if path == ("lines",):
            preparation.lines_rendered = len(selected)
        elif path == ("document", "notes"):
            preparation.notes_rendered = len(selected)
        elif len(selected) < min(len(values), limit):
            preparation.generic_lists_truncated = True
        return selected
    if value is None or isinstance(value, bool):
        return value
    return budget.general_text(value, preparation)


def _prepare_technical_rows(
    rows: Any,
    budget: _TextBudget,
    preparation: _PdfPreparation,
) -> list[dict[str, str]]:
    source_rows = list(rows) if isinstance(rows, (list, tuple)) else []
    preparation.technical_rows_total = len(source_rows)
    selected: list[dict[str, str]] = []
    remaining_characters = PDF_TECHNICAL_CHARACTER_LIMIT
    remaining_newlines = PDF_TECHNICAL_NEWLINE_LIMIT

    for source_row in source_rows[:PDF_TECHNICAL_ROW_LIMIT]:
        if remaining_characters <= 0 or budget.remaining_characters <= 0:
            break
        row = source_row if isinstance(source_row, dict) else {"value": source_row}
        rendered: dict[str, str] = {}
        for key in ("kind", "path", "name", "namespace", "value"):
            text, limited, _, _ = budget.special_text(
                row.get(key),
                character_limit=remaining_characters,
                newline_limit=remaining_newlines,
            )
            rendered[key] = text
            remaining_characters -= len(text)
            remaining_newlines -= text.count("\n")
            preparation.technical_limited |= limited
        selected.append(rendered)

    preparation.technical_rows_rendered = len(selected)
    preparation.technical_characters = PDF_TECHNICAL_CHARACTER_LIMIT - remaining_characters
    preparation.technical_limited |= (
        len(selected) < len(source_rows) or preparation.technical_characters >= PDF_TECHNICAL_CHARACTER_LIMIT
    )
    return selected


def _validate_scope(scope: str) -> ReportScope:
    if scope == "readable":
        return "readable"
    if scope == "complete":
        return "complete"
    raise ValueError("Unbekannter Berichtsumfang; erwartet wird 'readable' oder 'complete'.")


def _prepare_analysis_for_pdf(
    analysis: dict[str, Any],
    *,
    scope: ReportScope = "readable",
) -> tuple[dict[str, Any], _PdfPreparation]:
    if not isinstance(analysis, dict) or analysis.get("schema_version") != 2:
        raise ValueError("Der PDF-Bericht erfordert den Analysevertrag Schema 2.")
    scope = _validate_scope(scope)

    preparation = _PdfPreparation()
    core_budget = _TextBudget(PDF_CORE_CHARACTER_RESERVE, PDF_CORE_NEWLINE_RESERVE)
    budget = _TextBudget(
        PDF_TOTAL_CHARACTER_LIMIT - PDF_CORE_CHARACTER_RESERVE,
        PDF_GENERAL_NEWLINE_LIMIT - PDF_CORE_NEWLINE_RESERVE,
    )

    allowed_keys = (
        "document",
        "profile",
        "capabilities",
        "parties",
        "roles",
        "periods",
        "delivery",
        "references",
        "lines",
        "allowances_charges",
        "tax",
        "totals",
        "payment",
        "assessment",
        "source",
        "technical",
        "runtime",
    )
    source = {key: analysis.get(key) for key in allowed_keys}
    technical_source = source.pop("technical", {}) or {}
    document_source = dict(source.pop("document", {}) or {})
    notes_source = document_source.pop("notes", [])
    source = {"document": {"notes": notes_source}, **source}

    assessment_source = dict(source.pop("assessment", {}) or {})
    raw_official_report: Any = None
    core_assessment: dict[str, dict[str, Any]] = {}
    remaining_assessment: dict[str, dict[str, Any]] = {}
    remaining_finding_slots = PDF_FINDING_LIMIT
    core_fields = {
        "internal": ("status", "executed", "summary", "scope", "counts"),
        "official": (
            "status",
            "requested",
            "configured",
            "executed",
            "summary",
            "exit_code",
            "report_source",
            "counts",
        ),
        "processing": ("status", "summary", "counts"),
    }
    for axis_name in ("official", "internal", "processing"):
        axis_source = dict(assessment_source.get(axis_name) or {})
        findings = list(axis_source.pop("findings", []) or [])
        preparation.findings_total += len(findings)
        selected_findings = findings[:remaining_finding_slots]
        remaining_finding_slots -= len(selected_findings)
        if axis_name == "official":
            raw_official_report = axis_source.pop("raw_report", None)
            if scope == "readable":
                axis_source.pop("technical_output", None)
        core_assessment[axis_name] = {key: axis_source.pop(key, None) for key in core_fields[axis_name]}
        axis_source["findings"] = selected_findings
        remaining_assessment[axis_name] = axis_source
    if preparation.findings_total > PDF_FINDING_LIMIT:
        preparation.generic_lists_truncated = True
    source["assessment"] = remaining_assessment

    prepared_core = _bounded_value(
        {
            "document": document_source,
            "capabilities": source.pop("capabilities", {}) or {},
            "roles": source.pop("roles", {}) or {},
            "totals": source.pop("totals", {}) or {},
            "assessment": core_assessment,
        },
        core_budget,
        preparation,
        (),
    )
    budget.remaining_characters += core_budget.remaining_characters
    budget.remaining_general_newlines += core_budget.remaining_general_newlines
    prepared = _bounded_value(source, budget, preparation, ())
    prepared_document = prepared.setdefault("document", {})
    prepared_document.update(prepared_core["document"])
    prepared["capabilities"] = prepared_core["capabilities"]
    prepared["roles"] = prepared_core["roles"]
    prepared["totals"] = prepared_core["totals"]
    prepared_assessment = prepared.setdefault("assessment", {})
    for axis_name in ("internal", "official", "processing"):
        prepared_axis = prepared_assessment.setdefault(axis_name, {})
        prepared_axis.update(prepared_core["assessment"][axis_name])
    preparation.findings_rendered = sum(
        len((prepared_assessment.get(axis_name) or {}).get("findings") or [])
        for axis_name in ("internal", "official", "processing")
    )
    prepared_official = prepared_assessment["official"]
    if scope == "complete" and _present(raw_official_report):
        official_report, limited, original_length, _ = budget.special_text(
            raw_official_report,
            character_limit=PDF_OFFICIAL_REPORT_CHARACTER_LIMIT,
            newline_limit=PDF_OFFICIAL_REPORT_NEWLINE_LIMIT,
        )
        prepared_official["raw_report"] = official_report
        preparation.official_report_length = original_length
        preparation.official_report_limited = limited

    if scope == "complete":
        technical = technical_source if isinstance(technical_source, dict) else {}
        prepared_rows = _prepare_technical_rows(technical.get("fields"), budget, preparation)
        remaining_technical_newlines = max(
            0,
            PDF_TECHNICAL_NEWLINE_LIMIT - sum(value.count("\n") for row in prepared_rows for value in row.values()),
        )
        original_xml, xml_limited, xml_length, _ = budget.special_text(
            technical.get("source_xml"),
            character_limit=PDF_RAW_XML_CHARACTER_LIMIT,
            newline_limit=remaining_technical_newlines,
        )
        preparation.original_xml_length = xml_length
        preparation.original_xml_limited = xml_limited
        prepared["technical"] = {
            "root_element": budget.general_text(technical.get("root_element"), preparation),
            "root_namespace": budget.general_text(technical.get("root_namespace"), preparation),
            "field_count": technical.get("field_count", len(prepared_rows)),
            "fields": prepared_rows,
            "source_xml": original_xml,
            "truncated": bool(technical.get("truncated")),
        }
    else:
        prepared["technical"] = {}
    prepared["schema_version"] = 2
    preparation.total_truncated |= budget.truncated_by_total or core_budget.truncated_by_total
    return prepared, preparation


def _code_text(value: Any) -> str:
    code = value if isinstance(value, dict) else {}
    raw = code.get("value")
    label = code.get("label")
    list_id = code.get("list_id")
    rendered = _text(raw, "")
    if _present(raw) and _present(label):
        raw_text = str(raw)
        label_text = str(label)
        rendered = (
            label_text
            if label_text == raw_text
            or label_text.startswith(f"{raw_text} –")
            or label_text.startswith(f"{raw_text} -")
            else f"{raw_text} – {label_text}"
        )
    if _present(list_id):
        rendered = f"{rendered} (Liste: {list_id})" if rendered else f"Liste: {list_id}"
    return rendered


def _identifier_text(value: Any) -> str:
    identifier = value if isinstance(value, dict) else {}
    raw = identifier.get("value")
    scheme_id = identifier.get("scheme_id")
    if not _present(raw):
        return ""
    return f"{raw} ({scheme_id})" if _present(scheme_id) else _text(raw, "")


def _amount_text(value: Any) -> str:
    amount = value if isinstance(value, dict) else {}
    return _format_money(amount.get("value"), amount.get("currency"))


def _quantity_text(value: Any) -> str:
    quantity = value if isinstance(value, dict) else {}
    if not _present(quantity.get("value")):
        return ""
    return f"{_format_number(quantity.get('value'))} {_code_text(quantity.get('unit'))}".strip()


def _period_text(value: Any) -> str:
    period = value if isinstance(value, dict) else {}
    parts = [
        _format_date(period.get("start_date")) if _present(period.get("start_date")) else "",
        _format_date(period.get("end_date")) if _present(period.get("end_date")) else "",
        _text(period.get("description"), ""),
    ]
    return " - ".join(part for part in parts if part)


def _reference_text(value: Any) -> str:
    reference = value if isinstance(value, dict) else {}
    parts = [
        _identifier_text(reference.get("id")),
        _format_date(reference.get("issue_date")) if _present(reference.get("issue_date")) else "",
        _text(reference.get("description"), ""),
    ]
    return " - ".join(part for part in parts if part)


def _counts_text(value: Any) -> str:
    counts = value if isinstance(value, dict) else {}
    return f"Fehler {counts.get('error', 0)} - Warnungen {counts.get('warning', 0)} - Hinweise {counts.get('info', 0)}"


def _severity_text(value: Any) -> str:
    return {
        "error": "Fehler",
        "warning": "Warnung",
        "info": "Hinweis",
    }.get(str(value), _text(value))


def _finding_origin_text(value: Any) -> str:
    return {
        "internal": "Interne Prüfung",
        "official": "Offizielle Prüfung",
        "processing": "Verarbeitung",
    }.get(str(value), _text(value))


def _rule_class_text(value: Any) -> str:
    return {
        "core_precheck": "Kern-Vorprüfung",
        "profile_precheck": "Profil-Vorprüfung",
        "plausibility": "Plausibilitätsprüfung",
        "processing": "Verarbeitung",
        "official": "Offizielle Prüfung",
    }.get(str(value), _text(value))


def _occurrence_scope_text(value: Any) -> str:
    return {
        "document": "Dokument",
        "profile": "Profil",
        "party": "Partei",
        "period": "Zeitraum",
        "reference": "Referenz",
        "line": "Rechnungsposition",
        "allowance-charge": "Nachlass oder Zuschlag",
        "tax": "Umsatzsteuer",
        "total": "Summe",
        "payment": "Zahlung",
        "source": "Quelle",
        "technical": "Technik",
        "runtime": "Verarbeitungslauf",
    }.get(str(value), _text(value))


def _role_text(value: Any) -> str:
    return {
        "seller": "Verkäufer",
        "buyer": "Käufer",
        "payee": "Zahlungsempfänger",
        "invoice-recipient": "Rechnungsempfänger",
        "delivery-recipient": "Lieferempfänger",
        "seller-tax-representative": "Steuervertreter des Verkäufers",
        "unknown": "Unbekannt",
    }.get(str(value), _text(value))


def _direction_text(value: Any) -> str:
    return {
        "debtor-to-creditor": "Schuldner an Gläubiger",
        "creditor-to-debtor": "Gläubiger an Schuldner",
        "none": "Keine Zahlung erwartet",
        "unknown": "Unbekannt",
    }.get(str(value), _text(value))


def _masked_card_identifier(value: Any) -> str:
    raw = _safe_plain_text(value).strip()
    visible = "".join(character for character in raw if character.isalnum())[-4:]
    return f"•••• {visible}" if visible else ("••••" if raw else "")


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "InvoiceTitle",
            parent=base["Title"],
            fontName=_FONT_BOLD,
            fontSize=20,
            leading=24,
            textColor=colors.HexColor("#153842"),
            spaceAfter=4 * mm,
            wordWrap="CJK",
        ),
        "subtitle": ParagraphStyle(
            "InvoiceSubtitle",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#5f7078"),
            spaceAfter=5 * mm,
            wordWrap="CJK",
        ),
        "heading": ParagraphStyle(
            "InvoiceHeading",
            parent=base["Heading2"],
            fontName=_FONT_BOLD,
            fontSize=14,
            leading=17,
            textColor=colors.HexColor("#0b6477"),
            spaceBefore=5 * mm,
            spaceAfter=2.5 * mm,
            keepWithNext=True,
            wordWrap="CJK",
        ),
        "subheading": ParagraphStyle(
            "InvoiceSubheading",
            parent=base["Heading3"],
            fontName=_FONT_BOLD,
            fontSize=10,
            leading=13,
            textColor=colors.HexColor("#18272e"),
            spaceBefore=3 * mm,
            spaceAfter=1.5 * mm,
            keepWithNext=True,
            wordWrap="CJK",
        ),
        "body": ParagraphStyle(
            "InvoiceBody",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor("#18272e"),
            wordWrap="CJK",
        ),
        "body_bold": ParagraphStyle(
            "InvoiceBodyBold",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor("#18272e"),
            wordWrap="CJK",
        ),
        "label": ParagraphStyle(
            "InvoiceLabel",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=7.5,
            leading=10,
            textColor=colors.HexColor("#5f7078"),
            wordWrap="CJK",
        ),
        "small": ParagraphStyle(
            "InvoiceSmall",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=7,
            leading=9,
            textColor=colors.HexColor("#5f7078"),
            wordWrap="CJK",
        ),
        "technical": ParagraphStyle(
            "InvoiceTechnical",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=6.2,
            leading=8,
            textColor=colors.HexColor("#18272e"),
            wordWrap="CJK",
        ),
        "table_header": ParagraphStyle(
            "InvoiceTableHeader",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=6.2,
            leading=8,
            textColor=colors.white,
            wordWrap="CJK",
        ),
        "center": ParagraphStyle(
            "InvoiceCenter",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=9,
            leading=12,
            alignment=TA_CENTER,
            wordWrap="CJK",
        ),
        "right": ParagraphStyle(
            "InvoiceRight",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=9,
            leading=12,
            alignment=TA_RIGHT,
            wordWrap="CJK",
        ),
    }


def _paragraph(value: Any, style: ParagraphStyle, fallback: str = "-") -> Paragraph:
    return Paragraph(_markup(value, fallback), style)


def _heading(story: list[Flowable], title: str, styles: dict[str, ParagraphStyle]) -> None:
    story.append(_paragraph(title, styles["heading"]))


def _subheading(story: list[Flowable], title: str, styles: dict[str, ParagraphStyle]) -> None:
    story.append(_paragraph(title, styles["subheading"]))


def _render_limits_notice(
    story: list[Flowable], preparation: _PdfPreparation, styles: dict[str, ParagraphStyle]
) -> None:
    if not preparation.content_limited:
        return
    details = [
        "PDF-Darstellung gekürzt",
        f"Rechnungspositionen: {preparation.lines_rendered} von {preparation.lines_total}; "
        f"Prüfmeldungen: {preparation.findings_rendered} von {preparation.findings_total}; "
        f"Rechnungshinweise: {preparation.notes_rendered} von {preparation.notes_total}.",
        "Einzelwerte, Listen, Zeilenumbrüche oder technische Rohdaten wurden auf sichere Darstellungsbudgets begrenzt.",
        "Die vollständigen analysierten Daten bleiben im vollständigen HTML-Bericht (scope=complete) "
        "und über /api/analyze zugänglich. "
        "Die vollständige Rechnungsquelle bleibt im Originalanhang und über /api/xml verfügbar.",
    ]
    notice = Table(
        [[_paragraph(details[0], styles["body_bold"]), _paragraph("\n".join(details[1:]), styles["body"])]],
        colWidths=[45 * mm, 130 * mm],
        splitByRow=1,
        splitInRow=1,
    )
    notice.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fff4d6")),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#b56a00")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
            ]
        )
    )
    story.extend([Spacer(1, 3 * mm), notice])


def _key_value_table(
    rows: Iterable[tuple[str, Any]],
    styles: dict[str, ParagraphStyle],
    *,
    include_empty: bool = False,
) -> Table:
    rendered = [
        [_paragraph(label, styles["label"]), _paragraph(value, styles["body"])]
        for label, value in rows
        if include_empty or _present(value)
    ]
    if not rendered:
        rendered = [[_paragraph("Angaben", styles["label"]), _paragraph("Nicht angegeben", styles["body"])]]
    table = Table(rendered, colWidths=[49 * mm, 126 * mm], splitByRow=1, splitInRow=1)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2.2 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2.2 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 1.6 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.6 * mm),
                ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#d5dfe3")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f4f7f8")),
                ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#d5dfe3")),
            ]
        )
    )
    return table


def _party_identifiers(entries: Any) -> str:
    labels = {
        "party": "Parteikennung",
        "legal-registration": "Registerkennung",
        "vat": "USt-IdNr.",
        "tax-registration": "Steuerkennung",
        "other": "Weitere Kennung",
    }
    values: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        identifier = _identifier_text(entry.get("identifier"))
        if identifier:
            values.append(f"{labels.get(str(entry.get('kind')), _text(entry.get('kind')))}: {identifier}")
    return "\n".join(values)


def _address_text(address: Any) -> str:
    if not isinstance(address, dict):
        return ""
    lines = [_text(address.get(key), "") for key in ("line1", "line2", "line3") if _present(address.get(key))]
    locality = " ".join(_text(value, "") for value in (address.get("postcode"), address.get("city")) if _present(value))
    if locality:
        lines.append(locality)
    if _present(address.get("subdivision")):
        lines.append(_text(address.get("subdivision"), ""))
    if _present(address.get("country")):
        lines.append(_code_text(address.get("country")))
    return "\n".join(lines)


def _party_rows(party: dict[str, Any]) -> list[tuple[str, Any]]:
    address = party.get("postal_address") or {}
    contact = party.get("contact") or {}
    return [
        ("Rechtlicher Name", party.get("legal_name")),
        ("Handelsname", party.get("trading_name")),
        ("Zusätzliche rechtliche Angaben", party.get("additional_legal_information")),
        ("Anschrift", _address_text(address)),
        ("Kennungen", _party_identifiers(party.get("identifiers"))),
        ("Steuerkennungen", _party_identifiers(party.get("tax_identifiers"))),
        ("Elektronische Adresse", _identifier_text(party.get("electronic_address"))),
        ("Kontakt", contact.get("name")),
        ("Abteilung", contact.get("department")),
        ("Telefon", contact.get("phone")),
        ("E-Mail", contact.get("email")),
    ]


def _render_parties(story: list[Flowable], analysis: dict[str, Any], styles: dict[str, ParagraphStyle]) -> None:
    parties = analysis.get("parties") or {}
    _heading(story, "Parteien", styles)
    rendered = False
    for key, title in (
        ("seller", "Verkäufer"),
        ("buyer", "Käufer"),
        ("payee", "Zahlungsempfänger"),
        ("invoice_recipient", "Rechnungsempfänger"),
        ("seller_tax_representative", "Steuervertreter des Verkäufers"),
        ("delivery_recipient", "Lieferempfänger"),
    ):
        party = parties.get(key)
        if not isinstance(party, dict) or not party:
            continue
        rendered = True
        _subheading(story, title, styles)
        story.append(_key_value_table(_party_rows(party), styles))
    if not rendered:
        story.append(_paragraph("Keine Parteien erkannt.", styles["body"]))


def _render_periods_and_delivery(
    story: list[Flowable],
    analysis: dict[str, Any],
    styles: dict[str, ParagraphStyle],
) -> None:
    periods = analysis.get("periods") or {}
    delivery = analysis.get("delivery") or {}
    delivery_location = delivery.get("location") or {}
    _subheading(story, "Zeiträume und Lieferung", styles)
    story.append(
        _key_value_table(
            [
                ("Rechnungszeitraum", _period_text(periods.get("invoice"))),
                ("Liefer- oder Leistungszeitraum", _period_text(periods.get("delivery"))),
                ("Tatsächliches Lieferdatum (BT-72)", _format_date(delivery.get("actual_date"))),
                ("Kennung des Lieferorts (BT-71)", _identifier_text(delivery_location.get("id"))),
                ("Anschrift des Lieferorts", _address_text(delivery_location.get("postal_address"))),
            ],
            styles,
        )
    )


def _render_adjustments(
    story: list[Flowable],
    adjustments: list[dict[str, Any]],
    styles: dict[str, ParagraphStyle],
    *,
    heading: str,
) -> None:
    if not adjustments:
        return
    kind_labels = {"allowance": "Nachlass", "charge": "Zuschlag", "unknown": "Unbekannt"}
    _subheading(story, heading, styles)
    for index, item in enumerate(adjustments, start=1):
        kind = str(item.get("kind"))
        rows = [
            ("Art", kind_labels.get(kind, _text(item.get("kind")))),
            ("Originalindikator", item.get("indicator_raw")),
            ("Betrag", _amount_text(item.get("amount"))),
            ("Basisbetrag", _amount_text(item.get("base_amount"))),
            (
                "Prozentsatz",
                f"{_format_number(item.get('percentage'))} %" if _present(item.get("percentage")) else None,
            ),
            ("Grund", item.get("reason_text")),
            ("Grundcode", _code_text(item.get("reason_code"))),
            ("Steuerkategorie", _code_text(item.get("tax_category"))),
            (
                "Steuersatz",
                f"{_format_number(item.get('tax_rate_percent'))} %" if _present(item.get("tax_rate_percent")) else None,
            ),
        ]
        story.append(_paragraph(f"{heading} {index}", styles["body_bold"]))
        story.append(_key_value_table(rows, styles))


def _render_lines(
    story: list[Flowable],
    analysis: dict[str, Any],
    styles: dict[str, ParagraphStyle],
    preparation: _PdfPreparation,
) -> None:
    _heading(
        story,
        f"Rechnungspositionen ({preparation.lines_rendered} von {preparation.lines_total})",
        styles,
    )
    lines = analysis.get("lines") or []
    if not lines:
        story.append(_paragraph("Keine Positionen erkannt.", styles["body"]))
        return
    for index, line in enumerate(lines, start=1):
        item = line.get("item") or {}
        price = line.get("price") or {}
        discount = price.get("discount") or {}
        name = item.get("name") or "Ohne Bezeichnung"
        _subheading(story, f"Position {line.get('id') or index}: {name}", styles)
        classifications = []
        for classification in item.get("classifications") or []:
            parts = [
                _text(classification.get("code"), ""),
                _text(classification.get("name"), ""),
                (f"Schema {classification.get('scheme_id')}" if _present(classification.get("scheme_id")) else ""),
                (
                    f"Version {classification.get('scheme_version')}"
                    if _present(classification.get("scheme_version"))
                    else ""
                ),
            ]
            classifications.append(" - ".join(part for part in parts if part))
        properties = [
            f"{property_.get('name') or 'Eigenschaft'}: {_text(property_.get('value'))}"
            for property_ in item.get("properties") or []
        ]
        rows = [
            ("Bezeichnung", item.get("name")),
            ("Beschreibung", item.get("description")),
            ("Verkäufer-Artikelnummer", _identifier_text(item.get("seller_identifier"))),
            ("Käufer-Artikelnummer", _identifier_text(item.get("buyer_identifier"))),
            ("Standard-Artikelkennung", _identifier_text(item.get("standard_identifier"))),
            ("Menge", _quantity_text(line.get("quantity"))),
            ("Nettopreis", _amount_text(price.get("net"))),
            ("Preisbasis", _quantity_text(price.get("base_quantity"))),
            ("Bruttopreis", _amount_text(price.get("gross"))),
            ("Preisnachlass", _amount_text(discount.get("amount"))),
            (
                "Preisnachlass in Prozent",
                f"{_format_number(discount.get('percentage'))} %" if _present(discount.get("percentage")) else None,
            ),
            ("Steuerart", _code_text(line.get("tax_type"))),
            ("Steuerkategorie", _code_text(line.get("tax_category"))),
            (
                "Steuersatz",
                f"{_format_number(line.get('tax_rate_percent'))} %" if _present(line.get("tax_rate_percent")) else None,
            ),
            ("Positionsnetto", _amount_text(line.get("net_amount"))),
            ("Hinweise", "\n".join(_text(value, "") for value in line.get("notes") or [])),
            ("Abrechnungszeitraum", _period_text(line.get("period"))),
            ("Bestellposition", line.get("order_line_reference")),
            ("Kontierungsreferenz", line.get("accounting_reference")),
            ("Objektkennung", _identifier_text(line.get("object_identifier"))),
            ("Ursprungsland", _code_text(item.get("origin_country"))),
            ("Klassifikationen", "\n".join(classifications)),
            ("Weitere Eigenschaften", "\n".join(properties)),
        ]
        story.append(_key_value_table(rows, styles))
        _render_adjustments(
            story,
            line.get("allowances_charges") or [],
            styles,
            heading=f"Nachlass oder Zuschlag zu Position {line.get('id') or index}",
        )


def _render_taxes(
    story: list[Flowable],
    analysis: dict[str, Any],
    styles: dict[str, ParagraphStyle],
) -> None:
    tax_model = analysis.get("tax") or {}
    taxes = tax_model.get("breakdown") or []
    _subheading(story, "Umsatzsteuer", styles)
    if not taxes:
        story.append(_paragraph("Keine Steueraufschlüsselung erkannt.", styles["body"]))
    for index, tax in enumerate(taxes, start=1):
        category = tax.get("category") or {}
        exemption = tax.get("exemption") or {}
        label = _code_text(category) or "Steuer"
        _subheading(story, f"Steueraufschlüsselung {index}: {label}", styles)
        story.append(
            _key_value_table(
                [
                    ("Steuerart", _code_text(tax.get("tax_type"))),
                    ("Kategoriecode (Original)", category.get("value")),
                    ("Kategorie", _code_text(category)),
                    (
                        "Steuersatz",
                        f"{_format_number(tax.get('rate_percent'))} %" if _present(tax.get("rate_percent")) else None,
                    ),
                    ("Bemessungsgrundlage", _amount_text(tax.get("taxable_amount"))),
                    ("Steuerbetrag", _amount_text(tax.get("tax_amount"))),
                    (
                        "Befreiungsgrund",
                        "\n".join(_text(reason, "") for reason in exemption.get("reasons") or []),
                    ),
                    ("Befreiungsgrundcode", _code_text(exemption.get("reason_code"))),
                ],
                styles,
            )
        )
    tax_totals = tax_model.get("totals") or {}
    _subheading(story, "Steuersummen", styles)
    story.append(
        _key_value_table(
            [
                ("Steuerbetrag in Dokumentwährung", _amount_text(tax_totals.get("document_currency"))),
                (
                    "Steuerbetrag in Umsatzsteuer-Abrechnungswährung",
                    _amount_text(tax_totals.get("vat_accounting_currency")),
                ),
            ],
            styles,
        )
    )


def _render_payment(
    story: list[Flowable],
    analysis: dict[str, Any],
    presentation: Mapping[str, Any],
    styles: dict[str, ParagraphStyle],
) -> None:
    payment = analysis.get("payment") or {}
    _heading(story, "Zahlung", styles)
    payment_flow = presentation.get("payment_flow") or {}
    _subheading(story, "Dokument- und Zahlungsfluss", styles)
    story.append(
        _key_value_table(
            [
                ("Dokumentfluss", payment_flow.get("document_flow")),
                ("Erwarteter Zahlungsfluss", payment_flow.get("expected_payment_flow")),
                (
                    "Zahlungsreferenz",
                    payment_flow.get("reference") or payment.get("reference"),
                ),
            ],
            styles,
            include_empty=True,
        )
    )
    if _present(payment_flow.get("note")):
        story.append(_paragraph(payment_flow.get("note"), styles["small"]))
    _subheading(story, "Zahlungsdaten", styles)
    story.append(
        _key_value_table(
            [
                ("Fälligkeit", _format_date(payment.get("due_date"))),
                ("Zahlungsreferenz", payment.get("reference")),
            ],
            styles,
        )
    )
    for index, instruction in enumerate(payment.get("instructions") or [], start=1):
        _subheading(story, f"Zahlungsanweisung {index}", styles)
        story.append(
            _key_value_table(
                [
                    ("Zahlungsweg", _code_text(instruction.get("means"))),
                    ("Hinweis", instruction.get("instruction_note")),
                    ("Zahlungs-ID", instruction.get("payment_id")),
                ],
                styles,
            )
        )
        for transfer_index, transfer in enumerate(instruction.get("credit_transfers") or [], start=1):
            _subheading(story, f"Überweisungskonto {index}.{transfer_index}", styles)
            story.append(
                _key_value_table(
                    [
                        ("Konto-ID", _identifier_text(transfer.get("account_id"))),
                        ("Kontoinhaber", transfer.get("account_name")),
                        (
                            "Kennung des Zahlungsdienstleisters",
                            _identifier_text(transfer.get("service_provider_id")),
                        ),
                    ],
                    styles,
                )
            )
        card = instruction.get("payment_card") or {}
        if card:
            _subheading(story, f"Zahlungskarte {index}", styles)
            story.append(
                _key_value_table(
                    [
                        (
                            "Maskierte Kartenkennung",
                            _masked_card_identifier(card.get("masked_account_identifier")),
                        ),
                        ("Karteninhaber", card.get("holder_name")),
                    ],
                    styles,
                )
            )
        direct_debit = instruction.get("direct_debit") or {}
        if direct_debit:
            _subheading(story, f"Lastschrift {index}", styles)
            story.append(
                _key_value_table(
                    [
                        ("Mandatsreferenz", direct_debit.get("mandate_reference")),
                        ("Gläubiger-ID", _identifier_text(direct_debit.get("creditor_id"))),
                        (
                            "Zu belastendes Konto",
                            _identifier_text(direct_debit.get("debited_account_id")),
                        ),
                    ],
                    styles,
                )
            )
    for index, term in enumerate(payment.get("terms") or [], start=1):
        _subheading(story, f"Zahlungsbedingung {index}", styles)
        story.append(
            _key_value_table(
                [
                    ("Beschreibung", term.get("description")),
                    (
                        "Fälligkeit",
                        _format_date(term.get("due_date")) if _present(term.get("due_date")) else None,
                    ),
                    ("Teilzahlungsbetrag", _amount_text(term.get("partial_payment"))),
                ],
                styles,
            )
        )
    if (
        not payment.get("instructions")
        and not payment.get("terms")
        and not _present(payment.get("reference"))
        and not _present(payment.get("due_date"))
    ):
        story.append(_paragraph("Keine Zahlungsangaben erkannt.", styles["body"]))


def _render_references(story: list[Flowable], analysis: dict[str, Any], styles: dict[str, ParagraphStyle]) -> None:
    references = analysis.get("references") or {}
    _subheading(story, "Referenzen", styles)
    story.append(
        _key_value_table(
            [
                ("Bestellreferenz Käufer", _reference_text(references.get("buyer_order"))),
                ("Bestellreferenz Verkäufer", _reference_text(references.get("seller_order"))),
                ("Vertrag", _reference_text(references.get("contract"))),
                ("Ausschreibung", _reference_text(references.get("tender"))),
                ("Projekt", _reference_text(references.get("project"))),
                ("Kontierungsreferenz Käufer", references.get("buyer_accounting_reference")),
                ("Abgerechnetes Objekt", _reference_text(references.get("invoiced_object"))),
                (
                    "Vorgängerrechnungen",
                    "\n".join(_reference_text(reference) for reference in references.get("preceding_invoices") or []),
                ),
                ("Versandavis", _reference_text(references.get("despatch_advice"))),
                ("Wareneingangsavis", _reference_text(references.get("receiving_advice"))),
            ],
            styles,
        )
    )
    for index, reference in enumerate(references.get("supporting_documents") or [], start=1):
        _subheading(story, f"Unterstützendes Dokument {index}", styles)
        story.append(
            _key_value_table(
                [
                    ("ID", _identifier_text(reference.get("id"))),
                    ("Typ", _code_text(reference.get("type"))),
                    ("Name", reference.get("name")),
                    ("Beschreibung", reference.get("description")),
                    ("Datei", reference.get("attachment_filename")),
                    ("MIME-Typ", reference.get("attachment_mime_type")),
                    ("Eingebettet", reference.get("embedded")),
                    ("Externe URI", reference.get("external_uri")),
                ],
                styles,
                include_empty=True,
            )
        )


def _source_file_rows(prefix: str, source_file: Any) -> list[tuple[str, Any]]:
    file_data = source_file if isinstance(source_file, dict) else {}
    return [
        (f"{prefix} - Dateiname", file_data.get("filename")),
        (f"{prefix} - Medientyp", file_data.get("media_type")),
        (f"{prefix} - Größe", _format_bytes(file_data.get("size_bytes"))),
        (f"{prefix} - SHA-256", file_data.get("sha256")),
    ]


def _render_notes(
    story: list[Flowable],
    analysis: dict[str, Any],
    styles: dict[str, ParagraphStyle],
    preparation: _PdfPreparation,
) -> None:
    invoice = analysis.get("document") or {}
    rendered_notes = []
    for note in invoice.get("notes") or []:
        text = _text(note.get("text"), "")
        subject = _code_text(note.get("subject_code"))
        rendered_notes.append(f"{subject}: {text}" if subject else text)
    _subheading(
        story,
        f"Hinweise ({preparation.notes_rendered} von {preparation.notes_total})",
        styles,
    )
    story.append(
        _paragraph("\n\n".join(rendered_notes), styles["body"])
        if rendered_notes
        else _paragraph("Keine Hinweise enthalten.", styles["body"])
    )


def _render_source(story: list[Flowable], analysis: dict[str, Any], styles: dict[str, ParagraphStyle]) -> None:
    source = analysis.get("source") or {}
    container = source.get("container") or {}
    runtime = analysis.get("runtime") or {}
    _subheading(story, "Quelle", styles)
    rows = [
        *_source_file_rows("Upload", source.get("upload")),
        *_source_file_rows("Rechnungs-XML", source.get("invoice_xml")),
        ("Containerart", container.get("kind")),
        ("Seiten", container.get("page_count")),
        ("Ausgewählter Anhang", container.get("selected_attachment")),
        ("Anzahl eingebetteter Dateien", container.get("attachment_count")),
        ("Analysezeitpunkt", runtime.get("generated_at")),
        (
            "Verarbeitungsdauer",
            f"{runtime.get('duration_ms')} ms" if _present(runtime.get("duration_ms")) else None,
        ),
        ("Anwendungsversion", runtime.get("application_version")),
    ]
    story.append(_key_value_table(rows, styles, include_empty=True))
    for index, attachment in enumerate(source.get("attachments") or [], start=1):
        _subheading(story, f"Eingebettete Datei {index}", styles)
        story.append(
            _key_value_table(
                [
                    ("Name", attachment.get("name")),
                    ("Größe", _format_bytes(attachment.get("size_bytes"))),
                    ("XML", attachment.get("is_xml")),
                    ("Ausgewählt", attachment.get("selected")),
                    ("SHA-256", attachment.get("sha256")),
                ],
                styles,
                include_empty=True,
            )
        )


def _iter_text_chunks(value: str, *, chunk_size: int) -> Iterable[str]:
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")

    offset = 0
    while offset < len(value):
        end = min(len(value), offset + chunk_size)
        newline_positions = [index for index in range(offset, end) if value[index] == "\n"]
        if len(newline_positions) > 60:
            end = newline_positions[60] + 1
        elif end < len(value):
            boundary_start = offset + max(1, chunk_size // 2)
            boundary = max(value.rfind(separator, boundary_start, end) for separator in ("\n", " ", "\t"))
            if boundary >= boundary_start:
                end = boundary + 1
        yield value[offset:end]
        offset = end


def _append_text_chunks(
    story: list[Flowable], value: str, styles: dict[str, ParagraphStyle], *, chunk_size: int = 2_000
) -> None:
    if not value:
        story.append(_paragraph("Nicht vorhanden.", styles["small"]))
        return
    for chunk in _iter_text_chunks(value, chunk_size=chunk_size):
        story.append(_paragraph(chunk, styles["technical"], ""))
        story.append(Spacer(1, 1.2 * mm))


def _evidence_text(value: Any) -> str:
    evidence = value if isinstance(value, dict) else {}
    raw = _text(evidence.get("value"), "")
    unit = _text(evidence.get("unit"), "")
    data_type = {
        "text": "Text",
        "code": "Code",
        "date": "Datum",
        "datetime": "Datum und Uhrzeit",
        "decimal": "Dezimalzahl",
        "integer": "Ganzzahl",
        "boolean": "Wahrheitswert",
        "identifier": "Kennung",
        "count": "Anzahl",
    }.get(str(evidence.get("data_type")), _text(evidence.get("data_type"), ""))
    rendered = " ".join(part for part in (raw, unit) if part)
    return f"{rendered} (Datentyp: {data_type})" if rendered and data_type else rendered


def _semantic_references_text(value: Any) -> str:
    references = value if isinstance(value, list) else []
    rendered: list[str] = []
    for reference in references:
        if not isinstance(reference, dict):
            continue
        identifier = _text(reference.get("id"), "")
        label = _text(reference.get("label"), "")
        if identifier and label:
            rendered.append(f"{label} ({identifier})")
        elif identifier or label:
            rendered.append(identifier or label)
    return "\n".join(rendered)


def _render_findings(
    story: list[Flowable],
    findings: list[dict[str, Any]],
    styles: dict[str, ParagraphStyle],
) -> None:
    if not findings:
        story.append(_paragraph("Keine Prüfmeldungen vorhanden.", styles["body"]))
        return
    for index, finding in enumerate(findings, start=1):
        rule = finding.get("rule") or {}
        occurrence = finding.get("occurrence") or {}
        xml_location = finding.get("xml_location") or {}
        story.append(
            _paragraph(
                f"{index}. {rule.get('title') or 'Prüfmeldung'}",
                styles["body_bold"],
            )
        )
        story.append(
            _key_value_table(
                [
                    ("Herkunft", _finding_origin_text(finding.get("origin"))),
                    ("Regelklasse", _rule_class_text(finding.get("rule_class"))),
                    ("Kennung", rule.get("id")),
                    ("Schweregrad", _severity_text(finding.get("severity"))),
                    ("Meldung", rule.get("message")),
                    ("Fachliche Referenz", _semantic_references_text(finding.get("semantic_references"))),
                    ("Vorkommensbereich", _occurrence_scope_text(occurrence.get("scope"))),
                    ("Vorkommensindex", occurrence.get("index")),
                    ("Vorkommenskennung", occurrence.get("identifier")),
                    ("JSON-Pointer", occurrence.get("json_pointer")),
                    ("XML-Pfad", xml_location.get("path")),
                    ("XML-Zeile", xml_location.get("line")),
                    ("XML-Spalte", xml_location.get("column")),
                    ("Ist", _evidence_text(finding.get("actual"))),
                    ("Erwartet", _evidence_text(finding.get("expected"))),
                    ("Regelquelle", rule.get("source")),
                    ("Regelreferenz", rule.get("reference")),
                    ("Regelprofil", rule.get("profile")),
                    ("Regelversion", rule.get("version")),
                ],
                styles,
                include_empty=True,
            )
        )
        story.append(Spacer(1, 2 * mm))


def _render_assessment(
    story: list[Flowable],
    analysis: dict[str, Any],
    presentation: Mapping[str, Any],
    styles: dict[str, ParagraphStyle],
    preparation: _PdfPreparation,
    *,
    scope: ReportScope,
) -> None:
    assessment = analysis.get("assessment") or {}
    story.append(PageBreak())
    _heading(story, "Prüfbericht", styles)
    story.append(
        _paragraph(
            "Die offizielle Prüfung, die interne Vorabprüfung und der Verarbeitungsstatus sind "
            "voneinander unabhängige Bewertungsachsen.",
            styles["body_bold"],
        )
    )
    presented_axes = {
        str(axis.get("key")): axis for axis in presentation.get("axes") or [] if isinstance(axis, Mapping)
    }
    axis_titles = {
        "internal": "Interne Vorabprüfung",
        "official": "Offizielle Prüfung",
        "processing": "Verarbeitung",
    }
    for axis_name in ("official", "internal", "processing"):
        axis = assessment.get(axis_name) or {}
        presented_axis = presented_axes.get(axis_name) or {}
        _subheading(story, presented_axis.get("title") or axis_titles[axis_name], styles)
        localized_status = presented_axis.get("label") or "Nicht bestimmt"
        if axis_name == "internal":
            rows = [
                ("Status", localized_status),
                ("Ausgeführt", axis.get("executed")),
                ("Zusammenfassung", axis.get("summary")),
                ("Prüfumfang", axis.get("scope")),
                ("Meldungszahlen", _counts_text(axis.get("counts"))),
            ]
        elif axis_name == "official":
            rows = [
                ("Status", localized_status),
                ("Angefordert", axis.get("requested")),
                ("Konfiguriert", axis.get("configured")),
                ("Ausgeführt", axis.get("executed")),
                ("Zusammenfassung", axis.get("summary")),
                ("Prozess-Rückgabecode", axis.get("exit_code")),
                ("Berichtsquelle", axis.get("report_source")),
                ("Meldungszahlen", _counts_text(axis.get("counts"))),
            ]
        else:
            rows = [
                ("Status", localized_status),
                ("Zusammenfassung", axis.get("summary")),
                ("Meldungszahlen", _counts_text(axis.get("counts"))),
            ]
        story.append(_key_value_table(rows, styles, include_empty=True))
        if axis_name == "processing":
            for index, limitation in enumerate(axis.get("limitations") or [], start=1):
                story.append(_paragraph(f"Einschränkung {index}", styles["body_bold"]))
                story.append(
                    _key_value_table(
                        [
                            ("Code", limitation.get("code")),
                            ("Meldung", limitation.get("message")),
                            ("Betroffener JSON-Pointer", limitation.get("affected_json_pointer")),
                        ],
                        styles,
                        include_empty=True,
                    )
                )
        findings = axis.get("findings") or []
        _subheading(story, f"Prüfmeldungen der Achse ({len(findings)})", styles)
        _render_findings(story, findings, styles)
        if scope == "complete" and axis_name == "official" and _present(axis.get("technical_output")):
            _subheading(story, "Technische Ausgabe der offiziellen Prüfung", styles)
            _append_text_chunks(story, axis.get("technical_output") or "", styles)

    story.append(
        _paragraph(
            f"Dargestellte Prüfmeldungen insgesamt: {preparation.findings_rendered} von {preparation.findings_total}.",
            styles["small"],
        )
    )

    official = assessment.get("official") or {}
    raw_report = official.get("raw_report")
    if scope == "complete" and (_present(raw_report) or preparation.official_report_length):
        story.append(PageBreak())
        _subheading(story, "Technischer offizieller Bericht (Auszug)", styles)
        if preparation.official_report_limited:
            story.append(
                _paragraph(
                    f"Der offizielle Rohbericht wurde im PDF auf {len(_text(raw_report, '')):,} von "
                    f"{preparation.official_report_length:,} Zeichen und höchstens "
                    f"{PDF_OFFICIAL_REPORT_NEWLINE_LIMIT:,} Zeilenumbrüche begrenzt.",
                    styles["small"],
                )
            )
        _append_text_chunks(story, raw_report or "", styles)


def _render_technical_appendix(
    story: list[Flowable],
    analysis: dict[str, Any],
    styles: dict[str, ParagraphStyle],
    preparation: _PdfPreparation,
) -> None:
    technical = analysis.get("technical") or {}
    rows = technical.get("fields") or []
    original_xml = technical.get("source_xml") or ""

    story.append(PageBreak())
    _heading(story, "Technischer Anhang", styles)
    story.append(
        _paragraph(
            "Der technische PDF-Anhang ist für eine sichere, per E-Mail nutzbare Darstellung begrenzt.\n"
            "Das vollständige Original bleibt unverändert im ursprünglichen Rechnungsanhang erhalten und "
            "kann über den API-Endpunkt /api/xml exportiert werden.",
            styles["body_bold"],
        )
    )
    story.append(Spacer(1, 2 * mm))
    story.append(
        _key_value_table(
            [
                ("Wurzelelement", technical.get("root_element")),
                ("Wurzel-Namespace", technical.get("root_namespace")),
                ("Erkannte technische Felder", technical.get("field_count")),
            ],
            styles,
            include_empty=True,
        )
    )
    notices = [
        f"Dargestellte technische Einträge: {preparation.technical_rows_rendered} von "
        f"{preparation.technical_rows_total}; Zeichenbudget: {preparation.technical_characters:,}.",
        f"Dargestelltes Original-XML: {len(original_xml):,} von {preparation.original_xml_length:,} Zeichen.",
        f"Technischer Anhang: höchstens {PDF_TECHNICAL_NEWLINE_LIMIT:,} Zeilenumbrüche.",
    ]
    if technical.get("truncated"):
        notices.append("Bereits die vorgelagerte technische Analyse hatte ihre konfigurierte Zeilengrenze erreicht.")
    if preparation.technical_limited or preparation.original_xml_limited:
        notices.append("Mindestens ein technischer Bereich wurde im PDF gekürzt.")
    story.append(_paragraph("\n".join(notices), styles["small"]))

    _subheading(story, "Quell-XML (Auszug)", styles)
    _append_text_chunks(story, original_xml, styles)

    _subheading(story, "XML-Elemente, Attribute und Namespaces", styles)
    if rows:
        header = [
            _paragraph("Typ", styles["table_header"]),
            _paragraph("Name", styles["table_header"]),
            _paragraph("XML-Pfad", styles["table_header"]),
            _paragraph("Namespace", styles["table_header"]),
            _paragraph("Wert", styles["table_header"]),
        ]
        data = [header]
        data.extend(
            [
                _paragraph(row["kind"], styles["technical"], ""),
                _paragraph(row["name"], styles["technical"], ""),
                _paragraph(row["path"], styles["technical"], ""),
                _paragraph(row["namespace"], styles["technical"], ""),
                _paragraph(row["value"], styles["technical"], ""),
            ]
            for row in rows
        )
        table = LongTable(
            data,
            colWidths=[18 * mm, 27 * mm, 60 * mm, 38 * mm, 32 * mm],
            repeatRows=1,
            splitByRow=1,
            splitInRow=1,
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#153842")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d5dfe3")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 1.2 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 1.2 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 1.1 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1.1 * mm),
                ]
            )
        )
        story.append(table)
    else:
        story.append(_paragraph("Keine technischen Einträge vorhanden.", styles["body"]))


def _prepare_presentation_for_pdf(
    analysis: dict[str, Any],
    presentation: Mapping[str, Any] | None,
    *,
    scope: ReportScope,
    preparation: _PdfPreparation,
) -> dict[str, Any]:
    if presentation is None:
        presentation = build_report_presentation(analysis, scope=scope)
    if not isinstance(presentation, Mapping):
        raise ValueError("Das Präsentationsmodell für den PDF-Bericht ist ungültig.")
    if presentation.get("scope") != scope:
        raise ValueError("Präsentationsmodell und Berichtsumfang stimmen nicht überein.")
    axes = [
        {key: axis.get(key) for key in ("key", "title", "status", "label", "summary", "counts")}
        for axis in presentation.get("axes") or []
        if isinstance(axis, Mapping)
    ]
    source = {
        "scope": presentation.get("scope"),
        "include_technical": presentation.get("include_technical"),
        "overall_status": presentation.get("overall_status"),
        "axes": axes,
        "header": presentation.get("header"),
        "header_facts": presentation.get("header_facts"),
        "payment_flow": presentation.get("payment_flow"),
    }
    budget = _TextBudget(PDF_CORE_CHARACTER_RESERVE, PDF_CORE_NEWLINE_RESERVE)
    prepared = _bounded_value(source, budget, preparation, ("presentation",))
    if not isinstance(prepared, dict):
        raise ValueError("Das Präsentationsmodell für den PDF-Bericht ist ungültig.")
    preparation.total_truncated |= budget.truncated_by_total
    return prepared


def _overall_status_color(status_key: Any) -> colors.Color:
    return {
        "ok": colors.HexColor("#e5f4ed"),
        "warning": colors.HexColor("#fff4d6"),
        "invalid": colors.HexColor("#fde9e7"),
    }.get(str(status_key), colors.HexColor("#edf2f4"))


def _render_report_header(
    story: list[Flowable],
    presentation: Mapping[str, Any],
    styles: dict[str, ParagraphStyle],
) -> None:
    header = presentation.get("header") or {}
    overall = presentation.get("overall_status") or {}
    payable_lines = [
        _text(header.get("payable_label"), "Ausstehender Betrag (BT-115)"),
        _text(header.get("payable")),
    ]
    if _present(header.get("due_date")):
        payable_lines.append(_text(header.get("due_date")))
    document_cell: list[Flowable] = [
        _paragraph(header.get("document_type_summary"), styles["small"], "E-Rechnung"),
        _paragraph(header.get("document_title"), styles["title"], "E-Rechnung"),
    ]
    if _present(header.get("subtitle")):
        document_cell.append(_paragraph(header.get("subtitle"), styles["small"]))
    header_table = Table(
        [
            [
                [
                    _paragraph("Gesamtstatus", styles["small"]),
                    _paragraph(overall.get("label"), styles["center"], "Nicht bestimmt"),
                ],
                document_cell,
                [_paragraph("\n".join(payable_lines), styles["right"])],
            ]
        ],
        colWidths=[38 * mm, 91 * mm, 46 * mm],
        splitByRow=1,
    )
    header_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, 0), _overall_status_color(overall.get("key"))),
                ("BACKGROUND", (1, 0), (-1, 0), colors.HexColor("#f4f7f8")),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#b9c9cf")),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d5dfe3")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
            ]
        )
    )
    story.append(header_table)
    axes = [axis for axis in presentation.get("axes") or [] if isinstance(axis, Mapping)]
    if axes:
        cells = []
        for axis in axes:
            counts = axis.get("counts")
            count_text = _counts_text(counts) if isinstance(counts, Mapping) else _text(counts, "")
            cells.append(
                [
                    _paragraph(axis.get("title"), styles["small"]),
                    _paragraph(axis.get("label"), styles["center"], "Nicht bestimmt"),
                    _paragraph(count_text, styles["small"], ""),
                ]
            )
        axes_table = Table([cells], colWidths=[175 * mm / len(cells)] * len(cells), splitByRow=1)
        axes_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafb")),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#b9c9cf")),
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d5dfe3")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
                ]
            )
        )
        story.extend([Spacer(1, 2 * mm), axes_table])


def _render_header_facts(
    story: list[Flowable],
    presentation: Mapping[str, Any],
    styles: dict[str, ParagraphStyle],
) -> None:
    facts = [fact for fact in presentation.get("header_facts") or [] if isinstance(fact, Mapping)]
    _heading(story, "Übersicht", styles)
    if not facts:
        story.append(_paragraph("Keine Rechnungsfelder verfügbar.", styles["body"]))
        return
    rows: list[list[Flowable]] = []
    for offset in range(0, len(facts), 2):
        pair = facts[offset : offset + 2]
        row: list[Flowable] = []
        for fact in pair:
            row.extend(
                [
                    _paragraph(fact.get("label"), styles["label"]),
                    _paragraph(fact.get("value"), styles["body"]),
                ]
            )
        if len(pair) == 1:
            row.extend([_paragraph("", styles["label"], ""), _paragraph("", styles["body"], "")])
        rows.append(row)
    table = Table(rows, colWidths=[31 * mm, 56 * mm, 31 * mm, 57 * mm], splitByRow=1, splitInRow=1)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f4f7f8")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f4f7f8")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d5dfe3")),
                ("LEFTPADDING", (0, 0), (-1, -1), 1.8 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 1.8 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5 * mm),
            ]
        )
    )
    story.append(table)


def render_pdf_report(
    analysis: dict[str, Any],
    *,
    generated_at: str,
    version: str,
    scope: ReportScope = "readable",
    presentation: Mapping[str, Any] | None = None,
) -> bytes:
    """Render a self-contained, non-persisted PDF report from schema-2 analysis data."""

    scope = _validate_scope(scope)
    _register_fonts()
    original_analysis = analysis
    analysis, preparation = _prepare_analysis_for_pdf(analysis, scope=scope)
    presentation = _prepare_presentation_for_pdf(
        original_analysis,
        presentation,
        scope=scope,
        preparation=preparation,
    )
    styles = _styles()
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=17 * mm,
        rightMargin=18 * mm,
        topMargin=17 * mm,
        bottomMargin=17 * mm,
        pageCompression=1,
        allowSplitting=1,
    )
    story: list[Flowable] = []
    invoice = analysis.get("document") or {}
    capabilities = analysis.get("capabilities") or {}
    totals = analysis.get("totals") or {}
    assessment = analysis.get("assessment") or {}
    internal = assessment.get("internal") or {}
    official = assessment.get("official") or {}
    processing = assessment.get("processing") or {}
    presented_axes = {
        str(axis.get("key")): axis for axis in presentation.get("axes") or [] if isinstance(axis, Mapping)
    }
    presented_header = presentation.get("header") or {}
    overall_status = presentation.get("overall_status") or {}
    story.append(_paragraph("E-Rechnungs-Viewer & Prüfer", styles["subtitle"]))
    _render_report_header(story, presentation, styles)
    _render_limits_notice(story, preparation, styles)
    _render_header_facts(story, presentation, styles)
    _render_parties(story, analysis, styles)
    _heading(story, "Nachlässe und Zuschläge", styles)
    header_adjustments = analysis.get("allowances_charges") or []
    if header_adjustments:
        _render_adjustments(story, header_adjustments, styles, heading="Anpassung")
    else:
        story.append(
            _paragraph(
                "Keine Nachlässe oder Zuschläge auf Rechnungsebene erkannt.",
                styles["body"],
            )
        )
    _render_lines(story, analysis, styles, preparation)
    _heading(story, "Umsatzsteuer und Summen", styles)
    _render_taxes(story, analysis, styles)

    _subheading(story, "Summen", styles)
    story.append(
        _key_value_table(
            [
                ("Summe Positionsnettobeträge", _amount_text(totals.get("line_net_total"))),
                ("Nachlässe", _amount_text(totals.get("allowance_total"))),
                ("Zuschläge", _amount_text(totals.get("charge_total"))),
                ("Nettobetrag ohne Umsatzsteuer", _amount_text(totals.get("tax_exclusive_total"))),
                ("Bruttobetrag einschließlich Umsatzsteuer", _amount_text(totals.get("tax_inclusive_total"))),
                ("Vorauszahlungen", _amount_text(totals.get("prepaid_total"))),
                ("Rundung", _amount_text(totals.get("rounding"))),
                ("Ausstehender Betrag (BT-115)", _amount_text(totals.get("payable"))),
            ],
            styles,
        )
    )
    _render_payment(story, analysis, presentation, styles)
    _heading(story, "Referenzen und Lieferung", styles)
    _render_references(story, analysis, styles)
    _render_periods_and_delivery(story, analysis, styles)
    _heading(story, "Hinweise und Quelle", styles)
    _render_notes(story, analysis, styles, preparation)
    _render_source(story, analysis, styles)
    _render_assessment(
        story,
        analysis,
        presentation,
        styles,
        preparation,
        scope=scope,
    )
    if scope == "complete":
        _render_technical_appendix(story, analysis, styles, preparation)

    def draw_page(canvas: Any, doc: Any, *, enforce_limit: bool = True) -> None:
        if enforce_limit and doc.page > PDF_PAGE_LIMIT:
            raise _PdfPageLimitExceeded
        canvas.saveState()
        canvas.setTitle("E-Rechnungs-Prüfbericht")
        canvas.setAuthor("E-Rechnungs-Pruefer contributors")
        canvas.setSubject("Lesbare Darstellung und Prüfung einer strukturierten elektronischen Rechnung")
        canvas.setFont(_FONT_REGULAR, 6.8)
        canvas.setFillColor(colors.HexColor("#5f7078"))
        canvas.drawString(
            17 * mm,
            8 * mm,
            f"Erzeugt am {generated_at} - E-Rechnungs-Prüfer {version}",
        )
        canvas.drawRightString(A4[0] - 18 * mm, 8 * mm, f"Seite {doc.page}")
        canvas.restoreState()

    try:
        document.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    except (_PdfPageLimitExceeded, LayoutError):
        fallback_buffer = BytesIO()
        fallback_document = SimpleDocTemplate(
            fallback_buffer,
            pagesize=A4,
            leftMargin=17 * mm,
            rightMargin=18 * mm,
            topMargin=17 * mm,
            bottomMargin=17 * mm,
            pageCompression=1,
            allowSplitting=1,
        )
        fallback_story: list[Flowable] = [
            _paragraph("E-Rechnungs-Viewer & Prüfer", styles["subtitle"]),
            _paragraph("Kompakter Ersatz-Prüfbericht", styles["title"]),
            _paragraph(
                f"Der vollständige PDF-Bericht konnte nicht innerhalb der Sicherheitsgrenze von maximal "
                f"{PDF_PAGE_LIMIT} {'Seite' if PDF_PAGE_LIMIT == 1 else 'Seiten'} gesetzt werden. "
                "Deshalb wurde dieser kompakte, gültige Ersatzbericht erzeugt.",
                styles["body_bold"],
            ),
            Spacer(1, 3 * mm),
            _key_value_table(
                [
                    ("Gesamtstatus", overall_status.get("label")),
                    ("Dokumenttyp", presented_header.get("document_type_summary")),
                    ("Rechnungsnummer", invoice.get("id")),
                    ("Syntax", capabilities.get("syntax")),
                    (
                        "Interne Vorabprüfung",
                        f"{(presented_axes.get('internal') or {}).get('label')} - "
                        f"{_counts_text(internal.get('counts'))}",
                    ),
                    (
                        "Offizielle Prüfung",
                        f"{(presented_axes.get('official') or {}).get('label')} - "
                        f"{_counts_text(official.get('counts'))}",
                    ),
                    (
                        "Verarbeitung",
                        f"{(presented_axes.get('processing') or {}).get('label')} - "
                        f"{_counts_text(processing.get('counts'))}",
                    ),
                    ("Ausstehender Betrag (BT-115)", _amount_text(totals.get("payable"))),
                    (
                        "Rechnungspositionen",
                        f"0 von {preparation.lines_total} im kompakten Ersatzbericht",
                    ),
                    (
                        "Prüfmeldungen",
                        f"0 von {preparation.findings_total} im kompakten Ersatzbericht",
                    ),
                ],
                styles,
                include_empty=True,
            ),
            Spacer(1, 3 * mm),
            _paragraph(
                "Die vollständigen analysierten Daten bleiben im vollständigen HTML-Bericht "
                "(scope=complete) und über /api/analyze zugänglich. Die vollständige Rechnungsquelle "
                "bleibt im Originalanhang und über /api/xml verfügbar.",
                styles["body"],
            ),
        ]

        def draw_fallback_page(canvas: Any, doc: Any) -> None:
            draw_page(canvas, doc, enforce_limit=False)

        fallback_document.build(
            fallback_story,
            onFirstPage=draw_fallback_page,
            onLaterPages=draw_fallback_page,
        )
        return fallback_buffer.getvalue()
    return buffer.getvalue()
