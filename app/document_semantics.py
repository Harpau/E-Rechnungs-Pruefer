"""Deterministic role and accounting-effect derivation.

The expected flow produced here is an accounting-oriented settlement direction.
It is never evidence that a cash payment happened, is required, or will happen;
offsetting, prepayment and contractual settlement can lead to a different real
world outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from .document_types import (
    BasePolarity,
    DocumentTypeResolution,
    IssuanceMode,
    SettlementRelevance,
)
from .profiles import ProfileResolution


class PartyReference(StrEnum):
    SELLER = "seller"
    BUYER = "buyer"
    PAYEE = "payee"


class RoleResolutionStatus(StrEnum):
    DETERMINISTIC = "deterministic"
    UNDETERMINED = "undetermined"
    CONFLICT = "conflict"


class AmountSign(StrEnum):
    POSITIVE = "positive"
    ZERO = "zero"
    NEGATIVE = "negative"
    MISSING = "missing"
    INVALID = "invalid"


class EconomicEffect(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"
    NEUTRAL = "neutral"
    UNDETERMINED = "undetermined"


class ExpectedFlow(StrEnum):
    DEBTOR_TO_CREDITOR = "debtor_to_creditor"
    CREDITOR_TO_DEBTOR = "creditor_to_debtor"
    NONE = "none"
    UNDETERMINED = "undetermined"


class DerivationBasis(StrEnum):
    TYPE_AND_PROFILE = "type_and_profile"
    TYPE = "type"
    PROFILE = "profile"
    TYPE_AND_SIGN = "type_and_sign"
    ZERO_AMOUNT = "zero_amount"
    UNDETERMINED = "undetermined"


@dataclass(frozen=True, slots=True)
class RoleSemantics:
    status: RoleResolutionStatus
    basis: DerivationBasis
    issuance_mode: IssuanceMode
    document_issuer: PartyReference | None
    document_receiver: PartyReference | None
    commercial_creditor: PartyReference | None
    commercial_debtor: PartyReference | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "status": self.status.value,
            "basis": self.basis.value,
            "issuance_mode": self.issuance_mode.value,
            "document_issuer": self.document_issuer.value if self.document_issuer else None,
            "document_receiver": self.document_receiver.value if self.document_receiver else None,
            "commercial_creditor": self.commercial_creditor.value if self.commercial_creditor else None,
            "commercial_debtor": self.commercial_debtor.value if self.commercial_debtor else None,
        }


@dataclass(frozen=True, slots=True)
class SettlementInterpretation:
    amount_sign: AmountSign
    economic_effect: EconomicEffect
    expected_flow: ExpectedFlow
    expected_payer: PartyReference | None
    expected_recipient: PartyReference | None
    basis: DerivationBasis

    def to_dict(self) -> dict[str, str | None]:
        return {
            "amount_sign": self.amount_sign.value,
            "economic_effect": self.economic_effect.value,
            "expected_flow": self.expected_flow.value,
            "expected_payer": self.expected_payer.value if self.expected_payer else None,
            "expected_recipient": self.expected_recipient.value if self.expected_recipient else None,
            "basis": self.basis.value,
        }


@dataclass(frozen=True, slots=True)
class DocumentSemantics:
    roles: RoleSemantics
    settlement: SettlementInterpretation

    def to_dict(self) -> dict[str, object]:
        return {
            "roles": self.roles.to_dict(),
            "settlement": self.settlement.to_dict(),
        }


def _resolved_mode(mode: IssuanceMode) -> bool:
    return mode in {IssuanceMode.SUPPLIER_ISSUED, IssuanceMode.SELF_BILLING}


def _derive_roles(
    document_type: DocumentTypeResolution,
    profile: ProfileResolution,
) -> RoleSemantics:
    type_mode = document_type.info.issuance_mode if document_type.info else IssuanceMode.UNDETERMINED
    profile_mode = profile.issuance_mode
    type_resolved = _resolved_mode(type_mode)
    profile_resolved = _resolved_mode(profile_mode)

    if type_resolved and profile_resolved and type_mode is not profile_mode:
        return RoleSemantics(
            status=RoleResolutionStatus.CONFLICT,
            basis=DerivationBasis.UNDETERMINED,
            issuance_mode=IssuanceMode.UNDETERMINED,
            document_issuer=None,
            document_receiver=None,
            commercial_creditor=None,
            commercial_debtor=None,
        )

    if type_resolved and profile_resolved:
        mode = type_mode
        basis = DerivationBasis.TYPE_AND_PROFILE
    elif type_resolved:
        mode = type_mode
        basis = DerivationBasis.TYPE
    elif profile_resolved:
        mode = profile_mode
        basis = DerivationBasis.PROFILE
    else:
        return RoleSemantics(
            status=RoleResolutionStatus.UNDETERMINED,
            basis=DerivationBasis.UNDETERMINED,
            issuance_mode=IssuanceMode.UNDETERMINED,
            document_issuer=None,
            document_receiver=None,
            commercial_creditor=None,
            commercial_debtor=None,
        )

    if mode is IssuanceMode.SELF_BILLING:
        issuer = PartyReference.BUYER
        receiver = PartyReference.SELLER
    else:
        issuer = PartyReference.SELLER
        receiver = PartyReference.BUYER

    settlement_is_deterministic = bool(
        document_type.info and document_type.info.settlement_relevance is SettlementRelevance.DETERMINISTIC
    )
    return RoleSemantics(
        status=RoleResolutionStatus.DETERMINISTIC,
        basis=basis,
        issuance_mode=mode,
        document_issuer=issuer,
        document_receiver=receiver,
        commercial_creditor=PartyReference.SELLER if settlement_is_deterministic else None,
        commercial_debtor=PartyReference.BUYER if settlement_is_deterministic else None,
    )


def _amount_sign(value: Decimal | int | float | str | None) -> AmountSign:
    if value is None or (isinstance(value, str) and not value.strip()):
        return AmountSign.MISSING
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return AmountSign.INVALID
    if not amount.is_finite():
        return AmountSign.INVALID
    if amount > 0:
        return AmountSign.POSITIVE
    if amount < 0:
        return AmountSign.NEGATIVE
    return AmountSign.ZERO


def _effect_and_flow(
    document_type: DocumentTypeResolution,
    amount_sign: AmountSign,
) -> tuple[EconomicEffect, ExpectedFlow, DerivationBasis]:
    if amount_sign is AmountSign.ZERO:
        return EconomicEffect.NEUTRAL, ExpectedFlow.NONE, DerivationBasis.ZERO_AMOUNT
    if amount_sign in {AmountSign.MISSING, AmountSign.INVALID}:
        return EconomicEffect.UNDETERMINED, ExpectedFlow.UNDETERMINED, DerivationBasis.UNDETERMINED

    info = document_type.info
    if (
        info is None
        or info.settlement_relevance is not SettlementRelevance.DETERMINISTIC
        or info.base_polarity is BasePolarity.UNDETERMINED
    ):
        return EconomicEffect.UNDETERMINED, ExpectedFlow.UNDETERMINED, DerivationBasis.UNDETERMINED

    positive = amount_sign is AmountSign.POSITIVE
    debit_effect = (info.base_polarity is BasePolarity.DEBIT and positive) or (
        info.base_polarity is BasePolarity.CREDIT and not positive
    )
    if debit_effect:
        return (
            EconomicEffect.DEBIT,
            ExpectedFlow.DEBTOR_TO_CREDITOR,
            DerivationBasis.TYPE_AND_SIGN,
        )
    return (
        EconomicEffect.CREDIT,
        ExpectedFlow.CREDITOR_TO_DEBTOR,
        DerivationBasis.TYPE_AND_SIGN,
    )


def derive_document_semantics(
    document_type: DocumentTypeResolution,
    profile: ProfileResolution,
    due_payable_amount: Decimal | int | float | str | None,
    *,
    has_payee: bool = False,
) -> DocumentSemantics:
    roles = _derive_roles(document_type, profile)
    amount_sign = _amount_sign(due_payable_amount)
    effect, flow, basis = _effect_and_flow(document_type, amount_sign)

    payer: PartyReference | None = None
    recipient: PartyReference | None = None
    if roles.status is RoleResolutionStatus.DETERMINISTIC:
        if flow is ExpectedFlow.DEBTOR_TO_CREDITOR:
            payer = roles.commercial_debtor
            recipient = PartyReference.PAYEE if has_payee else roles.commercial_creditor
        elif flow is ExpectedFlow.CREDITOR_TO_DEBTOR:
            payer = roles.commercial_creditor
            recipient = roles.commercial_debtor

    return DocumentSemantics(
        roles=roles,
        settlement=SettlementInterpretation(
            amount_sign=amount_sign,
            economic_effect=effect,
            expected_flow=flow,
            expected_payer=payer,
            expected_recipient=recipient,
            basis=basis,
        ),
    )
