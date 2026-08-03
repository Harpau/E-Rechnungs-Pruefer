from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from .assessment import Assessment
from .findings import ContractModel


class Syntax(StrEnum):
    CII = "CII"
    UBL = "UBL"
    UNKNOWN = "UNKNOWN"


class DocumentFamily(StrEnum):
    INVOICE = "invoice"
    CREDIT_NOTE = "credit-note"
    CORRECTION = "correction"
    DEBIT_NOTE = "debit-note"
    PREPAYMENT_INVOICE = "prepayment-invoice"
    PAYMENT_REQUEST = "payment-request"
    PRO_FORMA = "pro-forma"
    INFORMATION = "information"
    CLAIM = "claim"
    OTHER = "other"
    UNKNOWN = "unknown"


class DocumentTypeStatus(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    MISSING = "missing"


class BasePolarity(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"
    NEUTRAL = "neutral"
    UNDETERMINED = "undetermined"


class SettlementRelevance(StrEnum):
    RELEVANT = "relevant"
    NOT_RELEVANT = "not-relevant"
    UNDETERMINED = "undetermined"


class UblRoot(StrEnum):
    INVOICE = "invoice"
    CREDIT_NOTE = "credit-note"


class RootCompatibility(StrEnum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    NOT_APPLICABLE = "not-applicable"
    UNDETERMINED = "undetermined"


class DocumentPartyRole(StrEnum):
    SELLER = "seller"
    BUYER = "buyer"
    PAYEE = "payee"
    INVOICE_RECIPIENT = "invoice-recipient"
    DELIVERY_RECIPIENT = "delivery-recipient"
    SELLER_TAX_REPRESENTATIVE = "seller-tax-representative"
    UNKNOWN = "unknown"


class PaymentDirection(StrEnum):
    DEBTOR_TO_CREDITOR = "debtor-to-creditor"
    CREDITOR_TO_DEBTOR = "creditor-to-debtor"
    NONE = "none"
    UNKNOWN = "unknown"


class TransactionDerivation(StrEnum):
    EXPLICIT = "explicit"
    DERIVED = "derived"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class PartyIdentifierKind(StrEnum):
    PARTY = "party"
    LEGAL_REGISTRATION = "legal-registration"
    VAT = "vat"
    TAX_REGISTRATION = "tax-registration"
    OTHER = "other"


class AllowanceChargeKind(StrEnum):
    ALLOWANCE = "allowance"
    CHARGE = "charge"
    UNKNOWN = "unknown"


class SourceContainerKind(StrEnum):
    XML = "xml"
    PDF = "pdf"
    UNKNOWN = "unknown"


class TechnicalFieldKind(StrEnum):
    NAMESPACE = "namespace"
    ELEMENT = "element"
    ATTRIBUTE = "attribute"


class DocumentTypeRecognition(StrEnum):
    RECOGNIZED = "recognized"
    UNKNOWN = "unknown"
    MISSING = "missing"


class RenderingCapability(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class InternalChecksCapability(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class OfficialValidationCapability(StrEnum):
    BUNDLED = "bundled"
    NOT_BUNDLED = "not-bundled"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class CodeValue(ContractModel):
    value: str = Field(min_length=1, max_length=200)
    label: str | None = Field(default=None, max_length=500)
    list_id: str | None = Field(default=None, max_length=200)


class Identifier(ContractModel):
    value: str = Field(min_length=1, max_length=1000)
    scheme_id: str | None = Field(default=None, max_length=200)


class Amount(ContractModel):
    value: Decimal
    currency: str | None = Field(default=None, max_length=20)


class Quantity(ContractModel):
    value: Decimal
    unit: CodeValue | None = None


class Period(ContractModel):
    start_date: date | None = None
    end_date: date | None = None
    description: str | None = Field(default=None, max_length=2000)


class DocumentNote(ContractModel):
    text: str = Field(min_length=1, max_length=20_000)
    subject_code: CodeValue | None = None


class DocumentType(ContractModel):
    status: DocumentTypeStatus = DocumentTypeStatus.MISSING
    code: CodeValue | None = None
    family: DocumentFamily = DocumentFamily.UNKNOWN
    base_polarity: BasePolarity = BasePolarity.UNDETERMINED
    settlement_relevance: SettlementRelevance = SettlementRelevance.UNDETERMINED
    self_billing: bool | None = None
    ubl_root: UblRoot | None = None
    root_compatibility: RootCompatibility = RootCompatibility.UNDETERMINED
    registry_version: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def code_matches_status(self) -> Self:
        if self.status is DocumentTypeStatus.MISSING and self.code is not None:
            raise ValueError("document type status 'missing' does not permit a code")
        if self.status is not DocumentTypeStatus.MISSING and self.code is None:
            raise ValueError("document type status 'known' or 'unknown' requires a code")
        return self


class DocumentModel(ContractModel):
    id: str | None = Field(default=None, max_length=1000)
    issue_date: date | None = None
    type: DocumentType = Field(default_factory=DocumentType)
    tax_point_date: date | None = None
    tax_point_date_code: CodeValue | None = None
    document_currency: CodeValue | None = None
    vat_accounting_currency: CodeValue | None = None
    buyer_reference: str | None = Field(default=None, max_length=1000)
    notes: list[DocumentNote] = Field(default_factory=list)


class ProfileModel(ContractModel):
    id: str | None = Field(default=None, max_length=2000)
    name: str | None = Field(default=None, max_length=500)
    business_process_id: str | None = Field(default=None, max_length=2000)


class CapabilitiesModel(ContractModel):
    syntax: Syntax = Syntax.UNKNOWN
    syntax_version: str | None = Field(default=None, max_length=200)
    format_name: str | None = Field(default=None, max_length=500)
    document_type_recognition: DocumentTypeRecognition = DocumentTypeRecognition.MISSING
    rendering: RenderingCapability = RenderingCapability.UNSUPPORTED
    internal_checks: InternalChecksCapability = InternalChecksCapability.UNSUPPORTED
    official_validation: OfficialValidationCapability = OfficialValidationCapability.UNKNOWN


class Contact(ContractModel):
    name: str | None = Field(default=None, max_length=1000)
    department: str | None = Field(default=None, max_length=1000)
    phone: str | None = Field(default=None, max_length=500)
    email: str | None = Field(default=None, max_length=1000)


class Address(ContractModel):
    line1: str | None = Field(default=None, max_length=2000)
    line2: str | None = Field(default=None, max_length=2000)
    line3: str | None = Field(default=None, max_length=2000)
    postcode: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=1000)
    subdivision: str | None = Field(default=None, max_length=1000)
    country: CodeValue | None = None


class DeliveryLocation(ContractModel):
    id: Identifier | None = None
    postal_address: Address | None = None


class DeliveryModel(ContractModel):
    actual_date: date | None = None
    location: DeliveryLocation | None = None


class PartyIdentifier(ContractModel):
    kind: PartyIdentifierKind = PartyIdentifierKind.OTHER
    identifier: Identifier


class Party(ContractModel):
    legal_name: str | None = Field(default=None, max_length=2000)
    trading_name: str | None = Field(default=None, max_length=2000)
    additional_legal_information: str | None = Field(default=None, max_length=4000)
    identifiers: list[PartyIdentifier] = Field(default_factory=list)
    tax_identifiers: list[PartyIdentifier] = Field(default_factory=list)
    electronic_address: Identifier | None = None
    postal_address: Address | None = None
    contact: Contact | None = None


class PartiesModel(ContractModel):
    seller: Party | None = None
    buyer: Party | None = None
    payee: Party | None = None
    invoice_recipient: Party | None = None
    seller_tax_representative: Party | None = None
    delivery_recipient: Party | None = None


class PartyRoleAssignments(ContractModel):
    issuer: DocumentPartyRole = DocumentPartyRole.UNKNOWN
    document_recipient: DocumentPartyRole = DocumentPartyRole.UNKNOWN
    creditor: DocumentPartyRole = DocumentPartyRole.UNKNOWN
    debtor: DocumentPartyRole = DocumentPartyRole.UNKNOWN
    expected_payer: DocumentPartyRole = DocumentPartyRole.UNKNOWN
    expected_recipient: DocumentPartyRole = DocumentPartyRole.UNKNOWN
    expected_payment_direction: PaymentDirection = PaymentDirection.UNKNOWN
    derivation: TransactionDerivation = TransactionDerivation.UNKNOWN


class PeriodsModel(ContractModel):
    invoice: Period | None = None
    delivery: Period | None = None


class Reference(ContractModel):
    id: Identifier | None = None
    issue_date: date | None = None
    description: str | None = Field(default=None, max_length=4000)


class SupportingDocument(ContractModel):
    id: Identifier | None = None
    type: CodeValue | None = None
    name: str | None = Field(default=None, max_length=1000)
    description: str | None = Field(default=None, max_length=4000)
    attachment_filename: str | None = Field(default=None, max_length=1000)
    attachment_mime_type: str | None = Field(default=None, max_length=500)
    embedded: bool = False
    external_uri: str | None = Field(default=None, max_length=4000)


class ReferencesModel(ContractModel):
    buyer_order: Reference | None = None
    seller_order: Reference | None = None
    contract: Reference | None = None
    tender: Reference | None = None
    project: Reference | None = None
    buyer_accounting_reference: str | None = Field(default=None, max_length=1000)
    invoiced_object: Reference | None = None
    preceding_invoices: list[Reference] = Field(default_factory=list)
    supporting_documents: list[SupportingDocument] = Field(default_factory=list)
    despatch_advice: Reference | None = None
    receiving_advice: Reference | None = None


class ItemClassification(ContractModel):
    code: str = Field(min_length=1, max_length=1000)
    name: str | None = Field(default=None, max_length=2000)
    scheme_id: str | None = Field(default=None, max_length=200)
    scheme_version: str | None = Field(default=None, max_length=200)


class ItemProperty(ContractModel):
    name: str = Field(min_length=1, max_length=1000)
    value: str | None = Field(default=None, max_length=4000)


class Item(ContractModel):
    name: str | None = Field(default=None, max_length=4000)
    description: str | None = Field(default=None, max_length=20_000)
    seller_identifier: Identifier | None = None
    buyer_identifier: Identifier | None = None
    standard_identifier: Identifier | None = None
    classifications: list[ItemClassification] = Field(default_factory=list)
    properties: list[ItemProperty] = Field(default_factory=list)
    origin_country: CodeValue | None = None


class PriceDiscount(ContractModel):
    amount: Amount | None = None
    percentage: Decimal | None = None


class Price(ContractModel):
    net: Amount | None = None
    base_quantity: Quantity | None = None
    gross: Amount | None = None
    discount: PriceDiscount | None = None


class AllowanceCharge(ContractModel):
    kind: AllowanceChargeKind = AllowanceChargeKind.UNKNOWN
    indicator_raw: str | None = Field(default=None, max_length=100)
    amount: Amount | None = None
    base_amount: Amount | None = None
    percentage: Decimal | None = None
    reason_text: str | None = Field(default=None, max_length=4000)
    reason_code: CodeValue | None = None
    tax_category: CodeValue | None = None
    tax_rate_percent: Decimal | None = None


class InvoiceLine(ContractModel):
    id: str | None = Field(default=None, max_length=1000)
    notes: list[str] = Field(default_factory=list)
    item: Item = Field(default_factory=Item)
    quantity: Quantity | None = None
    period: Period | None = None
    order_line_reference: str | None = Field(default=None, max_length=1000)
    accounting_reference: str | None = Field(default=None, max_length=1000)
    object_identifier: Identifier | None = None
    price: Price = Field(default_factory=Price)
    allowances_charges: list[AllowanceCharge] = Field(default_factory=list)
    tax_type: CodeValue | None = None
    tax_category: CodeValue | None = None
    tax_rate_percent: Decimal | None = None
    net_amount: Amount | None = None


class TaxExemption(ContractModel):
    reasons: list[str] = Field(default_factory=list)
    reason_code: CodeValue | None = None


class TaxBreakdown(ContractModel):
    tax_type: CodeValue | None = None
    category: CodeValue | None = None
    rate_percent: Decimal | None = None
    taxable_amount: Amount | None = None
    tax_amount: Amount | None = None
    exemption: TaxExemption | None = None


class TaxTotals(ContractModel):
    document_currency: Amount | None = None
    vat_accounting_currency: Amount | None = None


class TaxModel(ContractModel):
    breakdown: list[TaxBreakdown] = Field(default_factory=list)
    totals: TaxTotals = Field(default_factory=TaxTotals)


class TotalsModel(ContractModel):
    line_net_total: Amount | None = None
    allowance_total: Amount | None = None
    charge_total: Amount | None = None
    tax_exclusive_total: Amount | None = None
    tax_inclusive_total: Amount | None = None
    prepaid_total: Amount | None = None
    rounding: Amount | None = None
    payable: Amount | None = None


class CreditTransfer(ContractModel):
    account_id: Identifier | None = None
    account_name: str | None = Field(default=None, max_length=2000)
    service_provider_id: Identifier | None = None


class PaymentCard(ContractModel):
    masked_account_identifier: str | None = Field(default=None, max_length=1000)
    holder_name: str | None = Field(default=None, max_length=2000)


class DirectDebit(ContractModel):
    mandate_reference: str | None = Field(default=None, max_length=1000)
    creditor_id: Identifier | None = None
    debited_account_id: Identifier | None = None


class PaymentInstruction(ContractModel):
    means: CodeValue | None = None
    instruction_note: str | None = Field(default=None, max_length=4000)
    payment_id: str | None = Field(default=None, max_length=1000)
    credit_transfers: list[CreditTransfer] = Field(default_factory=list)
    payment_card: PaymentCard | None = None
    direct_debit: DirectDebit | None = None


class PaymentTerm(ContractModel):
    description: str | None = Field(default=None, max_length=4000)
    due_date: date | None = None
    partial_payment: Amount | None = None


class PaymentModel(ContractModel):
    due_date: date | None = None
    reference: str | None = Field(default=None, max_length=1000)
    terms: list[PaymentTerm] = Field(default_factory=list)
    instructions: list[PaymentInstruction] = Field(default_factory=list)


class SourceFile(ContractModel):
    filename: str = Field(min_length=1, max_length=1000)
    media_type: str = Field(min_length=1, max_length=500)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9A-Fa-f]{64}$")


class SourceContainer(ContractModel):
    kind: SourceContainerKind = SourceContainerKind.UNKNOWN
    page_count: int | None = Field(default=None, ge=0)
    selected_attachment: str | None = Field(default=None, max_length=1000)
    attachment_count: int = Field(default=0, ge=0)


class SourceAttachment(ContractModel):
    name: str = Field(min_length=1, max_length=1000)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9A-Fa-f]{64}$")
    is_xml: bool
    selected: bool = False


class SourceModel(ContractModel):
    upload: SourceFile | None = None
    invoice_xml: SourceFile | None = None
    container: SourceContainer = Field(default_factory=SourceContainer)
    attachments: list[SourceAttachment] = Field(default_factory=list)


class TechnicalField(ContractModel):
    kind: TechnicalFieldKind
    path: str = Field(min_length=1, max_length=4000)
    name: str | None = Field(default=None, max_length=1000)
    namespace: str | None = Field(default=None, max_length=4000)
    value: str | None = None


class TechnicalModel(ContractModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    root_element: str | None = Field(default=None, max_length=1000)
    root_namespace: str | None = Field(default=None, max_length=4000)
    field_count: int = Field(default=0, ge=0)
    truncated: bool = False
    fields: list[TechnicalField] = Field(default_factory=list)
    source_xml: str | None = None
    pretty_xml: str | None = None


class RuntimeModel(ContractModel):
    generated_at: datetime | None = None
    duration_ms: Decimal | None = Field(default=None, ge=0)
    application_version: str | None = Field(default=None, max_length=200)


class AnalysisResponse(ContractModel):
    schema_version: Literal[2] = 2
    document: DocumentModel = Field(default_factory=DocumentModel)
    profile: ProfileModel = Field(default_factory=ProfileModel)
    capabilities: CapabilitiesModel = Field(default_factory=CapabilitiesModel)
    parties: PartiesModel = Field(default_factory=PartiesModel)
    roles: PartyRoleAssignments = Field(default_factory=PartyRoleAssignments)
    periods: PeriodsModel = Field(default_factory=PeriodsModel)
    delivery: DeliveryModel = Field(default_factory=DeliveryModel)
    references: ReferencesModel = Field(default_factory=ReferencesModel)
    lines: list[InvoiceLine] = Field(default_factory=list)
    allowances_charges: list[AllowanceCharge] = Field(default_factory=list)
    tax: TaxModel = Field(default_factory=TaxModel)
    totals: TotalsModel = Field(default_factory=TotalsModel)
    payment: PaymentModel = Field(default_factory=PaymentModel)
    assessment: Assessment = Field(default_factory=Assessment)
    source: SourceModel = Field(default_factory=SourceModel)
    technical: TechnicalModel = Field(default_factory=TechnicalModel)
    runtime: RuntimeModel = Field(default_factory=RuntimeModel)


__all__ = [
    "Address",
    "AllowanceCharge",
    "AllowanceChargeKind",
    "Amount",
    "AnalysisResponse",
    "BasePolarity",
    "CapabilitiesModel",
    "CodeValue",
    "Contact",
    "CreditTransfer",
    "DirectDebit",
    "DeliveryLocation",
    "DeliveryModel",
    "DocumentFamily",
    "DocumentModel",
    "DocumentNote",
    "DocumentPartyRole",
    "DocumentType",
    "DocumentTypeRecognition",
    "DocumentTypeStatus",
    "Identifier",
    "InternalChecksCapability",
    "InvoiceLine",
    "Item",
    "ItemClassification",
    "ItemProperty",
    "OfficialValidationCapability",
    "PartiesModel",
    "Party",
    "PartyIdentifier",
    "PartyIdentifierKind",
    "PartyRoleAssignments",
    "PaymentCard",
    "PaymentDirection",
    "PaymentInstruction",
    "PaymentModel",
    "PaymentTerm",
    "Period",
    "PeriodsModel",
    "Price",
    "PriceDiscount",
    "ProfileModel",
    "Quantity",
    "Reference",
    "ReferencesModel",
    "RenderingCapability",
    "RootCompatibility",
    "RuntimeModel",
    "SettlementRelevance",
    "SourceAttachment",
    "SourceContainer",
    "SourceContainerKind",
    "SourceFile",
    "SourceModel",
    "SupportingDocument",
    "Syntax",
    "TaxBreakdown",
    "TaxExemption",
    "TaxModel",
    "TaxTotals",
    "TechnicalField",
    "TechnicalFieldKind",
    "TechnicalModel",
    "TotalsModel",
    "TransactionDerivation",
    "UblRoot",
]
