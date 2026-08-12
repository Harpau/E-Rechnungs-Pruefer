from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Literal

from lxml import etree


class InvoiceInputError(ValueError):
    """Raised when an uploaded document cannot safely be processed."""


FORBIDDEN_XML_MARKERS = (b"<!DOCTYPE", b"<!ENTITY")
XML_DECIMAL_PATTERN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
XSD_DATE_PATTERN = re.compile(r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})(?P<timezone>Z|[+-][0-9]{2}:[0-9]{2})?")
_TECHNICAL_TIME_CHECK_INTERVAL = 128


class _XmlStructureLimitExceeded(RuntimeError):
    pass


class _XmlStructureCounter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.count = 0

    def _add(self, amount: int = 1) -> None:
        self.count += amount
        if self.count > self.limit:
            raise _XmlStructureLimitExceeded

    def start(self, tag: str, attributes: Mapping[str, str]) -> None:
        del tag
        self._add(1 + len(attributes))

    def end(self, tag: str) -> None:
        del tag

    def data(self, data: str) -> None:
        del data

    def start_ns(self, prefix: str | None, uri: str) -> None:
        del prefix, uri
        self._add()

    def end_ns(self, prefix: str | None) -> None:
        del prefix

    def comment(self, text: str) -> None:
        del text
        self._add()

    def pi(self, target: str, data: str) -> None:
        del target, data
        self._add()

    def close(self) -> None:
        return None


def _preflight_xml_structure(xml_bytes: bytes, max_structure_items: int) -> None:
    if max_structure_items <= 0:
        raise ValueError("Das XML-Strukturlimit muss positiv sein.")
    parser = etree.XMLParser(
        target=_XmlStructureCounter(max_structure_items),
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        recover=False,
        huge_tree=False,
        remove_comments=False,
        remove_pis=False,
    )
    etree.fromstring(xml_bytes, parser=parser)


def safe_parse_xml(xml_bytes: bytes, *, max_structure_items: int | None = None) -> etree._Element:
    if not xml_bytes or not xml_bytes.strip():
        raise InvoiceInputError("Die Datei ist leer.")

    upper = xml_bytes.upper().replace(b"\x00", b"")
    if any(marker in upper for marker in FORBIDDEN_XML_MARKERS):
        raise InvoiceInputError(
            "XML-Dokumente mit DTD- oder ENTITY-Deklarationen werden aus Sicherheitsgründen nicht verarbeitet."
        )

    try:
        if max_structure_items is not None:
            _preflight_xml_structure(xml_bytes, max_structure_items)
        parser = etree.XMLParser(
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
            recover=False,
            huge_tree=False,
            remove_comments=False,
            remove_pis=False,
        )
        root = etree.fromstring(xml_bytes, parser=parser)
    except _XmlStructureLimitExceeded as exc:
        raise InvoiceInputError(
            f"Das XML überschreitet das zulässige Limit von {max_structure_items} XML-Struktureinträgen."
        ) from exc
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


def _xpath(
    node: etree._Element,
    expression: str,
    namespaces: Mapping[str, str] | None,
) -> Any:
    if namespaces is None:
        return node.xpath(expression)
    return node.xpath(expression, namespaces=dict(namespaces))


def nodes(
    node: etree._Element | None,
    expression: str,
    *,
    namespaces: Mapping[str, str] | None = None,
) -> list[etree._Element]:
    if node is None:
        return []
    result = _xpath(node, expression, namespaces)
    return [item for item in result if isinstance(item, etree._Element)]


def first_node(
    node: etree._Element | None,
    expression: str,
    *,
    namespaces: Mapping[str, str] | None = None,
) -> etree._Element | None:
    if node is None:
        return None
    result = _xpath(node, expression, namespaces)
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


def element_text(element: etree._Element | None) -> str | None:
    """Return direct text nodes without accepting text nested in child elements."""
    if element is None:
        return None
    direct_parts = [element.text or ""]
    direct_parts.extend(child.tail or "" for child in element)
    return clean_text("".join(direct_parts))


def first_text(
    node: etree._Element | None,
    expression: str,
    *,
    namespaces: Mapping[str, str] | None = None,
) -> str | None:
    if node is None:
        return None
    result = _xpath(node, expression, namespaces)
    for item in result:
        text = element_text(item) if isinstance(item, etree._Element) else clean_text(item)
        if text is not None:
            return text
    return None


def all_text(
    node: etree._Element | None,
    expression: str,
    *,
    namespaces: Mapping[str, str] | None = None,
) -> list[str]:
    if node is None:
        return []
    values: list[str] = []
    for item in _xpath(node, expression, namespaces):
        text = element_text(item) if isinstance(item, etree._Element) else clean_text(item)
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
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def xml_decimal_value(value: Any) -> Decimal | None:
    """Parse a finite value from the XML Schema ``decimal`` lexical space."""
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)

    text = clean_text(value)
    if text is None or XML_DECIMAL_PATTERN.fullmatch(text) is None:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


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
    try:
        rounded = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    return format(rounded, "f")


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


def parse_xsd_date_value(value: str | None) -> str | None:
    """Normalize an XML Schema ``date`` without accepting ``dateTime`` or compact dates."""
    text = clean_text(value)
    if text is None:
        return None

    match = XSD_DATE_PATTERN.fullmatch(text)
    if match is None:
        return text
    timezone = match.group("timezone")
    if timezone not in (None, "Z"):
        hours = int(timezone[1:3])
        minutes = int(timezone[4:6])
        if hours > 14 or minutes > 59 or (hours == 14 and minutes != 0):
            return text
    try:
        return date.fromisoformat(match.group("date")).isoformat()
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


TechnicalLimitReason = Literal["rows", "time"]
TechnicalRow = dict[str, str | None]


@dataclass(frozen=True, slots=True)
class TechnicalRowsResult:
    rows: list[TechnicalRow]
    truncated: bool
    limit_reason: TechnicalLimitReason | None

    def __iter__(self) -> Iterator[list[TechnicalRow] | bool]:
        # Preserve tuple unpacking for internal callers while exposing the limit reason.
        yield self.rows
        yield self.truncated


def technical_rows(
    root: etree._Element,
    max_rows: int = 100_000,
    *,
    include_namespaces: bool = False,
    max_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> TechnicalRowsResult:
    if max_seconds is not None and max_seconds <= 0:
        raise ValueError("Das Zeitbudget für die technische Darstellung muss positiv sein.")

    rows: list[TechnicalRow] = []
    limit_reason: TechnicalLimitReason | None = None
    deadline = clock() + max_seconds if max_seconds is not None else None
    visited = 0

    def time_exceeded() -> bool:
        nonlocal visited, limit_reason
        visited += 1
        if deadline is not None and visited % _TECHNICAL_TIME_CHECK_INTERVAL == 0 and clock() >= deadline:
            limit_reason = "time"
            return True
        return False

    def append_row(row: TechnicalRow) -> bool:
        nonlocal limit_reason
        if len(rows) >= max_rows:
            limit_reason = "rows"
            return False
        rows.append(row)
        return True

    root_path = f"/{local_name(root)}[1]"
    if include_namespaces:
        for prefix, uri in sorted(root.nsmap.items(), key=lambda item: item[0] or ""):
            if time_exceeded():
                break
            shown = "xmlns" if prefix is None else f"xmlns:{prefix}"
            if not append_row(
                {
                    "kind": "namespace",
                    "path": f"{root_path}/@{shown}",
                    "name": shown,
                    "namespace": None,
                    "value": uri,
                }
            ):
                break

    def add_element_rows(element: etree._Element, path: str) -> bool:
        if time_exceeded():
            return False
        direct_text = element_text(element)
        if direct_text is not None and not append_row(
            {
                "kind": "element",
                "path": path,
                "name": local_name(element),
                "namespace": namespace_uri(element),
                "value": direct_text,
            }
        ):
            return False
        for raw_name, raw_value in element.attrib.items():
            if time_exceeded():
                return False
            attr_ns, attr_name = qname_parts(raw_name)
            if not append_row(
                {
                    "kind": "attribute",
                    "path": f"{path}/@{attr_name}",
                    "name": attr_name,
                    "namespace": attr_ns,
                    "value": raw_value,
                }
            ):
                return False
        return True

    if limit_reason is None and add_element_rows(root, root_path):
        stack: list[tuple[etree._Element, str, Iterator[etree._Element], dict[str, int]]] = [
            (root, root_path, iter(root), {})
        ]
        while stack and limit_reason is None:
            parent, parent_path, child_iterator, sibling_counts = stack[-1]
            del parent
            try:
                child = next(child_iterator)
            except StopIteration:
                stack.pop()
                continue
            if not isinstance(child.tag, str):
                if time_exceeded():
                    break
                continue
            name = local_name(child)
            sibling_counts[name] = sibling_counts.get(name, 0) + 1
            child_path = f"{parent_path}/{name}[{sibling_counts[name]}]"
            if not add_element_rows(child, child_path):
                break
            stack.append((child, child_path, iter(child), {}))

    return TechnicalRowsResult(
        rows=rows,
        truncated=limit_reason is not None,
        limit_reason=limit_reason,
    )


def unique_nonempty(values: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = clean_text(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
