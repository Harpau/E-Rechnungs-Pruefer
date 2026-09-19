"""Standalone HTML-report filters, independent of the ASGI application."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader


def _format_number(value: Any, digits: int | None = None) -> str:
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    if value is None or value == "":
        return "–"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if digits is not None:
        raw = format(number, f".{digits}f")
    else:
        raw = format(number, "f")
        if "." in raw:
            raw = raw.rstrip("0").rstrip(".")
    integer, dot, fraction = raw.partition(".")
    sign = ""
    if integer.startswith("-"):
        sign, integer = "-", integer[1:]
    grouped = ".".join(
        [integer[max(0, len(integer) - offset - 3) : len(integer) - offset] for offset in range(0, len(integer), 3)][
            ::-1
        ]
    )
    return f"{sign}{grouped}{',' + fraction if dot else ''}"


def _format_money(value: Any, currency: str | None = None) -> str:
    if isinstance(value, dict):
        currency = str(value.get("currency") or currency or "") or None
        value = value.get("value")
    formatted = _format_number(value, 2)
    return f"{formatted} {currency}".strip() if formatted != "–" else formatted


def _format_date(value: Any) -> str:
    if not value:
        return "–"
    text = str(value)
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return text


def _format_bytes(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "–"
    units = ["B", "KB", "MB", "GB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.0f} {units[index]}" if index == 0 else f"{size:.1f} {units[index]}"


def _party_has_data(party: Any) -> bool:
    if not isinstance(party, dict):
        return False
    for key, value in party.items():
        if key in {"address", "contact"} and isinstance(value, dict):
            if any(item not in (None, "", [], {}) for item in value.values()):
                return True
        elif value not in (None, "", [], {}):
            return True
    return False


def report_environment() -> Environment:
    environment = Environment(loader=FileSystemLoader(Path(__file__).with_name("templates")), autoescape=True)
    environment.filters.update(
        {
            "de_number": _format_number,
            "money": _format_money,
            "de_date": _format_date,
            "bytes": _format_bytes,
            "party_has_data": _party_has_data,
        }
    )
    return environment
