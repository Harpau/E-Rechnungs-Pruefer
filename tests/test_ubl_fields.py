from __future__ import annotations

import pytest

from app.parsers.ubl import parse_ubl
from app.xml_utils import safe_parse_xml

UBL_INVOICE_NAMESPACE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
UBL_CREDIT_NOTE_NAMESPACE = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
UBL_CAC_NAMESPACE = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
UBL_CBC_NAMESPACE = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"


def _parse(body: str, root_kind: str = "Invoice") -> dict:
    namespace = UBL_CREDIT_NOTE_NAMESPACE if root_kind == "CreditNote" else UBL_INVOICE_NAMESPACE
    xml = f"""
    <{root_kind}
        xmlns="{namespace}"
        xmlns:cac="{UBL_CAC_NAMESPACE}"
        xmlns:cbc="{UBL_CBC_NAMESPACE}">
      {body}
    </{root_kind}>
    """
    return parse_ubl(safe_parse_xml(xml.encode()))


def test_parties_keep_legal_form_three_address_lines_and_tax_representative_role():
    result = _parse(
        """
        <cac:AccountingSupplierParty>
          <cac:Party>
            <cbc:MarkCareIndicator>true</cbc:MarkCareIndicator>
            <cac:PostalAddress>
              <cbc:StreetName>Erste Adresszeile</cbc:StreetName>
              <cbc:AdditionalStreetName>Zweite Adresszeile</cbc:AdditionalStreetName>
              <cac:AddressLine><cbc:Line>Dritte Adresszeile</cbc:Line></cac:AddressLine>
              <cbc:CityName>Musterstadt</cbc:CityName>
              <cac:Country><cbc:IdentificationCode>DE</cbc:IdentificationCode></cac:Country>
            </cac:PostalAddress>
            <cac:PartyLegalEntity>
              <cbc:RegistrationName>Synthetischer Lieferant</cbc:RegistrationName>
              <cbc:CompanyLegalForm>GmbH &amp; Co. KG</cbc:CompanyLegalForm>
            </cac:PartyLegalEntity>
          </cac:Party>
        </cac:AccountingSupplierParty>
        <cac:TaxRepresentativeParty>
          <cac:PartyLegalEntity>
            <cbc:RegistrationName>Synthetische Steuervertretung</cbc:RegistrationName>
          </cac:PartyLegalEntity>
        </cac:TaxRepresentativeParty>
        """
    )

    assert result["seller"]["description"] == "GmbH & Co. KG"
    assert result["seller"]["address"] == {
        "line1": "Erste Adresszeile",
        "line2": "Zweite Adresszeile",
        "line3": "Dritte Adresszeile",
        "postcode": None,
        "city": "Musterstadt",
        "subdivision": None,
        "country_code": "DE",
        "country": "DE – Deutschland",
    }
    assert result["seller_tax_representative"]["name"] == "Synthetische Steuervertretung"
    assert result["invoicee"]["name"] is None


def test_party_and_legal_registration_identifiers_remain_separate() -> None:
    result = _parse(
        """
        <cac:AccountingSupplierParty>
          <cac:Party>
            <cac:PartyIdentification>
              <cbc:ID schemeID="GLN">4000001000005</cbc:ID>
            </cac:PartyIdentification>
            <cac:PartyLegalEntity>
              <cbc:RegistrationName>Synthetischer Lieferant</cbc:RegistrationName>
              <cbc:CompanyID schemeID="HRB">HRB-4711</cbc:CompanyID>
            </cac:PartyLegalEntity>
          </cac:Party>
        </cac:AccountingSupplierParty>
        """
    )

    assert result["seller"]["ids"] == [{"value": "4000001000005", "scheme": "GLN"}]
    assert result["seller"]["legal_registration_ids"] == [{"value": "HRB-4711", "scheme": "HRB"}]
    assert result["buyer"]["legal_registration_ids"] == []


def test_delivery_location_maps_identifier_address_and_recipient():
    result = _parse(
        """
        <cac:Delivery>
          <cbc:ActualDeliveryDate>2026-07-20</cbc:ActualDeliveryDate>
          <cac:DeliveryLocation>
            <cbc:ID schemeID="GLN">4000001000005</cbc:ID>
            <cac:Address>
              <cbc:StreetName>Lieferweg 1</cbc:StreetName>
              <cbc:AdditionalStreetName>Tor 2</cbc:AdditionalStreetName>
              <cac:AddressLine><cbc:Line>Halle 3</cbc:Line></cac:AddressLine>
              <cbc:PostalZone>12345</cbc:PostalZone>
              <cbc:CityName>Lieferstadt</cbc:CityName>
              <cbc:CountrySubentity>DE-BE</cbc:CountrySubentity>
              <cac:Country><cbc:IdentificationCode>DE</cbc:IdentificationCode></cac:Country>
            </cac:Address>
          </cac:DeliveryLocation>
          <cac:DeliveryParty>
            <cac:PartyName><cbc:Name>Synthetischer Warenempfänger</cbc:Name></cac:PartyName>
          </cac:DeliveryParty>
        </cac:Delivery>
        """
    )

    assert result["delivery"]["location_id"] == {"value": "4000001000005", "scheme": "GLN"}
    assert result["delivery"]["address"]["line3"] == "Halle 3"
    assert result["ship_to"]["name"] == "Synthetischer Warenempfänger"
    assert all(value is None for value in result["ship_to"]["address"].values())


def test_credit_note_due_date_and_payment_identifiers_keep_their_semantics():
    result = _parse(
        """
        <cac:PaymentMeans>
          <cbc:PaymentMeansCode>59</cbc:PaymentMeansCode>
          <cbc:PaymentDueDate>2026-08-15</cbc:PaymentDueDate>
          <cac:PayeeFinancialAccount>
            <cbc:ID schemeID="LOCAL">KONTO-4711</cbc:ID>
            <cbc:Name>Synthetisches Verrechnungskonto</cbc:Name>
            <cac:FinancialInstitutionBranch>
              <cbc:ID schemeID="BANK">INSTITUT-42</cbc:ID>
            </cac:FinancialInstitutionBranch>
          </cac:PayeeFinancialAccount>
          <cac:PaymentMandate>
            <cbc:ID>MANDAT-1</cbc:ID>
            <cac:PayerParty>
              <cac:PartyIdentification><cbc:ID schemeID="SEPA">GLAEUBIGER-1</cbc:ID></cac:PartyIdentification>
            </cac:PayerParty>
            <cac:PayerFinancialAccount>
              <cbc:ID schemeID="LOCAL">BELASTUNG-9</cbc:ID>
            </cac:PayerFinancialAccount>
          </cac:PaymentMandate>
          <cac:CardAccount>
            <cbc:PrimaryAccountNumberID>1234</cbc:PrimaryAccountNumberID>
            <cbc:HolderName>Synthetische Karteninhaberin</cbc:HolderName>
          </cac:CardAccount>
        </cac:PaymentMeans>
        """,
        root_kind="CreditNote",
    )

    means = result["payment"]["means"][0]
    assert result["document"]["due_date"] == "2026-08-15"
    assert means["account_id"] == {"value": "KONTO-4711", "scheme": "LOCAL"}
    assert means["iban"] is None
    assert means["service_provider_id"] == {"value": "INSTITUT-42", "scheme": "BANK"}
    assert means["bic"] is None
    assert means["debited_account_id"] == {"value": "BELASTUNG-9", "scheme": "LOCAL"}
    assert means["payer_iban"] is None
    assert means["creditor_id"] == {"value": "GLAEUBIGER-1", "scheme": "SEPA"}
    assert means["card_holder_name"] == "Synthetische Karteninhaberin"


def test_price_discount_and_gross_price_are_not_line_allowances():
    result = _parse(
        """
        <cac:InvoiceLine>
          <cbc:ID>1</cbc:ID>
          <cbc:InvoicedQuantity unitCode="C62">1</cbc:InvoicedQuantity>
          <cbc:LineExtensionAmount currencyID="EUR">100.00</cbc:LineExtensionAmount>
          <cac:Item><cbc:Name>Synthetische Leistung</cbc:Name></cac:Item>
          <cac:Price>
            <cbc:PriceAmount currencyID="EUR">100.00</cbc:PriceAmount>
            <cac:AllowanceCharge>
              <cbc:ChargeIndicator>false</cbc:ChargeIndicator>
              <cbc:Amount currencyID="EUR">20.00</cbc:Amount>
              <cbc:BaseAmount currencyID="EUR">120.00</cbc:BaseAmount>
            </cac:AllowanceCharge>
          </cac:Price>
        </cac:InvoiceLine>
        """
    )

    line = result["lines"][0]
    assert line["price"] == "100.00"
    assert line["gross_price"] == "120.00"
    assert line["gross_price_currency"] == "EUR"
    assert line["price_allowance"] == "20.00"
    assert line["price_allowance_currency"] == "EUR"
    assert line["allowances_charges"] == []


def test_explicit_gross_price_is_supported_when_present():
    result = _parse(
        """
        <cac:InvoiceLine>
          <cbc:ID>1</cbc:ID>
          <cac:Price>
            <cbc:PriceAmount currencyID="EUR">100.00</cbc:PriceAmount>
            <cac:GrossPrice>
              <cbc:PriceAmount currencyID="EUR">125.00</cbc:PriceAmount>
            </cac:GrossPrice>
          </cac:Price>
        </cac:InvoiceLine>
        """
    )

    assert result["lines"][0]["gross_price"] == "125.00"
    assert result["lines"][0]["gross_price_currency"] == "EUR"


def test_header_allowance_charge_keeps_unknown_indicator_and_tax_fields():
    result = _parse(
        """
        <cac:AllowanceCharge>
          <cbc:ChargeIndicator>vielleicht</cbc:ChargeIndicator>
          <cbc:Amount currencyID="EUR">10.00</cbc:Amount>
          <cac:TaxCategory>
            <cbc:ID>S</cbc:ID>
            <cbc:Percent>19</cbc:Percent>
            <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
          </cac:TaxCategory>
        </cac:AllowanceCharge>
        """
    )

    adjustment = result["header_allowances_charges"][0]
    assert adjustment["type"] == "unknown"
    assert adjustment["type_label"] == "Unbekannt"
    assert adjustment["indicator_raw"] == "vielleicht"
    assert adjustment["tax_category"] == "S"
    assert adjustment["tax_rate"] == "19"
    assert adjustment["tax_type"] == "VAT"


def test_tax_totals_are_selected_by_currency_without_inventing_subtotal_amounts():
    result = _parse(
        """
        <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
        <cbc:TaxCurrencyCode>USD</cbc:TaxCurrencyCode>
        <cac:TaxTotal>
          <cbc:TaxAmount currencyID="USD">20.50</cbc:TaxAmount>
        </cac:TaxTotal>
        <cac:TaxTotal>
          <cbc:TaxAmount currencyID="EUR">19.00</cbc:TaxAmount>
          <cac:TaxSubtotal>
            <cbc:TaxableAmount currencyID="EUR">100.00</cbc:TaxableAmount>
            <cbc:TaxAmount currencyID="EUR">19.00</cbc:TaxAmount>
            <cac:TaxCategory>
              <cbc:ID>S</cbc:ID>
              <cbc:Percent>19</cbc:Percent>
              <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
            </cac:TaxCategory>
          </cac:TaxSubtotal>
          <cac:TaxSubtotal>
            <cbc:TaxableAmount currencyID="EUR">50.00</cbc:TaxableAmount>
            <cac:TaxCategory>
              <cbc:ID>Z</cbc:ID>
              <cbc:Percent>0</cbc:Percent>
              <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
            </cac:TaxCategory>
          </cac:TaxSubtotal>
        </cac:TaxTotal>
        """
    )

    assert result["document"]["tax_currency"] == "USD"
    assert result["totals"]["tax_total"] == "19.00"
    assert result["totals"]["tax_total_currency"] == "EUR"
    assert result["totals"]["tax_total_accounting"] == "20.50"
    assert result["totals"]["tax_total_accounting_currency"] == "USD"
    assert len(result["taxes"]) == 2
    assert result["taxes"][1]["tax_amount"] is None
    assert result["taxes"][1]["tax_currency"] is None


def test_tax_total_without_subtotal_does_not_create_a_tax_breakdown():
    result = _parse(
        """
        <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
        <cac:TaxTotal>
          <cbc:TaxAmount currencyID="EUR">19.00</cbc:TaxAmount>
        </cac:TaxTotal>
        """
    )

    assert result["totals"]["tax_total"] == "19.00"
    assert result["taxes"] == []


def test_references_line_note_object_identifier_and_invoice_period_are_mapped():
    result = _parse(
        """
        <cbc:AccountingCost>KST-42</cbc:AccountingCost>
        <cac:InvoicePeriod>
          <cbc:StartDate>2026-07-01</cbc:StartDate>
          <cbc:EndDate>2026-07-31</cbc:EndDate>
          <cbc:Description>Abrechnungsmonat Juli</cbc:Description>
          <cbc:DescriptionCode>35</cbc:DescriptionCode>
        </cac:InvoicePeriod>
        <cac:OriginatorDocumentReference>
          <cbc:ID>AUSSCHREIBUNG-1</cbc:ID>
        </cac:OriginatorDocumentReference>
        <cac:AdditionalDocumentReference>
          <cbc:ID schemeID="ABT">OBJEKT-18</cbc:ID>
          <cbc:DocumentTypeCode>130</cbc:DocumentTypeCode>
        </cac:AdditionalDocumentReference>
        <cac:AdditionalDocumentReference>
          <cbc:ID>ANLAGE-1</cbc:ID>
          <cbc:DocumentTypeCode>916</cbc:DocumentTypeCode>
        </cac:AdditionalDocumentReference>
        <cac:InvoiceLine>
          <cbc:ID>1</cbc:ID>
          <cbc:Note>Synthetischer Positionshinweis</cbc:Note>
          <cac:DocumentReference>
            <cbc:ID schemeID="OBJ">OBJEKT-128</cbc:ID>
          </cac:DocumentReference>
        </cac:InvoiceLine>
        """
    )

    assert result["invoice_period"] == {
        "start": "2026-07-01",
        "end": "2026-07-31",
        "description": "Abrechnungsmonat Juli",
    }
    assert result["document"]["tax_point_date_code"] == "35"
    assert result["references"]["tender"] == "AUSSCHREIBUNG-1"
    assert result["references"]["invoiced_object"] == {"value": "OBJEKT-18", "scheme": "ABT"}
    assert result["references"]["buyer_accounting_reference"] == "KST-42"
    assert [item["id"] for item in result["references"]["additional_documents"]] == [
        {"value": "ANLAGE-1", "scheme": None}
    ]
    assert result["lines"][0]["notes"] == ["Synthetischer Positionshinweis"]
    assert result["lines"][0]["object_identifier"] == {"value": "OBJEKT-128", "scheme": "OBJ"}


@pytest.mark.parametrize("root_kind", ["Invoice", "CreditNote"])
def test_invoice_period_description_code_is_bt_8_not_a_period_description(root_kind: str):
    result = _parse(
        """
        <cac:InvoicePeriod>
          <cbc:DescriptionCode>3</cbc:DescriptionCode>
        </cac:InvoicePeriod>
        """,
        root_kind=root_kind,
    )

    assert result["document"]["tax_point_date_code"] == "3"
    assert result["invoice_period"] is None


@pytest.mark.parametrize("root_kind", ["Invoice", "CreditNote"])
def test_document_notes_split_the_conforming_ubl_subject_prefix(root_kind: str):
    result = _parse(
        """
        <cbc:Note>#AAI#Allgemeine Information</cbc:Note>
        <cbc:Note>Hinweis ohne Betreffcode</cbc:Note>
        """,
        root_kind=root_kind,
    )

    assert result["document"]["notes"] == [
        {"text": "Allgemeine Information", "subject_code": "AAI"},
        {"text": "Hinweis ohne Betreffcode", "subject_code": None},
    ]


def test_non_conforming_document_note_subject_patterns_remain_unstructured():
    result = _parse(
        """
        <cbc:Note>#AA#Zu kurzer Code</cbc:Note>
        <cbc:Note>#aai#Code nicht in Großbuchstaben</cbc:Note>
        <cbc:Note>Text vor #AAI# dem Code</cbc:Note>
        <cbc:Note>#AAI#</cbc:Note>
        """
    )

    assert result["document"]["notes"] == [
        {"text": "#AA#Zu kurzer Code", "subject_code": None},
        {"text": "#aai#Code nicht in Großbuchstaben", "subject_code": None},
        {"text": "Text vor #AAI# dem Code", "subject_code": None},
        {"text": "#AAI#", "subject_code": None},
    ]


def test_preceding_invoice_keeps_issue_date_and_supporting_document_id_scheme():
    result = _parse(
        """
        <cac:BillingReference>
          <cac:InvoiceDocumentReference>
            <cbc:ID>VORGAENGER-1</cbc:ID>
            <cbc:IssueDate>2026-06-30</cbc:IssueDate>
          </cac:InvoiceDocumentReference>
        </cac:BillingReference>
        <cac:AdditionalDocumentReference>
          <cbc:ID schemeID="LOCAL">ANLAGE-2</cbc:ID>
          <cbc:DocumentTypeCode>916</cbc:DocumentTypeCode>
        </cac:AdditionalDocumentReference>
        """
    )

    assert result["references"]["preceding_invoice_documents"] == [{"id": "VORGAENGER-1", "issue_date": "2026-06-30"}]
    assert result["references"]["additional_documents"][0]["id"] == {
        "value": "ANLAGE-2",
        "scheme": "LOCAL",
    }
