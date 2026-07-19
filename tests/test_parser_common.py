from __future__ import annotations

from decimal import Decimal

import pytest
from lxml import etree

from app.parsers.common import document_kind, id_entry, make_amount, parse_period, profile_name
from app.xml_utils import (
    decimal_string,
    decimal_value,
    decode_xml_bytes,
    money_string,
    parse_date_value,
    technical_rows,
)


@pytest.mark.parametrize(
    ("profile_id", "expected"),
    [
        ("urn:example:XRECHNUNG:3.0", "XRechnung"),
        ("urn:fdc:peppol.eu:2017:billing:3.0", "Peppol BIS Billing 3.0"),
        ("urn:example:poacc:billing:3.0", "Peppol BIS Billing 3.0"),
        ("urn:factur-x.eu:1p0:basic", "Factur-X"),
        ("urn:ferd:zugferd:2p0:comfort", "ZUGFeRD"),
        ("urn:cen.eu:en16931:2017", "EN 16931"),
        ("urn:cen.eu:en:16931:2017", "EN 16931"),
        ("urn:example:custom-profile", "Unbekanntes/individuelles Profil"),
        (None, "Nicht angegeben"),
    ],
)
def test_profile_name_maps_known_and_unknown_identifiers(profile_id, expected):
    assert profile_name(profile_id) == expected


@pytest.mark.parametrize(
    ("type_code", "root_kind", "expected"),
    [
        ("380", None, "Rechnung"),
        ("381", None, "Gutschrift"),
        ("396", None, "Gutschrift"),
        ("532", None, "Gutschrift"),
        ("384", None, "Korrekturrechnung"),
        (None, "CreditNote", "Gutschrift"),
        (None, None, "Rechnung"),
    ],
)
def test_document_kind_uses_type_code_and_ubl_root(type_code, root_kind, expected):
    assert document_kind(type_code, root_kind) == expected


def test_id_entry_rejects_missing_values_and_preserves_scheme():
    assert id_entry(None) is None
    assert id_entry(etree.fromstring(b"<ID>  </ID>")) is None

    identifier = etree.fromstring(b'<ID schemeID="  GLN ">  1234567890123  </ID>')
    custom_identifier = etree.fromstring(b'<ID scheme="VAT"> DE123456789 </ID>')

    assert id_entry(identifier) == {"value": "1234567890123", "scheme": "GLN"}
    assert id_entry(custom_identifier, scheme_attribute="scheme") == {
        "value": "DE123456789",
        "scheme": "VAT",
    }


def test_make_amount_handles_empty_zero_and_currency_attributes():
    assert make_amount(None) is None
    assert make_amount(etree.fromstring(b"<Amount>  </Amount>")) is None

    amount = etree.fromstring(b'<Amount currencyID=" EUR " currency="USD"> 0.00 </Amount>')
    fallback_currency = etree.fromstring(b'<Amount currency="CHF"> 12.50 </Amount>')

    assert make_amount(amount) == {"value": "0.00", "currency": "EUR"}
    assert make_amount(fallback_currency) == {"value": "12.50", "currency": "CHF"}


def test_parse_period_reads_complete_period_with_formatted_dates():
    period = etree.fromstring(
        b"""
        <Period>
          <Start format="102">20240101</Start>
          <End>2024-01-31</End>
          <Description> Januar 2024 </Description>
        </Period>
        """
    )

    assert parse_period(period, "./Start", "./End", "./Description") == {
        "start": "2024-01-01",
        "end": "2024-01-31",
        "description": "Januar 2024",
    }


def test_parse_period_preserves_partial_periods_and_omits_empty_ones():
    end_only = etree.fromstring(b"<Period><End>2024-02</End></Period>")
    description_only = etree.fromstring(b"<Period><Description>Abrechnungsmonat</Description></Period>")
    empty = etree.fromstring(b"<Period><Start> </Start><End /></Period>")

    assert parse_period(end_only, "./Start", "./End") == {
        "start": None,
        "end": "2024-02",
        "description": None,
    }
    assert parse_period(description_only, "./Start", "./End", "./Description") == {
        "start": None,
        "end": None,
        "description": "Abrechnungsmonat",
    }
    assert parse_period(empty, "./Start", "./End") is None
    assert parse_period(None, "./Start", "./End") is None


@pytest.mark.parametrize(
    ("value", "format_code", "expected"),
    [
        (None, None, None),
        (" ", None, None),
        ("20240131", None, "2024-01-31"),
        ("240131", "101", "2024-01-31"),
        ("2024-01-31", None, "2024-01-31"),
        ("202401", None, "2024-01"),
        ("2024-01", None, "2024-01"),
        ("2024-01-31T23:59:00Z", None, "2024-01-31"),
        ("2024-02-30", None, "2024-02-30"),
        ("31.01.2024", None, "31.01.2024"),
    ],
)
def test_parse_date_value_normalizes_supported_formats_and_preserves_unknown_values(value, format_code, expected):
    assert parse_date_value(value, format_code) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (" ", None),
        ("1 234,50", Decimal("1234.50")),
        ("\u00a0-0,25", Decimal("-0.25")),
        ("1,234.50", None),
        ("kein Betrag", None),
    ],
)
def test_decimal_value_handles_localized_whitespace_and_invalid_values(value, expected):
    assert decimal_value(value) == expected


def test_decimal_rendering_normalizes_and_rounds_half_up():
    assert decimal_string("001.2300") == "1.23"
    assert decimal_string(Decimal("0.000")) == "0"
    assert decimal_string("ungültig") is None
    assert money_string("2.345") == "2.35"
    assert money_string("-2.345") == "-2.35"
    assert money_string(None) is None


def test_decode_xml_bytes_honors_declarations_boms_and_fallbacks():
    latin1 = b'<?xml version="1.0" encoding="iso-8859-1"?><root>\xe4</root>'
    utf16_text = '<?xml version="1.0" encoding="UTF-16"?><root>\u20ac</root>'
    unknown_encoding = '<?xml version="1.0" encoding="not-real"?><root>\u00e4</root>'.encode()

    assert decode_xml_bytes(latin1).endswith("<root>\u00e4</root>")
    assert decode_xml_bytes(utf16_text.encode("utf-16")) == utf16_text
    assert decode_xml_bytes(b"\xef\xbb\xbf<root>UTF-8</root>") == "<root>UTF-8</root>"
    assert decode_xml_bytes(unknown_encoding).endswith("<root>\u00e4</root>")


def test_technical_rows_keeps_sibling_paths_and_attribute_namespaces():
    root = etree.fromstring(
        b"""
        <root xmlns="urn:invoice" xmlns:meta="urn:metadata" meta:version="1">
          <item code="A">Erster Wert</item>
          <item>Zweiter Wert</item>
        </root>
        """
    )

    rows, truncated = technical_rows(root, max_rows=4)

    assert truncated is False
    assert rows == [
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


def test_technical_rows_stops_exactly_at_configured_limit():
    root = etree.fromstring(b'<root version="1"><item code="A">Wert</item></root>')

    rows, truncated = technical_rows(root, max_rows=2)

    assert truncated is True
    assert len(rows) == 2
    assert [row["path"] for row in rows] == ["/root[1]/@version", "/root[1]/item[1]"]
