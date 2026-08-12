from __future__ import annotations

from decimal import Decimal

import pytest
from lxml import etree

from app import xml_utils
from app.xml_utils import (
    InvoiceInputError,
    decimal_string,
    decimal_value,
    element_text,
    money_string,
    safe_parse_xml,
    technical_rows,
    xml_decimal_value,
)


def test_decimal_value_distinguishes_zero_from_missing_and_invalid_values():
    assert decimal_value("0") == Decimal("0")
    assert decimal_value(None) is None
    assert decimal_value("") is None
    assert decimal_value("kein Betrag") is None


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "+Infinity", "-Infinity"])
def test_decimal_value_rejects_non_finite_numbers(value):
    assert decimal_value(value) is None


def test_decimal_value_keeps_tolerant_input_normalization_for_display_helpers():
    assert decimal_value("1 234,50") == Decimal("1234.50")
    assert decimal_value("1e2") == Decimal("1E+2")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", Decimal("0")),
        ("-12.50", Decimal("-12.50")),
        ("+3", Decimal("3")),
        (".5", Decimal("0.5")),
        ("5.", Decimal("5")),
        (" 1.25 ", Decimal("1.25")),
    ],
)
def test_xml_decimal_value_accepts_xml_schema_decimal_lexical_space(value, expected):
    assert xml_decimal_value(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "NaN",
        "Infinity",
        "-Infinity",
        "1,5",
        "1e2",
        "1 2",
        "kein Betrag",
    ],
)
def test_xml_decimal_value_rejects_values_outside_xml_schema_decimal_lexical_space(value):
    assert xml_decimal_value(value) is None


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_decimal_rendering_rejects_non_finite_numbers(value):
    assert decimal_string(value) is None
    assert money_string(value) is None


def test_money_string_does_not_raise_for_an_unrepresentable_exponent():
    assert money_string("1e999999") is None


def test_safe_parse_xml_enforces_the_complete_structure_budget() -> None:
    payload = b'<root xmlns:a="urn:a" x="1"><!--c--><?p v?><a:item y="2"/></root>'

    root = safe_parse_xml(payload, max_structure_items=7)

    assert root.tag == "root"
    with pytest.raises(InvoiceInputError, match="XML-Struktur"):
        safe_parse_xml(payload, max_structure_items=6)


@pytest.mark.parametrize("limit", [0, -1])
def test_safe_parse_xml_rejects_non_positive_structure_limits(limit: int) -> None:
    with pytest.raises(ValueError, match="positiv"):
        safe_parse_xml(b"<root/>", max_structure_items=limit)


def test_element_text_keeps_all_direct_text_around_comments_and_processing_instructions() -> None:
    element = etree.fromstring(
        b"<value>vor<!-- Kommentar -->mitte<?synthetic test?>nach<foreign>ignoriert</foreign>ende</value>"
    )

    assert element_text(element) == "vormittenachende"


def test_technical_rows_include_namespaces_and_keep_existing_paths() -> None:
    root = etree.fromstring(
        b"""
        <root xmlns="urn:invoice" xmlns:meta="urn:metadata" meta:version="1">
          <item code="A">Erster Wert</item>
          <item>Zweiter Wert</item>
        </root>
        """
    )

    result = technical_rows(root, max_rows=10, include_namespaces=True)

    assert result.truncated is False
    assert result.limit_reason is None
    assert result.rows == [
        {
            "kind": "namespace",
            "path": "/root[1]/@xmlns",
            "name": "xmlns",
            "namespace": None,
            "value": "urn:invoice",
        },
        {
            "kind": "namespace",
            "path": "/root[1]/@xmlns:meta",
            "name": "xmlns:meta",
            "namespace": None,
            "value": "urn:metadata",
        },
        {
            "kind": "attribute",
            "path": "/root[1]/@version",
            "name": "version",
            "namespace": "urn:metadata",
            "value": "1",
        },
        {
            "kind": "element",
            "path": "/root[1]/item[1]",
            "name": "item",
            "namespace": "urn:invoice",
            "value": "Erster Wert",
        },
        {
            "kind": "attribute",
            "path": "/root[1]/item[1]/@code",
            "name": "code",
            "namespace": None,
            "value": "A",
        },
        {
            "kind": "element",
            "path": "/root[1]/item[2]",
            "name": "item",
            "namespace": "urn:invoice",
            "value": "Zweiter Wert",
        },
    ]


def test_technical_rows_apply_the_row_budget_to_namespaces() -> None:
    root = etree.fromstring(b'<root xmlns="urn:root" xmlns:a="urn:a"><item>value</item></root>')

    result = technical_rows(root, max_rows=1, include_namespaces=True)

    assert result.truncated is True
    assert result.limit_reason == "rows"
    assert [row["name"] for row in result.rows] == ["xmlns"]


def test_technical_rows_honor_a_monotonic_time_budget() -> None:
    root = etree.Element("root")
    for _ in range(300):
        etree.SubElement(root, "item")

    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        return 0.0 if calls == 1 else 2.0

    result = technical_rows(root, max_seconds=1.0, clock=clock)

    assert result.truncated is True
    assert result.limit_reason == "time"
    assert calls >= 2


def test_technical_rows_use_a_linear_number_of_local_name_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    root = etree.Element("root")
    for _ in range(500):
        etree.SubElement(root, "item")

    original = xml_utils.local_name
    calls = 0

    def counted(value: object) -> str:
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(xml_utils, "local_name", counted)

    result = technical_rows(root)

    assert result.truncated is False
    assert result.rows == []
    assert calls < 6 * 501
