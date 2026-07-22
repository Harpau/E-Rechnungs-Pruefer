from __future__ import annotations

import threading
from collections.abc import Iterable
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

PDF_TECHNICAL_ROW_LIMIT = 2_000
PDF_TECHNICAL_CHARACTER_LIMIT = 250_000
PDF_RAW_XML_CHARACTER_LIMIT = 50_000
PDF_KOSIT_REPORT_CHARACTER_LIMIT = 100_000
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
PDF_KOSIT_NEWLINE_LIMIT = 2_000
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
    kosit_report_length: int = 0
    kosit_report_limited: bool = False

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
                self.kosit_report_limited,
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
        elif path == ("validation", "findings"):
            preparation.findings_total = len(values)
            limit = PDF_FINDING_LIMIT
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
        elif path == ("validation", "findings"):
            preparation.findings_rendered = len(selected)
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
        for key in ("kind", "path", "value"):
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


def _prepare_analysis_for_pdf(analysis: dict[str, Any]) -> tuple[dict[str, Any], _PdfPreparation]:
    preparation = _PdfPreparation()
    core_budget = _TextBudget(PDF_CORE_CHARACTER_RESERVE, PDF_CORE_NEWLINE_RESERVE)
    budget = _TextBudget(
        PDF_TOTAL_CHARACTER_LIMIT - PDF_CORE_CHARACTER_RESERVE,
        PDF_GENERAL_NEWLINE_LIMIT - PDF_CORE_NEWLINE_RESERVE,
    )

    source = dict(analysis)
    technical_source = source.pop("technical", {}) or {}
    document_source = dict(source.get("document") or {})
    core_document = {key: document_source.pop(key, None) for key in ("id", "syntax", "kind")}
    source["document"] = document_source

    totals_source = dict(source.pop("totals", {}) or {})
    core_totals = {
        key: totals_source.get(key)
        for key in (
            "line_total",
            "allowance_total",
            "charge_total",
            "tax_basis_total",
            "tax_total",
            "grand_total",
            "prepaid_amount",
            "rounding_amount",
            "due_payable_amount",
            "currency",
        )
    }

    validation_source = dict(source.get("validation") or {})
    official_source = dict(validation_source.get("official") or {})
    raw_kosit_report = official_source.pop("raw_report", None)
    counts_source = dict(validation_source.pop("counts", {}) or {})
    core_official = {key: official_source.get(key) for key in ("configured", "executed", "accepted", "summary")}
    core_validation = {
        "status": validation_source.pop("status", None),
        "counts": {key: counts_source.get(key) for key in ("error", "warning", "info")},
        "official": core_official,
        "assessment": validation_source.pop("assessment", None),
        "builtin": {"scope": (validation_source.get("builtin") or {}).get("scope")},
    }
    remaining_builtin = dict(validation_source.pop("builtin", {}) or {})
    remaining_builtin.pop("scope", None)
    validation_source["builtin"] = remaining_builtin
    remaining_official = {
        key: value
        for key, value in official_source.items()
        if key not in {"configured", "executed", "accepted", "summary"}
    }
    validation_source["official"] = remaining_official
    source["validation"] = validation_source

    prepared_core = _bounded_value(
        {"validation": core_validation, "totals": core_totals, "document": core_document},
        core_budget,
        preparation,
        (),
    )
    budget.remaining_characters += core_budget.remaining_characters
    budget.remaining_general_newlines += core_budget.remaining_general_newlines
    prepared = _bounded_value(source, budget, preparation, ())
    prepared_document = prepared.setdefault("document", {})
    prepared_document.update(prepared_core["document"])
    prepared["totals"] = prepared_core["totals"]
    prepared_validation = prepared.setdefault("validation", {})
    prepared_validation.update(
        {
            "status": prepared_core["validation"]["status"],
            "counts": prepared_core["validation"]["counts"],
            "assessment": prepared_core["validation"]["assessment"],
        }
    )
    prepared_builtin = prepared_validation.setdefault("builtin", {})
    prepared_builtin.update(prepared_core["validation"]["builtin"])
    prepared_official = prepared_validation.setdefault("official", {})
    prepared_official.update(prepared_core["validation"]["official"])
    if _present(raw_kosit_report):
        kosit_report, limited, original_length, _ = budget.special_text(
            raw_kosit_report,
            character_limit=PDF_KOSIT_REPORT_CHARACTER_LIMIT,
            newline_limit=PDF_KOSIT_NEWLINE_LIMIT,
        )
        prepared_official["raw_report"] = kosit_report
        preparation.kosit_report_length = original_length
        preparation.kosit_report_limited = limited

    technical = technical_source if isinstance(technical_source, dict) else {}
    prepared_rows = _prepare_technical_rows(technical.get("rows"), budget, preparation)
    remaining_technical_newlines = max(
        0,
        PDF_TECHNICAL_NEWLINE_LIMIT - sum(value.count("\n") for row in prepared_rows for value in row.values()),
    )
    original_xml, xml_limited, xml_length, _ = budget.special_text(
        technical.get("original_xml"),
        character_limit=PDF_RAW_XML_CHARACTER_LIMIT,
        newline_limit=remaining_technical_newlines,
    )
    preparation.original_xml_length = xml_length
    preparation.original_xml_limited = xml_limited
    prepared["technical"] = {
        "rows": prepared_rows,
        "original_xml": original_xml,
        "truncated": bool(technical.get("truncated")),
    }
    preparation.total_truncated |= budget.truncated_by_total or core_budget.truncated_by_total
    return prepared, preparation


def _joined_entries(entries: Iterable[dict[str, Any]], *, value_key: str = "value") -> str:
    values: list[str] = []
    for entry in entries:
        value = entry.get(value_key)
        if not _present(value):
            continue
        scheme = entry.get("scheme")
        values.append(f"{value} ({scheme})" if _present(scheme) else str(value))
    return ", ".join(values)


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
        "Die vollständigen analysierten Daten bleiben im HTML-Bericht und über /api/analyze zugänglich. "
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


def _party_rows(party: dict[str, Any]) -> list[tuple[str, Any]]:
    address = party.get("address") or {}
    contact = party.get("contact") or {}
    endpoint = party.get("endpoint") or {}
    address_lines: list[str] = [str(address[key]) for key in ("line1", "line2", "line3") if _present(address.get(key))]
    locality = " ".join(str(value) for value in (address.get("postcode"), address.get("city")) if _present(value))
    if locality:
        address_lines.append(locality)
    if _present(address.get("subdivision")):
        address_lines.append(str(address["subdivision"]))
    country = address.get("country") or address.get("country_code")
    if _present(country):
        address_lines.append(str(country))
    endpoint_value = endpoint.get("value")
    if _present(endpoint.get("scheme")) and _present(endpoint_value):
        endpoint_value = f"{endpoint_value} ({endpoint['scheme']})"
    return [
        ("Name", party.get("name")),
        ("Handelsname", party.get("trading_name")),
        ("Beschreibung", party.get("description")),
        ("Anschrift", "\n".join(address_lines)),
        ("Kennungen", _joined_entries(party.get("ids") or [])),
        ("Steuerkennungen", _joined_entries(party.get("tax_ids") or [])),
        ("Elektronische Adresse", endpoint_value),
        ("Kontakt", contact.get("name")),
        ("Abteilung", contact.get("department")),
        ("Telefon", contact.get("phone")),
        ("E-Mail", contact.get("email")),
    ]


def _render_parties(story: list[Flowable], analysis: dict[str, Any], styles: dict[str, ParagraphStyle]) -> None:
    _heading(story, "Beteiligte Parteien", styles)
    for key, title in (
        ("seller", "Verkäufer"),
        ("buyer", "Käufer"),
        ("payee", "Zahlungsempfänger"),
        ("invoicee", "Rechnungsempfänger"),
        ("ship_to", "Lieferempfänger"),
    ):
        party = analysis.get(key) or {}
        rows = _party_rows(party)
        if key not in {"seller", "buyer"} and not any(_present(value) for _, value in rows):
            continue
        _subheading(story, title, styles)
        story.append(_key_value_table(rows, styles))


def _render_adjustments(
    story: list[Flowable],
    adjustments: list[dict[str, Any]],
    styles: dict[str, ParagraphStyle],
    currency: str,
    *,
    heading: str,
) -> None:
    if not adjustments:
        return
    _subheading(story, heading, styles)
    for index, item in enumerate(adjustments, start=1):
        rows = [
            ("Art", item.get("type_label") or item.get("type")),
            ("Betrag", _format_money(item.get("amount"), item.get("currency") or currency)),
            ("Prozentsatz", f"{_format_number(item.get('percent'))} %" if _present(item.get("percent")) else None),
            (
                "Basis",
                _format_money(item.get("basis_amount"), item.get("basis_currency") or currency)
                if _present(item.get("basis_amount"))
                else None,
            ),
            ("Grund", item.get("reason")),
            ("Grundcode", item.get("reason_code")),
        ]
        story.append(_paragraph(f"{heading} {index}", styles["body_bold"]))
        story.append(_key_value_table(rows, styles))


def _render_lines(
    story: list[Flowable],
    analysis: dict[str, Any],
    styles: dict[str, ParagraphStyle],
    currency: str,
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
        _subheading(story, f"Position {line.get('id') or index}: {line.get('name') or 'Ohne Bezeichnung'}", styles)
        period = line.get("period") or {}
        period_text = " - ".join(
            value
            for value in (
                _format_date(period.get("start")) if _present(period.get("start")) else "",
                _format_date(period.get("end")) if _present(period.get("end")) else "",
                _text(period.get("description"), ""),
            )
            if value
        )
        classifications = [
            " · ".join(
                str(value)
                for value in (
                    item.get("code"),
                    item.get("name"),
                    f"Schema {item['scheme']}" if _present(item.get("scheme")) else None,
                    f"Version {item['version']}" if _present(item.get("version")) else None,
                )
                if _present(value)
            )
            for item in line.get("classifications") or []
        ]
        properties = [
            f"{item.get('name') or 'Eigenschaft'}: {_text(item.get('value'))}"
            for item in line.get("additional_properties") or []
        ]
        tax_category = line.get("tax_category_display") or line.get("tax_category_label") or line.get("tax_category")
        rows = [
            ("Bezeichnung", line.get("name")),
            ("Beschreibung", line.get("description")),
            ("Verkäufer-Artikelnummer", line.get("seller_item_id")),
            ("Käufer-Artikelnummer", line.get("buyer_item_id")),
            (
                "Standard-Artikelkennung",
                f"{line.get('standard_item_id')} ({line.get('standard_item_scheme')})"
                if _present(line.get("standard_item_id")) and _present(line.get("standard_item_scheme"))
                else line.get("standard_item_id"),
            ),
            (
                "Menge",
                f"{_format_number(line.get('quantity'))} {line.get('unit_label') or line.get('unit_code') or ''}",
            ),
            ("Preis", _format_money(line.get("price"), line.get("price_currency") or currency)),
            (
                "Preisbasis",
                f"{_format_number(line.get('base_quantity'))} {line.get('base_unit_label') or line.get('base_unit_code') or ''}",
            ),
            ("Steuerkategorie", tax_category),
            ("Steuersatz", f"{_format_number(line.get('tax_rate'))} %" if _present(line.get("tax_rate")) else None),
            ("Steuerart", line.get("tax_type")),
            ("Positionsnetto", _format_money(line.get("line_total"), line.get("line_currency") or currency)),
            ("Hinweise", "\n".join(str(value) for value in line.get("notes") or [])),
            ("Abrechnungszeitraum", period_text),
            ("Bestellposition", line.get("order_line_reference")),
            ("Kontierung", line.get("accounting_cost")),
            ("Ursprungsland", line.get("origin_country_label") or line.get("origin_country")),
            ("Klassifikationen", "\n".join(classifications)),
            ("Weitere Eigenschaften", "\n".join(properties)),
        ]
        story.append(_key_value_table(rows, styles))
        _render_adjustments(
            story,
            line.get("allowances_charges") or [],
            styles,
            currency,
            heading=f"Nachlass/Zuschlag zu Position {line.get('id') or index}",
        )


def _render_taxes(
    story: list[Flowable], analysis: dict[str, Any], styles: dict[str, ParagraphStyle], currency: str
) -> None:
    _heading(story, "Umsatzsteuer", styles)
    taxes = analysis.get("taxes") or []
    if not taxes:
        story.append(_paragraph("Keine Steueraufschlüsselung erkannt.", styles["body"]))
        return
    for index, tax in enumerate(taxes, start=1):
        raw_code = tax.get("category_code")
        label = tax.get("category_display") or tax.get("category_label") or raw_code or "Steuer"
        _subheading(story, f"Steueraufschlüsselung {index}: {label}", styles)
        story.append(
            _key_value_table(
                [
                    ("Steuerart", tax.get("type")),
                    ("Kategoriecode (Original)", raw_code),
                    ("Kategorie", label),
                    ("Steuersatz", f"{_format_number(tax.get('rate'))} %" if _present(tax.get("rate")) else None),
                    ("Bezeichnung der Basis", tax.get("basis_label")),
                    (
                        "Bemessungsgrundlage",
                        _format_money(tax.get("basis_amount"), tax.get("basis_currency") or currency)
                        if _present(tax.get("basis_amount"))
                        else None,
                    ),
                    ("Steuerbetrag", _format_money(tax.get("tax_amount"), tax.get("tax_currency") or currency)),
                    ("Befreiungsgrund", tax.get("exemption_reason")),
                    ("Befreiungsgrundcode", tax.get("exemption_reason_code")),
                ],
                styles,
            )
        )


def _render_payment_and_references(
    story: list[Flowable], analysis: dict[str, Any], styles: dict[str, ParagraphStyle], currency: str
) -> None:
    payment = analysis.get("payment") or {}
    _heading(story, "Zahlungsinformationen", styles)
    if _present(payment.get("reference")):
        story.append(_key_value_table([("Zahlungsreferenz", payment.get("reference"))], styles))
    for index, means in enumerate(payment.get("means") or [], start=1):
        _subheading(story, f"Zahlungsweg {index}", styles)
        story.append(
            _key_value_table(
                [
                    ("Art", means.get("type_label") or means.get("type_code")),
                    ("Information", means.get("information")),
                    ("IBAN / Konto", means.get("iban")),
                    ("Kontoinhaber", means.get("account_name")),
                    ("BIC", means.get("bic")),
                    ("IBAN des Zahlers", means.get("payer_iban")),
                    ("Mandatsreferenz", means.get("mandate_reference")),
                    ("Gläubiger-ID", means.get("creditor_id")),
                    ("Kartennummer/Konto", means.get("card_account")),
                    ("Zahlungs-ID", means.get("payment_id")),
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
                    ("Fälligkeit", _format_date(term.get("due_date")) if _present(term.get("due_date")) else None),
                    ("Lastschriftmandat", term.get("direct_debit_mandate_id")),
                    (
                        "Teilzahlungsbetrag",
                        _format_money(term.get("partial_payment_amount"), currency)
                        if _present(term.get("partial_payment_amount"))
                        else None,
                    ),
                ],
                styles,
            )
        )
    if not payment.get("means") and not payment.get("terms") and not _present(payment.get("reference")):
        story.append(_paragraph("Keine Zahlungsangaben erkannt.", styles["body"]))

    references = analysis.get("references") or {}
    delivery = analysis.get("delivery") or {}
    _heading(story, "Referenzen und Lieferung", styles)
    story.append(
        _key_value_table(
            [
                ("Bestellreferenz Käufer", references.get("buyer_order")),
                ("Bestellreferenz Verkäufer", references.get("seller_order")),
                ("Vertrag", references.get("contract")),
                ("Projekt", references.get("project")),
                ("Vorgängerrechnungen", ", ".join(str(value) for value in references.get("preceding_invoices") or [])),
                ("Lieferdatum", _format_date(delivery.get("date")) if _present(delivery.get("date")) else None),
                ("Versandavis", delivery.get("despatch_advice_reference")),
                ("Wareneingangsavis", delivery.get("receiving_advice_reference")),
            ],
            styles,
        )
    )
    for index, reference in enumerate(references.get("additional_documents") or [], start=1):
        _subheading(story, f"Weiteres Dokument {index}", styles)
        story.append(
            _key_value_table(
                [
                    ("ID", reference.get("id")),
                    ("Typ", reference.get("name") or reference.get("type_code")),
                    ("Beschreibung", reference.get("description")),
                    ("Datei", reference.get("attachment_filename")),
                    ("MIME", reference.get("attachment_mime")),
                    ("URI", reference.get("external_uri")),
                ],
                styles,
            )
        )


def _render_source(story: list[Flowable], analysis: dict[str, Any], styles: dict[str, ParagraphStyle]) -> None:
    source = analysis.get("source") or {}
    container = source.get("container") or {}
    processing = analysis.get("processing") or {}
    _heading(story, "Quelle und Verarbeitung", styles)
    story.append(
        _key_value_table(
            [
                ("Datei", source.get("filename")),
                ("Dateityp", source.get("media_type")),
                ("Größe", _format_bytes(source.get("size"))),
                ("SHA-256 Quelldatei", source.get("sha256")),
                ("Container", container.get("type")),
                ("Seiten", container.get("page_count")),
                ("Ausgewählter Anhang", container.get("selected_attachment")),
                ("Anzahl eingebetteter Dateien", container.get("attachment_count")),
                ("XML-Datei", source.get("xml_filename")),
                ("XML-Größe", _format_bytes(source.get("xml_size"))),
                ("SHA-256 XML", source.get("xml_sha256")),
                ("Verarbeitungsdauer", f"{processing.get('duration_ms')} ms"),
                ("Anwendungsversion", processing.get("application_version")),
            ],
            styles,
        )
    )
    for index, attachment in enumerate(source.get("attachments") or [], start=1):
        _subheading(story, f"Eingebettete Datei {index}", styles)
        story.append(
            _key_value_table(
                [
                    ("Name", attachment.get("name")),
                    ("Größe", _format_bytes(attachment.get("size"))),
                    ("XML", bool(attachment.get("is_xml"))),
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


def _render_validation(
    story: list[Flowable],
    analysis: dict[str, Any],
    styles: dict[str, ParagraphStyle],
    preparation: _PdfPreparation,
) -> None:
    validation = analysis.get("validation") or {}
    official = validation.get("official") or {}
    counts = validation.get("counts") or {}
    story.append(PageBreak())
    _heading(story, "Prüfbericht", styles)
    story.append(
        _key_value_table(
            [
                ("Gesamtstatus", validation.get("status")),
                ("Bewertung", validation.get("assessment")),
                ("Fehler", counts.get("error")),
                ("Warnungen", counts.get("warning")),
                ("Hinweise", counts.get("info")),
                ("Interner Prüfumfang", (validation.get("builtin") or {}).get("scope")),
                ("KoSIT konfiguriert", official.get("configured")),
                ("KoSIT ausgeführt", official.get("executed")),
                ("KoSIT angenommen", official.get("accepted")),
                ("KoSIT Zusammenfassung", official.get("summary")),
            ],
            styles,
            include_empty=True,
        )
    )
    findings = validation.get("findings") or []
    _subheading(
        story,
        f"Prüfmeldungen ({preparation.findings_rendered} von {preparation.findings_total})",
        styles,
    )
    if not findings:
        story.append(_paragraph("Keine Prüfmeldungen vorhanden.", styles["body"]))
    for index, finding in enumerate(findings, start=1):
        story.append(_paragraph(f"{index}. {finding.get('title') or 'Prüfmeldung'}", styles["body_bold"]))
        story.append(
            _key_value_table(
                [
                    ("Kennung", finding.get("id")),
                    ("Schweregrad", finding.get("severity")),
                    ("Meldung", finding.get("message")),
                    ("Ort", finding.get("location")),
                    ("Ist", finding.get("actual")),
                    ("Erwartet", finding.get("expected")),
                    ("Quelle", finding.get("source")),
                ],
                styles,
                include_empty=True,
            )
        )
        story.append(Spacer(1, 2 * mm))

    raw_report = official.get("raw_report")
    if _present(raw_report) or preparation.kosit_report_length:
        story.append(PageBreak())
        _subheading(story, "Technischer KoSIT-Bericht (Auszug)", styles)
        if preparation.kosit_report_limited:
            story.append(
                _paragraph(
                    f"Der KoSIT-Rohbericht wurde im PDF auf {len(_text(raw_report, '')):,} von "
                    f"{preparation.kosit_report_length:,} Zeichen und höchstens "
                    f"{PDF_KOSIT_NEWLINE_LIMIT:,} Zeilenumbrüche begrenzt.",
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
    rows = technical.get("rows") or []
    original_xml = technical.get("original_xml") or ""

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

    _subheading(story, "Original-XML (Auszug)", styles)
    _append_text_chunks(story, original_xml, styles)

    _subheading(story, "XML-Elemente, Attribute und Namespaces", styles)
    if rows:
        header = [
            _paragraph("Typ", styles["body_bold"]),
            _paragraph("XML-Pfad", styles["body_bold"]),
            _paragraph("Wert", styles["body_bold"]),
        ]
        data = [header]
        data.extend(
            [
                _paragraph(row["kind"], styles["technical"], ""),
                _paragraph(row["path"], styles["technical"], ""),
                _paragraph(row["value"], styles["technical"], ""),
            ]
            for row in rows
        )
        table = LongTable(
            data,
            colWidths=[24 * mm, 69 * mm, 82 * mm],
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


def render_pdf_report(
    analysis: dict[str, Any],
    *,
    generated_at: str,
    version: str,
) -> bytes:
    """Render a self-contained, non-persisted PDF report from normalized analysis data."""

    _register_fonts()
    analysis, preparation = _prepare_analysis_for_pdf(analysis)
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
    totals = analysis.get("totals") or {}
    profile = analysis.get("profile") or {}
    validation = analysis.get("validation") or {}
    counts = validation.get("counts") or {}
    currency = invoice.get("currency") or totals.get("currency") or "EUR"

    story.append(_paragraph("E-Rechnungs-Viewer & Prüfer", styles["subtitle"]))
    story.append(_paragraph(f"{invoice.get('kind') or 'E-Rechnung'} {invoice.get('id') or ''}", styles["title"]))
    summary = Table(
        [
            [
                _paragraph(f"Status\n{validation.get('status') or '-'}", styles["center"]),
                _paragraph(
                    f"Fehler {counts.get('error', 0)} · Warnungen {counts.get('warning', 0)} · "
                    f"Hinweise {counts.get('info', 0)}",
                    styles["center"],
                ),
                _paragraph(f"Zahlbetrag\n{_format_money(totals.get('due_payable_amount'), currency)}", styles["right"]),
            ]
        ],
        colWidths=[42 * mm, 75 * mm, 58 * mm],
    )
    summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#e5f3f5")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#0b6477")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
            ]
        )
    )
    story.append(summary)
    _render_limits_notice(story, preparation, styles)

    _heading(story, "Rechnungsübersicht", styles)
    story.append(
        _key_value_table(
            [
                ("Rechnungsnummer", invoice.get("id")),
                ("Rechnungsdatum", _format_date(invoice.get("issue_date"))),
                ("Liefer-/Leistungsdatum", _format_date(invoice.get("delivery_date"))),
                ("Fälligkeit", _format_date(invoice.get("due_date"))),
                ("Steuerdatum", _format_date(invoice.get("tax_point_date"))),
                ("Rechnungsart", invoice.get("type_label") or invoice.get("type_code")),
                ("Währung", invoice.get("currency_label") or invoice.get("currency")),
                ("Käuferreferenz", invoice.get("buyer_reference")),
                ("Syntax", invoice.get("syntax")),
                ("Format", invoice.get("format")),
                ("Profil", invoice.get("profile_name")),
                ("Profilkennung", invoice.get("profile_id")),
                ("Geschäftsprozess", profile.get("business_process_id")),
            ],
            styles,
            include_empty=True,
        )
    )
    notes = invoice.get("notes") or []
    _subheading(
        story,
        f"Rechnungshinweise ({preparation.notes_rendered} von {preparation.notes_total})",
        styles,
    )
    story.append(
        _paragraph("\n\n".join(str(note) for note in notes), styles["body"])
        if notes
        else _paragraph("Keine Hinweise enthalten.", styles["body"])
    )

    _render_parties(story, analysis, styles)
    _heading(story, "Nachlässe und Zuschläge auf Rechnungsebene", styles)
    header_adjustments = analysis.get("header_allowances_charges") or []
    if header_adjustments:
        _render_adjustments(story, header_adjustments, styles, currency, heading="Anpassung")
    else:
        story.append(_paragraph("Keine Nachlässe oder Zuschläge auf Rechnungsebene erkannt.", styles["body"]))
    _render_lines(story, analysis, styles, currency, preparation)
    _render_taxes(story, analysis, styles, currency)

    _heading(story, "Summen", styles)
    story.append(
        _key_value_table(
            [
                ("Summe Positionen", _format_money(totals.get("line_total"), currency)),
                ("Nachlässe", _format_money(totals.get("allowance_total"), currency)),
                ("Zuschläge", _format_money(totals.get("charge_total"), currency)),
                ("Nettobetrag / Steuerbasis", _format_money(totals.get("tax_basis_total"), currency)),
                ("Umsatzsteuer", _format_money(totals.get("tax_total"), currency)),
                ("Bruttobetrag", _format_money(totals.get("grand_total"), currency)),
                ("Vorauszahlungen", _format_money(totals.get("prepaid_amount"), currency)),
                ("Rundung", _format_money(totals.get("rounding_amount"), currency)),
                ("Zahlbetrag", _format_money(totals.get("due_payable_amount"), currency)),
            ],
            styles,
        )
    )
    _render_payment_and_references(story, analysis, styles, currency)
    _render_source(story, analysis, styles)
    _render_validation(story, analysis, styles, preparation)
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
        canvas.drawString(17 * mm, 8 * mm, f"Erzeugt am {generated_at} · E-Rechnungs-Prüfer {version}")
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
                "Deshalb wurde dieser kompakte, gültige "
                "Ersatzbericht erzeugt.",
                styles["body_bold"],
            ),
            Spacer(1, 3 * mm),
            _key_value_table(
                [
                    ("Rechnungsart", invoice.get("kind")),
                    ("Rechnungsnummer", invoice.get("id")),
                    ("Syntax", invoice.get("syntax")),
                    ("Gesamtstatus", validation.get("status")),
                    ("Fehler", counts.get("error")),
                    ("Warnungen", counts.get("warning")),
                    ("Hinweise", counts.get("info")),
                    ("KoSIT konfiguriert", (validation.get("official") or {}).get("configured")),
                    ("KoSIT ausgeführt", (validation.get("official") or {}).get("executed")),
                    ("KoSIT angenommen", (validation.get("official") or {}).get("accepted")),
                    ("KoSIT Zusammenfassung", (validation.get("official") or {}).get("summary")),
                    ("Summe Positionen", _format_money(totals.get("line_total"), currency)),
                    ("Nachlässe", _format_money(totals.get("allowance_total"), currency)),
                    ("Zuschläge", _format_money(totals.get("charge_total"), currency)),
                    ("Nettobetrag / Steuerbasis", _format_money(totals.get("tax_basis_total"), currency)),
                    ("Umsatzsteuer", _format_money(totals.get("tax_total"), currency)),
                    ("Bruttobetrag", _format_money(totals.get("grand_total"), currency)),
                    ("Vorauszahlungen", _format_money(totals.get("prepaid_amount"), currency)),
                    ("Rundung", _format_money(totals.get("rounding_amount"), currency)),
                    ("Zahlbetrag", _format_money(totals.get("due_payable_amount"), currency)),
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
                "Die vollständigen analysierten Daten bleiben im HTML-Bericht und über /api/analyze "
                "zugänglich. Die vollständige Rechnungsquelle bleibt im Originalanhang und über /api/xml "
                "verfügbar.",
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
