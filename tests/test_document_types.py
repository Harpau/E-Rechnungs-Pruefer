from __future__ import annotations

import pytest

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

EXPECTED_CEN_1315_CODES = frozenset(
    """
    71 80 81 82 83 84 102 130 202 203 204 211 218 219 261 262 295 296 308
    325 326 331 380 381 382 383 384 385 386 387 388 389 390 393 394 395 396
    420 456 457 458 471 472 473 500 501 502 503 527 532 553 575 623 633 751
    780 817 870 875 876 877 935
    """.split()
)

EXPECTED_CEN_1315_UBL_INVOICE_CODES = frozenset(
    """
    71 80 81 82 84 102 130 202 203 204 211 218 219 295 325 326 331 380 382
    383 384 385 386 387 388 389 390 393 394 395 456 457 471 472 473 500 501
    502 503 527 553 575 623 633 751 780 817 870 875 876 877 935
    """.split()
)

EXPECTED_CEN_1315_UBL_CREDIT_NOTE_CODES = frozenset("81 83 261 262 296 308 381 396 420 458 532".split())


def test_registry_matches_bundled_cen_1315_code_sets() -> None:
    assert frozenset(DOCUMENT_TYPE_REGISTRY) == EXPECTED_CEN_1315_CODES
    assert CEN_UBL_INVOICE_CODES == EXPECTED_CEN_1315_UBL_INVOICE_CODES
    assert CEN_UBL_CREDIT_NOTE_CODES == EXPECTED_CEN_1315_UBL_CREDIT_NOTE_CODES
    assert len(DOCUMENT_TYPE_REGISTRY) == 62


def test_every_registered_type_has_explicit_readable_semantics() -> None:
    for code, item in DOCUMENT_TYPE_REGISTRY.items():
        assert item.code == code
        assert item.label_de.strip()
        assert item.family is not DocumentFamily.UNKNOWN
        assert item.allowed_ubl_roots
        assert item.source_version == "CEN-EN16931-validation-1.3.15"


@pytest.mark.parametrize("code", ["502", "503"])
def test_cen_1315_keeps_502_and_503_on_ubl_invoice(code: str) -> None:
    item = DOCUMENT_TYPE_REGISTRY[code]

    assert item.allowed_ubl_roots == frozenset({UblRoot.INVOICE})
    assert resolve_document_type(code, UblRoot.INVOICE).root_compatibility is RootCompatibility.COMPATIBLE
    assert resolve_document_type(code, UblRoot.CREDIT_NOTE).root_compatibility is RootCompatibility.INCOMPATIBLE


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
        "registry_version": "CEN-EN16931-validation-1.3.15",
    }


def test_wrong_root_is_reported_without_changing_type_semantics() -> None:
    result = resolve_document_type("381", UblRoot.INVOICE)

    assert result.status is DocumentTypeStatus.KNOWN
    assert result.info is not None
    assert result.info.family is DocumentFamily.CREDIT_NOTE
    assert result.info.base_polarity is BasePolarity.CREDIT
    assert result.root_compatibility is RootCompatibility.INCOMPATIBLE
