from __future__ import annotations

from app.parsers.cii import parse_cii
from app.xml_utils import safe_parse_xml

CII_NAMESPACE = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
RAM_NAMESPACE = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
UDT_NAMESPACE = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"
QDT_NAMESPACE = "urn:un:unece:uncefact:data:standard:QualifiedDataType:100"


def _parse(body: str) -> dict:
    xml = f"""
    <rsm:CrossIndustryInvoice
        xmlns:rsm="{CII_NAMESPACE}"
        xmlns:ram="{RAM_NAMESPACE}"
        xmlns:udt="{UDT_NAMESPACE}"
        xmlns:qdt="{QDT_NAMESPACE}">
      <rsm:ExchangedDocumentContext/>
      <rsm:ExchangedDocument/>
      <rsm:SupplyChainTradeTransaction>
        {body}
      </rsm:SupplyChainTradeTransaction>
    </rsm:CrossIndustryInvoice>
    """
    return parse_cii(safe_parse_xml(xml.encode()))


def test_parties_do_not_replace_legal_name_with_trading_name_and_keep_tax_representative():
    result = _parse(
        """
        <ram:ApplicableHeaderTradeAgreement>
          <ram:SellerTradeParty>
            <ram:SpecifiedLegalOrganization>
              <ram:TradingBusinessName>Synthetischer Handelsname</ram:TradingBusinessName>
            </ram:SpecifiedLegalOrganization>
          </ram:SellerTradeParty>
          <ram:SellerTaxRepresentativeTradeParty>
            <ram:Name>Synthetische Steuervertretung</ram:Name>
          </ram:SellerTaxRepresentativeTradeParty>
        </ram:ApplicableHeaderTradeAgreement>
        """
    )

    assert result["seller"]["name"] is None
    assert result["seller"]["trading_name"] == "Synthetischer Handelsname"
    assert result["seller_tax_representative"]["name"] == "Synthetische Steuervertretung"


def test_party_and_legal_registration_identifiers_remain_separate():
    result = _parse(
        """
        <ram:ApplicableHeaderTradeAgreement>
          <ram:SellerTradeParty>
            <ram:ID schemeID="PARTY">PARTY-4711</ram:ID>
            <ram:GlobalID schemeID="0088">4000001123452</ram:GlobalID>
            <ram:SpecifiedLegalOrganization>
              <ram:ID schemeID="HRB">HRB 12345</ram:ID>
              <ram:GlobalID schemeID="0204">LEI-SYNTHETIC-1</ram:GlobalID>
            </ram:SpecifiedLegalOrganization>
          </ram:SellerTradeParty>
        </ram:ApplicableHeaderTradeAgreement>
        """
    )

    assert result["seller"]["ids"] == [
        {"value": "PARTY-4711", "scheme": "PARTY"},
        {"value": "4000001123452", "scheme": "0088"},
    ]
    assert result["seller"]["legal_registration_ids"] == [
        {"value": "HRB 12345", "scheme": "HRB"},
        {"value": "LEI-SYNTHETIC-1", "scheme": "0204"},
    ]


def test_missing_cii_party_has_empty_legal_registration_identifiers():
    result = _parse("")

    assert result["seller"]["legal_registration_ids"] == []
    assert result["buyer"]["legal_registration_ids"] == []


def test_tax_point_period_and_totals_preserve_their_currency_semantics():
    result = _parse(
        """
        <ram:ApplicableHeaderTradeSettlement>
          <ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>
          <ram:TaxCurrencyCode>USD</ram:TaxCurrencyCode>
          <ram:ApplicableTradeTax>
            <ram:CalculatedAmount currencyID="EUR">19.00</ram:CalculatedAmount>
            <ram:TypeCode>VAT</ram:TypeCode>
            <ram:BasisAmount currencyID="EUR">100.00</ram:BasisAmount>
            <ram:CategoryCode>S</ram:CategoryCode>
            <ram:DueDateTypeCode>5</ram:DueDateTypeCode>
            <ram:RateApplicablePercent>19</ram:RateApplicablePercent>
            <ram:TaxPointDate>
              <udt:DateString format="102">20260731</udt:DateString>
            </ram:TaxPointDate>
          </ram:ApplicableTradeTax>
          <ram:BillingSpecifiedPeriod>
            <ram:StartDateTime>
              <udt:DateTimeString format="102">20260701</udt:DateTimeString>
            </ram:StartDateTime>
            <ram:EndDateTime>
              <udt:DateTimeString format="102">20260731</udt:DateTimeString>
            </ram:EndDateTime>
          </ram:BillingSpecifiedPeriod>
          <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
            <ram:LineTotalAmount currencyID="EUR">100.00</ram:LineTotalAmount>
            <ram:AllowanceTotalAmount currencyID="EUR">2.00</ram:AllowanceTotalAmount>
            <ram:ChargeTotalAmount currencyID="EUR">3.00</ram:ChargeTotalAmount>
            <ram:TaxBasisTotalAmount currencyID="EUR">101.00</ram:TaxBasisTotalAmount>
            <ram:TaxTotalAmount currencyID="USD">20.50</ram:TaxTotalAmount>
            <ram:TaxTotalAmount currencyID="EUR">19.19</ram:TaxTotalAmount>
            <ram:GrandTotalAmount currencyID="EUR">120.19</ram:GrandTotalAmount>
            <ram:TotalPrepaidAmount currencyID="EUR">10.00</ram:TotalPrepaidAmount>
            <ram:RoundingAmount currencyID="EUR">0.01</ram:RoundingAmount>
            <ram:DuePayableAmount currencyID="EUR">110.20</ram:DuePayableAmount>
          </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        </ram:ApplicableHeaderTradeSettlement>
        """
    )

    assert result["document"]["tax_point_date"] == "2026-07-31"
    assert result["document"]["tax_point_date_code"] == "5"
    assert result["document"]["vat_accounting_currency"] == "USD"
    assert result["invoice_period"] == {
        "start": "2026-07-01",
        "end": "2026-07-31",
        "description": None,
    }
    assert result["totals"]["tax_total"] == "19.19"
    assert result["totals"]["tax_total_currency"] == "EUR"
    assert result["totals"]["tax_total_accounting"] == "20.50"
    assert result["totals"]["tax_total_accounting_currency"] == "USD"
    assert result["totals"]["line_total_currency"] == "EUR"
    assert result["totals"]["allowance_total_currency"] == "EUR"
    assert result["totals"]["charge_total_currency"] == "EUR"
    assert result["totals"]["tax_basis_total_currency"] == "EUR"
    assert result["totals"]["grand_total_currency"] == "EUR"
    assert result["totals"]["prepaid_amount_currency"] == "EUR"
    assert result["totals"]["rounding_amount_currency"] == "EUR"
    assert result["totals"]["due_payable_amount_currency"] == "EUR"


def test_xsd_date_choices_and_indicator_string_are_parsed_from_direct_children() -> None:
    xml = f"""
    <rsm:CrossIndustryInvoice
        xmlns:rsm="{CII_NAMESPACE}"
        xmlns:ram="{RAM_NAMESPACE}"
        xmlns:udt="{UDT_NAMESPACE}"
        xmlns:qdt="{QDT_NAMESPACE}">
      <rsm:ExchangedDocumentContext/>
      <rsm:ExchangedDocument>
        <ram:ID>SYNTHETISCH-DATUM</ram:ID>
        <ram:IssueDateTime><udt:DateTime>2026-07-15T08:30:00Z</udt:DateTime></ram:IssueDateTime>
      </rsm:ExchangedDocument>
      <rsm:SupplyChainTradeTransaction>
        <ram:ApplicableHeaderTradeDelivery>
          <ram:ActualDeliverySupplyChainEvent>
            <ram:OccurrenceDateTime>
              <udt:DateTimeString format="102">20260716</udt:DateTimeString>
            </ram:OccurrenceDateTime>
          </ram:ActualDeliverySupplyChainEvent>
        </ram:ApplicableHeaderTradeDelivery>
        <ram:ApplicableHeaderTradeSettlement>
          <ram:ApplicableTradeTax>
            <ram:TaxPointDate><udt:Date>2026-07-17</udt:Date></ram:TaxPointDate>
          </ram:ApplicableTradeTax>
          <ram:SpecifiedTradeAllowanceCharge>
            <ram:ChargeIndicator><udt:IndicatorString>false</udt:IndicatorString></ram:ChargeIndicator>
            <ram:ActualAmount>1.00</ram:ActualAmount>
          </ram:SpecifiedTradeAllowanceCharge>
        </ram:ApplicableHeaderTradeSettlement>
      </rsm:SupplyChainTradeTransaction>
    </rsm:CrossIndustryInvoice>
    """

    result = parse_cii(safe_parse_xml(xml.encode()))

    assert result["document"]["issue_date"] == "2026-07-15"
    assert result["document"]["delivery_date"] == "2026-07-16"
    assert result["document"]["tax_point_date"] == "2026-07-17"
    assert result["header_allowances_charges"][0]["type"] == "allowance"
    assert result["header_allowances_charges"][0]["indicator_raw"] == "false"


def test_references_are_separated_by_business_term_and_keep_metadata():
    result = _parse(
        """
        <ram:IncludedSupplyChainTradeLineItem>
          <ram:AssociatedDocumentLineDocument>
            <ram:LineID>1</ram:LineID>
          </ram:AssociatedDocumentLineDocument>
          <ram:SpecifiedLineTradeSettlement>
            <ram:AdditionalReferencedDocument>
              <ram:IssuerAssignedID>OBJEKT-128</ram:IssuerAssignedID>
              <ram:TypeCode>130</ram:TypeCode>
              <ram:ReferenceTypeCode>OBJ</ram:ReferenceTypeCode>
            </ram:AdditionalReferencedDocument>
          </ram:SpecifiedLineTradeSettlement>
        </ram:IncludedSupplyChainTradeLineItem>
        <ram:ApplicableHeaderTradeAgreement>
          <ram:AdditionalReferencedDocument>
            <ram:IssuerAssignedID>AUSSCHREIBUNG-17</ram:IssuerAssignedID>
            <ram:TypeCode>50</ram:TypeCode>
          </ram:AdditionalReferencedDocument>
          <ram:AdditionalReferencedDocument>
            <ram:IssuerAssignedID>OBJEKT-18</ram:IssuerAssignedID>
            <ram:TypeCode>130</ram:TypeCode>
            <ram:ReferenceTypeCode>ABT</ram:ReferenceTypeCode>
          </ram:AdditionalReferencedDocument>
          <ram:AdditionalReferencedDocument>
            <ram:IssuerAssignedID>ANLAGE-24</ram:IssuerAssignedID>
            <ram:URIID>https://example.invalid/anlage</ram:URIID>
            <ram:TypeCode>916</ram:TypeCode>
            <ram:Name>Synthetische Anlage</ram:Name>
            <ram:AttachmentBinaryObject
                mimeCode="application/pdf"
                filename="synthetische-anlage.pdf">UERG</ram:AttachmentBinaryObject>
          </ram:AdditionalReferencedDocument>
        </ram:ApplicableHeaderTradeAgreement>
        <ram:ApplicableHeaderTradeSettlement>
          <ram:InvoiceReferencedDocument>
            <ram:IssuerAssignedID>ALT-26</ram:IssuerAssignedID>
            <ram:FormattedIssueDateTime>
              <qdt:DateTimeString format="102">20260630</qdt:DateTimeString>
            </ram:FormattedIssueDateTime>
          </ram:InvoiceReferencedDocument>
          <ram:ReceivableSpecifiedTradeAccountingAccount>
            <ram:ID>KST-19</ram:ID>
          </ram:ReceivableSpecifiedTradeAccountingAccount>
        </ram:ApplicableHeaderTradeSettlement>
        """
    )

    assert result["references"]["tender"] == "AUSSCHREIBUNG-17"
    assert result["references"]["invoiced_object"] == "OBJEKT-18"
    assert result["references"]["invoiced_object_scheme"] == "ABT"
    assert result["references"]["buyer_accounting_reference"] == "KST-19"
    assert result["references"]["preceding_invoices"] == ["ALT-26"]
    assert result["references"]["preceding_invoice_documents"] == [{"id": "ALT-26", "issue_date": "2026-06-30"}]
    assert result["references"]["additional_documents"] == [
        {
            "id": "ANLAGE-24",
            "type_code": "916",
            "name": "Synthetische Anlage",
            "description": None,
            "attachment_filename": "synthetische-anlage.pdf",
            "attachment_mime": "application/pdf",
            "external_uri": "https://example.invalid/anlage",
        }
    ]
    assert result["lines"][0]["object_identifier"] == "OBJEKT-128"
    assert result["lines"][0]["object_identifier_scheme"] == "OBJ"


def test_price_discount_is_not_duplicated_as_line_allowance_and_missing_basis_stays_missing():
    result = _parse(
        """
        <ram:IncludedSupplyChainTradeLineItem>
          <ram:AssociatedDocumentLineDocument>
            <ram:LineID>1</ram:LineID>
          </ram:AssociatedDocumentLineDocument>
          <ram:SpecifiedLineTradeAgreement>
            <ram:GrossPriceProductTradePrice>
              <ram:ChargeAmount currencyID="EUR">120.00</ram:ChargeAmount>
              <ram:AppliedTradeAllowanceCharge>
                <ram:ChargeIndicator><udt:Indicator>false</udt:Indicator></ram:ChargeIndicator>
                <ram:ActualAmount currencyID="EUR">20.00</ram:ActualAmount>
                <ram:CalculationPercent>16.6667</ram:CalculationPercent>
              </ram:AppliedTradeAllowanceCharge>
            </ram:GrossPriceProductTradePrice>
            <ram:NetPriceProductTradePrice>
              <ram:ChargeAmount currencyID="EUR">100.00</ram:ChargeAmount>
            </ram:NetPriceProductTradePrice>
          </ram:SpecifiedLineTradeAgreement>
          <ram:SpecifiedLineTradeDelivery>
            <ram:BilledQuantity unitCode="C62">1</ram:BilledQuantity>
          </ram:SpecifiedLineTradeDelivery>
          <ram:SpecifiedLineTradeSettlement>
            <ram:SpecifiedTradeAllowanceCharge>
              <ram:ChargeIndicator><udt:Indicator>vielleicht</udt:Indicator></ram:ChargeIndicator>
              <ram:ActualAmount currencyID="EUR">4.00</ram:ActualAmount>
              <ram:CategoryTradeTax>
                <ram:TypeCode>VAT</ram:TypeCode>
                <ram:CategoryCode>S</ram:CategoryCode>
                <ram:RateApplicablePercent>19</ram:RateApplicablePercent>
              </ram:CategoryTradeTax>
            </ram:SpecifiedTradeAllowanceCharge>
          </ram:SpecifiedLineTradeSettlement>
        </ram:IncludedSupplyChainTradeLineItem>
        """
    )

    line = result["lines"][0]
    assert line["price"] == "100.00"
    assert line["gross_price"] == "120.00"
    assert line["gross_price_currency"] == "EUR"
    assert line["price_allowance"] == "20.00"
    assert line["price_allowance_currency"] == "EUR"
    assert line["price_allowance_percent"] == "16.6667"
    assert line["base_quantity"] is None
    assert line["base_unit_code"] is None
    assert line["base_unit_label"] is None
    assert len(line["allowances_charges"]) == 1
    adjustment = line["allowances_charges"][0]
    assert adjustment["type"] == "unknown"
    assert adjustment["type_label"] == "Unbekannt"
    assert adjustment["indicator_raw"] == "vielleicht"
    assert adjustment["tax_category"] == "S"
    assert adjustment["tax_rate"] == "19"
    assert adjustment["tax_type"] == "VAT"


def test_header_allowance_charge_keeps_tax_fields_and_explicit_indicator():
    result = _parse(
        """
        <ram:ApplicableHeaderTradeSettlement>
          <ram:SpecifiedTradeAllowanceCharge>
            <ram:ChargeIndicator><udt:Indicator>false</udt:Indicator></ram:ChargeIndicator>
            <ram:ActualAmount currencyID="EUR">10.00</ram:ActualAmount>
            <ram:CategoryTradeTax>
              <ram:TypeCode>VAT</ram:TypeCode>
              <ram:CategoryCode>E</ram:CategoryCode>
              <ram:RateApplicablePercent>0</ram:RateApplicablePercent>
            </ram:CategoryTradeTax>
          </ram:SpecifiedTradeAllowanceCharge>
        </ram:ApplicableHeaderTradeSettlement>
        """
    )

    adjustment = result["header_allowances_charges"][0]
    assert adjustment["type"] == "allowance"
    assert adjustment["indicator_raw"] == "false"
    assert adjustment["tax_category"] == "E"
    assert adjustment["tax_rate"] == "0"
    assert adjustment["tax_type"] == "VAT"


def test_payment_accounts_card_data_and_partial_payment_currency_keep_their_semantics():
    result = _parse(
        """
        <ram:ApplicableHeaderTradeSettlement>
          <ram:SpecifiedTradeSettlementPaymentMeans>
            <ram:TypeCode>48</ram:TypeCode>
            <ram:Information>Synthetischer Zahlungsweg</ram:Information>
            <ram:ApplicableTradeSettlementFinancialCard>
              <ram:ID>KARTE-88</ram:ID>
              <ram:CardholderName>Synthetische Karteninhaberin</ram:CardholderName>
            </ram:ApplicableTradeSettlementFinancialCard>
            <ram:PayerPartyDebtorFinancialAccount>
              <ram:ProprietaryID schemeID="LOCAL">BELASTUNG-91</ram:ProprietaryID>
            </ram:PayerPartyDebtorFinancialAccount>
            <ram:PayeePartyCreditorFinancialAccount>
              <ram:ProprietaryID schemeID="LOCAL">KONTO-84</ram:ProprietaryID>
              <ram:AccountName>Synthetisches Verrechnungskonto</ram:AccountName>
            </ram:PayeePartyCreditorFinancialAccount>
            <ram:PayeeSpecifiedCreditorFinancialInstitution>
              <ram:BICID>TESTDEFFXXX</ram:BICID>
            </ram:PayeeSpecifiedCreditorFinancialInstitution>
            <ram:ApplicableTradePaymentMandate>
              <ram:ID>MANDAT-89</ram:ID>
              <ram:CreditorReferenceID schemeID="SEPA">GLAEUBIGER-90</ram:CreditorReferenceID>
            </ram:ApplicableTradePaymentMandate>
          </ram:SpecifiedTradeSettlementPaymentMeans>
          <ram:SpecifiedTradePaymentTerms>
            <ram:PartialPaymentAmount currencyID="EUR">25.00</ram:PartialPaymentAmount>
          </ram:SpecifiedTradePaymentTerms>
        </ram:ApplicableHeaderTradeSettlement>
        """
    )

    means = result["payment"]["means"][0]
    assert means["account_id"] == {"value": "KONTO-84", "scheme": "LOCAL"}
    assert means["iban"] is None
    assert means["service_provider_id"] == {"value": "TESTDEFFXXX", "scheme": "BIC"}
    assert means["bic"] == "TESTDEFFXXX"
    assert means["debited_account_id"] == {
        "value": "BELASTUNG-91",
        "scheme": "LOCAL",
    }
    assert means["payer_iban"] is None
    assert means["card_account"] == "KARTE-88"
    assert means["card_holder_name"] == "Synthetische Karteninhaberin"
    assert means["mandate_reference"] == "MANDAT-89"
    assert means["creditor_id"] == {"value": "GLAEUBIGER-90", "scheme": "SEPA"}
    assert result["payment"]["terms"][0]["partial_payment_amount"] == "25.00"
    assert result["payment"]["terms"][0]["partial_payment_currency"] == "EUR"
