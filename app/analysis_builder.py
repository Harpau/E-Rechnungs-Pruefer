from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from .api_models import (
    Address,
    AllowanceCharge,
    AllowanceChargeKind,
    Amount,
    AnalysisResponse,
    BasePolarity,
    CapabilitiesModel,
    CodeValue,
    Contact,
    CreditTransfer,
    DeliveryLocation,
    DeliveryModel,
    DirectDebit,
    DocumentFamily,
    DocumentModel,
    DocumentNote,
    DocumentPartyRole,
    DocumentType,
    DocumentTypeRecognition,
    DocumentTypeStatus,
    Identifier,
    InternalChecksCapability,
    InvoiceLine,
    Item,
    ItemClassification,
    ItemProperty,
    OfficialValidationCapability,
    PartiesModel,
    Party,
    PartyIdentifier,
    PartyIdentifierKind,
    PartyRoleAssignments,
    PaymentCard,
    PaymentDirection,
    PaymentInstruction,
    PaymentModel,
    PaymentTerm,
    Period,
    PeriodsModel,
    Price,
    PriceDiscount,
    ProfileModel,
    Quantity,
    Reference,
    ReferencesModel,
    RenderingCapability,
    RootCompatibility,
    RuntimeModel,
    SettlementRelevance,
    SourceAttachment,
    SourceContainer,
    SourceContainerKind,
    SourceFile,
    SourceModel,
    SupportingDocument,
    Syntax,
    TaxBreakdown,
    TaxExemption,
    TaxModel,
    TaxTotals,
    TechnicalField,
    TechnicalFieldKind,
    TechnicalModel,
    TotalsModel,
    TransactionDerivation,
    UblRoot,
)
from .assessment import (
    Assessment,
    InternalAssessment,
    InternalAssessmentStatus,
    OfficialAssessment,
    OfficialAssessmentStatus,
    OfficialReportSource,
    ProcessingAssessment,
    ProcessingAssessmentStatus,
    ProcessingLimitation,
)
from .document_semantics import derive_document_semantics
from .document_types import UblRoot as RegistryUblRoot
from .document_types import resolve_document_type
from .findings import (
    EvidenceDataType,
    Finding,
    FindingEvidence,
    FindingOccurrence,
    FindingOrigin,
    FindingRule,
    FindingRuleClass,
    FindingSeverity,
    OccurrenceScope,
    SemanticReference,
    XmlLocation,
)
from .profiles import OfficialValidationCapability as ProfileOfficialValidationCapability
from .profiles import resolve_profile
from .xml_utils import xml_decimal_value

SEMANTIC_LABELS = {
    "BG-4": "Verkäufer",
    "BG-7": "Käufer",
    "BG-10": "Zahlungsempfänger",
    "BG-11": "Steuervertreter des Verkäufers",
    "BG-14": "Rechnungszeitraum",
    "BG-16": "Zahlungsanweisungen",
    "BG-20": "Nachlass auf Dokumentenebene",
    "BG-21": "Zuschlag auf Dokumentenebene",
    "BG-23": "Umsatzsteueraufschlüsselung",
    "BG-25": "Rechnungsposition",
    "BT-1": "Rechnungsnummer",
    "BT-2": "Rechnungsdatum",
    "BT-3": "Rechnungsartcode",
    "BT-5": "Rechnungswährung",
    "BT-6": "Umsatzsteuer-Abrechnungswährung",
    "BT-9": "Fälligkeitsdatum",
    "BT-20": "Zahlungsbedingungen",
    "BT-24": "Spezifikationskennung",
    "BT-27": "Name des Verkäufers",
    "BT-44": "Name des Käufers",
    "BT-72": "Tatsächliches Lieferdatum",
    "BT-81": "Zahlungsartcode",
    "BT-88": "Name des Karteninhabers",
    "BT-91": "Kennung des belasteten Kontos",
    "BT-106": "Summe der Rechnungspositionen",
    "BT-107": "Summe der Nachlässe auf Dokumentenebene",
    "BT-108": "Summe der Zuschläge auf Dokumentenebene",
    "BT-109": "Rechnungsbetrag ohne Umsatzsteuer",
    "BT-110": "Umsatzsteuerbetrag",
    "BT-111": "Umsatzsteuerbetrag in Abrechnungswährung",
    "BT-112": "Rechnungsbetrag mit Umsatzsteuer",
    "BT-115": "Ausstehender Betrag",
    "BT-126": "Kennung der Rechnungsposition",
    "BT-129": "Fakturierte Menge",
    "BT-131": "Nettobetrag der Rechnungsposition",
    "BT-146": "Nettopreis des Artikels",
    "BT-149": "Preisbasismenge",
    "BT-151": "Umsatzsteuerkategorie der Position",
    "BT-153": "Artikelname",
}
SEMANTIC_REFERENCE_PATTERN = re.compile(r"\b(?:BT|BG)-\d+\b")


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _date_value(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _amount(value: Any, currency: Any = None) -> Amount | None:
    number = xml_decimal_value(value)
    if number is None:
        return None
    shown_currency = str(currency).strip() if currency not in (None, "") else None
    return Amount(value=number, currency=shown_currency)


def _code(value: Any, label: Any = None, list_id: str | None = None) -> CodeValue | None:
    if value in (None, ""):
        return None
    return CodeValue(
        value=str(value),
        label=str(label) if label not in (None, "") else None,
        list_id=list_id,
    )


def _identifier(value: Any, scheme: Any = None) -> Identifier | None:
    if isinstance(value, dict):
        scheme = value.get("scheme") or value.get("scheme_id") or scheme
        value = value.get("value") or value.get("id")
    if value in (None, ""):
        return None
    return Identifier(value=str(value), scheme_id=str(scheme) if scheme not in (None, "") else None)


def _party_has_data(source: Any) -> bool:
    if isinstance(source, dict):
        return any(_party_has_data(value) for value in source.values())
    if isinstance(source, (list, tuple)):
        return any(_party_has_data(value) for value in source)
    return source not in (None, "")


def _address(source: Any) -> Address | None:
    address_source = _mapping(source)
    if not any(value not in (None, "", [], {}) for value in address_source.values()):
        return None
    return Address(
        line1=address_source.get("line1"),
        line2=address_source.get("line2"),
        line3=address_source.get("line3"),
        postcode=address_source.get("postcode"),
        city=address_source.get("city"),
        subdivision=address_source.get("subdivision"),
        country=_code(
            address_source.get("country_code"),
            address_source.get("country"),
            "ISO3166-1",
        ),
    )


def _party(source: Any) -> Party | None:
    if not _party_has_data(source):
        return None
    assert isinstance(source, dict)
    identifiers = [
        PartyIdentifier(
            kind=PartyIdentifierKind.PARTY,
            identifier=Identifier(value=str(item["value"]), scheme_id=item.get("scheme")),
        )
        for item in source.get("ids", [])
        if isinstance(item, dict) and item.get("value")
    ]
    identifiers.extend(
        PartyIdentifier(
            kind=PartyIdentifierKind.LEGAL_REGISTRATION,
            identifier=Identifier(value=str(item["value"]), scheme_id=item.get("scheme")),
        )
        for item in source.get("legal_registration_ids", [])
        if isinstance(item, dict) and item.get("value")
    )
    tax_identifiers: list[PartyIdentifier] = []
    for item in source.get("tax_ids", []):
        if not isinstance(item, dict) or not item.get("value"):
            continue
        scheme = str(item.get("scheme") or "")
        kind = PartyIdentifierKind.VAT if scheme.upper() in {"VA", "VAT"} else PartyIdentifierKind.TAX_REGISTRATION
        tax_identifiers.append(
            PartyIdentifier(
                kind=kind,
                identifier=Identifier(value=str(item["value"]), scheme_id=scheme or None),
            )
        )

    contact_source = _mapping(source.get("contact"))
    contact = None
    if any(value not in (None, "", [], {}) for value in contact_source.values()):
        contact = Contact(
            name=contact_source.get("name"),
            department=contact_source.get("department"),
            phone=contact_source.get("phone"),
            email=contact_source.get("email"),
        )

    endpoint = _mapping(source.get("endpoint")) or None
    return Party(
        legal_name=source.get("name"),
        trading_name=source.get("trading_name"),
        additional_legal_information=source.get("description"),
        identifiers=identifiers,
        tax_identifiers=tax_identifiers,
        electronic_address=(
            _identifier(endpoint.get("value"), endpoint.get("scheme"))
            if endpoint
            else _identifier(source.get("endpoint_value"), source.get("endpoint_scheme"))
        ),
        postal_address=_address(source.get("address")),
        contact=contact,
    )


def _allowance_charge(source: dict[str, Any], default_currency: str | None) -> AllowanceCharge:
    kind = {
        "allowance": AllowanceChargeKind.ALLOWANCE,
        "charge": AllowanceChargeKind.CHARGE,
    }.get(str(source.get("type") or ""), AllowanceChargeKind.UNKNOWN)
    return AllowanceCharge(
        kind=kind,
        indicator_raw=source.get("indicator_raw"),
        amount=_amount(source.get("amount"), source.get("currency") or default_currency),
        base_amount=_amount(source.get("basis_amount"), source.get("basis_currency") or default_currency),
        percentage=xml_decimal_value(source.get("percent")),
        reason_text=source.get("reason"),
        reason_code=_code(source.get("reason_code")),
        tax_category=_code(
            source.get("tax_category"),
            source.get("tax_category_display") or source.get("tax_category_label"),
        ),
        tax_rate_percent=xml_decimal_value(source.get("tax_rate")),
    )


def _exemption_reasons(source: dict[str, Any]) -> list[str]:
    raw_reasons = source.get("exemption_reasons")
    values = raw_reasons if isinstance(raw_reasons, list) else [source.get("exemption_reason")]
    return [str(value) for value in values if value not in (None, "")]


def _period(source: Any) -> Period | None:
    if not isinstance(source, dict):
        return None
    start = _date_value(source.get("start") or source.get("start_date"))
    end = _date_value(source.get("end") or source.get("end_date"))
    description = source.get("description")
    if start is None and end is None and not description:
        return None
    return Period(start_date=start, end_date=end, description=description)


def _document_notes(source: Any) -> list[DocumentNote]:
    notes: list[DocumentNote] = []
    for item in source if isinstance(source, list) else []:
        if isinstance(item, dict):
            text = item.get("text")
            subject_code = item.get("subject_code")
        else:
            text = item
            subject_code = None
        if text in (None, ""):
            continue
        notes.append(
            DocumentNote(
                text=str(text),
                subject_code=_code(
                    subject_code,
                    list_id="UNCL4451",
                ),
            )
        )
    return notes


def _line(source: dict[str, Any], default_currency: str | None) -> InvoiceLine:
    classifications = [
        ItemClassification(
            code=str(item["code"]),
            name=item.get("name"),
            scheme_id=item.get("scheme"),
            scheme_version=item.get("version"),
        )
        for item in source.get("classifications", [])
        if isinstance(item, dict) and item.get("code")
    ]
    properties = [
        ItemProperty(name=str(item["name"]), value=item.get("value"))
        for item in source.get("additional_properties", [])
        if isinstance(item, dict) and item.get("name")
    ]
    base_quantity = xml_decimal_value(source.get("base_quantity"))
    base_unit = _code(
        source.get("base_unit_code"),
        source.get("base_unit_label"),
        "UNECERec20",
    )
    gross_price = source.get("gross_price")
    price_discount_amount = source.get("price_discount_amount") or source.get("price_allowance")
    price_discount_percent = source.get("price_discount_percent") or source.get("price_allowance_percent")
    return InvoiceLine(
        id=source.get("id"),
        notes=[str(item) for item in source.get("notes", []) if item not in (None, "")],
        item=Item(
            name=source.get("name"),
            description=source.get("description"),
            seller_identifier=_identifier(source.get("seller_item_id")),
            buyer_identifier=_identifier(source.get("buyer_item_id")),
            standard_identifier=_identifier(
                source.get("standard_item_id"),
                source.get("standard_item_scheme"),
            ),
            classifications=classifications,
            properties=properties,
            origin_country=_code(
                source.get("origin_country"),
                source.get("origin_country_label"),
                "ISO3166-1",
            ),
        ),
        quantity=(
            Quantity(
                value=quantity,
                unit=_code(source.get("unit_code"), source.get("unit_label"), "UNECERec20"),
            )
            if (quantity := xml_decimal_value(source.get("quantity"))) is not None
            else None
        ),
        period=_period(source.get("period")),
        order_line_reference=source.get("order_line_reference"),
        accounting_reference=source.get("accounting_cost"),
        object_identifier=_identifier(
            source.get("object_identifier"),
            source.get("object_identifier_scheme"),
        ),
        price=Price(
            net=_amount(source.get("price"), source.get("price_currency") or default_currency),
            base_quantity=(Quantity(value=base_quantity, unit=base_unit) if base_quantity is not None else None),
            gross=_amount(
                gross_price,
                source.get("gross_price_currency") or source.get("price_currency") or default_currency,
            ),
            discount=(
                PriceDiscount(
                    amount=_amount(
                        price_discount_amount,
                        source.get("price_discount_currency")
                        or source.get("price_allowance_currency")
                        or default_currency,
                    ),
                    percentage=xml_decimal_value(price_discount_percent),
                )
                if price_discount_amount not in (None, "") or price_discount_percent not in (None, "")
                else None
            ),
        ),
        allowances_charges=[
            _allowance_charge(item, default_currency)
            for item in source.get("allowances_charges", [])
            if isinstance(item, dict)
        ],
        tax_type=_code(source.get("tax_type")),
        tax_category=_code(
            source.get("tax_category"),
            source.get("tax_category_display") or source.get("tax_category_label"),
        ),
        tax_rate_percent=xml_decimal_value(source.get("tax_rate")),
        net_amount=_amount(source.get("line_total"), source.get("line_currency") or default_currency),
    )


def _reference(value: Any, *, issue_date: Any = None, scheme: Any = None) -> Reference | None:
    if isinstance(value, dict):
        return Reference(
            id=_identifier(value.get("id") or value.get("value"), value.get("scheme")),
            issue_date=_date_value(value.get("issue_date") or value.get("date")),
            description=value.get("description"),
        )
    identifier = _identifier(value, scheme)
    if identifier is None and issue_date is None:
        return None
    return Reference(id=identifier, issue_date=_date_value(issue_date))


def _references(source: dict[str, Any], delivery: dict[str, Any]) -> ReferencesModel:
    preceding = [
        reference
        for item in source.get(
            "preceding_invoice_documents",
            source.get("preceding_invoices", []),
        )
        if (reference := _reference(item)) is not None
    ]
    supporting = [
        SupportingDocument(
            id=_identifier(item.get("id"), item.get("id_scheme")),
            type=_code(item.get("type_code"), item.get("type_label")),
            name=item.get("name"),
            description=item.get("description"),
            attachment_filename=item.get("attachment_filename"),
            attachment_mime_type=item.get("attachment_mime") or item.get("attachment_mime_type"),
            embedded=bool(item.get("embedded") or item.get("attachment_filename")),
            external_uri=item.get("external_uri"),
        )
        for item in source.get("additional_documents", source.get("supporting_documents", []))
        if isinstance(item, dict)
    ]
    return ReferencesModel(
        buyer_order=_reference(source.get("buyer_order")),
        seller_order=_reference(source.get("seller_order")),
        contract=_reference(source.get("contract")),
        tender=_reference(source.get("tender")),
        project=_reference(source.get("project")),
        buyer_accounting_reference=source.get("buyer_accounting_reference"),
        invoiced_object=_reference(
            source.get("invoiced_object"),
            scheme=source.get("invoiced_object_scheme"),
        ),
        preceding_invoices=preceding,
        supporting_documents=supporting,
        despatch_advice=_reference(source.get("despatch_advice") or delivery.get("despatch_advice_reference")),
        receiving_advice=_reference(source.get("receiving_advice") or delivery.get("receiving_advice_reference")),
    )


def _masked_account(value: Any) -> str | None:
    if value in (None, ""):
        return None
    compact = re.sub(r"\s+", "", str(value))
    return "••••" if len(compact) <= 4 else f"•••• {compact[-4:]}"


def _payment(
    source: dict[str, Any],
    *,
    due_date: Any,
    default_currency: str | None,
) -> PaymentModel:
    instructions: list[PaymentInstruction] = []
    for item in source.get("means", source.get("instructions", [])):
        if not isinstance(item, dict):
            continue
        account_value = item.get("account_id") or item.get("iban")
        account_scheme = item.get("account_scheme") or ("IBAN" if item.get("iban") else None)
        service_value = item.get("service_provider_id") or item.get("bic")
        service_scheme = item.get("service_provider_scheme") or ("BIC" if item.get("bic") else None)
        payer_account = item.get("debited_account_id") or item.get("payer_iban")
        payer_scheme = item.get("debited_account_scheme") or ("IBAN" if item.get("payer_iban") else None)
        transfers = []
        if account_value or item.get("account_name") or service_value:
            transfers.append(
                CreditTransfer(
                    account_id=_identifier(account_value, account_scheme),
                    account_name=item.get("account_name"),
                    service_provider_id=_identifier(service_value, service_scheme),
                )
            )
        card_value = item.get("card_account")
        card_holder = item.get("card_holder") or item.get("card_holder_name")
        instructions.append(
            PaymentInstruction(
                means=_code(item.get("type_code"), item.get("type_label")),
                instruction_note=item.get("information"),
                payment_id=item.get("payment_id"),
                credit_transfers=transfers,
                payment_card=(
                    PaymentCard(
                        masked_account_identifier=_masked_account(card_value),
                        holder_name=card_holder,
                    )
                    if card_value or card_holder
                    else None
                ),
                direct_debit=(
                    DirectDebit(
                        mandate_reference=item.get("mandate_reference"),
                        creditor_id=_identifier(
                            item.get("creditor_id"),
                            item.get("creditor_id_scheme"),
                        ),
                        debited_account_id=_identifier(payer_account, payer_scheme),
                    )
                    if item.get("mandate_reference") or item.get("creditor_id") or payer_account
                    else None
                ),
            )
        )

    terms = [
        PaymentTerm(
            description=item.get("description"),
            due_date=_date_value(item.get("due_date")),
            partial_payment=_amount(
                item.get("partial_payment_amount"),
                item.get("partial_payment_currency") or default_currency,
            ),
        )
        for item in source.get("terms", [])
        if isinstance(item, dict)
    ]
    term_mandate_references = [
        str(item["direct_debit_mandate_id"])
        for item in source.get("terms", [])
        if isinstance(item, dict) and item.get("direct_debit_mandate_id")
    ]
    if term_mandate_references:
        mandate_reference = term_mandate_references[0]
        if instructions:
            if instructions[0].direct_debit is None:
                instructions[0].direct_debit = DirectDebit(mandate_reference=mandate_reference)
            elif instructions[0].direct_debit.mandate_reference is None:
                instructions[0].direct_debit.mandate_reference = mandate_reference
        else:
            instructions.append(
                PaymentInstruction(
                    direct_debit=DirectDebit(mandate_reference=mandate_reference),
                )
            )
    return PaymentModel(
        due_date=_date_value(due_date),
        reference=source.get("reference"),
        terms=terms,
        instructions=instructions,
    )


def _semantic_references(location: Any) -> list[SemanticReference]:
    if not isinstance(location, str):
        return []
    return [
        SemanticReference(id=item, label=SEMANTIC_LABELS.get(item))
        for item in dict.fromkeys(SEMANTIC_REFERENCE_PATTERN.findall(location))
    ]


def _finding_semantic_references(source: dict[str, Any], location: Any) -> list[SemanticReference]:
    explicit = source.get("semantic_references")
    if explicit is None:
        explicit = source.get("semantic_reference")
    if explicit is None:
        return _semantic_references(location)
    values = explicit if isinstance(explicit, list) else [explicit]
    references: list[SemanticReference] = []
    for value in values:
        if isinstance(value, dict):
            identifier = value.get("id") or value.get("value")
            label = value.get("label")
        else:
            identifier = value
            label = None
        if not identifier:
            continue
        identifier_text = str(identifier)
        references.append(
            SemanticReference(
                id=identifier_text,
                label=str(label) if label else SEMANTIC_LABELS.get(identifier_text),
            )
        )
    return references


def _occurrence(location: Any, finding_id: str) -> FindingOccurrence | None:
    if not isinstance(location, str) or not location:
        return None
    scope = OccurrenceScope.DOCUMENT
    pointer: str | None = None
    lowered = location.casefold()
    index_match = re.search(
        r"(?:Position|Rechnungsposition|Steuergruppe|Zahlungsweg|Zahlungsanweisung)\s+(\d+)",
        location,
        flags=re.IGNORECASE,
    )
    index = int(index_match.group(1)) - 1 if index_match else None
    if "position" in lowered or finding_id.startswith("LINE") or finding_id.startswith("CALC-LINE"):
        scope = OccurrenceScope.LINE
        pointer = f"/lines/{index}" if index is not None else "/lines"
    elif "steuergruppe" in lowered or finding_id.startswith("TAX"):
        scope = OccurrenceScope.TAX
        pointer = f"/tax/breakdown/{index}" if index is not None else "/tax"
    elif "zahlungsweg" in lowered or "zahlungsanweisung" in lowered or finding_id.startswith("PAY"):
        scope = OccurrenceScope.PAYMENT
        pointer = f"/payment/instructions/{index}" if index is not None else "/payment"
    elif finding_id.startswith("TECH"):
        scope = OccurrenceScope.TECHNICAL
        pointer = "/technical"
    return FindingOccurrence(scope=scope, index=index, json_pointer=pointer)


def _rule_class(finding_id: str, origin: FindingOrigin) -> FindingRuleClass:
    if origin is FindingOrigin.OFFICIAL:
        return FindingRuleClass.OFFICIAL
    if origin is FindingOrigin.PROCESSING:
        return FindingRuleClass.PROCESSING
    if finding_id.startswith(("DATE-", "TAX-SEM-", "CHECK-", "PAY-004", "LINE-002")):
        return FindingRuleClass.PLAUSIBILITY
    if finding_id.startswith(("PROFILE-", "XR-", "XRECHNUNG-")):
        return FindingRuleClass.PROFILE_PRECHECK
    return FindingRuleClass.CORE_PRECHECK


def _finding(
    source: dict[str, Any],
    *,
    origin: FindingOrigin,
) -> Finding:
    finding_id = str(source.get("id") or "UNKNOWN")
    severity_value = str(source.get("severity") or "info")
    severity = (
        FindingSeverity(severity_value)
        if severity_value in {item.value for item in FindingSeverity}
        else FindingSeverity.INFO
    )
    location = source.get("location")
    source_name = source.get("source")
    explicit_rule_class = source.get("rule_class")
    rule_class = (
        FindingRuleClass(str(explicit_rule_class))
        if explicit_rule_class in {item.value for item in FindingRuleClass}
        else _rule_class(finding_id, origin)
    )
    xml_path = source.get("xml_path")
    if xml_path is None and isinstance(location, str) and location.startswith("/"):
        xml_path = location
    xml_line = source.get("line")
    xml_column = source.get("column")
    return Finding(
        origin=origin,
        rule_class=rule_class,
        severity=severity,
        rule=FindingRule(
            id=finding_id,
            title=str(source.get("title") or finding_id)[:500],
            message=str(source.get("message") or "")[:4000] or "Keine Detailmeldung verfügbar.",
            source=str(source_name)[:500] if source_name else None,
            reference=str(source.get("reference") or finding_id),
            profile=source.get("profile"),
            version=source.get("version"),
        ),
        semantic_references=_finding_semantic_references(source, location),
        occurrence=_occurrence(location, finding_id),
        xml_location=(
            XmlLocation(
                path=str(xml_path)[:4000] if xml_path else None,
                line=xml_line,
                column=xml_column,
            )
            if xml_path or xml_line is not None or xml_column is not None
            else None
        ),
        actual=(
            FindingEvidence(value=str(source["actual"])[:4000], data_type=EvidenceDataType.TEXT)
            if source.get("actual") is not None
            else None
        ),
        expected=(
            FindingEvidence(value=str(source["expected"])[:4000], data_type=EvidenceDataType.TEXT)
            if source.get("expected") is not None
            else None
        ),
    )


def _is_processing_finding(source: dict[str, Any]) -> bool:
    finding_id = str(source.get("id") or "")
    source_name = str(source.get("source") or "")
    return (
        finding_id.startswith(
            (
                "TECH-",
                "KOSIT-CONFIG",
                "KOSIT-START",
                "KOSIT-TIMEOUT",
                "KOSIT-REPORT",
                "KOSIT-EXEC",
                "KOSIT-OUTPUT-TRUNCATED",
                "KOSIT-RESULT-MISMATCH",
            )
        )
        or finding_id == "KOSIT-EXIT"
        or source_name == "KoSIT-Anbindung"
    )


def _internal_assessment(
    builtin: dict[str, Any] | None,
) -> tuple[InternalAssessment, list[Finding]]:
    if builtin is None:
        return InternalAssessment(status=InternalAssessmentStatus.NOT_RUN, executed=False), []
    internal_findings: list[Finding] = []
    processing_findings: list[Finding] = []
    for source in builtin.get("findings", []):
        if _is_processing_finding(source):
            processing_findings.append(_finding(source, origin=FindingOrigin.PROCESSING))
        else:
            internal_findings.append(_finding(source, origin=FindingOrigin.INTERNAL))
    errors = sum(item.severity is FindingSeverity.ERROR for item in internal_findings)
    warnings = sum(item.severity is FindingSeverity.WARNING for item in internal_findings)
    status = (
        InternalAssessmentStatus.ERRORS
        if errors
        else InternalAssessmentStatus.ATTENTION
        if warnings
        else InternalAssessmentStatus.CLEAR
    )
    return (
        InternalAssessment(
            status=status,
            executed=True,
            summary=("Interne Vorprüfungen und Plausibilitätskontrollen wurden ausgeführt."),
            scope=builtin.get("scope"),
            findings=internal_findings,
        ),
        processing_findings,
    )


def _official_assessment(
    official: dict[str, Any],
    *,
    requested: bool,
    profile_official_capability: ProfileOfficialValidationCapability,
) -> tuple[OfficialAssessment, list[Finding]]:
    official_findings: list[Finding] = []
    processing_findings: list[Finding] = []
    for source in official.get("findings", []):
        if _is_processing_finding(source):
            processing_findings.append(_finding(source, origin=FindingOrigin.PROCESSING))
        else:
            official_findings.append(_finding(source, origin=FindingOrigin.OFFICIAL))

    executed = bool(official.get("executed"))
    configured = bool(official.get("configured"))
    accepted = official.get("accepted")
    if not requested:
        status = OfficialAssessmentStatus.NOT_REQUESTED
        executed = False
    elif executed and accepted is True:
        status = OfficialAssessmentStatus.ACCEPTED
    elif executed and accepted is False:
        status = OfficialAssessmentStatus.REJECTED
    elif profile_official_capability is ProfileOfficialValidationCapability.NOT_BUNDLED:
        status = OfficialAssessmentStatus.UNSUPPORTED
        executed = False
    elif not configured:
        status = OfficialAssessmentStatus.UNAVAILABLE
        executed = False
    else:
        status = OfficialAssessmentStatus.INDETERMINATE

    report_source = official.get("report_source")
    return (
        OfficialAssessment(
            status=status,
            requested=requested,
            configured=configured,
            executed=executed,
            summary=official.get("summary"),
            exit_code=official.get("exit_code"),
            report_source=(
                OfficialReportSource(report_source)
                if report_source in {item.value for item in OfficialReportSource}
                else None
            ),
            raw_report=official.get("raw_report"),
            technical_output=official.get("technical_output"),
            findings=official_findings,
        ),
        processing_findings,
    )


def _processing_assessment(
    *,
    syntax_error: str | None,
    technical_truncated: bool,
    official: OfficialAssessment,
    findings: list[Finding],
) -> ProcessingAssessment:
    limitations: list[ProcessingLimitation] = []
    if technical_truncated:
        limitations.append(
            ProcessingLimitation(
                code="TECHNICAL-FIELDS-TRUNCATED",
                message="Die technische Feldliste wurde an der konfigurierten Grenze beendet.",
                affected_json_pointer="/technical/fields",
            )
        )
    if syntax_error:
        findings.append(
            Finding(
                origin=FindingOrigin.PROCESSING,
                rule_class=FindingRuleClass.PROCESSING,
                severity=FindingSeverity.ERROR,
                rule=FindingRule(
                    id="SYNTAX-001",
                    title="Nicht unterstützte E-Rechnungssyntax",
                    message=syntax_error,
                    source="E-Rechnungs-Prüfer",
                ),
                occurrence=FindingOccurrence(scope=OccurrenceScope.DOCUMENT, json_pointer="/document"),
            )
        )
    incomplete = bool(syntax_error) or official.status in {
        OfficialAssessmentStatus.UNAVAILABLE,
        OfficialAssessmentStatus.INDETERMINATE,
    }
    if incomplete:
        status = ProcessingAssessmentStatus.INCOMPLETE
    elif limitations or findings:
        status = ProcessingAssessmentStatus.LIMITED
    else:
        status = ProcessingAssessmentStatus.COMPLETE
    return ProcessingAssessment(
        status=status,
        summary=(
            "Die angeforderten Verarbeitungsschritte konnten nicht vollständig abgeschlossen werden."
            if status is ProcessingAssessmentStatus.INCOMPLETE
            else "Die Analyse wurde mit begrenzter technischer Darstellung abgeschlossen."
            if status is ProcessingAssessmentStatus.LIMITED
            else "Die Analyse wurde vollständig abgeschlossen."
        ),
        limitations=limitations,
        findings=findings,
    )


def _document_family(value: str) -> DocumentFamily:
    mapping = {
        "invoice": DocumentFamily.INVOICE,
        "prepayment_invoice": DocumentFamily.PREPAYMENT_INVOICE,
        "payment_request": DocumentFamily.PAYMENT_REQUEST,
        "credit-note": DocumentFamily.CREDIT_NOTE,
        "credit_note": DocumentFamily.CREDIT_NOTE,
        "correction": DocumentFamily.CORRECTION,
        "corrective_invoice": DocumentFamily.CORRECTION,
        "debit-note": DocumentFamily.DEBIT_NOTE,
        "debit_note": DocumentFamily.DEBIT_NOTE,
        "information": DocumentFamily.INFORMATION,
        "pro-forma": DocumentFamily.PRO_FORMA,
        "pro_forma": DocumentFamily.PRO_FORMA,
        "claim": DocumentFamily.CLAIM,
        "other": DocumentFamily.OTHER,
    }
    return mapping.get(value, DocumentFamily.UNKNOWN)


def _role(value: Any) -> DocumentPartyRole:
    mapping = {
        "seller": DocumentPartyRole.SELLER,
        "buyer": DocumentPartyRole.BUYER,
        "payee": DocumentPartyRole.PAYEE,
        "invoicee": DocumentPartyRole.INVOICE_RECIPIENT,
        "invoice-recipient": DocumentPartyRole.INVOICE_RECIPIENT,
        "delivery-recipient": DocumentPartyRole.DELIVERY_RECIPIENT,
        "seller-tax-representative": DocumentPartyRole.SELLER_TAX_REPRESENTATIVE,
    }
    return mapping.get(str(value or ""), DocumentPartyRole.UNKNOWN)


def _roles(semantics: dict[str, Any]) -> PartyRoleAssignments:
    roles = _mapping(semantics.get("roles"))
    settlement = _mapping(semantics.get("settlement"))
    expected_flow = settlement.get("expected_flow")
    direction = {
        "debtor_to_creditor": PaymentDirection.DEBTOR_TO_CREDITOR,
        "creditor_to_debtor": PaymentDirection.CREDITOR_TO_DEBTOR,
        "none": PaymentDirection.NONE,
    }.get(str(expected_flow or ""), PaymentDirection.UNKNOWN)
    role_status = roles.get("status")
    derivation = (
        TransactionDerivation.DERIVED
        if role_status == "deterministic"
        else TransactionDerivation.AMBIGUOUS
        if role_status in {"conflict", "ambiguous"}
        else TransactionDerivation.UNKNOWN
    )
    return PartyRoleAssignments(
        issuer=_role(roles.get("document_issuer")),
        document_recipient=_role(roles.get("document_receiver")),
        creditor=_role(roles.get("commercial_creditor")),
        debtor=_role(roles.get("commercial_debtor")),
        expected_payer=_role(settlement.get("expected_payer")),
        expected_recipient=_role(settlement.get("expected_recipient")),
        expected_payment_direction=direction,
        derivation=derivation,
    )


def _source(source: dict[str, Any]) -> SourceModel:
    upload_media_type = str(source.get("media_type") or "application/octet-stream")
    upload = None
    if source.get("filename") and source.get("sha256"):
        upload = SourceFile(
            filename=str(source["filename"]),
            media_type=upload_media_type,
            size_bytes=int(source.get("size") or 0),
            sha256=str(source["sha256"]),
        )
    invoice_xml = None
    if source.get("xml_filename") and source.get("xml_sha256"):
        invoice_xml = SourceFile(
            filename=str(source["xml_filename"]),
            media_type="application/xml",
            size_bytes=int(source.get("xml_size") or 0),
            sha256=str(source["xml_sha256"]),
        )
    container_source = _mapping(source.get("container"))
    type_text = str(container_source.get("type") or "").casefold()
    kind = (
        SourceContainerKind.PDF
        if "pdf" in type_text
        else SourceContainerKind.XML
        if "xml" in type_text
        else SourceContainerKind.UNKNOWN
    )
    attachments = [
        SourceAttachment(
            name=str(item.get("name") or "attachment"),
            size_bytes=int(item.get("size") or item.get("size_bytes") or 0),
            sha256=str(item.get("sha256") or ("0" * 64)),
            is_xml=bool(item.get("is_xml")),
            selected=bool(item.get("selected") or item.get("name") == container_source.get("selected_attachment")),
        )
        for item in source.get("attachments", [])
        if isinstance(item, dict)
    ]
    return SourceModel(
        upload=upload,
        invoice_xml=invoice_xml,
        container=SourceContainer(
            kind=kind,
            page_count=container_source.get("page_count"),
            selected_attachment=container_source.get("selected_attachment"),
            attachment_count=len(attachments),
        ),
        attachments=attachments,
    )


def _redact_card_values(value: str | None, card_values: list[str]) -> str | None:
    if value is None:
        return None
    redacted = value
    for card_value in card_values:
        compact = re.sub(r"\s+", "", card_value)
        if not compact:
            continue
        replacement = _masked_account(compact) or "••••"
        if len(compact) > 256:
            redacted = redacted.replace(card_value, replacement)
            redacted = redacted.replace(compact, replacement)
            continue
        xml_whitespace = r"(?:\s|&#0*(?:9|10|13|32);|&#[xX]0*(?:9|[aA]|[dD]|20);)*"
        characters: list[str] = []
        named_entities = {
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&apos;",
        }
        for character in compact:
            codepoint = ord(character)
            hexadecimal = "".join(
                f"[{digit.lower()}{digit.upper()}]" if digit.isalpha() else digit for digit in f"{codepoint:x}"
            )
            variants = [
                re.escape(character),
                rf"&#0*{codepoint};",
                rf"&#[xX]0*{hexadecimal};",
            ]
            named_entity = named_entities.get(character)
            if named_entity is not None:
                variants.append(re.escape(named_entity))
            characters.append(f"(?:{'|'.join(variants)})")
        pattern = xml_whitespace.join(characters)
        redacted = re.sub(pattern, replacement, redacted)
    return redacted


def _redact_public_value(value: Any, card_values: list[str]) -> Any:
    if isinstance(value, str):
        return _redact_card_values(value, card_values)
    if isinstance(value, list):
        return [_redact_public_value(item, card_values) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_public_value(item, card_values) for item in value)
    if isinstance(value, dict):
        return {key: _redact_public_value(item, card_values) for key, item in value.items()}
    return value


def _redact_public_response(
    response: AnalysisResponse,
    card_values: list[str],
) -> AnalysisResponse:
    if not card_values:
        return response
    redacted = _redact_public_value(response.model_dump(mode="python"), card_values)
    return AnalysisResponse.model_validate(redacted)


def _technical(source: dict[str, Any], card_values: list[str]) -> TechnicalModel:
    fields: list[TechnicalField] = []
    for item in source.get("rows", source.get("fields", [])):
        if not isinstance(item, dict):
            continue
        kind_value = str(item.get("kind") or "element")
        if kind_value not in {member.value for member in TechnicalFieldKind}:
            continue
        fields.append(
            TechnicalField(
                kind=TechnicalFieldKind(kind_value),
                path=str(item.get("path") or "/"),
                name=item.get("name"),
                namespace=item.get("namespace"),
                value=_redact_card_values(
                    str(item["value"]) if item.get("value") is not None else None,
                    card_values,
                ),
            )
        )
    return TechnicalModel(
        root_element=source.get("root_element"),
        root_namespace=source.get("root_namespace"),
        field_count=int(source.get("field_count") or len(fields)),
        truncated=bool(source.get("truncated")),
        fields=fields,
        source_xml=_redact_card_values(source.get("original_xml") or source.get("source_xml"), card_values),
        pretty_xml=_redact_card_values(source.get("raw_xml") or source.get("pretty_xml"), card_values),
    )


def build_analysis_response(
    parsed: dict[str, Any],
    *,
    builtin: dict[str, Any] | None,
    official: dict[str, Any],
    official_requested: bool,
    syntax_error: str | None,
    duration_ms: Decimal,
    application_version: str,
) -> AnalysisResponse:
    document_source = _mapping(parsed.get("document"))
    profile_source = _mapping(parsed.get("profile"))
    profile_id = profile_source.get("id") or document_source.get("profile_id")
    profile_resolution = resolve_profile(profile_id)
    syntax_value = str(document_source.get("syntax") or "UNKNOWN")
    syntax = Syntax(syntax_value) if syntax_value in {item.value for item in Syntax} else Syntax.UNKNOWN
    ubl_root = None
    if syntax is Syntax.UBL:
        root = str(_mapping(parsed.get("technical")).get("root_element") or "")
        ubl_root = (
            RegistryUblRoot.CREDIT_NOTE
            if root == "CreditNote"
            else RegistryUblRoot.INVOICE
            if root == "Invoice"
            else None
        )
    type_resolution = resolve_document_type(document_source.get("type_code"), ubl_root)
    type_data = type_resolution.to_dict()
    semantics = derive_document_semantics(
        type_resolution,
        profile_resolution,
        (parsed.get("totals") or {}).get("due_payable_amount"),
        has_payee=_party_has_data(parsed.get("payee")),
    ).to_dict()

    default_currency = document_source.get("currency") or _mapping(parsed.get("totals")).get("currency")
    type_status = DocumentTypeStatus(str(type_data["status"]))
    type_code = _code(
        type_data.get("code"),
        type_data.get("label"),
        "UNCL1001",
    )
    api_ubl_root = (
        UblRoot.INVOICE
        if type_data.get("ubl_root") == "Invoice"
        else UblRoot.CREDIT_NOTE
        if type_data.get("ubl_root") == "CreditNote"
        else None
    )
    root_compatibility = {
        "compatible": RootCompatibility.COMPATIBLE,
        "incompatible": RootCompatibility.INCOMPATIBLE,
        "not_applicable": RootCompatibility.NOT_APPLICABLE,
        "not-applicable": RootCompatibility.NOT_APPLICABLE,
        "undetermined": RootCompatibility.UNDETERMINED,
    }.get(str(type_data.get("root_compatibility") or ""), RootCompatibility.UNDETERMINED)
    base_polarity = BasePolarity(str(type_data.get("base_polarity") or BasePolarity.UNDETERMINED.value))
    settlement_relevance = {
        "deterministic": SettlementRelevance.RELEVANT,
        "relevant": SettlementRelevance.RELEVANT,
        "non_settlement": SettlementRelevance.NOT_RELEVANT,
        "not-relevant": SettlementRelevance.NOT_RELEVANT,
        "undetermined": SettlementRelevance.UNDETERMINED,
    }.get(str(type_data.get("settlement_relevance") or ""), SettlementRelevance.UNDETERMINED)

    internal, builtin_processing = _internal_assessment(builtin)
    official_assessment, official_processing = _official_assessment(
        official,
        requested=official_requested,
        profile_official_capability=profile_resolution.capabilities.official_validation,
    )
    processing_findings = builtin_processing + official_processing
    technical_source = _mapping(parsed.get("technical"))
    processing = _processing_assessment(
        syntax_error=syntax_error,
        technical_truncated=bool(technical_source.get("truncated")),
        official=official_assessment,
        findings=processing_findings,
    )

    profile_official = profile_resolution.capabilities.official_validation
    official_capability = (
        OfficialValidationCapability.UNAVAILABLE
        if profile_official is ProfileOfficialValidationCapability.BUNDLED and not official.get("configured")
        else OfficialValidationCapability.BUNDLED
        if profile_official is ProfileOfficialValidationCapability.BUNDLED
        else OfficialValidationCapability.NOT_BUNDLED
        if profile_official is ProfileOfficialValidationCapability.NOT_BUNDLED
        else OfficialValidationCapability.UNKNOWN
    )
    type_recognition = {
        DocumentTypeStatus.KNOWN: DocumentTypeRecognition.RECOGNIZED,
        DocumentTypeStatus.UNKNOWN: DocumentTypeRecognition.UNKNOWN,
        DocumentTypeStatus.MISSING: DocumentTypeRecognition.MISSING,
    }[type_status]
    rendering = (
        RenderingCapability.FULL
        if syntax is not Syntax.UNKNOWN and type_status is DocumentTypeStatus.KNOWN
        else RenderingCapability.PARTIAL
        if syntax is not Syntax.UNKNOWN
        else RenderingCapability.UNSUPPORTED
    )
    internal_capability = (
        InternalChecksCapability.UNSUPPORTED
        if syntax is Syntax.UNKNOWN
        else InternalChecksCapability.FULL
        if profile_resolution.capabilities.internal_semantics.value == "supported"
        else InternalChecksCapability.PARTIAL
    )

    tax_rows = [
        TaxBreakdown(
            tax_type=_code(item.get("type")),
            category=_code(
                item.get("category_code"),
                item.get("category_display") or item.get("category_label"),
            ),
            rate_percent=xml_decimal_value(item.get("rate")),
            taxable_amount=_amount(
                item.get("basis_amount"),
                item.get("basis_currency") or default_currency,
            ),
            tax_amount=_amount(
                item.get("tax_amount"),
                item.get("tax_currency") or default_currency,
            ),
            exemption=(
                TaxExemption(
                    reasons=_exemption_reasons(item),
                    reason_code=_code(item.get("exemption_reason_code")),
                )
                if item.get("exemption_reason") or item.get("exemption_reasons") or item.get("exemption_reason_code")
                else None
            ),
        )
        for item in parsed.get("taxes", [])
        if isinstance(item, dict)
    ]
    totals_source = _mapping(parsed.get("totals"))
    tax_model = TaxModel(
        breakdown=tax_rows,
        totals=TaxTotals(
            document_currency=_amount(
                totals_source.get("tax_total"),
                totals_source.get("tax_total_currency") or default_currency,
            ),
            vat_accounting_currency=_amount(
                totals_source.get("tax_total_accounting"),
                totals_source.get("tax_total_accounting_currency")
                or document_source.get("vat_accounting_currency")
                or document_source.get("tax_currency"),
            ),
        ),
    )

    payment_source = _mapping(parsed.get("payment"))
    card_values = [
        str(item["card_account"])
        for item in payment_source.get("means", payment_source.get("instructions", []))
        if isinstance(item, dict) and item.get("card_account")
    ]
    references_source = _mapping(parsed.get("references"))
    delivery_source = _mapping(parsed.get("delivery"))
    invoice_period = _period(parsed.get("invoice_period") or (parsed.get("periods") or {}).get("invoice"))
    delivery_period = _period((parsed.get("periods") or {}).get("delivery"))
    delivery_date = _date_value(document_source.get("delivery_date") or delivery_source.get("date"))
    delivery_location_id = _identifier(delivery_source.get("location_id"))
    delivery_location_address = _address(delivery_source.get("address"))
    delivery_location = (
        DeliveryLocation(
            id=delivery_location_id,
            postal_address=delivery_location_address,
        )
        if delivery_location_id is not None or delivery_location_address is not None
        else None
    )
    issuance_mode = str(type_data.get("issuance_mode") or "")
    self_billing = True if issuance_mode == "self_billing" else False if issuance_mode == "supplier_issued" else None

    response = AnalysisResponse(
        document=DocumentModel(
            id=document_source.get("id"),
            issue_date=_date_value(document_source.get("issue_date")),
            type=DocumentType(
                status=type_status,
                code=type_code,
                family=_document_family(str(type_data.get("family") or "")),
                base_polarity=base_polarity,
                settlement_relevance=settlement_relevance,
                self_billing=self_billing,
                ubl_root=api_ubl_root,
                root_compatibility=root_compatibility,
                registry_version=(
                    str(type_data["registry_version"]) if type_data.get("registry_version") is not None else None
                ),
            ),
            tax_point_date=_date_value(document_source.get("tax_point_date")),
            tax_point_date_code=_code(
                document_source.get("tax_point_date_code"),
                list_id="UNCL2005",
            ),
            document_currency=_code(default_currency, document_source.get("currency_label"), "ISO4217"),
            vat_accounting_currency=_code(
                document_source.get("vat_accounting_currency") or document_source.get("tax_currency"),
                document_source.get("vat_accounting_currency_label"),
                "ISO4217",
            ),
            buyer_reference=document_source.get("buyer_reference"),
            notes=_document_notes(document_source.get("notes")),
        ),
        profile=ProfileModel(
            id=profile_resolution.identifier,
            name=profile_resolution.label,
            business_process_id=profile_source.get("business_process_id"),
        ),
        capabilities=CapabilitiesModel(
            syntax=syntax,
            syntax_version=document_source.get("syntax_version") or profile_source.get("ubl_version"),
            format_name=document_source.get("format"),
            document_type_recognition=type_recognition,
            rendering=rendering,
            internal_checks=internal_capability,
            official_validation=official_capability,
        ),
        parties=PartiesModel(
            seller=_party(parsed.get("seller")),
            buyer=_party(parsed.get("buyer")),
            payee=_party(parsed.get("payee")),
            invoice_recipient=_party(parsed.get("invoicee")),
            seller_tax_representative=_party(parsed.get("seller_tax_representative")),
            delivery_recipient=_party(parsed.get("ship_to")),
        ),
        roles=_roles(semantics),
        periods=PeriodsModel(
            invoice=invoice_period,
            delivery=delivery_period,
        ),
        delivery=DeliveryModel(
            actual_date=delivery_date,
            location=delivery_location,
        ),
        references=_references(references_source, delivery_source),
        lines=[_line(item, default_currency) for item in parsed.get("lines", []) if isinstance(item, dict)],
        allowances_charges=[
            _allowance_charge(item, default_currency)
            for item in parsed.get("header_allowances_charges", [])
            if isinstance(item, dict)
        ],
        tax=tax_model,
        totals=TotalsModel(
            line_net_total=_amount(
                totals_source.get("line_total"),
                totals_source.get("line_total_currency") or default_currency,
            ),
            allowance_total=_amount(
                totals_source.get("allowance_total"),
                totals_source.get("allowance_total_currency") or default_currency,
            ),
            charge_total=_amount(
                totals_source.get("charge_total"),
                totals_source.get("charge_total_currency") or default_currency,
            ),
            tax_exclusive_total=_amount(
                totals_source.get("tax_basis_total"),
                totals_source.get("tax_basis_total_currency") or default_currency,
            ),
            tax_inclusive_total=_amount(
                totals_source.get("grand_total"),
                totals_source.get("grand_total_currency") or default_currency,
            ),
            prepaid_total=_amount(
                totals_source.get("prepaid_amount"),
                totals_source.get("prepaid_amount_currency") or default_currency,
            ),
            rounding=_amount(
                totals_source.get("rounding_amount"),
                totals_source.get("rounding_amount_currency") or default_currency,
            ),
            payable=_amount(
                totals_source.get("due_payable_amount"),
                totals_source.get("due_payable_amount_currency") or default_currency,
            ),
        ),
        payment=_payment(
            payment_source,
            due_date=document_source.get("due_date"),
            default_currency=default_currency,
        ),
        assessment=Assessment(
            official=official_assessment,
            internal=internal,
            processing=processing,
        ),
        source=_source(parsed.get("source") or {}),
        technical=_technical(technical_source, card_values),
        runtime=RuntimeModel(
            generated_at=datetime.now(UTC),
            duration_ms=duration_ms,
            application_version=application_version,
        ),
    )
    return _redact_public_response(response, card_values)


__all__ = ["build_analysis_response"]
