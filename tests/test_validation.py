from __future__ import annotations

import pytest

from app.analyzer import analyze_bytes
from app.xml_utils import InvoiceInputError


def test_wrong_line_total_is_detected(cii_path):
    xml = (
        cii_path.read_text(encoding="utf-8")
        .replace(
            "<ram:LineTotalAmount>1098.80</ram:LineTotalAmount>",
            "<ram:LineTotalAmount>1198.80</ram:LineTotalAmount>",
            1,
        )
        .encode("utf-8")
    )
    result = analyze_bytes(xml, "wrong.xml", "application/xml", run_official_validation=False)
    ids = {item["id"] for item in result["validation"]["findings"]}
    assert "CALC-LINE-001" in ids
    assert "CALC-HDR-001" in ids
    assert result["validation"]["status"] == "invalid"


def test_invalid_iban_is_detected(cii_path):
    xml = (
        cii_path.read_text(encoding="utf-8")
        .replace(
            "DE89370400440532013000",
            "DE89370400440532013001",
        )
        .encode("utf-8")
    )
    result = analyze_bytes(xml, "bad-iban.xml", "application/xml", run_official_validation=False)
    assert any(item["id"] == "PAY-002" for item in result["validation"]["findings"])


def test_dtd_and_entities_are_rejected():
    payload = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "test">]><x>&a;</x>'
    with pytest.raises(InvoiceInputError, match="DTD"):
        analyze_bytes(payload, "unsafe.xml", "application/xml", run_official_validation=False)


def test_unknown_xml_is_shown_but_marked_unsupported():
    payload = b'<?xml version="1.0"?><root><value>123</value></root>'
    result = analyze_bytes(payload, "generic.xml", "application/xml", run_official_validation=False)
    assert result["document"]["syntax"] == "UNKNOWN"
    assert result["validation"]["status"] == "invalid"
    assert result["validation"]["findings"][0]["id"] == "SYNTAX-001"
    assert any(row["value"] == "123" for row in result["technical"]["rows"])


def test_utf16_xml_is_supported(ubl_path):
    text = ubl_path.read_text(encoding="utf-8")
    text = text.replace('encoding="UTF-8"', 'encoding="UTF-16"', 1)
    result = analyze_bytes(
        text.encode("utf-16"),
        "utf16.xml",
        "application/xml",
        run_official_validation=False,
    )
    assert result["document"]["id"] == "UBL-DEMO-1"


def test_utf16_dtd_is_rejected():
    payload = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "test">]><x>&a;</x>'.encode("utf-16")
    with pytest.raises(InvoiceInputError, match="DTD"):
        analyze_bytes(payload, "unsafe-utf16.xml", "application/xml", run_official_validation=False)
