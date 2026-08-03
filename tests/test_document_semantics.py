from __future__ import annotations

from decimal import Decimal

import pytest

from app.document_semantics import (
    AmountSign,
    DerivationBasis,
    EconomicEffect,
    ExpectedFlow,
    PartyReference,
    RoleResolutionStatus,
    derive_document_semantics,
)
from app.document_types import resolve_document_type
from app.profiles import resolve_profile


@pytest.mark.parametrize(
    ("code", "amount", "expected_sign", "expected_effect", "expected_flow"),
    [
        ("380", "100.00", AmountSign.POSITIVE, EconomicEffect.DEBIT, ExpectedFlow.DEBTOR_TO_CREDITOR),
        ("380", "-100.00", AmountSign.NEGATIVE, EconomicEffect.CREDIT, ExpectedFlow.CREDITOR_TO_DEBTOR),
        ("381", "100.00", AmountSign.POSITIVE, EconomicEffect.CREDIT, ExpectedFlow.CREDITOR_TO_DEBTOR),
        ("381", "-100.00", AmountSign.NEGATIVE, EconomicEffect.DEBIT, ExpectedFlow.DEBTOR_TO_CREDITOR),
        ("389", "100.00", AmountSign.POSITIVE, EconomicEffect.DEBIT, ExpectedFlow.DEBTOR_TO_CREDITOR),
        ("261", "100.00", AmountSign.POSITIVE, EconomicEffect.CREDIT, ExpectedFlow.CREDITOR_TO_DEBTOR),
        ("380", "0.00", AmountSign.ZERO, EconomicEffect.NEUTRAL, ExpectedFlow.NONE),
    ],
)
def test_type_and_sign_determine_economic_effect_but_not_actual_payment(
    code: str,
    amount: str,
    expected_sign: AmountSign,
    expected_effect: EconomicEffect,
    expected_flow: ExpectedFlow,
) -> None:
    result = derive_document_semantics(
        resolve_document_type(code),
        resolve_profile("urn:cen.eu:en16931:2017"),
        amount,
    )

    assert result.settlement.amount_sign is expected_sign
    assert result.settlement.economic_effect is expected_effect
    assert result.settlement.expected_flow is expected_flow
    assert result.settlement.basis in {DerivationBasis.TYPE_AND_SIGN, DerivationBasis.ZERO_AMOUNT}
    assert "actual_payment" not in result.to_dict()["settlement"]


def test_positive_self_billed_invoice_resolves_document_and_commercial_roles() -> None:
    result = derive_document_semantics(
        resolve_document_type("389"),
        resolve_profile("urn:fdc:peppol.eu:2017:poacc:selfbilling:3.0"),
        Decimal("119.00"),
    )

    assert result.roles.status is RoleResolutionStatus.DETERMINISTIC
    assert result.roles.document_issuer is PartyReference.BUYER
    assert result.roles.document_receiver is PartyReference.SELLER
    assert result.roles.commercial_creditor is PartyReference.SELLER
    assert result.roles.commercial_debtor is PartyReference.BUYER
    assert result.settlement.expected_payer is PartyReference.BUYER
    assert result.settlement.expected_recipient is PartyReference.SELLER


def test_explicit_payee_is_expected_recipient_only_for_debtor_to_creditor_flow() -> None:
    debit = derive_document_semantics(
        resolve_document_type("393"),
        resolve_profile("urn:cen.eu:en16931:2017"),
        "100",
        has_payee=True,
    )
    credit = derive_document_semantics(
        resolve_document_type("396"),
        resolve_profile("urn:cen.eu:en16931:2017"),
        "100",
        has_payee=True,
    )

    assert debit.settlement.expected_flow is ExpectedFlow.DEBTOR_TO_CREDITOR
    assert debit.settlement.expected_recipient is PartyReference.PAYEE
    assert credit.settlement.expected_flow is ExpectedFlow.CREDITOR_TO_DEBTOR
    assert credit.settlement.expected_recipient is PartyReference.BUYER


def test_profile_and_type_issuance_conflict_is_not_guessed() -> None:
    result = derive_document_semantics(
        resolve_document_type("380"),
        resolve_profile("urn:fdc:peppol.eu:2017:poacc:selfbilling:3.0"),
        "100",
    )

    assert result.roles.status is RoleResolutionStatus.CONFLICT
    assert result.roles.document_issuer is None
    assert result.roles.document_receiver is None
    assert result.roles.commercial_creditor is None
    assert result.roles.commercial_debtor is None
    assert result.settlement.expected_flow is ExpectedFlow.DEBTOR_TO_CREDITOR
    assert result.settlement.expected_payer is None
    assert result.settlement.expected_recipient is None


@pytest.mark.parametrize(
    ("code", "amount", "expected_sign"),
    [
        (None, "100", AmountSign.POSITIVE),
        ("999", "100", AmountSign.POSITIVE),
        ("325", "100", AmountSign.POSITIVE),
        ("380", None, AmountSign.MISSING),
        ("380", "kein Betrag", AmountSign.INVALID),
    ],
)
def test_unknown_non_settlement_or_unusable_amount_remains_undetermined(
    code: str | None,
    amount: str | None,
    expected_sign: AmountSign,
) -> None:
    result = derive_document_semantics(
        resolve_document_type(code),
        resolve_profile(None),
        amount,
    )

    assert result.settlement.amount_sign is expected_sign
    assert result.settlement.economic_effect is EconomicEffect.UNDETERMINED
    assert result.settlement.expected_flow is ExpectedFlow.UNDETERMINED
    assert result.settlement.expected_payer is None
    assert result.settlement.expected_recipient is None
    assert result.settlement.basis is DerivationBasis.UNDETERMINED


def test_zero_amount_is_neutral_even_when_type_is_unknown() -> None:
    result = derive_document_semantics(resolve_document_type("999"), resolve_profile(None), "0")

    assert result.settlement.amount_sign is AmountSign.ZERO
    assert result.settlement.economic_effect is EconomicEffect.NEUTRAL
    assert result.settlement.expected_flow is ExpectedFlow.NONE
    assert result.settlement.basis is DerivationBasis.ZERO_AMOUNT
