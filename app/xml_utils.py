from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from lxml import etree


class InvoiceInputError(ValueError):
    """Raised when an uploaded document cannot safely be processed."""


FORBIDDEN_XML_MARKERS = (b"<!DOCTYPE", b"<!ENTITY")


def safe_parse_xml(xml_bytes: bytes) -> etree._Element:
    if not xml_bytes or not xml_bytes.strip():
        raise InvoiceInputError("Die Datei ist leer.")

    upper = xml_bytes.upper().replace(b"\x00", b"")
    if any(marker in upper for marker in FORBIDDEN_XML_MARKERS):
        raise InvoiceInputError(
            "XML-Dokumente mit DTD- oder ENTITY-Deklarationen werden aus Sicherheitsgründen nicht verarbeitet."
        )

    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        recover=False,
        huge_tree=False,
        remove_comments=False,
        remove_pis=False,
    )
    try:
        root = etree.fromstring(xml_bytes, parser=parser)
    except etree.XMLSyntaxError as exc:
        message = str(exc.error_log.last_error or exc)
        raise InvoiceInputError(f"Das XML ist nicht wohlgeformt: {message}") from exc

    if not isinstance(root.tag, str):
        raise InvoiceInputError("Das XML enthält kein auswertbares Wurzelelement.")
    return root


def qname_parts(name: str) -> tuple[str | None, str]:
    if name.startswith("{") and "}" in name:
        namespace, local = name[1:].split("}", 1)
        return namespace, local
    return None, name


def local_name(node_or_tag: etree._Element | str | Any) -> str:
    tag = node_or_tag.tag if hasattr(node_or_tag, "tag") else node_or_tag
    if not isinstance(tag, str):
        return ""
    return qname_parts(tag)[1]


def namespace_uri(node_or_tag: etree._Element | str | Any) -> str | None:
    tag = node_or_tag.tag if hasattr(node_or_tag, "tag") else node_or_tag
    if not isinstance(tag, str):
        return None
    return qname_parts(tag)[0]


def nodes(node: etree._Element, expression: str) -> list[etree._Element]:
    result = node.xpath(expression)
    return [item for item in result if isinstance(item, etree._Element)]


def first_node(node: etree._Element | None, expression: str) -> etree._Element | None:
    if node is None:
        return None
    result = node.xpath(expression)
    for item in result:
        if isinstance(item, etree._Element):
            return item
    return None


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, etree._Element):
        value = "".join(value.itertext())
    value = str(value).strip()
    return value or None


def first_text(node: etree._Element | None, expression: str) -> str | None:
    if node is None:
        return None
    result = node.xpath(expression)
    for item in result:
        text = clean_text(item)
        if text is not None:
            return text
    return None


def all_text(node: etree._Element | None, expression: str) -> list[str]:
    if node is None:
        return []
    values: list[str] = []
    for item in node.xpath(expression):
        text = clean_text(item)
        if text is not None:
            values.append(text)
    return values


def attr_value(node: etree._Element | None, name: str) -> str | None:
    if node is None:
        return None
    value = node.get(name)
    return clean_text(value)


def decimal_value(value: Any) -> Decimal | None:
    text = clean_text(value)
    if text is None:
        return None
    text = text.replace("\u00a0", "").replace(" ", "")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def decimal_string(value: Decimal | str | int | float | None) -> str | None:
    number = decimal_value(value)
    if number is None:
        return None
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def money_string(value: Decimal | str | int | float | None) -> str | None:
    number = decimal_value(value)
    if number is None:
        return None
    return format(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def parse_date_value(value: str | None, format_code: str | None = None) -> str | None:
    text = clean_text(value)
    if text is None:
        return None

    candidates: list[str] = []
    if format_code == "102" or re.fullmatch(r"\d{8}", text):
        candidates.append("%Y%m%d")
    if format_code == "101" or re.fullmatch(r"\d{6}", text):
        candidates.append("%y%m%d")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        candidates.append("%Y-%m-%d")
    if re.fullmatch(r"\d{4}\d{2}", text):
        candidates.append("%Y%m")
    if re.fullmatch(r"\d{4}-\d{2}", text):
        candidates.append("%Y-%m")

    for pattern in candidates:
        try:
            parsed = datetime.strptime(text, pattern)
            if pattern in {"%Y%m", "%Y-%m"}:
                return parsed.strftime("%Y-%m")
            return parsed.date().isoformat()
        except ValueError:
            continue

    # Preserve valid ISO date-times while normalising the date portion.
    try:
        parsed_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed_dt.date().isoformat()
    except ValueError:
        return text


def date_object(value: str | None) -> date | None:
    if not value:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
    except ValueError:
        pass
    return None


def sha256_hex(data: bytes) -> str:
    return sha256(data).hexdigest()


def decode_xml_bytes(xml_bytes: bytes) -> str:
    """Decode XML bytes according to the declaration while preserving source text."""
    declaration = xml_bytes[:300]
    match = re.search(rb"encoding\s*=\s*['\"]([A-Za-z0-9._-]+)['\"]", declaration, re.IGNORECASE)
    encodings = []
    if match:
        encodings.append(match.group(1).decode("ascii", errors="ignore"))
    encodings.extend(["utf-8-sig", "utf-16", "iso-8859-1"])
    for encoding in encodings:
        try:
            return xml_bytes.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return xml_bytes.decode("utf-8", errors="replace")


def pretty_xml(root: etree._Element) -> str:
    return etree.tostring(
        root,
        encoding="unicode",
        pretty_print=True,
        xml_declaration=False,
        with_tail=False,
    )


def _sibling_index(element: etree._Element) -> int:
    parent = element.getparent()
    if parent is None:
        return 1
    name = local_name(element)
    index = 0
    for sibling in parent:
        if isinstance(sibling.tag, str) and local_name(sibling) == name:
            index += 1
        if sibling is element:
            return index
    return 1


def element_path(element: etree._Element) -> str:
    segments: list[str] = []
    current: etree._Element | None = element
    while current is not None and isinstance(current.tag, str):
        segments.append(f"{local_name(current)}[{_sibling_index(current)}]")
        current = current.getparent()
    return "/" + "/".join(reversed(segments))


def technical_rows(root: etree._Element, max_rows: int = 100_000) -> tuple[list[dict[str, str | None]], bool]:
    rows: list[dict[str, str | None]] = []
    truncated = False

    for element in root.iter():
        if len(rows) >= max_rows:
            truncated = True
            break
        if not isinstance(element.tag, str):
            continue

        path = element_path(element)
        direct_text = clean_text(element.text)
        if direct_text is not None:
            rows.append(
                {
                    "kind": "element",
                    "path": path,
                    "name": local_name(element),
                    "namespace": namespace_uri(element),
                    "value": direct_text,
                }
            )

        for raw_name, raw_value in element.attrib.items():
            if len(rows) >= max_rows:
                truncated = True
                break
            attr_ns, attr_name = qname_parts(raw_name)
            rows.append(
                {
                    "kind": "attribute",
                    "path": f"{path}/@{attr_name}",
                    "name": attr_name,
                    "namespace": attr_ns,
                    "value": raw_value,
                }
            )

    return rows, truncated


def unique_nonempty(values: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = clean_text(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
