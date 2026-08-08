from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.analyzer import analyze_bytes
from app.main import app
from app.settings import settings
from app.validators.builtin import MAX_DECIMAL_DIGITS
from app.xml_utils import TechnicalRowsResult

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
    issue_date: str = "2026-07-31",
    document_id: str | None = None,
    invoice_lines: str = "",
) -> bytes:
    shown_document_id = document_id if document_id is not None else f"SYNTHETISCH-{type_code}"
    return f"""
    <Invoice
      xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
      xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
      xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2">
      <cbc:CustomizationID>{profile}</cbc:CustomizationID>
      <cbc:ID>{shown_document_id}</cbc:ID>
      <cbc:IssueDate>{issue_date}</cbc:IssueDate>
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
      {invoice_lines}
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


def _invoice_line(
    identifier: str | None,
    *,
    quantity: str = "NaN",
    price: str = "100.00",
    base_quantity: str | None = None,
) -> str:
    identifier_xml = f"<cbc:ID>{identifier}</cbc:ID>" if identifier is not None else ""
    base_quantity_xml = (
        f'<cbc:BaseQuantity unitCode="C62">{base_quantity}</cbc:BaseQuantity>' if base_quantity is not None else ""
    )
    return f"""
    <cac:InvoiceLine>
      {identifier_xml}
      <cbc:InvoicedQuantity unitCode="C62">{quantity}</cbc:InvoicedQuantity>
      <cbc:LineExtensionAmount currencyID="EUR">100.00</cbc:LineExtensionAmount>
      <cac:Item>
        <cbc:Name>Synthetische Leistung</cbc:Name>
        <cac:ClassifiedTaxCategory>
          <cbc:ID>S</cbc:ID>
          <cbc:Percent>19</cbc:Percent>
          <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
        </cac:ClassifiedTaxCategory>
      </cac:Item>
      <cac:Price>
        <cbc:PriceAmount currencyID="EUR">{price}</cbc:PriceAmount>
        {base_quantity_xml}
      </cac:Price>
    </cac:InvoiceLine>
    """


def _ubl_with_large_consistent_amount(amount: str) -> bytes:
    payload = _ubl(
        profile=EN_PROFILE,
        payable=amount,
        invoice_lines=_invoice_line("1", quantity="1"),
    )
    return payload.replace(b"100.00", amount.encode()).replace(b"119.00", amount.encode())


def test_invalid_required_typed_values_are_reported_without_losing_schema_two_response() -> None:
    result = analyze_bytes(
        _ubl(profile=EN_PROFILE, issue_date="2026-99-99", payable="NaN"),
        "synthetische-ungueltige-pflichtwerte.xml",
        "application/xml",
        run_official_validation=False,
    )

    findings = _finding_by_id(result, "internal")
    assert result["document"]["issue_date"] is None
    assert result["totals"]["payable"] is None
    assert result["assessment"]["internal"]["status"] == "errors"
    assert result["assessment"]["processing"]["status"] == "complete"
    assert "CHECK-000" not in findings
    assert findings["FORMAT-DATE-001"]["occurrence"] == {
        "scope": "document",
        "index": None,
        "identifier": None,
        "json_pointer": "/document/issue_date",
    }
    assert findings["FORMAT-DECIMAL-001"]["occurrence"] == {
        "scope": "total",
        "index": None,
        "identifier": None,
        "json_pointer": "/totals/payable",
    }


@pytest.mark.parametrize("issue_date", ["20260731", "2026-07-31T12:00:00Z"])
def test_ubl_issue_date_rejects_non_xsd_date_lexemes_before_normalization(issue_date: str) -> None:
    result = analyze_bytes(
        _ubl(profile=EN_PROFILE, issue_date=issue_date),
        "synthetisches-ubl-datumsformat.xml",
        "application/xml",
        run_official_validation=False,
    )

    findings = _finding_by_id(result, "internal")
    assert result["document"]["issue_date"] is None
    assert result["assessment"]["internal"]["status"] == "errors"
    assert findings["FORMAT-DATE-001"]["actual"]["value"] == issue_date
    assert "CHECK-000" not in findings


@pytest.mark.parametrize("issue_date", ["2026-07-31", "2026-07-31Z", "2026-07-31+02:00"])
def test_ubl_issue_date_accepts_xsd_date_timezone_forms(issue_date: str) -> None:
    result = analyze_bytes(
        _ubl(profile=EN_PROFILE, issue_date=issue_date),
        "synthetisches-ubl-datum.xml",
        "application/xml",
        run_official_validation=False,
    )

    findings = _finding_by_id(result, "internal")
    assert result["document"]["issue_date"] == "2026-07-31"
    assert "FORMAT-DATE-001" not in findings


@pytest.mark.parametrize("identifier", ["42", "0", "A-42", None])
def test_line_finding_keeps_array_index_separate_from_business_identifier(identifier: str | None) -> None:
    result = analyze_bytes(
        _ubl(profile=EN_PROFILE, invoice_lines=_invoice_line(identifier)),
        "synthetische-position.xml",
        "application/xml",
        run_official_validation=False,
    )

    occurrence = _finding_by_id(result, "internal")["LINE-004"]["occurrence"]
    assert occurrence == {
        "scope": "line",
        "index": 0,
        "identifier": identifier,
        "json_pointer": "/lines/0",
    }


@pytest.mark.parametrize("path", ["/api/analyze", "/api/report", "/api/report/pdf"])
def test_analysis_endpoints_return_sanitized_422_for_overlong_public_value(path: str) -> None:
    overlong_id = "X" * 1001
    response = TestClient(app).post(
        path,
        files={
            "file": (
                "synthetische-zu-lange-kennung.xml",
                _ubl(profile=EN_PROFILE, document_id=overlong_id),
                "application/xml",
            )
        },
        data={"official": "false"},
    )

    assert response.status_code == 422
    assert response.json()["type"] == "invoice_input_error"
    assert overlong_id not in response.text
    assert "validation error" not in response.text.casefold()


def test_public_value_at_declared_length_boundary_remains_exact() -> None:
    document_id = "X" * 1000
    response = TestClient(app).post(
        "/api/analyze",
        files={
            "file": (
                "synthetische-grenzwert-kennung.xml",
                _ubl(profile=EN_PROFILE, document_id=document_id),
                "application/xml",
            )
        },
        data={"official": "false"},
    )

    assert response.status_code == 200
    assert response.json()["document"]["id"] == document_id


@pytest.mark.parametrize("path", ["/api/analyze", "/api/report", "/api/report/pdf"])
def test_analysis_endpoints_process_large_finite_decimals_without_http_500(path: str) -> None:
    amount = f"{'9' * 128}.00"
    response = TestClient(app, raise_server_exceptions=False).post(
        path,
        files={
            "file": (
                "synthetischer-grosser-betrag.xml",
                _ubl_with_large_consistent_amount(amount),
                "application/xml",
            )
        },
        data={"official": "false"},
    )

    assert response.status_code == 200
    if path == "/api/analyze":
        payload = response.json()
        assert payload["lines"][0]["price"]["net"]["value"] == amount
        assert payload["totals"]["payable"]["value"] == amount


def test_analyze_handles_three_wide_decimal_operands_with_a_controlled_finding() -> None:
    operand = "9" * 100
    base_quantity = f".{('0' * 99)}1"
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/analyze",
        files={
            "file": (
                "synthetische-dreifach-breite-dezimalwerte.xml",
                _ubl(
                    profile=EN_PROFILE,
                    invoice_lines=_invoice_line(
                        "1",
                        quantity=operand,
                        price=operand,
                        base_quantity=base_quantity,
                    ),
                ),
                "application/xml",
            )
        },
        data={"official": "false"},
    )

    assert response.status_code == 200
    findings = _finding_by_id(response.json(), "internal")
    assert "CALC-LINE-001" in findings
    expected = findings["CALC-LINE-001"]["expected"]["value"]
    assert expected.endswith(".00")
    assert len(expected.partition(".")[0]) == 300


@pytest.mark.parametrize("path", ["/api/analyze", "/api/report", "/api/report/pdf"])
def test_analysis_endpoints_reject_oversized_decimal_operands_without_expanding_the_context(path: str) -> None:
    amount = "9" * (MAX_DECIMAL_DIGITS + 1)
    response = TestClient(app, raise_server_exceptions=False).post(
        path,
        files={
            "file": (
                "synthetischer-zu-grosser-betrag.xml",
                _ubl(profile=EN_PROFILE, payable=amount),
                "application/xml",
            )
        },
        data={"official": "false"},
    )

    assert response.status_code == 422
    assert response.json()["type"] == "invoice_input_error"
    assert str(MAX_DECIMAL_DIGITS) in response.json()["detail"]
    assert amount not in response.text


@pytest.mark.parametrize(
    ("limit_reason", "expected_code", "message_fragment"),
    [
        ("rows", "TECHNICAL-FIELDS-TRUNCATED", "Zeilengrenze"),
        ("time", "TECHNICAL-TIME-BUDGET-EXCEEDED", "Zeitbudget"),
    ],
)
def test_technical_limit_reason_is_exposed_as_processing_limitation_only(
    monkeypatch: pytest.MonkeyPatch,
    limit_reason: str,
    expected_code: str,
    message_fragment: str,
) -> None:
    monkeypatch.setattr(
        "app.analyzer.technical_rows",
        lambda *_args, **_kwargs: TechnicalRowsResult(
            rows=[],
            truncated=True,
            limit_reason=limit_reason,
        ),
    )

    result = analyze_bytes(
        _ubl(profile=EN_PROFILE),
        "synthetisch-begrenzt.xml",
        "application/xml",
        run_official_validation=False,
    )

    processing = result["assessment"]["processing"]
    assert processing["status"] == "limited"
    assert len(processing["limitations"]) == 1
    limitation = processing["limitations"][0]
    assert limitation["code"] == expected_code
    assert limitation["affected_json_pointer"] == "/technical/fields"
    assert message_fragment in limitation["message"]
    assert "limit_reason" not in result["technical"]


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


def test_kosit_receives_original_xml_bytes_after_structure_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    split_at = len(XRECHNUNG_PROFILE) // 2
    shown_profile = (
        XRECHNUNG_PROFILE[:split_at]
        + "<!-- synthetischer Kommentar -->"
        + "<?synthetic test?>"
        + XRECHNUNG_PROFILE[split_at:]
    )
    payload = (
        _ubl(profile=shown_profile)
        .replace(b"\n", b"\r\n")
        .replace(
            b"SYNTHETISCH-380",
            b"SYNTHETISCH-&#51;80",
        )
    )
    received: list[tuple[bytes, str]] = []

    def capture_original_bytes(_self, xml: bytes, filename: str) -> dict:
        received.append((xml, filename))
        return {
            "configured": True,
            "executed": True,
            "accepted": True,
            "exit_code": 0,
            "summary": "Synthetische KoSIT-Annahme.",
            "findings": [],
            "raw_report": "<rep:report xmlns:rep='http://www.xoev.de/de/validator/varl/1'><rep:assessment><rep:accept/></rep:assessment></rep:report>",
            "technical_output": None,
            "report_source": "varl",
        }

    monkeypatch.setattr("app.analyzer.KositValidator.validate", capture_original_bytes)

    result = analyze_bytes(
        payload,
        "synthetische-originalbytes.xml",
        "application/xml",
        run_official_validation=True,
        app_settings=replace(settings, kosit_enabled=True),
    )

    assert received == [(payload, "synthetische-originalbytes.xml")]
    assert result["profile"]["id"] == XRECHNUNG_PROFILE
    assert result["assessment"]["official"]["executed"] is True


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
