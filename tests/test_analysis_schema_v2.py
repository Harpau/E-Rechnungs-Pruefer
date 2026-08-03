from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.analyzer import analyze_bytes
from app.main import app
from app.settings import settings

EN_PROFILE = "urn:cen.eu:en16931:2017"
XRECHNUNG_PROFILE = "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
PEPPOL_PROFILE = "urn:fdc:peppol.eu:2017:poacc:billing:3.0"


def _ubl(
    *,
    profile: str,
    type_code: str = "380",
    payable: str = "119.00",
    payment_means: str = "",
    due_date: str = "2026-08-31",
) -> bytes:
    return f"""
    <Invoice
      xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
      xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
      xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2">
      <cbc:CustomizationID>{profile}</cbc:CustomizationID>
      <cbc:ID>SYNTHETISCH-{type_code}</cbc:ID>
      <cbc:IssueDate>2026-07-31</cbc:IssueDate>
      <cbc:DueDate>{due_date}</cbc:DueDate>
      <cbc:InvoiceTypeCode>{type_code}</cbc:InvoiceTypeCode>
      <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
      <cac:AccountingSupplierParty>
        <cac:Party><cac:PartyLegalEntity><cbc:RegistrationName>Synthetischer Verkäufer</cbc:RegistrationName></cac:PartyLegalEntity></cac:Party>
      </cac:AccountingSupplierParty>
      <cac:AccountingCustomerParty>
        <cac:Party><cac:PartyLegalEntity><cbc:RegistrationName>Synthetischer Käufer</cbc:RegistrationName></cac:PartyLegalEntity></cac:Party>
      </cac:AccountingCustomerParty>
      {payment_means}
      <cac:LegalMonetaryTotal>
        <cbc:LineExtensionAmount currencyID="EUR">100.00</cbc:LineExtensionAmount>
        <cbc:TaxExclusiveAmount currencyID="EUR">100.00</cbc:TaxExclusiveAmount>
        <cbc:TaxInclusiveAmount currencyID="EUR">119.00</cbc:TaxInclusiveAmount>
        <cbc:PayableAmount currencyID="EUR">{payable}</cbc:PayableAmount>
      </cac:LegalMonetaryTotal>
    </Invoice>
    """.encode()


def _finding_by_id(result: dict, axis: str) -> dict[str, dict]:
    return {item["rule"]["id"]: item for item in result["assessment"][axis]["findings"]}


def test_positive_type_389_without_payment_instruction_has_no_generic_payment_route_finding() -> None:
    result = analyze_bytes(
        _ubl(profile=EN_PROFILE, type_code="389"),
        "synthetische-eigenabrechnung.xml",
        "application/xml",
        run_official_validation=False,
    )

    findings = _finding_by_id(result, "internal")
    assert "PAY-001" not in findings
    assert "XRECHNUNG-BR-DE-1" not in findings
    assert result["document"]["type"]["self_billing"] is True
    assert result["roles"] == {
        "issuer": "buyer",
        "document_recipient": "seller",
        "creditor": "seller",
        "debtor": "buyer",
        "expected_payer": "buyer",
        "expected_recipient": "seller",
        "expected_payment_direction": "debtor-to-creditor",
        "derivation": "derived",
    }


def test_xrechnung_type_389_reports_profile_requirement_with_structured_bg16_reference() -> None:
    result = analyze_bytes(
        _ubl(profile=XRECHNUNG_PROFILE, type_code="389"),
        "synthetische-xrechnung-eigenabrechnung.xml",
        "application/xml",
        run_official_validation=False,
    )

    finding = _finding_by_id(result, "internal")["XRECHNUNG-BR-DE-1"]
    assert finding["rule_class"] == "profile_precheck"
    assert finding["rule"]["reference"] == "BR-DE-1"
    assert finding["semantic_references"] == [{"id": "BG-16", "label": "Zahlungsanweisungen"}]
    assert finding["occurrence"]["scope"] == "payment"
    assert finding["occurrence"]["index"] is None
    assert finding["xml_location"] is None
    assert "Käufer" not in finding["rule"]["message"]
    assert "Verkäufer" not in finding["rule"]["message"]


def test_br49_uses_payment_occurrence_and_does_not_treat_bg16_as_xml_location() -> None:
    payment = """
    <cac:PaymentMeans>
      <cbc:InstructionNote>Synthetische Anweisung ohne Zahlungsart</cbc:InstructionNote>
    </cac:PaymentMeans>
    """
    result = analyze_bytes(
        _ubl(profile=EN_PROFILE, payment_means=payment),
        "synthetische-zahlungsanweisung.xml",
        "application/xml",
        run_official_validation=False,
    )

    finding = _finding_by_id(result, "internal")["BR-49"]
    assert finding["semantic_references"] == [
        {"id": "BG-16", "label": "Zahlungsanweisungen"},
        {"id": "BT-81", "label": "Zahlungsartcode"},
    ]
    assert finding["occurrence"]["json_pointer"] == "/payment/instructions/0"
    assert finding["xml_location"] is None


def test_non_bundled_profile_does_not_execute_kosit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def unexpected_validate(_self, _xml: bytes, filename: str) -> dict:
        calls.append(filename)
        raise AssertionError("KoSIT darf für ein nicht gebündeltes Profil nicht ausgeführt werden.")

    monkeypatch.setattr("app.analyzer.KositValidator.validate", unexpected_validate)
    monkeypatch.setattr(
        "app.analyzer.KositValidator.configuration_state",
        lambda _self: {"configured": True, "problems": []},
    )

    result = analyze_bytes(
        _ubl(profile=PEPPOL_PROFILE),
        "synthetische-peppol-rechnung.xml",
        "application/xml",
        run_official_validation=True,
        app_settings=replace(settings, kosit_enabled=True),
    )

    assert calls == []
    assert result["capabilities"]["official_validation"] == "not-bundled"
    assert result["assessment"]["official"]["status"] == "unsupported"
    assert result["assessment"]["official"]["executed"] is False


def test_unknown_document_type_does_not_claim_self_billing_status() -> None:
    result = analyze_bytes(
        _ubl(profile=EN_PROFILE, type_code="999"),
        "synthetischer-unbekannter-dokumenttyp.xml",
        "application/xml",
        run_official_validation=False,
    )

    assert result["document"]["type"]["status"] == "unknown"
    assert result["document"]["type"]["self_billing"] is None


def test_card_identifier_is_masked_at_every_analysis_representation_boundary() -> None:
    card_identifier = "4111111111111234"
    payment = f"""
    <cac:PaymentMeans>
      <cbc:PaymentMeansCode>48</cbc:PaymentMeansCode>
      <cac:CardAccount>
        <cbc:PrimaryAccountNumberID>{card_identifier}</cbc:PrimaryAccountNumberID>
        <cbc:HolderName>Synthetische Karteninhaberin</cbc:HolderName>
      </cac:CardAccount>
    </cac:PaymentMeans>
    """
    result = analyze_bytes(
        _ubl(profile=EN_PROFILE, payment_means=payment),
        "synthetische-kartenzahlung.xml",
        "application/xml",
        run_official_validation=False,
    )

    card = result["payment"]["instructions"][0]["payment_card"]
    assert card["masked_account_identifier"] == "•••• 1234"
    assert card["holder_name"] == "Synthetische Karteninhaberin"
    assert card_identifier not in json.dumps(result, ensure_ascii=False)


def test_card_identifier_is_redacted_from_official_and_xml_representations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_identifier = "KARTE-41111234"
    entity_and_whitespace_variant = "&#x4B;ARTE-4111 &#x31;234"
    payment = f"""
    <cac:PaymentMeans>
      <cbc:PaymentMeansCode>48</cbc:PaymentMeansCode>
      <cac:CardAccount>
        <cbc:PrimaryAccountNumberID>{entity_and_whitespace_variant}</cbc:PrimaryAccountNumberID>
      </cac:CardAccount>
    </cac:PaymentMeans>
    """

    def validate_with_card_data(_self, _xml: bytes, _filename: str) -> dict:
        return {
            "configured": True,
            "executed": True,
            "accepted": False,
            "exit_code": 1,
            "summary": f"Abgelehnt für Karte {card_identifier}",
            "findings": [
                {
                    "id": "SYNTH-CARD",
                    "severity": "error",
                    "title": f"Karte {card_identifier}",
                    "message": f"Numerische Referenz {entity_and_whitespace_variant}",
                    "location": "/Invoice/CardAccount",
                    "actual": "KARTE-4111 1234",
                    "expected": card_identifier,
                    "source": f"Prüfer {card_identifier}",
                }
            ],
            "raw_report": f"<report><card>{entity_and_whitespace_variant}</card></report>",
            "technical_output": "stderr: KARTE-4111 1234",
            "report_source": "varl",
        }

    monkeypatch.setattr("app.analyzer.KositValidator.validate", validate_with_card_data)

    result = analyze_bytes(
        _ubl(profile=XRECHNUNG_PROFILE, payment_means=payment),
        "synthetische-kartenzahlung-mit-entities.xml",
        "application/xml",
        run_official_validation=True,
        app_settings=replace(settings, kosit_enabled=True),
    )

    serialized = json.dumps(result, ensure_ascii=False)
    assert card_identifier not in serialized
    assert entity_and_whitespace_variant not in serialized
    assert "KARTE-4111 1234" not in serialized
    assert result["payment"]["instructions"][0]["payment_card"]["masked_account_identifier"] == "•••• 1234"
    assert "•••• 1234" in result["assessment"]["official"]["summary"]
    assert "•••• 1234" in result["assessment"]["official"]["raw_report"]
    assert "•••• 1234" in result["technical"]["source_xml"]
    assert "&#49;" not in result["technical"]["source_xml"]
    assert "&#x31;" not in result["technical"]["source_xml"]


def test_xml_export_keeps_original_card_identifier_bytes_unchanged() -> None:
    payload = _ubl(
        profile=EN_PROFILE,
        payment_means="""
        <cac:PaymentMeans>
          <cbc:PaymentMeansCode>48</cbc:PaymentMeansCode>
          <cac:CardAccount>
            <cbc:PrimaryAccountNumberID>4111&#49;111 1111&#x31;234</cbc:PrimaryAccountNumberID>
          </cac:CardAccount>
        </cac:PaymentMeans>
        """,
    )

    response = TestClient(app).post(
        "/api/xml",
        files={"file": ("synthetische-kartenzahlung.xml", payload, "application/xml")},
    )

    assert response.status_code == 200
    assert response.content == payload
