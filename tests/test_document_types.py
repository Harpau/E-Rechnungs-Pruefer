from __future__ import annotations

from pathlib import Path

import pytest

from app.analyzer import analyze_bytes
from app.document_types import (
    CEN_UBL_CREDIT_NOTE_CODES,
    CEN_UBL_INVOICE_CODES,
    DOCUMENT_TYPE_REGISTRY,
    BasePolarity,
    DocumentFamily,
    DocumentTypeStatus,
    IssuanceMode,
    RootCompatibility,
    SettlementRelevance,
    UblRoot,
    resolve_document_type,
)
from app.profiles import resolve_profile

# Verified against BR-CL-01 in the official XRechnung 2026-08-31 bundle,
# resources/ubl/2.1/xsl/EN16931-UBL-validation.xsl (CEN 1.3.16).
EXPECTED_CEN_1316_CODES = frozenset(
    """
    71 80 81 82 83 84 102 130 202 203 204 211 218 219 261 262 295 296 308
    325 326 331 380 381 382 383 384 385 386 387 388 389 390 393 394 395 396
    420 456 457 458 471 472 473 500 501 502 503 527 532 553 575 623 633 751
    780 817 870 875 876 877 935
    """.split()
)

EXPECTED_CEN_1316_UBL_INVOICE_CODES = frozenset(
    """
    71 80 81 82 84 102 130 202 203 204 211 218 219 295 325 326 331 380 382
    383 384 385 386 387 388 389 390 393 394 395 456 457 471 472 473 500 501
    527 553 575 623 633 751 780 817 870 875 876 877 935
    """.split()
)

EXPECTED_CEN_1316_UBL_CREDIT_NOTE_CODES = frozenset("81 83 261 262 296 308 381 396 420 458 502 503 532".split())


def test_registry_matches_bundled_cen_1316_code_sets() -> None:
    assert frozenset(DOCUMENT_TYPE_REGISTRY) == EXPECTED_CEN_1316_CODES
    assert CEN_UBL_INVOICE_CODES == EXPECTED_CEN_1316_UBL_INVOICE_CODES
    assert CEN_UBL_CREDIT_NOTE_CODES == EXPECTED_CEN_1316_UBL_CREDIT_NOTE_CODES
    assert len(DOCUMENT_TYPE_REGISTRY) == 62


def test_every_registered_type_has_explicit_readable_semantics() -> None:
    for code, item in DOCUMENT_TYPE_REGISTRY.items():
        assert item.code == code
        assert item.label_de.strip()
        assert item.family is not DocumentFamily.UNKNOWN
        assert item.allowed_ubl_roots
        assert item.source_version == "CEN-EN16931-validation-1.3.16"


@pytest.mark.parametrize("code", ["502", "503"])
def test_cen_1316_moves_502_and_503_to_ubl_credit_note(code: str) -> None:
    item = DOCUMENT_TYPE_REGISTRY[code]

    assert item.allowed_ubl_roots == frozenset({UblRoot.CREDIT_NOTE})
    assert resolve_document_type(code, UblRoot.INVOICE).root_compatibility is RootCompatibility.INCOMPATIBLE
    assert resolve_document_type(code, UblRoot.CREDIT_NOTE).root_compatibility is RootCompatibility.COMPATIBLE
    assert item.family is DocumentFamily.CREDIT_NOTE
    assert item.base_polarity is BasePolarity.CREDIT


def test_code_81_is_accepted_for_both_bundled_ubl_roots() -> None:
    item = DOCUMENT_TYPE_REGISTRY["81"]

    assert item.allowed_ubl_roots == frozenset({UblRoot.INVOICE, UblRoot.CREDIT_NOTE})
    assert resolve_document_type("81", UblRoot.INVOICE).root_compatibility is RootCompatibility.COMPATIBLE
    assert resolve_document_type("81", UblRoot.CREDIT_NOTE).root_compatibility is RootCompatibility.COMPATIBLE


def test_known_self_billing_and_non_payment_types_have_no_generic_invoice_default() -> None:
    self_billing = resolve_document_type(" 389 ")
    pro_forma = resolve_document_type("325")

    assert self_billing.status is DocumentTypeStatus.KNOWN
    assert self_billing.info is not None
    assert self_billing.info.issuance_mode is IssuanceMode.SELF_BILLING
    assert self_billing.info.base_polarity is BasePolarity.DEBIT

    assert pro_forma.info is not None
    assert pro_forma.info.family is DocumentFamily.PRO_FORMA
    assert pro_forma.info.settlement_relevance is SettlementRelevance.NON_SETTLEMENT
    assert pro_forma.info.base_polarity is BasePolarity.UNDETERMINED


@pytest.mark.parametrize("code", [None, "", "   "])
def test_missing_type_stays_missing_without_invoice_default(code: str | None) -> None:
    result = resolve_document_type(code, UblRoot.INVOICE)

    assert result.code is None
    assert result.status is DocumentTypeStatus.MISSING
    assert result.info is None
    assert result.root_compatibility is RootCompatibility.UNDETERMINED
    assert result.to_dict()["family"] == DocumentFamily.UNKNOWN.value


def test_unknown_type_is_preserved_without_invoice_default() -> None:
    result = resolve_document_type(" 999 ", UblRoot.CREDIT_NOTE)

    assert result.code == "999"
    assert result.status is DocumentTypeStatus.UNKNOWN
    assert result.info is None
    assert result.root_compatibility is RootCompatibility.UNDETERMINED
    assert result.to_dict() == {
        "code": "999",
        "status": "unknown",
        "label": None,
        "family": "unknown",
        "base_polarity": "undetermined",
        "settlement_relevance": "undetermined",
        "issuance_mode": "undetermined",
        "allowed_ubl_roots": [],
        "ubl_root": "CreditNote",
        "root_compatibility": "undetermined",
        "registry_version": "CEN-EN16931-validation-1.3.16",
    }


def test_wrong_root_is_reported_without_changing_type_semantics() -> None:
    result = resolve_document_type("381", UblRoot.INVOICE)

    assert result.status is DocumentTypeStatus.KNOWN
    assert result.info is not None
    assert result.info.family is DocumentFamily.CREDIT_NOTE
    assert result.info.base_polarity is BasePolarity.CREDIT
    assert result.root_compatibility is RootCompatibility.INCOMPATIBLE


@pytest.mark.parametrize("code", ["502", "503"])
@pytest.mark.parametrize("root", [UblRoot.INVOICE, UblRoot.CREDIT_NOTE])
def test_cen_1316_root_allocation_reaches_findings_and_profile(
    code: str, root: UblRoot, ubl_path: Path, ubl_credit_note_path: Path
) -> None:
    # The new BR-CL-01 code sets are taken from the official bundled CEN XSL.
    path = ubl_path if root is UblRoot.INVOICE else ubl_credit_note_path
    code_tag = "InvoiceTypeCode" if root is UblRoot.INVOICE else "CreditNoteTypeCode"
    original_code = "380" if root is UblRoot.INVOICE else "381"
    xml = path.read_text(encoding="utf-8").replace(
        f"<cbc:{code_tag}>{original_code}</cbc:{code_tag}>", f"<cbc:{code_tag}>{code}</cbc:{code_tag}>"
    )
    result = analyze_bytes(xml.encode(), "synthetic-cen-1316.xml", "application/xml", run_official_validation=False)
    assert result["document"]["type"]["code"]["value"] == code
    findings = [f for f in result["assessment"]["internal"]["findings"] if f["rule"]["id"] == "BR-CL-01"]
    assert bool(findings) is (root is UblRoot.INVOICE)
    if findings:
        assert "1.3.16" in findings[0]["rule"]["message"]
    profile = resolve_profile(result["profile"]["id"])
    assert profile.capabilities.document_type_policy == "cen-en16931-1.3.16"
    assert profile.capabilities.official_rules_version == "CEN-EN16931-validation-1.3.16 / XRechnung-3.0.2"
