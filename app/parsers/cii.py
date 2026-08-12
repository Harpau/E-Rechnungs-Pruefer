from __future__ import annotations

from typing import Any

from lxml import etree

from ..xml_utils import (
    all_text as _all_text,
)
from ..xml_utils import (
    attr_value,
    element_text,
    local_name,
    parse_date_value,
    unique_nonempty,
)
from ..xml_utils import (
    first_node as _first_node,
)
from ..xml_utils import (
    first_text as _first_text,
)
from ..xml_utils import (
    nodes as _nodes,
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
from .namespaces import CII_NAMESPACES


def nodes(node: etree._Element | None, expression: str) -> list[etree._Element]:
    return _nodes(node, expression, namespaces=CII_NAMESPACES)


def first_node(node: etree._Element | None, expression: str) -> etree._Element | None:
    return _first_node(node, expression, namespaces=CII_NAMESPACES)


def first_text(node: etree._Element | None, expression: str) -> str | None:
    return _first_text(node, expression, namespaces=CII_NAMESPACES)


def all_text(node: etree._Element | None, expression: str) -> list[str]:
    return _all_text(node, expression, namespaces=CII_NAMESPACES)


def _date_from_node(node: etree._Element | None) -> str | None:
    if node is None:
        return None
    date_node = first_node(
        node,
        "./udt:DateTimeString | ./qdt:DateTimeString | ./udt:DateString | ./udt:DateTime | ./udt:Date",
    )
    if date_node is None:
        return None
    return parse_date_value(element_text(date_node), attr_value(date_node, "format"))


def _append_unique(
    entries: list[dict],
    seen: set[tuple[Any, Any]],
    entry: dict | None,
) -> None:
    if not entry:
        return
    key = (entry.get("value"), entry.get("scheme"))
    if key not in seen:
        seen.add(key)
        entries.append(entry)


def _parse_period(node: etree._Element | None) -> dict | None:
    if node is None:
        return None
    start = _date_from_node(first_node(node, "./ram:StartDateTime"))
    end = _date_from_node(first_node(node, "./ram:EndDateTime"))
    description = first_text(node, "./ram:Description")
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
    candidates = nodes(parent, f"./ram:{element_name}")
    if currency is None:
        return candidates[0] if candidates else None
    return next((item for item in candidates if attr_value(item, "currencyID") == currency), None)


def _reference_identifier(node: etree._Element | None) -> dict | None:
    value = first_text(node, "./ram:IssuerAssignedID")
    if value is None:
        return None
    return {
        "value": value,
        "scheme": first_text(node, "./ram:ReferenceTypeCode"),
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
        node = first_node(account, f"./ram:{element_name}")
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
    identifier_keys: set[tuple[Any, Any]] = set()
    legal_registration_keys: set[tuple[Any, Any]] = set()
    tax_identifier_keys: set[tuple[Any, Any]] = set()

    legal_org = first_node(party, "./ram:SpecifiedLegalOrganization")
    result["name"] = first_text(party, "./ram:Name")
    result["trading_name"] = first_text(legal_org, "./ram:TradingBusinessName")
    result["description"] = first_text(party, "./ram:Description")

    for node in nodes(party, "./ram:ID | ./ram:GlobalID"):
        _append_unique(result["ids"], identifier_keys, id_entry(node))
    for node in nodes(legal_org, "./ram:ID | ./ram:GlobalID"):
        _append_unique(
            result["legal_registration_ids"],
            legal_registration_keys,
            id_entry(node),
        )

    for node in nodes(party, "./ram:SpecifiedTaxRegistration/ram:ID"):
        _append_unique(result["tax_ids"], tax_identifier_keys, id_entry(node))

    endpoint_node = first_node(
        party,
        "./ram:URIUniversalCommunication/ram:URIID",
    )
    endpoint = id_entry(endpoint_node)
    result["endpoint"] = endpoint

    contact = first_node(party, "./ram:DefinedTradeContact")
    if contact is not None:
        result["contact"] = {
            "name": first_text(contact, "./ram:PersonName"),
            "department": first_text(contact, "./ram:DepartmentName"),
            "phone": first_text(
                contact,
                "./ram:TelephoneUniversalCommunication/ram:CompleteNumber",
            ),
            "email": first_text(
                contact,
                "./ram:EmailURIUniversalCommunication/ram:URIID",
            ),
        }

    address = first_node(party, "./ram:PostalTradeAddress")
    if address is not None:
        country_code = first_text(address, "./ram:CountryID")
        result["address"] = {
            "line1": first_text(address, "./ram:LineOne"),
            "line2": first_text(address, "./ram:LineTwo"),
            "line3": first_text(address, "./ram:LineThree"),
            "postcode": first_text(address, "./ram:PostcodeCode"),
            "city": first_text(address, "./ram:CityName"),
            "subdivision": first_text(address, "./ram:CountrySubDivisionName"),
            "country_code": country_code,
            "country": readable_country(country_code),
        }
    return result


def _indicator_text(node: etree._Element | None) -> str | None:
    return first_text(
        node,
        "./ram:ChargeIndicator/udt:Indicator | ./ram:ChargeIndicator/udt:IndicatorString",
    )


def _parse_allowance_charge(node: etree._Element) -> dict:
    indicator_raw = _indicator_text(node)
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
    amount_node = first_node(node, "./ram:ActualAmount")
    if amount_node is None:
        amount_node = first_node(node, "./ram:ChargeAmount")
    basis_node = first_node(node, "./ram:BasisAmount")
    tax = first_node(node, "./ram:CategoryTradeTax")
    category = first_text(tax, "./ram:CategoryCode")
    return {
        "type": item_type,
        "type_label": type_label,
        "indicator_raw": indicator_raw,
        "amount": element_text(amount_node),
        "currency": attr_value(amount_node, "currencyID"),
        "basis_amount": element_text(basis_node),
        "basis_currency": attr_value(basis_node, "currencyID"),
        "percent": first_text(node, "./ram:CalculationPercent"),
        "reason": first_text(node, "./ram:Reason"),
        "reason_code": first_text(node, "./ram:ReasonCode"),
        "tax_category": category,
        "tax_category_label": readable_tax_category(category),
        "tax_category_display": readable_tax_category_display(category),
        "tax_rate": first_text(tax, "./ram:RateApplicablePercent"),
        "tax_type": first_text(tax, "./ram:TypeCode"),
    }


def _parse_line(line: etree._Element) -> dict:
    doc = first_node(line, "./ram:AssociatedDocumentLineDocument")
    product = first_node(line, "./ram:SpecifiedTradeProduct")
    agreement = first_node(line, "./ram:SpecifiedLineTradeAgreement")
    delivery = first_node(line, "./ram:SpecifiedLineTradeDelivery")
    settlement = first_node(line, "./ram:SpecifiedLineTradeSettlement")

    price_node = first_node(agreement, "./ram:NetPriceProductTradePrice")
    price_amount = first_node(price_node, "./ram:ChargeAmount")
    basis_quantity = first_node(price_node, "./ram:BasisQuantity")
    gross_price_node = first_node(agreement, "./ram:GrossPriceProductTradePrice")
    gross_price_amount = first_node(gross_price_node, "./ram:ChargeAmount")
    price_allowance = None
    if gross_price_node is not None:
        for candidate in nodes(gross_price_node, "./ram:AppliedTradeAllowanceCharge"):
            indicator = (_indicator_text(candidate) or "").casefold()
            if indicator in {"false", "0"}:
                price_allowance = candidate
                break
    price_allowance_amount = first_node(price_allowance, "./ram:ActualAmount")
    quantity = first_node(delivery, "./ram:BilledQuantity")
    line_total = first_node(
        settlement,
        "./ram:SpecifiedTradeSettlementLineMonetarySummation/ram:LineTotalAmount",
    )
    tax = first_node(settlement, "./ram:ApplicableTradeTax")
    category = first_text(tax, "./ram:CategoryCode")

    allowances_charges = (
        [_parse_allowance_charge(item) for item in nodes(settlement, "./ram:SpecifiedTradeAllowanceCharge")]
        if settlement is not None
        else []
    )
    classifications: list[dict[str, str | None]] = []
    if product is not None:
        for classification in nodes(product, "./ram:DesignatedProductClassification"):
            class_code = first_node(classification, "./ram:ClassCode")
            classifications.append(
                {
                    "code": element_text(class_code),
                    "scheme": attr_value(class_code, "listID"),
                    "version": attr_value(class_code, "listVersionID"),
                    "name": first_text(classification, "./ram:ClassName"),
                }
            )

    properties: list[dict[str, str | None]] = []
    if product is not None:
        for prop in nodes(product, "./ram:ApplicableProductCharacteristic"):
            properties.append(
                {
                    "name": first_text(prop, "./ram:Description") or first_text(prop, "./ram:TypeCode"),
                    "value": first_text(prop, "./ram:Value"),
                }
            )

    standard_id_node = first_node(product, "./ram:GlobalID")
    origin = first_text(product, "./ram:OriginTradeCountry/ram:ID")

    period_node = first_node(settlement, "./ram:BillingSpecifiedPeriod")
    start_node = first_node(period_node, "./ram:StartDateTime")
    end_node = first_node(period_node, "./ram:EndDateTime")
    period = None
    if period_node is not None:
        period = {
            "start": _date_from_node(start_node),
            "end": _date_from_node(end_node),
            "description": None,
        }
        if not period["start"] and not period["end"]:
            period = None

    notes = unique_nonempty(all_text(doc, "./ram:IncludedNote/ram:Content") + all_text(product, "./ram:Description"))
    object_reference = _reference_identifier(
        first_node(
            settlement,
            "./ram:AdditionalReferencedDocument[ram:TypeCode='130']",
        )
    )

    return {
        "id": first_text(doc, "./ram:LineID"),
        "name": first_text(product, "./ram:Name"),
        "description": first_text(product, "./ram:Description"),
        "seller_item_id": first_text(product, "./ram:SellerAssignedID"),
        "buyer_item_id": first_text(product, "./ram:BuyerAssignedID"),
        "standard_item_id": element_text(standard_id_node),
        "standard_item_scheme": attr_value(standard_id_node, "schemeID"),
        "quantity": element_text(quantity),
        "unit_code": attr_value(quantity, "unitCode"),
        "unit_label": readable_unit(attr_value(quantity, "unitCode")),
        "price": element_text(price_amount),
        "price_currency": attr_value(price_amount, "currencyID"),
        "gross_price": element_text(gross_price_amount),
        "gross_price_currency": attr_value(gross_price_amount, "currencyID"),
        "price_allowance": element_text(price_allowance_amount),
        "price_allowance_currency": attr_value(price_allowance_amount, "currencyID"),
        "price_allowance_percent": first_text(
            price_allowance,
            "./ram:CalculationPercent",
        ),
        "base_quantity": element_text(basis_quantity),
        "base_unit_code": attr_value(basis_quantity, "unitCode"),
        "base_unit_label": readable_unit(attr_value(basis_quantity, "unitCode")),
        "line_total": element_text(line_total),
        "line_currency": attr_value(line_total, "currencyID"),
        "tax_category": category,
        "tax_category_label": readable_tax_category(category),
        "tax_category_display": readable_tax_category_display(category),
        "tax_rate": first_text(tax, "./ram:RateApplicablePercent"),
        "tax_type": first_text(tax, "./ram:TypeCode"),
        "allowances_charges": allowances_charges,
        "notes": notes,
        "period": period,
        "order_line_reference": first_text(
            agreement,
            "./ram:BuyerOrderReferencedDocument/ram:LineID",
        ),
        "object_identifier": (object_reference or {}).get("value"),
        "object_identifier_scheme": (object_reference or {}).get("scheme"),
        "accounting_cost": first_text(settlement, "./ram:ReceivableSpecifiedTradeAccountingAccount/ram:ID"),
        "classifications": classifications,
        "origin_country": origin,
        "origin_country_label": readable_country(origin),
        "additional_properties": properties,
    }


def _parse_tax(tax: etree._Element) -> dict:
    amount = first_node(tax, "./ram:CalculatedAmount")
    basis = first_node(tax, "./ram:BasisAmount")
    category = first_text(tax, "./ram:CategoryCode")
    exemption_reasons = all_text(tax, "./ram:ExemptionReason")
    return {
        "type": first_text(tax, "./ram:TypeCode"),
        "category_code": category,
        "category_label": readable_tax_category(category),
        "category_display": readable_tax_category_display(category),
        "rate": first_text(tax, "./ram:RateApplicablePercent"),
        "basis_amount": element_text(basis),
        "basis_label": readable_tax_basis_label(category),
        "basis_currency": attr_value(basis, "currencyID"),
        "tax_amount": element_text(amount),
        "tax_currency": attr_value(amount, "currencyID"),
        "exemption_reason": " | ".join(exemption_reasons) if exemption_reasons else None,
        "exemption_reason_code": first_text(tax, "./ram:ExemptionReasonCode"),
    }


def _parse_payment_means(node: etree._Element) -> dict:
    type_code = first_text(node, "./ram:TypeCode")
    account = first_node(node, "./ram:PayeePartyCreditorFinancialAccount")
    institution = first_node(node, "./ram:PayeeSpecifiedCreditorFinancialInstitution")
    payer_account = first_node(node, "./ram:PayerPartyDebtorFinancialAccount")
    mandate = first_node(node, "./ram:ApplicableTradePaymentMandate")
    card = first_node(node, "./ram:ApplicableTradeSettlementFinancialCard")
    creditor_id = first_node(mandate, "./ram:CreditorReferenceID")
    account_entry = _financial_identifier(account, iban=True)
    institution_entry = _financial_identifier(institution, bic=True)
    payer_account_entry = _financial_identifier(payer_account, iban=True)
    account_scheme = ((account_entry or {}).get("scheme") or "").upper()
    institution_scheme = ((institution_entry or {}).get("scheme") or "").upper()
    payer_account_scheme = ((payer_account_entry or {}).get("scheme") or "").upper()
    return {
        "type_code": type_code,
        "type_label": readable_payment_means(type_code),
        "information": first_text(node, "./ram:Information"),
        "account_id": account_entry,
        "iban": (account_entry or {}).get("value") if account_scheme == "IBAN" else None,
        "account_name": first_text(account, "./ram:AccountName"),
        "service_provider_id": institution_entry,
        "bic": ((institution_entry or {}).get("value") if institution_scheme in {"BIC", "BICFI"} else None),
        "debited_account_id": payer_account_entry,
        "payer_iban": ((payer_account_entry or {}).get("value") if payer_account_scheme == "IBAN" else None),
        "mandate_reference": first_text(mandate, "./ram:ID"),
        "creditor_id": id_entry(creditor_id),
        "card_account": first_text(card, "./ram:ID"),
        "card_holder_name": first_text(card, "./ram:CardholderName"),
    }


def parse_cii(root: etree._Element) -> dict[str, Any]:
    context = first_node(root, "./rsm:ExchangedDocumentContext")
    document = first_node(root, "./rsm:ExchangedDocument")
    transaction = first_node(root, "./rsm:SupplyChainTradeTransaction")
    agreement = first_node(transaction, "./ram:ApplicableHeaderTradeAgreement")
    delivery = first_node(transaction, "./ram:ApplicableHeaderTradeDelivery")
    settlement = first_node(transaction, "./ram:ApplicableHeaderTradeSettlement")

    profile_id = first_text(
        context,
        "./ram:GuidelineSpecifiedDocumentContextParameter/ram:ID",
    )
    issue_node = first_node(document, "./ram:IssueDateTime")
    delivery_event = first_node(delivery, "./ram:ActualDeliverySupplyChainEvent")
    delivery_date = _date_from_node(first_node(delivery_event, "./ram:OccurrenceDateTime"))

    terms_nodes = nodes(settlement, "./ram:SpecifiedTradePaymentTerms")
    payment_terms: list[dict[str, str | None]] = []
    due_date: str | None = None
    for term in terms_nodes:
        term_due = _date_from_node(first_node(term, "./ram:DueDateDateTime"))
        partial_payment = first_node(term, "./ram:PartialPaymentAmount")
        due_date = due_date or term_due
        payment_terms.append(
            {
                "description": first_text(term, "./ram:Description"),
                "due_date": term_due,
                "direct_debit_mandate_id": first_text(term, "./ram:DirectDebitMandateID"),
                "partial_payment_amount": element_text(partial_payment),
                "partial_payment_currency": attr_value(partial_payment, "currencyID"),
            }
        )

    tax_point_date = _date_from_node(
        first_node(
            settlement,
            "./ram:ApplicableTradeTax/ram:TaxPointDate",
        )
    )
    tax_point_date_code = first_text(
        settlement,
        "./ram:ApplicableTradeTax/ram:DueDateTypeCode",
    )
    currency = first_text(settlement, "./ram:InvoiceCurrencyCode")
    vat_accounting_currency = first_text(settlement, "./ram:TaxCurrencyCode")
    invoice_period = _parse_period(first_node(settlement, "./ram:BillingSpecifiedPeriod"))

    lines = (
        [_parse_line(item) for item in nodes(transaction, "./ram:IncludedSupplyChainTradeLineItem")]
        if transaction is not None
        else []
    )

    taxes = (
        [_parse_tax(item) for item in nodes(settlement, "./ram:ApplicableTradeTax")] if settlement is not None else []
    )

    monetary = first_node(settlement, "./ram:SpecifiedTradeSettlementHeaderMonetarySummation")
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
        "line_total": element_text(line_total),
        "line_total_currency": attr_value(line_total, "currencyID"),
        "allowance_total": element_text(allowance_total),
        "allowance_total_currency": attr_value(allowance_total, "currencyID"),
        "charge_total": element_text(charge_total),
        "charge_total_currency": attr_value(charge_total, "currencyID"),
        "tax_basis_total": element_text(tax_basis_total),
        "tax_basis_total_currency": attr_value(tax_basis_total, "currencyID"),
        "tax_total": element_text(document_tax_total),
        "tax_total_currency": attr_value(document_tax_total, "currencyID"),
        "tax_total_accounting": element_text(accounting_tax_total),
        "tax_total_accounting_currency": attr_value(accounting_tax_total, "currencyID"),
        "grand_total": element_text(grand_total),
        "grand_total_currency": attr_value(grand_total, "currencyID"),
        "prepaid_amount": element_text(prepaid_amount),
        "prepaid_amount_currency": attr_value(prepaid_amount, "currencyID"),
        "rounding_amount": element_text(rounding_amount),
        "rounding_amount_currency": attr_value(rounding_amount, "currencyID"),
        "due_payable_amount": element_text(due_payable_amount),
        "due_payable_amount_currency": attr_value(due_payable_amount, "currencyID"),
        "currency": currency,
    }

    payment_means = (
        [_parse_payment_means(item) for item in nodes(settlement, "./ram:SpecifiedTradeSettlementPaymentMeans")]
        if settlement is not None
        else []
    )

    header_allowances_charges = (
        [_parse_allowance_charge(item) for item in nodes(settlement, "./ram:SpecifiedTradeAllowanceCharge")]
        if settlement is not None
        else []
    )

    preceding_invoice_documents: list[dict[str, str | None]] = []
    if settlement is not None:
        for ref in nodes(settlement, "./ram:InvoiceReferencedDocument"):
            reference_id = first_text(ref, "./ram:IssuerAssignedID")
            if reference_id is None:
                continue
            preceding_invoice_documents.append(
                {
                    "id": reference_id,
                    "issue_date": _date_from_node(first_node(ref, "./ram:FormattedIssueDateTime")),
                }
            )

    references: dict[str, Any] = {
        "buyer_order": first_text(agreement, "./ram:BuyerOrderReferencedDocument/ram:IssuerAssignedID"),
        "seller_order": first_text(agreement, "./ram:SellerOrderReferencedDocument/ram:IssuerAssignedID"),
        "contract": first_text(agreement, "./ram:ContractReferencedDocument/ram:IssuerAssignedID"),
        "tender": None,
        "project": first_text(agreement, "./ram:SpecifiedProcuringProject/ram:ID")
        or first_text(agreement, "./ram:ProjectReferencedDocument/ram:IssuerAssignedID"),
        "buyer_accounting_reference": first_text(
            settlement,
            "./ram:ReceivableSpecifiedTradeAccountingAccount/ram:ID",
        ),
        "invoiced_object": None,
        "invoiced_object_scheme": None,
        "preceding_invoices": [item["id"] for item in preceding_invoice_documents],
        "preceding_invoice_documents": preceding_invoice_documents,
        "additional_documents": [],
    }
    if agreement is not None:
        for ref in nodes(agreement, "./ram:AdditionalReferencedDocument"):
            type_code = first_text(ref, "./ram:TypeCode")
            if type_code == "50":
                references["tender"] = references["tender"] or first_text(
                    ref,
                    "./ram:IssuerAssignedID",
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
            attachment = first_node(ref, "./ram:AttachmentBinaryObject")
            references["additional_documents"].append(
                {
                    "id": first_text(ref, "./ram:IssuerAssignedID"),
                    "type_code": type_code,
                    "name": first_text(ref, "./ram:Name"),
                    "description": None,
                    "attachment_filename": attr_value(attachment, "filename"),
                    "attachment_mime": attr_value(attachment, "mimeCode"),
                    "external_uri": first_text(ref, "./ram:URIID"),
                }
            )

    seller = _parse_party(first_node(agreement, "./ram:SellerTradeParty"))
    buyer = _parse_party(first_node(agreement, "./ram:BuyerTradeParty"))
    payee = _parse_party(first_node(settlement, "./ram:PayeeTradeParty"))
    invoicee = _parse_party(first_node(settlement, "./ram:InvoiceeTradeParty"))
    seller_tax_representative = _parse_party(first_node(agreement, "./ram:SellerTaxRepresentativeTradeParty"))
    ship_to = _parse_party(first_node(delivery, "./ram:ShipToTradeParty"))

    root_namespace = root.nsmap.get(root.prefix) if root.prefix else root.nsmap.get(None)
    format_name = "UN/CEFACT CrossIndustryInvoice (CII)"
    if root_namespace and root_namespace.endswith(":100"):
        format_name += " D16B/EN 16931"

    notes = unique_nonempty(all_text(document, "./ram:IncludedNote/ram:Content"))
    type_code = first_text(document, "./ram:TypeCode")
    document_data = document_meta(
        syntax="CII",
        format_name=format_name,
        profile_id=profile_id,
        document_id=first_text(document, "./ram:ID"),
        type_code=type_code,
        issue_date=_date_from_node(issue_node),
        due_date=due_date,
        tax_point_date=tax_point_date,
        delivery_date=delivery_date,
        currency=currency,
        buyer_reference=first_text(agreement, "./ram:BuyerReference"),
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
            "reference": first_text(settlement, "./ram:PaymentReference"),
            "means": payment_means,
            "terms": payment_terms,
        },
        "references": references,
        "invoice_period": invoice_period,
        "header_allowances_charges": header_allowances_charges,
        "delivery": {
            "date": delivery_date,
            "despatch_advice_reference": first_text(
                delivery, "./ram:DespatchAdviceReferencedDocument/ram:IssuerAssignedID"
            ),
            "receiving_advice_reference": first_text(
                delivery, "./ram:ReceivingAdviceReferencedDocument/ram:IssuerAssignedID"
            ),
        },
        "profile": {
            "id": profile_id,
            "name": profile_name(profile_id),
            "business_process_id": first_text(
                context,
                "./ram:BusinessProcessSpecifiedDocumentContextParameter/ram:ID",
            ),
        },
    }
