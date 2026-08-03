from __future__ import annotations

import pytest

from app.analyzer import _detect_and_parse
from app.xml_utils import safe_parse_xml


@pytest.mark.parametrize(
    "payload",
    [
        b"<CrossIndustryInvoice xmlns='urn:example:not-cii'/>",
        (b"<Invoice xmlns='urn:example:not-ubl'><AccountingSupplierParty/></Invoice>"),
        b"<CreditNote xmlns='urn:oasis:names:specification:ubl:schema:xsd:Invoice-2'/>",
    ],
)
def test_invoice_like_roots_with_foreign_namespaces_are_not_accepted(payload: bytes) -> None:
    parsed, syntax_error = _detect_and_parse(safe_parse_xml(payload))

    assert parsed["document"]["syntax"] == "UNKNOWN"
    assert syntax_error is not None


@pytest.mark.parametrize(
    "payload",
    [
        (b"<rsm:CrossIndustryInvoice xmlns:rsm='urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100'/>"),
        b"<Invoice xmlns='urn:oasis:names:specification:ubl:schema:xsd:Invoice-2'/>",
        b"<CreditNote xmlns='urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2'/>",
    ],
)
def test_supported_roots_require_their_exact_namespace(payload: bytes) -> None:
    parsed, syntax_error = _detect_and_parse(safe_parse_xml(payload))

    assert parsed["document"]["syntax"] in {"CII", "UBL"}
    assert syntax_error is None
