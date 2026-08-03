from __future__ import annotations

from typing import Any

from lxml import etree

from ..xml_utils import (
    all_text,
    attr_value,
    clean_text,
    first_node,
    first_text,
    local_name,
    parse_date_value,
    unique_nonempty,
)
from .common import (
    document_meta,
    empty_party,
    id_entry,
    profile_name,
    readable_country,
    readable_payment_means,
    readable_tax_basis_label,
    readable_tax_category,
    readable_tax_category_display,
    readable_unit,
)


def _date_from_node(node: etree._Element | None) -> str | None:
    if node is None:
        return None
    date_node = first_node(node, ".//*[local-name()='DateTimeString']")
    if date_node is None:
        date_node = first_node(node, ".//*[local-name()='DateString']")
    if date_node is None:
        return parse_date_value(clean_text(node), attr_value(node, "format"))
    return parse_date_value(clean_text(date_node), attr_value(date_node, "format"))


def _append_unique(entries: list[dict], entry: dict | None) -> None:
    if not entry:
        return
    key = (entry.get("value"), entry.get("scheme"))
    if key not in {(item.get("value"), item.get("scheme")) for item in entries}:
        entries.append(entry)


def _parse_period(node: etree._Element | None) -> dict | None:
    if node is None:
        return None
    start = _date_from_node(first_node(node, "./*[local-name()='StartDateTime']"))
    end = _date_from_node(first_node(node, "./*[local-name()='EndDateTime']"))
    description = first_text(node, "./*[local-name()='Description']")
    if not any((start, end, description)):
        return None
    return {"start": start, "end": end, "description": description}


def _amount_node_by_currency(
    parent: etree._Element | None,
    element_name: str,
    currency: str | None = None,
) -> etree._Element | None:
    if parent is None:
        return None
    candidates = [
        item for item in parent.xpath(f"./*[local-name()='{element_name}']") if isinstance(item, etree._Element)
    ]
    if currency is None:
        return candidates[0] if candidates else None
    return next((item for item in candidates if attr_value(item, "currencyID") == currency), None)


def _reference_identifier(node: etree._Element | None) -> dict | None:
    value = first_text(node, "./*[local-name()='IssuerAssignedID']")
    if value is None:
        return None
    return {
        "value": value,
        "scheme": first_text(node, "./*[local-name()='ReferenceTypeCode']"),
    }


def _financial_identifier(
    account: etree._Element | None,
    *,
    iban: bool = False,
    bic: bool = False,
) -> dict | None:
    if account is None:
        return None
    element_names = ("IBANID", "ProprietaryID") if iban else ("BICID", "GermanBankleitzahlID")
    for element_name in element_names:
        node = first_node(account, f"./*[local-name()='{element_name}']")
        entry = id_entry(node)
        if entry is None:
            continue
        if entry["scheme"] is None and element_name == "IBANID":
            entry["scheme"] = "IBAN"
        elif entry["scheme"] is None and bic and element_name == "BICID":
            entry["scheme"] = "BIC"
        return entry
    return None


def _parse_party(party: etree._Element | None) -> dict:
    result = empty_party()
    if party is None:
        return result

    legal_org = first_node(party, "./*[local-name()='SpecifiedLegalOrganization']")
    result["name"] = first_text(party, "./*[local-name()='Name']")
    result["trading_name"] = first_text(legal_org, "./*[local-name()='TradingBusinessName']")
    result["description"] = first_text(party, "./*[local-name()='Description']")

    for node in party.xpath("./*[local-name()='ID'] | ./*[local-name()='GlobalID']"):
        if isinstance(node, etree._Element):
            _append_unique(result["ids"], id_entry(node))
    for node in (
        legal_org.xpath("./*[local-name()='ID'] | ./*[local-name()='GlobalID']") if legal_org is not None else []
    ):
        if isinstance(node, etree._Element):
            _append_unique(result["legal_registration_ids"], id_entry(node))

    for node in party.xpath("./*[local-name()='SpecifiedTaxRegistration']/*[local-name()='ID']"):
        if isinstance(node, etree._Element):
            _append_unique(result["tax_ids"], id_entry(node))

    endpoint_node = first_node(
        party,
        "./*[local-name()='URIUniversalCommunication']/*[local-name()='URIID']",
    )
    endpoint = id_entry(endpoint_node)
    result["endpoint"] = endpoint

    contact = first_node(party, "./*[local-name()='DefinedTradeContact']")
    if contact is not None:
        result["contact"] = {
            "name": first_text(contact, "./*[local-name()='PersonName']"),
            "department": first_text(contact, "./*[local-name()='DepartmentName']"),
            "phone": first_text(
                contact,
                "./*[local-name()='TelephoneUniversalCommunication']/*[local-name()='CompleteNumber']",
            ),
            "email": first_text(
                contact,
                "./*[local-name()='EmailURIUniversalCommunication']/*[local-name()='URIID']",
            ),
        }

    address = first_node(party, "./*[local-name()='PostalTradeAddress']")
    if address is not None:
        country_code = first_text(address, "./*[local-name()='CountryID']")
        result["address"] = {
            "line1": first_text(address, "./*[local-name()='LineOne']"),
            "line2": first_text(address, "./*[local-name()='LineTwo']"),
            "line3": first_text(address, "./*[local-name()='LineThree']"),
            "postcode": first_text(address, "./*[local-name()='PostcodeCode']"),
            "city": first_text(address, "./*[local-name()='CityName']"),
            "subdivision": first_text(address, "./*[local-name()='CountrySubDivisionName']"),
            "country_code": country_code,
            "country": readable_country(country_code),
        }
    return result


def _parse_allowance_charge(node: etree._Element) -> dict:
    indicator_raw = first_text(node, "./*[local-name()='ChargeIndicator']")
    indicator = (indicator_raw or "").casefold()
    if indicator in {"true", "1"}:
        item_type = "charge"
        type_label = "Zuschlag"
    elif indicator in {"false", "0"}:
        item_type = "allowance"
        type_label = "Nachlass"
    else:
        item_type = "unknown"
        type_label = "Unbekannt"
    amount_node = first_node(node, "./*[local-name()='ActualAmount']")
    if amount_node is None:
        amount_node = first_node(node, "./*[local-name()='ChargeAmount']")
    basis_node = first_node(node, "./*[local-name()='BasisAmount']")
    tax = first_node(node, "./*[local-name()='CategoryTradeTax']")
    category = first_text(tax, "./*[local-name()='CategoryCode']")
    return {
        "type": item_type,
        "type_label": type_label,
        "indicator_raw": indicator_raw,
        "amount": clean_text(amount_node),
        "currency": attr_value(amount_node, "currencyID"),
        "basis_amount": clean_text(basis_node),
        "basis_currency": attr_value(basis_node, "currencyID"),
        "percent": first_text(node, "./*[local-name()='CalculationPercent']"),
        "reason": first_text(node, "./*[local-name()='Reason']"),
        "reason_code": first_text(node, "./*[local-name()='ReasonCode']"),
        "tax_category": category,
        "tax_category_label": readable_tax_category(category),
        "tax_category_display": readable_tax_category_display(category),
        "tax_rate": first_text(tax, "./*[local-name()='RateApplicablePercent']"),
        "tax_type": first_text(tax, "./*[local-name()='TypeCode']"),
    }


def _parse_line(line: etree._Element) -> dict:
    doc = first_node(line, "./*[local-name()='AssociatedDocumentLineDocument']")
    product = first_node(line, "./*[local-name()='SpecifiedTradeProduct']")
    agreement = first_node(line, "./*[local-name()='SpecifiedLineTradeAgreement']")
    delivery = first_node(line, "./*[local-name()='SpecifiedLineTradeDelivery']")
    settlement = first_node(line, "./*[local-name()='SpecifiedLineTradeSettlement']")

    price_node = first_node(agreement, "./*[local-name()='NetPriceProductTradePrice']")
    price_amount = first_node(price_node, "./*[local-name()='ChargeAmount']")
    basis_quantity = first_node(price_node, "./*[local-name()='BasisQuantity']")
    gross_price_node = first_node(agreement, "./*[local-name()='GrossPriceProductTradePrice']")
    gross_price_amount = first_node(gross_price_node, "./*[local-name()='ChargeAmount']")
    price_allowance = None
    if gross_price_node is not None:
        for candidate in gross_price_node.xpath("./*[local-name()='AppliedTradeAllowanceCharge']"):
            if not isinstance(candidate, etree._Element):
                continue
            indicator = (first_text(candidate, "./*[local-name()='ChargeIndicator']") or "").casefold()
            if indicator in {"false", "0"}:
                price_allowance = candidate
                break
    price_allowance_amount = first_node(price_allowance, "./*[local-name()='ActualAmount']")
    quantity = first_node(delivery, "./*[local-name()='BilledQuantity']")
    line_total = first_node(
        settlement,
        "./*[local-name()='SpecifiedTradeSettlementLineMonetarySummation']/*[local-name()='LineTotalAmount']",
    )
    tax = first_node(settlement, "./*[local-name()='ApplicableTradeTax']")
    category = first_text(tax, "./*[local-name()='CategoryCode']")

    allowances_charges = (
        [
            _parse_allowance_charge(item)
            for item in settlement.xpath("./*[local-name()='SpecifiedTradeAllowanceCharge']")
            if isinstance(item, etree._Element)
        ]
        if settlement is not None
        else []
    )
    classifications: list[dict[str, str | None]] = []
    if product is not None:
        for classification in product.xpath("./*[local-name()='DesignatedProductClassification']"):
            if not isinstance(classification, etree._Element):
                continue
            class_code = first_node(classification, "./*[local-name()='ClassCode']")
            classifications.append(
                {
                    "code": clean_text(class_code),
                    "scheme": attr_value(class_code, "listID"),
                    "version": attr_value(class_code, "listVersionID"),
                    "name": first_text(classification, "./*[local-name()='ClassName']"),
                }
            )

    properties: list[dict[str, str | None]] = []
    if product is not None:
        for prop in product.xpath("./*[local-name()='ApplicableProductCharacteristic']"):
            if not isinstance(prop, etree._Element):
                continue
            properties.append(
                {
                    "name": first_text(prop, "./*[local-name()='Description']")
                    or first_text(prop, "./*[local-name()='TypeCode']"),
                    "value": first_text(prop, "./*[local-name()='Value']"),
                }
            )

    standard_id_node = first_node(product, "./*[local-name()='GlobalID']")
    origin = first_text(product, "./*[local-name()='OriginTradeCountry']/*[local-name()='ID']")

    period_node = first_node(settlement, "./*[local-name()='BillingSpecifiedPeriod']")
    start_node = first_node(period_node, "./*[local-name()='StartDateTime']")
    end_node = first_node(period_node, "./*[local-name()='EndDateTime']")
    period = None
    if period_node is not None:
        period = {
            "start": _date_from_node(start_node),
            "end": _date_from_node(end_node),
            "description": None,
        }
        if not period["start"] and not period["end"]:
            period = None

    notes = unique_nonempty(
        all_text(doc, "./*[local-name()='IncludedNote']/*[local-name()='Content']")
        + all_text(product, "./*[local-name()='Description']")
    )
    object_reference = _reference_identifier(
        first_node(
            settlement,
            "./*[local-name()='AdditionalReferencedDocument'][*[local-name()='TypeCode']='130']",
        )
    )

    return {
        "id": first_text(doc, "./*[local-name()='LineID']"),
        "name": first_text(product, "./*[local-name()='Name']"),
        "description": first_text(product, "./*[local-name()='Description']"),
        "seller_item_id": first_text(product, "./*[local-name()='SellerAssignedID']"),
        "buyer_item_id": first_text(product, "./*[local-name()='BuyerAssignedID']"),
        "standard_item_id": clean_text(standard_id_node),
        "standard_item_scheme": attr_value(standard_id_node, "schemeID"),
        "quantity": clean_text(quantity),
        "unit_code": attr_value(quantity, "unitCode"),
        "unit_label": readable_unit(attr_value(quantity, "unitCode")),
        "price": clean_text(price_amount),
        "price_currency": attr_value(price_amount, "currencyID"),
        "gross_price": clean_text(gross_price_amount),
        "gross_price_currency": attr_value(gross_price_amount, "currencyID"),
        "price_allowance": clean_text(price_allowance_amount),
        "price_allowance_currency": attr_value(price_allowance_amount, "currencyID"),
        "price_allowance_percent": first_text(
            price_allowance,
            "./*[local-name()='CalculationPercent']",
        ),
        "base_quantity": clean_text(basis_quantity),
        "base_unit_code": attr_value(basis_quantity, "unitCode"),
        "base_unit_label": readable_unit(attr_value(basis_quantity, "unitCode")),
        "line_total": clean_text(line_total),
        "line_currency": attr_value(line_total, "currencyID"),
        "tax_category": category,
        "tax_category_label": readable_tax_category(category),
        "tax_category_display": readable_tax_category_display(category),
        "tax_rate": first_text(tax, "./*[local-name()='RateApplicablePercent']"),
        "tax_type": first_text(tax, "./*[local-name()='TypeCode']"),
        "allowances_charges": allowances_charges,
        "notes": notes,
        "period": period,
        "order_line_reference": first_text(
            agreement,
            "./*[local-name()='BuyerOrderReferencedDocument']/*[local-name()='LineID']",
        ),
        "object_identifier": (object_reference or {}).get("value"),
        "object_identifier_scheme": (object_reference or {}).get("scheme"),
        "accounting_cost": first_text(
            settlement, "./*[local-name()='ReceivableSpecifiedTradeAccountingAccount']/*[local-name()='ID']"
        ),
        "classifications": classifications,
        "origin_country": origin,
        "origin_country_label": readable_country(origin),
        "additional_properties": properties,
    }


def _parse_tax(tax: etree._Element) -> dict:
    amount = first_node(tax, "./*[local-name()='CalculatedAmount']")
    basis = first_node(tax, "./*[local-name()='BasisAmount']")
    category = first_text(tax, "./*[local-name()='CategoryCode']")
    exemption_reasons = all_text(tax, "./*[local-name()='ExemptionReason']")
    return {
        "type": first_text(tax, "./*[local-name()='TypeCode']"),
        "category_code": category,
        "category_label": readable_tax_category(category),
        "category_display": readable_tax_category_display(category),
        "rate": first_text(tax, "./*[local-name()='RateApplicablePercent']"),
        "basis_amount": clean_text(basis),
        "basis_label": readable_tax_basis_label(category),
        "basis_currency": attr_value(basis, "currencyID"),
        "tax_amount": clean_text(amount),
        "tax_currency": attr_value(amount, "currencyID"),
        "exemption_reason": " | ".join(exemption_reasons) if exemption_reasons else None,
        "exemption_reason_code": first_text(tax, "./*[local-name()='ExemptionReasonCode']"),
    }


def _parse_payment_means(node: etree._Element) -> dict:
    type_code = first_text(node, "./*[local-name()='TypeCode']")
    account = first_node(node, "./*[local-name()='PayeePartyCreditorFinancialAccount']")
    institution = first_node(node, "./*[local-name()='PayeeSpecifiedCreditorFinancialInstitution']")
    payer_account = first_node(node, "./*[local-name()='PayerPartyDebtorFinancialAccount']")
    mandate = first_node(node, "./*[local-name()='ApplicableTradePaymentMandate']")
    card = first_node(node, "./*[local-name()='ApplicableTradeSettlementFinancialCard']")
    creditor_id = first_node(mandate, "./*[local-name()='CreditorReferenceID']")
    account_entry = _financial_identifier(account, iban=True)
    institution_entry = _financial_identifier(institution, bic=True)
    payer_account_entry = _financial_identifier(payer_account, iban=True)
    account_scheme = ((account_entry or {}).get("scheme") or "").upper()
    institution_scheme = ((institution_entry or {}).get("scheme") or "").upper()
    payer_account_scheme = ((payer_account_entry or {}).get("scheme") or "").upper()
    return {
        "type_code": type_code,
        "type_label": readable_payment_means(type_code),
        "information": first_text(node, "./*[local-name()='Information']"),
        "account_id": account_entry,
        "iban": (account_entry or {}).get("value") if account_scheme == "IBAN" else None,
        "account_name": first_text(account, "./*[local-name()='AccountName']"),
        "service_provider_id": institution_entry,
        "bic": ((institution_entry or {}).get("value") if institution_scheme in {"BIC", "BICFI"} else None),
        "debited_account_id": payer_account_entry,
        "payer_iban": ((payer_account_entry or {}).get("value") if payer_account_scheme == "IBAN" else None),
        "mandate_reference": first_text(mandate, "./*[local-name()='ID']"),
        "creditor_id": id_entry(creditor_id),
        "card_account": first_text(card, "./*[local-name()='ID']"),
        "card_holder_name": first_text(card, "./*[local-name()='CardholderName']"),
    }


def parse_cii(root: etree._Element) -> dict[str, Any]:
    context = first_node(root, "./*[local-name()='ExchangedDocumentContext']")
    document = first_node(root, "./*[local-name()='ExchangedDocument']")
    transaction = first_node(root, "./*[local-name()='SupplyChainTradeTransaction']")
    agreement = first_node(transaction, "./*[local-name()='ApplicableHeaderTradeAgreement']")
    delivery = first_node(transaction, "./*[local-name()='ApplicableHeaderTradeDelivery']")
    settlement = first_node(transaction, "./*[local-name()='ApplicableHeaderTradeSettlement']")

    profile_id = first_text(
        context,
        "./*[local-name()='GuidelineSpecifiedDocumentContextParameter']/*[local-name()='ID']",
    )
    issue_node = first_node(document, "./*[local-name()='IssueDateTime']")
    delivery_event = first_node(delivery, "./*[local-name()='ActualDeliverySupplyChainEvent']")
    delivery_date = _date_from_node(first_node(delivery_event, "./*[local-name()='OccurrenceDateTime']"))

    terms_nodes = settlement.xpath("./*[local-name()='SpecifiedTradePaymentTerms']") if settlement is not None else []
    payment_terms: list[dict[str, str | None]] = []
    due_date: str | None = None
    for term in terms_nodes:
        if not isinstance(term, etree._Element):
            continue
        term_due = _date_from_node(first_node(term, "./*[local-name()='DueDateDateTime']"))
        partial_payment = first_node(term, "./*[local-name()='PartialPaymentAmount']")
        due_date = due_date or term_due
        payment_terms.append(
            {
                "description": first_text(term, "./*[local-name()='Description']"),
                "due_date": term_due,
                "direct_debit_mandate_id": first_text(term, "./*[local-name()='DirectDebitMandateID']"),
                "partial_payment_amount": clean_text(partial_payment),
                "partial_payment_currency": attr_value(partial_payment, "currencyID"),
            }
        )

    tax_point_date = _date_from_node(
        first_node(
            settlement,
            "./*[local-name()='ApplicableTradeTax']/*[local-name()='TaxPointDate']",
        )
    )
    tax_point_date_code = first_text(
        settlement,
        "./*[local-name()='ApplicableTradeTax']/*[local-name()='DueDateTypeCode']",
    )
    currency = first_text(settlement, "./*[local-name()='InvoiceCurrencyCode']")
    vat_accounting_currency = first_text(settlement, "./*[local-name()='TaxCurrencyCode']")
    invoice_period = _parse_period(first_node(settlement, "./*[local-name()='BillingSpecifiedPeriod']"))

    lines = (
        [
            _parse_line(item)
            for item in transaction.xpath("./*[local-name()='IncludedSupplyChainTradeLineItem']")
            if isinstance(item, etree._Element)
        ]
        if transaction is not None
        else []
    )

    taxes = (
        [
            _parse_tax(item)
            for item in settlement.xpath("./*[local-name()='ApplicableTradeTax']")
            if isinstance(item, etree._Element)
        ]
        if settlement is not None
        else []
    )

    monetary = first_node(settlement, "./*[local-name()='SpecifiedTradeSettlementHeaderMonetarySummation']")
    line_total = _amount_node_by_currency(monetary, "LineTotalAmount")
    allowance_total = _amount_node_by_currency(monetary, "AllowanceTotalAmount")
    charge_total = _amount_node_by_currency(monetary, "ChargeTotalAmount")
    tax_basis_total = _amount_node_by_currency(monetary, "TaxBasisTotalAmount")
    document_tax_total = _amount_node_by_currency(monetary, "TaxTotalAmount", currency)
    accounting_tax_total = (
        _amount_node_by_currency(monetary, "TaxTotalAmount", vat_accounting_currency)
        if vat_accounting_currency is not None
        else None
    )
    grand_total = _amount_node_by_currency(monetary, "GrandTotalAmount")
    prepaid_amount = _amount_node_by_currency(monetary, "TotalPrepaidAmount")
    rounding_amount = _amount_node_by_currency(monetary, "RoundingAmount")
    due_payable_amount = _amount_node_by_currency(monetary, "DuePayableAmount")
    totals = {
        "line_total": clean_text(line_total),
        "line_total_currency": attr_value(line_total, "currencyID"),
        "allowance_total": clean_text(allowance_total),
        "allowance_total_currency": attr_value(allowance_total, "currencyID"),
        "charge_total": clean_text(charge_total),
        "charge_total_currency": attr_value(charge_total, "currencyID"),
        "tax_basis_total": clean_text(tax_basis_total),
        "tax_basis_total_currency": attr_value(tax_basis_total, "currencyID"),
        "tax_total": clean_text(document_tax_total),
        "tax_total_currency": attr_value(document_tax_total, "currencyID"),
        "tax_total_accounting": clean_text(accounting_tax_total),
        "tax_total_accounting_currency": attr_value(accounting_tax_total, "currencyID"),
        "grand_total": clean_text(grand_total),
        "grand_total_currency": attr_value(grand_total, "currencyID"),
        "prepaid_amount": clean_text(prepaid_amount),
        "prepaid_amount_currency": attr_value(prepaid_amount, "currencyID"),
        "rounding_amount": clean_text(rounding_amount),
        "rounding_amount_currency": attr_value(rounding_amount, "currencyID"),
        "due_payable_amount": clean_text(due_payable_amount),
        "due_payable_amount_currency": attr_value(due_payable_amount, "currencyID"),
        "currency": currency,
    }

    payment_means = (
        [
            _parse_payment_means(item)
            for item in settlement.xpath("./*[local-name()='SpecifiedTradeSettlementPaymentMeans']")
            if isinstance(item, etree._Element)
        ]
        if settlement is not None
        else []
    )

    header_allowances_charges = (
        [
            _parse_allowance_charge(item)
            for item in settlement.xpath("./*[local-name()='SpecifiedTradeAllowanceCharge']")
            if isinstance(item, etree._Element)
        ]
        if settlement is not None
        else []
    )

    preceding_invoice_documents: list[dict[str, str | None]] = []
    if settlement is not None:
        for ref in settlement.xpath("./*[local-name()='InvoiceReferencedDocument']"):
            if not isinstance(ref, etree._Element):
                continue
            reference_id = first_text(ref, "./*[local-name()='IssuerAssignedID']")
            if reference_id is None:
                continue
            preceding_invoice_documents.append(
                {
                    "id": reference_id,
                    "issue_date": _date_from_node(first_node(ref, "./*[local-name()='FormattedIssueDateTime']")),
                }
            )

    references: dict[str, Any] = {
        "buyer_order": first_text(
            agreement, "./*[local-name()='BuyerOrderReferencedDocument']/*[local-name()='IssuerAssignedID']"
        ),
        "seller_order": first_text(
            agreement, "./*[local-name()='SellerOrderReferencedDocument']/*[local-name()='IssuerAssignedID']"
        ),
        "contract": first_text(
            agreement, "./*[local-name()='ContractReferencedDocument']/*[local-name()='IssuerAssignedID']"
        ),
        "tender": None,
        "project": first_text(agreement, "./*[local-name()='SpecifiedProcuringProject']/*[local-name()='ID']")
        or first_text(agreement, "./*[local-name()='ProjectReferencedDocument']/*[local-name()='IssuerAssignedID']"),
        "buyer_accounting_reference": first_text(
            settlement,
            "./*[local-name()='ReceivableSpecifiedTradeAccountingAccount']/*[local-name()='ID']",
        ),
        "invoiced_object": None,
        "invoiced_object_scheme": None,
        "preceding_invoices": [item["id"] for item in preceding_invoice_documents],
        "preceding_invoice_documents": preceding_invoice_documents,
        "additional_documents": [],
    }
    if agreement is not None:
        for ref in agreement.xpath("./*[local-name()='AdditionalReferencedDocument']"):
            if not isinstance(ref, etree._Element):
                continue
            type_code = first_text(ref, "./*[local-name()='TypeCode']")
            if type_code == "50":
                references["tender"] = references["tender"] or first_text(
                    ref,
                    "./*[local-name()='IssuerAssignedID']",
                )
                continue
            if type_code == "130":
                if references["invoiced_object"] is None:
                    invoiced_object = _reference_identifier(ref)
                    references["invoiced_object"] = (invoiced_object or {}).get("value")
                    references["invoiced_object_scheme"] = (invoiced_object or {}).get("scheme")
                continue
            if type_code != "916":
                continue
            attachment = first_node(ref, "./*[local-name()='AttachmentBinaryObject']")
            references["additional_documents"].append(
                {
                    "id": first_text(ref, "./*[local-name()='IssuerAssignedID']"),
                    "type_code": type_code,
                    "name": first_text(ref, "./*[local-name()='Name']"),
                    "description": None,
                    "attachment_filename": attr_value(attachment, "filename"),
                    "attachment_mime": attr_value(attachment, "mimeCode"),
                    "external_uri": first_text(ref, "./*[local-name()='URIID']"),
                }
            )

    seller = _parse_party(first_node(agreement, "./*[local-name()='SellerTradeParty']"))
    buyer = _parse_party(first_node(agreement, "./*[local-name()='BuyerTradeParty']"))
    payee = _parse_party(first_node(settlement, "./*[local-name()='PayeeTradeParty']"))
    invoicee = _parse_party(first_node(settlement, "./*[local-name()='InvoiceeTradeParty']"))
    seller_tax_representative = _parse_party(
        first_node(agreement, "./*[local-name()='SellerTaxRepresentativeTradeParty']")
    )
    ship_to = _parse_party(first_node(delivery, "./*[local-name()='ShipToTradeParty']"))

    root_namespace = root.nsmap.get(root.prefix) if root.prefix else root.nsmap.get(None)
    format_name = "UN/CEFACT CrossIndustryInvoice (CII)"
    if root_namespace and root_namespace.endswith(":100"):
        format_name += " D16B/EN 16931"

    notes = unique_nonempty(all_text(document, "./*[local-name()='IncludedNote']/*[local-name()='Content']"))
    type_code = first_text(document, "./*[local-name()='TypeCode']")
    document_data = document_meta(
        syntax="CII",
        format_name=format_name,
        profile_id=profile_id,
        document_id=first_text(document, "./*[local-name()='ID']"),
        type_code=type_code,
        issue_date=_date_from_node(issue_node),
        due_date=due_date,
        tax_point_date=tax_point_date,
        delivery_date=delivery_date,
        currency=currency,
        buyer_reference=first_text(agreement, "./*[local-name()='BuyerReference']"),
        notes=notes,
        root_kind=local_name(root),
    )
    document_data["tax_point_date_code"] = tax_point_date_code
    document_data["vat_accounting_currency"] = vat_accounting_currency

    return {
        "document": document_data,
        "seller": seller,
        "buyer": buyer,
        "payee": payee,
        "invoicee": invoicee,
        "seller_tax_representative": seller_tax_representative,
        "ship_to": ship_to,
        "lines": lines,
        "taxes": taxes,
        "totals": totals,
        "payment": {
            "reference": first_text(settlement, "./*[local-name()='PaymentReference']"),
            "means": payment_means,
            "terms": payment_terms,
        },
        "references": references,
        "invoice_period": invoice_period,
        "header_allowances_charges": header_allowances_charges,
        "delivery": {
            "date": delivery_date,
            "despatch_advice_reference": first_text(
                delivery, "./*[local-name()='DespatchAdviceReferencedDocument']/*[local-name()='IssuerAssignedID']"
            ),
            "receiving_advice_reference": first_text(
                delivery, "./*[local-name()='ReceivingAdviceReferencedDocument']/*[local-name()='IssuerAssignedID']"
            ),
        },
        "profile": {
            "id": profile_id,
            "name": profile_name(profile_id),
            "business_process_id": first_text(
                context,
                "./*[local-name()='BusinessProcessSpecifiedDocumentContextParameter']/*[local-name()='ID']",
            ),
        },
    }
