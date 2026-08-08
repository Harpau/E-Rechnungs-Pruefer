from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.analysis_builder import _only_string_too_long_errors
from app.api_models import (
    Address,
    AllowanceCharge,
    Amount,
    AnalysisResponse,
    BasePolarity,
    CodeValue,
    Contact,
    CreditTransfer,
    DocumentFamily,
    DocumentModel,
    DocumentPartyRole,
    DocumentTypeRecognition,
    DocumentTypeStatus,
    Identifier,
    InternalChecksCapability,
    InvoiceLine,
    Item,
    OfficialValidationCapability,
    Party,
    PartyIdentifier,
    PartyIdentifierKind,
    PartyRoleAssignments,
    PaymentDirection,
    PaymentInstruction,
    PaymentModel,
    Period,
    Price,
    Quantity,
    Reference,
    RenderingCapability,
    RootCompatibility,
    SettlementRelevance,
    SourceAttachment,
    SourceContainerKind,
    SupportingDocument,
    Syntax,
    TaxBreakdown,
    TaxModel,
    TaxTotals,
    TechnicalField,
    TechnicalModel,
    TransactionDerivation,
    UblRoot,
)
from app.assessment import (
    Assessment,
    InternalAssessment,
    InternalAssessmentStatus,
    OfficialAssessment,
    OfficialAssessmentStatus,
    ProcessingAssessment,
    ProcessingAssessmentStatus,
)
from app.findings import (
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


def _finding(
    *,
    severity: FindingSeverity = FindingSeverity.WARNING,
    origin: FindingOrigin = FindingOrigin.INTERNAL,
) -> Finding:
    return Finding(
        origin=origin,
        rule_class=FindingRuleClass.PLAUSIBILITY,
        severity=severity,
        rule=FindingRule(
            id="PAY-001",
            title="Zahlungsangaben prüfen",
            message="Die Zahlungsangaben passen möglicherweise nicht zum Geschäftsvorfall.",
            source="E-Rechnungs-Prüfer",
            reference="PAY-001",
            profile="internal",
            version="2",
        ),
        semantic_references=[SemanticReference(id="BG-16", label="Zahlungsanweisungen")],
        occurrence=FindingOccurrence(
            scope=OccurrenceScope.PAYMENT,
            index=0,
            json_pointer="/payment/instructions/0",
        ),
        xml_location=XmlLocation(
            path="/rsm:CrossIndustryInvoice/ram:ApplicableHeaderTradeSettlement",
            line=42,
        ),
        actual=FindingEvidence(value="keine Zahlungsanweisung", data_type=EvidenceDataType.TEXT),
        expected=FindingEvidence(value="profilabhängig prüfen", data_type=EvidenceDataType.TEXT),
    )


def test_analysis_response_has_safe_complete_schema_two_defaults() -> None:
    first = AnalysisResponse()
    second = AnalysisResponse()

    assert first.schema_version == 2
    assert first.capabilities.syntax is Syntax.UNKNOWN
    assert first.capabilities.document_type_recognition is DocumentTypeRecognition.MISSING
    assert first.capabilities.rendering is RenderingCapability.UNSUPPORTED
    assert first.capabilities.internal_checks is InternalChecksCapability.UNSUPPORTED
    assert first.capabilities.official_validation is OfficialValidationCapability.UNKNOWN
    assert first.document.type.status is DocumentTypeStatus.MISSING
    assert first.document.type.family is DocumentFamily.UNKNOWN
    assert first.parties.seller is None
    assert first.roles.issuer is DocumentPartyRole.UNKNOWN
    assert first.roles.document_recipient is DocumentPartyRole.UNKNOWN
    assert first.roles.expected_payer is DocumentPartyRole.UNKNOWN
    assert first.roles.expected_recipient is DocumentPartyRole.UNKNOWN
    assert first.roles.expected_payment_direction is PaymentDirection.UNKNOWN
    assert first.periods.invoice is None
    assert first.delivery.actual_date is None
    assert first.delivery.location is None
    assert first.references.preceding_invoices == []
    assert first.lines == []
    assert first.allowances_charges == []
    assert first.tax.breakdown == []
    assert first.totals.payable is None
    assert first.payment.instructions == []
    assert first.assessment.official.status is OfficialAssessmentStatus.NOT_REQUESTED
    assert first.assessment.internal.status is InternalAssessmentStatus.NOT_RUN
    assert first.assessment.processing.status is ProcessingAssessmentStatus.INCOMPLETE
    assert first.source.attachments == []
    assert first.technical.fields == []
    assert first.runtime.application_version is None

    first.lines.append(InvoiceLine(id="1"))
    assert second.lines == []


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (AnalysisResponse, {"unexpected": True}),
        (DocumentModel, {"unexpected": True}),
        (Party, {"unexpected": True}),
        (Finding, {**_finding().model_dump(), "location": "BG-16"}),
        (Assessment, {"unexpected": True}),
    ],
)
def test_contract_models_reject_extra_fields(model: type, payload: dict) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate(payload)


def test_schema_version_is_literal_two() -> None:
    with pytest.raises(ValidationError):
        AnalysisResponse(schema_version=1)


def test_finding_occurrence_identifier_matches_invoice_line_identifier_limit() -> None:
    occurrence = FindingOccurrence(scope=OccurrenceScope.LINE, identifier="X" * 1000)

    assert occurrence.identifier == "X" * 1000
    with pytest.raises(ValidationError):
        FindingOccurrence(scope=OccurrenceScope.LINE, identifier="X" * 1001)


def test_only_string_too_long_errors_are_safe_to_translate_to_input_errors() -> None:
    with pytest.raises(ValidationError) as length_error:
        DocumentModel(id="X" * 1001)
    with pytest.raises(ValidationError) as index_error:
        FindingOccurrence(scope=OccurrenceScope.LINE, index=-1)
    with pytest.raises(ValidationError) as mixed_error:
        DocumentModel.model_validate({"id": "X" * 1001, "unexpected": True})

    assert _only_string_too_long_errors(length_error.value) is True
    assert _only_string_too_long_errors(index_error.value) is False
    assert _only_string_too_long_errors(mixed_error.value) is False


def test_technical_xml_representation_preserves_boundary_whitespace() -> None:
    source = "\n<Invoice>synthetisch</Invoice>\n"

    technical = TechnicalModel(source_xml=source, pretty_xml=source)

    assert technical.source_xml == source
    assert technical.pretty_xml == source


def test_assessment_axis_values_are_closed() -> None:
    assert {item.value for item in OfficialAssessmentStatus} == {
        "accepted",
        "rejected",
        "not-requested",
        "unsupported",
        "unavailable",
        "indeterminate",
    }
    assert {item.value for item in InternalAssessmentStatus} == {
        "clear",
        "attention",
        "errors",
        "not-run",
    }
    assert {item.value for item in ProcessingAssessmentStatus} == {
        "complete",
        "limited",
        "incomplete",
    }
    assert {item.value for item in FindingRuleClass} == {
        "core_precheck",
        "profile_precheck",
        "plausibility",
        "processing",
        "official",
    }
    assert {item.value for item in DocumentTypeRecognition} == {"recognized", "unknown", "missing"}
    assert {item.value for item in RenderingCapability} == {"full", "partial", "unsupported"}
    assert {item.value for item in InternalChecksCapability} == {"full", "partial", "unsupported"}
    assert {item.value for item in OfficialValidationCapability} == {
        "bundled",
        "not-bundled",
        "unknown",
        "unavailable",
    }
    assert {item.value for item in DocumentTypeStatus} == {"known", "unknown", "missing"}
    assert {item.value for item in BasePolarity} == {"debit", "credit", "neutral", "undetermined"}
    assert {item.value for item in SettlementRelevance} == {"relevant", "not-relevant", "undetermined"}
    assert {item.value for item in UblRoot} == {"invoice", "credit-note"}
    assert {item.value for item in RootCompatibility} == {
        "compatible",
        "incompatible",
        "not-applicable",
        "undetermined",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "missing", "code": {"value": "380"}},
        {"status": "known"},
        {"status": "unknown"},
    ],
)
def test_document_type_status_and_code_must_agree(payload: dict) -> None:
    from app.api_models import DocumentType

    with pytest.raises(ValidationError):
        DocumentType.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "accepted", "requested": False, "executed": True},
        {"status": "rejected", "requested": True, "executed": False},
        {"status": "not-requested", "requested": True, "executed": False},
        {"status": "unsupported", "requested": False, "executed": False},
        {"status": "unavailable", "requested": False, "executed": False},
        {"status": "indeterminate", "requested": False, "executed": False},
    ],
)
def test_official_assessment_rejects_inconsistent_execution_state(payload: dict) -> None:
    with pytest.raises(ValidationError):
        OfficialAssessment.model_validate(payload)


def test_finding_uses_structured_references_and_no_legacy_location_or_source() -> None:
    finding = _finding()
    payload = finding.model_dump(mode="json")

    assert set(payload) == {
        "origin",
        "rule_class",
        "severity",
        "rule",
        "semantic_references",
        "occurrence",
        "xml_location",
        "actual",
        "expected",
    }
    assert payload["semantic_references"] == [{"id": "BG-16", "label": "Zahlungsanweisungen"}]
    assert payload["rule"]["source"] == "E-Rechnungs-Prüfer"
    assert payload["rule"]["reference"] == "PAY-001"
    assert payload["rule"]["profile"] == "internal"
    assert payload["rule"]["version"] == "2"
    assert payload["occurrence"]["json_pointer"] == "/payment/instructions/0"
    assert payload["xml_location"]["line"] == 42
    assert "location" not in payload
    assert "source" not in payload


def test_xml_location_accepts_source_coordinates_without_inventing_a_path() -> None:
    location = XmlLocation(line=7, column=13)

    assert location.model_dump(mode="json") == {
        "path": None,
        "line": 7,
        "column": 13,
    }


def test_xml_location_rejects_an_empty_location() -> None:
    with pytest.raises(ValidationError, match="path, line or column"):
        XmlLocation()


def test_assessment_keeps_findings_and_counts_on_their_own_axes() -> None:
    internal_finding = _finding(severity=FindingSeverity.WARNING)
    official_finding = _finding(severity=FindingSeverity.ERROR, origin=FindingOrigin.OFFICIAL)
    processing_finding = _finding(severity=FindingSeverity.INFO, origin=FindingOrigin.PROCESSING)
    assessment = Assessment(
        internal=InternalAssessment(
            status=InternalAssessmentStatus.ATTENTION,
            executed=True,
            findings=[internal_finding],
        ),
        official=OfficialAssessment(
            status=OfficialAssessmentStatus.REJECTED,
            requested=True,
            executed=True,
            findings=[official_finding],
        ),
        processing=ProcessingAssessment(
            status=ProcessingAssessmentStatus.LIMITED,
            findings=[processing_finding],
        ),
    )

    payload = assessment.model_dump(mode="json")
    assert set(payload) == {"official", "internal", "processing"}
    assert payload["internal"]["counts"] == {"error": 0, "warning": 1, "info": 0}
    assert payload["official"]["counts"] == {"error": 1, "warning": 0, "info": 0}
    assert payload["processing"]["counts"] == {"error": 0, "warning": 0, "info": 1}


@pytest.mark.parametrize(
    ("model", "finding"),
    [
        (
            InternalAssessment,
            _finding(origin=FindingOrigin.OFFICIAL),
        ),
        (
            OfficialAssessment,
            _finding(origin=FindingOrigin.PROCESSING),
        ),
        (
            ProcessingAssessment,
            _finding(origin=FindingOrigin.INTERNAL),
        ),
    ],
)
def test_assessment_axes_reject_findings_from_other_origins(model: type, finding: Finding) -> None:
    payload: dict = {"findings": [finding]}
    if model is InternalAssessment:
        payload.update(status="attention", executed=True)
    elif model is OfficialAssessment:
        payload.update(status="accepted", requested=True, executed=True)
    else:
        payload.update(status="limited")

    with pytest.raises(ValidationError, match="accepts only findings"):
        model.model_validate(payload)


def test_realistic_analysis_payload_serializes_with_stable_machine_types() -> None:
    response = AnalysisResponse(
        capabilities={
            "syntax": Syntax.UBL,
            "syntax_version": "2.1",
            "document_type_recognition": DocumentTypeRecognition.RECOGNIZED,
            "rendering": RenderingCapability.FULL,
            "internal_checks": InternalChecksCapability.FULL,
            "official_validation": OfficialValidationCapability.BUNDLED,
        },
        document={
            "id": "SYNTHETIC-389",
            "issue_date": date(2026, 7, 31),
            "type": {
                "status": DocumentTypeStatus.KNOWN,
                "code": CodeValue(value="389", label="Eigenabrechnung", list_id="UNCL1001"),
                "family": DocumentFamily.INVOICE,
                "base_polarity": BasePolarity.DEBIT,
                "settlement_relevance": SettlementRelevance.RELEVANT,
                "self_billing": True,
                "ubl_root": UblRoot.INVOICE,
                "root_compatibility": RootCompatibility.COMPATIBLE,
                "registry_version": "2026-07",
            },
            "document_currency": CodeValue(value="EUR", label="Euro", list_id="ISO4217"),
            "buyer_reference": "SYNTHETIC-BUYER-REF",
        },
        profile={
            "id": "urn:cen.eu:en16931:2017",
            "name": "EN 16931",
            "business_process_id": None,
        },
        parties={
            "seller": Party(
                legal_name="Synthetic Supplier GmbH",
                identifiers=[
                    PartyIdentifier(
                        kind=PartyIdentifierKind.LEGAL_REGISTRATION,
                        identifier=Identifier(value="SYNTHETIC-HRB", scheme_id="0204"),
                    )
                ],
                electronic_address=Identifier(value="supplier@example.invalid", scheme_id="EM"),
                postal_address=Address(
                    line1="Beispielweg 1",
                    postcode="10115",
                    city="Berlin",
                    country=CodeValue(value="DE", label="Deutschland", list_id="ISO3166-1"),
                ),
                contact=Contact(email="supplier@example.invalid"),
            ),
            "buyer": Party(legal_name="Synthetic Buyer AG"),
        },
        roles=PartyRoleAssignments(
            issuer=DocumentPartyRole.BUYER,
            document_recipient=DocumentPartyRole.SELLER,
            creditor=DocumentPartyRole.SELLER,
            debtor=DocumentPartyRole.BUYER,
            expected_payer=DocumentPartyRole.BUYER,
            expected_recipient=DocumentPartyRole.SELLER,
            expected_payment_direction=PaymentDirection.DEBTOR_TO_CREDITOR,
            derivation=TransactionDerivation.DERIVED,
        ),
        periods={"invoice": Period(start_date=date(2026, 7, 1), end_date=date(2026, 7, 31))},
        references={
            "buyer_order": Reference(id=Identifier(value="PO-SYNTHETIC")),
            "supporting_documents": [
                SupportingDocument(
                    id=Identifier(value="DOC-SYNTHETIC"),
                    description="Synthetische Leistungsunterlage",
                    attachment_filename="synthetic.txt",
                    attachment_mime_type="text/plain",
                    embedded=True,
                )
            ],
        },
        lines=[
            InvoiceLine(
                id="1",
                item=Item(name="Synthetische Leistung"),
                quantity=Quantity(
                    value=Decimal("2.000"),
                    unit=CodeValue(value="HUR", label="Stunde", list_id="UNECERec20"),
                ),
                price=Price(
                    net=Amount(value=Decimal("50.00"), currency="EUR"),
                    base_quantity=Quantity(value=Decimal("1"), unit=CodeValue(value="HUR")),
                ),
                tax_category=CodeValue(value="S", label="Standardsteuersatz"),
                tax_rate_percent=Decimal("19.00"),
                net_amount=Amount(value=Decimal("100.00"), currency="EUR"),
            )
        ],
        allowances_charges=[
            AllowanceCharge(
                kind="allowance",
                amount=Amount(value=Decimal("5.00"), currency="EUR"),
                reason_text="Synthetischer Nachlass",
                tax_category=CodeValue(value="S", label="Standardsteuersatz"),
                tax_rate_percent=Decimal("19.00"),
            )
        ],
        tax=TaxModel(
            breakdown=[
                TaxBreakdown(
                    category=CodeValue(value="S", label="Standardsteuersatz"),
                    rate_percent=Decimal("19.00"),
                    taxable_amount=Amount(value=Decimal("95.00"), currency="EUR"),
                    tax_amount=Amount(value=Decimal("18.05"), currency="EUR"),
                )
            ],
            totals=TaxTotals(document_currency=Amount(value=Decimal("18.05"), currency="EUR")),
        ),
        totals={
            "line_net_total": Amount(value=Decimal("100.00"), currency="EUR"),
            "allowance_total": Amount(value=Decimal("5.00"), currency="EUR"),
            "tax_exclusive_total": Amount(value=Decimal("95.00"), currency="EUR"),
            "tax_inclusive_total": Amount(value=Decimal("113.05"), currency="EUR"),
            "payable": Amount(value=Decimal("113.05"), currency="EUR"),
        },
        payment=PaymentModel(
            due_date=date(2026, 8, 14),
            reference="SYNTHETIC-389",
            instructions=[
                PaymentInstruction(
                    means=CodeValue(value="58", label="SEPA-Überweisung"),
                    credit_transfers=[
                        CreditTransfer(
                            account_id=Identifier(value="DE89370400440532013000", scheme_id="IBAN"),
                            account_name="Synthetic Supplier GmbH",
                        )
                    ],
                )
            ],
        ),
        assessment=Assessment(
            official=OfficialAssessment(
                status=OfficialAssessmentStatus.ACCEPTED,
                requested=True,
                executed=True,
                summary="Synthetische Annahme.",
            ),
            internal=InternalAssessment(
                status=InternalAssessmentStatus.ATTENTION,
                executed=True,
                summary="Synthetischer Hinweis.",
                findings=[_finding()],
            ),
            processing=ProcessingAssessment(status=ProcessingAssessmentStatus.COMPLETE),
        ),
        source={
            "upload": {
                "filename": "synthetic.xml",
                "media_type": "application/xml",
                "size_bytes": 123,
                "sha256": "a" * 64,
            },
            "invoice_xml": {
                "filename": "synthetic.xml",
                "media_type": "application/xml",
                "size_bytes": 123,
                "sha256": "a" * 64,
            },
            "container": {"kind": SourceContainerKind.XML},
            "attachments": [
                SourceAttachment(
                    name="synthetic.xml",
                    size_bytes=123,
                    sha256="a" * 64,
                    is_xml=True,
                    selected=True,
                )
            ],
        },
        technical={
            "root_element": "Invoice",
            "root_namespace": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
            "field_count": 1,
            "truncated": False,
            "fields": [
                TechnicalField(
                    kind="element",
                    path="/Invoice[1]/ID[1]",
                    name="ID",
                    namespace="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
                    value="SYNTHETIC-389",
                )
            ],
            "source_xml": "<Invoice />",
            "pretty_xml": "<Invoice/>\n",
        },
        runtime={
            "generated_at": datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
            "duration_ms": Decimal("12.34"),
            "application_version": "2.0.0",
        },
    )

    payload = response.model_dump(mode="json")

    assert payload["schema_version"] == 2
    assert payload["capabilities"]["syntax"] == "UBL"
    assert payload["document"]["issue_date"] == "2026-07-31"
    assert payload["lines"][0]["quantity"]["value"] == "2.000"
    assert payload["totals"]["payable"] == {"value": "113.05", "currency": "EUR"}
    assert payload["assessment"]["official"]["status"] == "accepted"
    assert payload["assessment"]["internal"]["status"] == "attention"
    assert payload["assessment"]["processing"]["status"] == "complete"
    assert payload["runtime"]["generated_at"] == "2026-07-31T12:00:00Z"


def test_generated_json_schema_closes_every_object_definition() -> None:
    schema = AnalysisResponse.model_json_schema()

    assert schema["additionalProperties"] is False
    for name, definition in schema["$defs"].items():
        if definition.get("type") == "object":
            assert definition.get("additionalProperties") is False, name
