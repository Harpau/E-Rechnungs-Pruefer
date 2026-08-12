from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from lxml import etree

from app.analyzer import analyze_bytes
from app.parsers import cii as cii_parser
from app.parsers import ubl as ubl_parser
from app.parsers.cii import parse_cii
from app.parsers.ubl import parse_ubl
from app.xml_utils import safe_parse_xml, technical_rows

UBL_INVOICE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
UBL_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
UBL_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
UBL_EXT = "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2"
CII_RSM = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
CII_RAM = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
CII_UDT = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"
CII_QDT = "urn:un:unece:uncefact:data:standard:QualifiedDataType:100"
EVIL = "urn:example:foreign"


def test_ubl_parser_uses_namespace_uris_and_ignores_extension_collisions() -> None:
    payload = f"""
    <Invoice xmlns="{UBL_INVOICE}" xmlns:b="{UBL_CBC}" xmlns:a="{UBL_CAC}"
             xmlns:ext="{UBL_EXT}" xmlns:x="{EVIL}">
      <x:ID>FOREIGN</x:ID>
      <a:ID>CROSS-NAMESPACE</a:ID>
      <b:ID>REAL-1</b:ID>
      <b:BuyerReference><x:value>FOREIGN-NESTED</x:value></b:BuyerReference>
      <ext:UBLExtensions>
        <ext:UBLExtension><ext:ExtensionContent>
          <x:ID>EXTENSION-FOREIGN</x:ID><b:ID>EXTENSION-CBC</b:ID>
        </ext:ExtensionContent></ext:UBLExtension>
      </ext:UBLExtensions>
      <a:AccountingSupplierParty><a:Party><a:PartyLegalEntity>
        <b:RegistrationName>URI-basierter Lieferant</b:RegistrationName>
      </a:PartyLegalEntity></a:Party></a:AccountingSupplierParty>
    </Invoice>
    """.encode()

    root = safe_parse_xml(payload)
    parsed = parse_ubl(root)
    rows = technical_rows(root).rows

    assert parsed["document"]["id"] == "REAL-1"
    assert parsed["document"]["buyer_reference"] is None
    assert parsed["seller"]["name"] == "URI-basierter Lieferant"
    assert any(row["value"] == "EXTENSION-FOREIGN" for row in rows)
    assert any(row["value"] == "EXTENSION-CBC" for row in rows)


def test_ubl_document_with_only_foreign_business_children_is_not_clear() -> None:
    payload = f"""
    <Invoice xmlns="{UBL_INVOICE}" xmlns:x="{EVIL}">
      <x:CustomizationID>urn:cen.eu:en16931:2017</x:CustomizationID>
      <x:ID>FOREIGN-ID</x:ID><x:IssueDate>2026-08-08</x:IssueDate>
      <x:DocumentCurrencyCode>EUR</x:DocumentCurrencyCode>
      <x:AccountingSupplierParty><x:Party><x:PartyLegalEntity>
        <x:RegistrationName>Fremd</x:RegistrationName>
      </x:PartyLegalEntity></x:Party></x:AccountingSupplierParty>
      <x:LegalMonetaryTotal><x:PayableAmount>1.00</x:PayableAmount></x:LegalMonetaryTotal>
    </Invoice>
    """.encode()

    result = analyze_bytes(payload, "foreign-ubl.xml", "application/xml", run_official_validation=False)
    ids = {item["rule"]["id"] for item in result["assessment"]["internal"]["findings"]}

    assert result["document"]["id"] is None
    assert result["assessment"]["internal"]["status"] == "errors"
    assert "CHECK-000" not in ids


def test_cii_parser_uses_namespace_uris_for_roots_dates_values_and_indicators() -> None:
    payload = f"""
    <doc:CrossIndustryInvoice xmlns:doc="{CII_RSM}" xmlns:biz="{CII_RAM}"
        xmlns:data="{CII_UDT}" xmlns:qual="{CII_QDT}" xmlns:x="{EVIL}">
      <x:ExchangedDocument><x:ID>FOREIGN-DOCUMENT</x:ID></x:ExchangedDocument>
      <doc:ExchangedDocumentContext/>
      <doc:ExchangedDocument>
        <x:ID>FOREIGN-ID</x:ID><biz:ID>REAL-CII</biz:ID>
        <biz:IssueDateTime><x:Wrapper>
          <data:DateTimeString format="102">20260101</data:DateTimeString>
        </x:Wrapper></biz:IssueDateTime>
      </doc:ExchangedDocument>
      <doc:SupplyChainTradeTransaction>
        <biz:ApplicableHeaderTradeSettlement>
          <biz:SpecifiedTradeAllowanceCharge>
            <biz:ChargeIndicator><x:Indicator>true</x:Indicator></biz:ChargeIndicator>
            <biz:ActualAmount>1.00</biz:ActualAmount>
          </biz:SpecifiedTradeAllowanceCharge>
          <biz:SpecifiedTradeAllowanceCharge>
            <biz:ChargeIndicator><data:Indicator>false</data:Indicator></biz:ChargeIndicator>
            <biz:ActualAmount>2.00</biz:ActualAmount>
          </biz:SpecifiedTradeAllowanceCharge>
        </biz:ApplicableHeaderTradeSettlement>
      </doc:SupplyChainTradeTransaction>
    </doc:CrossIndustryInvoice>
    """.encode()

    parsed = parse_cii(safe_parse_xml(payload))

    assert parsed["document"]["id"] == "REAL-CII"
    assert parsed["document"]["issue_date"] is None
    assert [item["type"] for item in parsed["header_allowances_charges"]] == ["unknown", "allowance"]


@pytest.mark.parametrize(
    ("parser_module", "parser", "payload", "expected_count"),
    [
        (
            ubl_parser,
            parse_ubl,
            lambda count: f"""
            <Invoice xmlns="{UBL_INVOICE}" xmlns:cbc="{UBL_CBC}" xmlns:cac="{UBL_CAC}">
              <cac:AccountingSupplierParty><cac:Party>
                {"".join(f"<cac:PartyIdentification><cbc:ID>UBL-{index}</cbc:ID></cac:PartyIdentification>" for index in range(count))}
              </cac:Party></cac:AccountingSupplierParty>
            </Invoice>
            """.encode(),
            1_000,
        ),
        (
            cii_parser,
            parse_cii,
            lambda count: f"""
            <rsm:CrossIndustryInvoice xmlns:rsm="{CII_RSM}" xmlns:ram="{CII_RAM}" xmlns:udt="{CII_UDT}">
              <rsm:ExchangedDocumentContext/><rsm:ExchangedDocument/>
              <rsm:SupplyChainTradeTransaction><ram:ApplicableHeaderTradeAgreement>
                <ram:SellerTradeParty>
                  {"".join(f"<ram:ID>CII-{index}</ram:ID>" for index in range(count))}
                </ram:SellerTradeParty>
              </ram:ApplicableHeaderTradeAgreement></rsm:SupplyChainTradeTransaction>
            </rsm:CrossIndustryInvoice>
            """.encode(),
            1_000,
        ),
    ],
    ids=["UBL", "CII"],
)
def test_many_unique_party_identifiers_use_linear_membership_work(
    monkeypatch: pytest.MonkeyPatch,
    parser_module: ModuleType,
    parser: Callable[[etree._Element], dict],
    payload: Callable[[int], bytes],
    expected_count: int,
) -> None:
    class CountedText(str):
        hash_calls = 0

        def __hash__(self) -> int:
            type(self).hash_calls += 1
            return super().__hash__()

    original_id_entry = parser_module.id_entry

    def counted_id_entry(node: etree._Element | None) -> dict | None:
        entry = original_id_entry(node)
        if entry is not None:
            entry["value"] = CountedText(entry["value"])
        return entry

    monkeypatch.setattr(parser_module, "id_entry", counted_id_entry)

    parsed = parser(safe_parse_xml(payload(expected_count)))

    assert len(parsed["seller"]["ids"]) == expected_count
    assert CountedText.hash_calls <= 6 * expected_count


def test_cii_document_with_only_foreign_business_children_is_not_clear() -> None:
    payload = f"""
    <doc:CrossIndustryInvoice xmlns:doc="{CII_RSM}" xmlns:x="{EVIL}">
      <x:ExchangedDocumentContext/><x:ExchangedDocument><x:ID>FOREIGN</x:ID></x:ExchangedDocument>
      <x:SupplyChainTradeTransaction/>
    </doc:CrossIndustryInvoice>
    """.encode()

    result = analyze_bytes(payload, "foreign-cii.xml", "application/xml", run_official_validation=False)
    ids = {item["rule"]["id"] for item in result["assessment"]["internal"]["findings"]}

    assert result["document"]["id"] is None
    assert result["assessment"]["internal"]["status"] == "errors"
    assert "CHECK-000" not in ids


@pytest.mark.parametrize("filename", ["ubl.py", "cii.py"])
def test_invoice_parsers_do_not_use_namespace_agnostic_xpath(filename: str) -> None:
    source = (Path(__file__).parents[1] / "app" / "parsers" / filename).read_text(encoding="utf-8")

    assert "[local-name()" not in source
    assert ".xpath(" not in source
