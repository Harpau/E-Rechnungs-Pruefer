from __future__ import annotations

from fastapi.testclient import TestClient

from app.analyzer import analyze_bytes
from app.main import app

EN_PROFILE = "urn:cen.eu:en16931:2017"
client = TestClient(app)


def _invoice(delivery_party: str = "") -> bytes:
    return f"""
    <Invoice
      xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
      xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
      xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2">
      <cbc:CustomizationID>{EN_PROFILE}</cbc:CustomizationID>
      <cbc:ID>SYNTHETISCH-LIEFERUNG</cbc:ID>
      <cbc:IssueDate>2026-07-31</cbc:IssueDate>
      <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
      <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
      <cbc:Note>#AAI#Synthetischer Rechnungshinweis</cbc:Note>
      <cac:InvoicePeriod>
        <cbc:StartDate>2026-07-01</cbc:StartDate>
        <cbc:EndDate>2026-07-31</cbc:EndDate>
        <cbc:DescriptionCode>35</cbc:DescriptionCode>
        <cbc:Description>Synthetischer Abrechnungszeitraum</cbc:Description>
      </cac:InvoicePeriod>
      <cac:AccountingSupplierParty>
        <cac:Party>
          <cac:PartyIdentification><cbc:ID schemeID="GLN">4000001000005</cbc:ID></cac:PartyIdentification>
          <cac:PartyLegalEntity>
            <cbc:RegistrationName>Synthetischer Verkäufer</cbc:RegistrationName>
            <cbc:CompanyID schemeID="HRB">HRB-4711</cbc:CompanyID>
          </cac:PartyLegalEntity>
        </cac:Party>
      </cac:AccountingSupplierParty>
      <cac:AccountingCustomerParty>
        <cac:Party><cac:PartyLegalEntity><cbc:RegistrationName>Synthetischer Käufer</cbc:RegistrationName></cac:PartyLegalEntity></cac:Party>
      </cac:AccountingCustomerParty>
      <cac:Delivery>
        <cbc:ActualDeliveryDate>2026-07-20</cbc:ActualDeliveryDate>
        <cac:DeliveryLocation>
          <cbc:ID schemeID="GLN">4000001000005</cbc:ID>
          <cac:Address>
            <cbc:StreetName>Lieferweg 1</cbc:StreetName>
            <cbc:PostalZone>12345</cbc:PostalZone>
            <cbc:CityName>Lieferstadt</cbc:CityName>
            <cac:Country><cbc:IdentificationCode>DE</cbc:IdentificationCode></cac:Country>
          </cac:Address>
        </cac:DeliveryLocation>
        {delivery_party}
      </cac:Delivery>
      <cac:LegalMonetaryTotal>
        <cbc:PayableAmount currencyID="EUR">0.00</cbc:PayableAmount>
      </cac:LegalMonetaryTotal>
    </Invoice>
    """.encode()


def _analyze(delivery_party: str = "") -> dict:
    return analyze_bytes(
        _invoice(delivery_party),
        "synthetische-lieferung.xml",
        "application/xml",
        run_official_validation=False,
    )


def test_delivery_location_is_not_invented_as_a_delivery_recipient() -> None:
    result = _analyze()

    assert result["delivery"] == {
        "actual_date": "2026-07-20",
        "location": {
            "id": {"value": "4000001000005", "scheme_id": "GLN"},
            "postal_address": {
                "line1": "Lieferweg 1",
                "line2": None,
                "line3": None,
                "postcode": "12345",
                "city": "Lieferstadt",
                "subdivision": None,
                "country": {
                    "value": "DE",
                    "label": "DE – Deutschland",
                    "list_id": "ISO3166-1",
                },
            },
        },
    }
    assert result["parties"]["delivery_recipient"] is None
    assert result["periods"]["delivery"] is None


def test_real_delivery_party_remains_separate_from_the_delivery_location() -> None:
    party = """
    <cac:DeliveryParty>
      <cac:PartyName><cbc:Name>Synthetischer Warenempfänger</cbc:Name></cac:PartyName>
    </cac:DeliveryParty>
    """
    result = _analyze(party)

    assert result["parties"]["delivery_recipient"]["legal_name"] == "Synthetischer Warenempfänger"
    assert result["parties"]["delivery_recipient"]["postal_address"] is None
    assert result["delivery"]["location"]["postal_address"]["city"] == "Lieferstadt"


def test_tax_point_code_note_subject_and_party_identifier_kinds_are_structured() -> None:
    result = _analyze()

    assert result["document"]["tax_point_date_code"] == {
        "value": "35",
        "label": None,
        "list_id": "UNCL2005",
    }
    assert result["document"]["notes"] == [
        {
            "text": "Synthetischer Rechnungshinweis",
            "subject_code": {
                "value": "AAI",
                "label": None,
                "list_id": "UNCL4451",
            },
        }
    ]
    assert result["periods"]["invoice"] == {
        "start_date": "2026-07-01",
        "end_date": "2026-07-31",
        "description": "Synthetischer Abrechnungszeitraum",
    }
    assert result["parties"]["seller"]["identifiers"] == [
        {
            "kind": "party",
            "identifier": {"value": "4000001000005", "scheme_id": "GLN"},
        },
        {
            "kind": "legal-registration",
            "identifier": {"value": "HRB-4711", "scheme_id": "HRB"},
        },
    ]


def test_html_report_labels_delivery_location_note_subject_and_registration_id() -> None:
    response = client.post(
        "/api/report",
        files={
            "file": (
                "synthetische-lieferung.xml",
                _invoice(),
                "application/xml",
            )
        },
        data={"official": "false"},
    )

    assert response.status_code == 200
    assert "Tatsächliches Lieferdatum (BT-72)" in response.text
    assert "Kennung des Lieferorts (BT-71)" in response.text
    assert "4000001000005 (GLN)" in response.text
    assert "Registerkennung" in response.text
    assert "AAI" in response.text
