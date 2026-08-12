from __future__ import annotations

import re
from typing import Any

from lxml import etree

from ..xml_utils import (
    all_text as _all_text,
)
from ..xml_utils import (
    attr_value,
    element_text,
    local_name,
    parse_xsd_date_value,
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
from .namespaces import UBL_NAMESPACES


def nodes(node: etree._Element | None, expression: str) -> list[etree._Element]:
    return _nodes(node, expression, namespaces=UBL_NAMESPACES)


def first_node(node: etree._Element | None, expression: str) -> etree._Element | None:
    return _first_node(node, expression, namespaces=UBL_NAMESPACES)


def first_text(node: etree._Element | None, expression: str) -> str | None:
    return _first_text(node, expression, namespaces=UBL_NAMESPACES)


def all_text(node: etree._Element | None, expression: str) -> list[str]:
    return _all_text(node, expression, namespaces=UBL_NAMESPACES)


def _coalesce_node(*values: etree._Element | None) -> etree._Element | None:
    for value in values:
        if value is not None:
            return value
    return None


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


def _parse_address(address: etree._Element | None) -> dict | None:
    if address is None:
        return None
    country_code = first_text(address, "./cac:Country/cbc:IdentificationCode")
    additional_lines = all_text(address, "./cac:AddressLine/cbc:Line")
    return {
        "line1": first_text(address, "./cbc:StreetName"),
        "line2": first_text(address, "./cbc:AdditionalStreetName"),
        "line3": additional_lines[0] if additional_lines else None,
        "postcode": first_text(address, "./cbc:PostalZone"),
        "city": first_text(address, "./cbc:CityName"),
        "subdivision": first_text(address, "./cbc:CountrySubentity"),
        "country_code": country_code,
        "country": readable_country(country_code),
    }


def _parse_party(wrapper: etree._Element | None) -> dict:
    result = empty_party()
    if wrapper is None:
        return result
    identifier_keys: set[tuple[Any, Any]] = set()
    legal_registration_keys: set[tuple[Any, Any]] = set()
    tax_identifier_keys: set[tuple[Any, Any]] = set()
    party = first_node(wrapper, "./cac:Party")
    if party is None:
        party = wrapper

    legal_entity = first_node(party, "./cac:PartyLegalEntity")
    party_name = first_node(party, "./cac:PartyName")
    result["name"] = first_text(legal_entity, "./cbc:RegistrationName") or first_text(party_name, "./cbc:Name")
    result["trading_name"] = first_text(party_name, "./cbc:Name")
    result["description"] = first_text(legal_entity, "./cbc:CompanyLegalForm")

    for node in nodes(party, "./cac:PartyIdentification/cbc:ID"):
        _append_unique(result["ids"], identifier_keys, id_entry(node))
    company_id = first_node(legal_entity, "./cbc:CompanyID")
    _append_unique(
        result["legal_registration_ids"],
        legal_registration_keys,
        id_entry(company_id),
    )

    for tax_scheme in nodes(party, "./cac:PartyTaxScheme"):
        company = first_node(tax_scheme, "./cbc:CompanyID")
        entry = id_entry(company)
        if entry and not entry.get("scheme"):
            entry["scheme"] = first_text(tax_scheme, "./cac:TaxScheme/cbc:ID")
        _append_unique(result["tax_ids"], tax_identifier_keys, entry)

    result["endpoint"] = id_entry(first_node(party, "./cbc:EndpointID"))

    contact = first_node(party, "./cac:Contact")
    if contact is not None:
        result["contact"] = {
            "name": first_text(contact, "./cbc:Name"),
            "department": first_text(contact, "./cbc:Department"),
            "phone": first_text(contact, "./cbc:Telephone"),
            "email": first_text(contact, "./cbc:ElectronicMail"),
        }

    address = _parse_address(first_node(party, "./cac:PostalAddress"))
    if address is not None:
        result["address"] = address
    return result


def _parse_allowance_charge(node: etree._Element) -> dict:
    indicator_raw = first_text(node, "./cbc:ChargeIndicator")
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
    amount_node = first_node(node, "./cbc:Amount")
    basis_node = first_node(node, "./cbc:BaseAmount")
    tax_category = first_node(node, "./cac:TaxCategory")
    category = first_text(tax_category, "./cbc:ID")
    return {
        "type": item_type,
        "type_label": type_label,
        "indicator_raw": indicator_raw,
        "amount": element_text(amount_node),
        "currency": attr_value(amount_node, "currencyID"),
        "basis_amount": element_text(basis_node),
        "basis_currency": attr_value(basis_node, "currencyID"),
        "percent": first_text(node, "./cbc:MultiplierFactorNumeric"),
        "reason": first_text(node, "./cbc:AllowanceChargeReason"),
        "reason_code": first_text(node, "./cbc:AllowanceChargeReasonCode"),
        "tax_category": category,
        "tax_category_label": readable_tax_category(category),
        "tax_category_display": readable_tax_category_display(category),
        "tax_rate": first_text(tax_category, "./cbc:Percent"),
        "tax_type": first_text(tax_category, "./cac:TaxScheme/cbc:ID"),
    }


def _parse_period(node: etree._Element | None) -> dict | None:
    if node is None:
        return None
    start = parse_xsd_date_value(first_text(node, "./cbc:StartDate"))
    end = parse_xsd_date_value(first_text(node, "./cbc:EndDate"))
    description = first_text(node, "./cbc:Description")
    if not any((start, end, description)):
        return None
    return {"start": start, "end": end, "description": description}


_NOTE_SUBJECT_PREFIX = re.compile(r"^#([A-Z]{3})#")


def _parse_document_notes(root: etree._Element) -> list[dict[str, str | None]]:
    notes: list[dict[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    for raw_text in all_text(root, "./cbc:Note"):
        text = raw_text
        subject_code = None
        prefix = _NOTE_SUBJECT_PREFIX.match(raw_text)
        if prefix is not None:
            candidate_text = raw_text[prefix.end() :].strip()
            if candidate_text:
                text = candidate_text
                subject_code = prefix.group(1)
        key = (text, subject_code)
        if key not in seen:
            seen.add(key)
            notes.append({"text": text, "subject_code": subject_code})
    return notes


def _parse_line(line: etree._Element, root_kind: str) -> dict:
    quantity_name = "CreditedQuantity" if root_kind == "CreditNote" else "InvoicedQuantity"
    quantity = first_node(line, f"./cbc:{quantity_name}")
    if quantity is None:
        quantity = _coalesce_node(
            first_node(line, "./cbc:InvoicedQuantity"),
            first_node(line, "./cbc:CreditedQuantity"),
        )
    line_total = first_node(line, "./cbc:LineExtensionAmount")
    item = first_node(line, "./cac:Item")
    price = first_node(line, "./cac:Price")
    price_amount = first_node(price, "./cbc:PriceAmount")
    base_quantity = first_node(price, "./cbc:BaseQuantity")
    gross_price_amount = first_node(price, "./cac:GrossPrice/cbc:PriceAmount")
    if gross_price_amount is None:
        gross_price_amount = first_node(price, "./cac:GrossPrice/cbc:Amount")
    price_allowance = None
    if price is not None:
        for candidate in nodes(price, "./cac:AllowanceCharge"):
            indicator = (first_text(candidate, "./cbc:ChargeIndicator") or "").casefold()
            if indicator in {"false", "0"}:
                price_allowance = candidate
                break
    price_allowance_amount = first_node(price_allowance, "./cbc:Amount")
    price_allowance_base = first_node(price_allowance, "./cbc:BaseAmount")
    if gross_price_amount is None:
        gross_price_amount = price_allowance_base
    tax = first_node(item, "./cac:ClassifiedTaxCategory")
    category = first_text(tax, "./cbc:ID")
    standard_id = first_node(item, "./cac:StandardItemIdentification/cbc:ID")

    classifications: list[dict[str, str | None]] = []
    if item is not None:
        for classification in nodes(item, "./cac:CommodityClassification"):
            code_node = first_node(classification, "./cbc:ItemClassificationCode")
            classifications.append(
                {
                    "code": element_text(code_node),
                    "scheme": attr_value(code_node, "listID"),
                    "version": attr_value(code_node, "listVersionID"),
                    "name": attr_value(code_node, "name"),
                }
            )

    properties: list[dict[str, str | None]] = []
    if item is not None:
        for prop in nodes(item, "./cac:AdditionalItemProperty"):
            properties.append(
                {
                    "name": first_text(prop, "./cbc:Name"),
                    "value": first_text(prop, "./cbc:Value"),
                }
            )

    allowances_charges = [_parse_allowance_charge(item_node) for item_node in nodes(line, "./cac:AllowanceCharge")]

    origin = first_text(item, "./cac:OriginCountry/cbc:IdentificationCode")
    notes = unique_nonempty(all_text(line, "./cbc:Note") + all_text(item, "./cbc:Description"))

    return {
        "id": first_text(line, "./cbc:ID"),
        "name": first_text(item, "./cbc:Name"),
        "description": first_text(item, "./cbc:Description"),
        "seller_item_id": first_text(item, "./cac:SellersItemIdentification/cbc:ID"),
        "buyer_item_id": first_text(item, "./cac:BuyersItemIdentification/cbc:ID"),
        "standard_item_id": element_text(standard_id),
        "standard_item_scheme": attr_value(standard_id, "schemeID"),
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
            "./cbc:MultiplierFactorNumeric",
        ),
        "base_quantity": element_text(base_quantity) or "1",
        "base_unit_code": attr_value(base_quantity, "unitCode") or attr_value(quantity, "unitCode"),
        "base_unit_label": readable_unit(attr_value(base_quantity, "unitCode") or attr_value(quantity, "unitCode")),
        "line_total": element_text(line_total),
        "line_currency": attr_value(line_total, "currencyID"),
        "tax_category": category,
        "tax_category_label": readable_tax_category(category),
        "tax_category_display": readable_tax_category_display(category),
        "tax_rate": first_text(tax, "./cbc:Percent"),
        "tax_type": first_text(tax, "./cac:TaxScheme/cbc:ID"),
        "allowances_charges": allowances_charges,
        "notes": notes,
        "period": _parse_period(first_node(line, "./cac:InvoicePeriod")),
        "order_line_reference": first_text(line, "./cac:OrderLineReference/cbc:LineID"),
        "object_identifier": id_entry(first_node(line, "./cac:DocumentReference/cbc:ID")),
        "accounting_cost": first_text(line, "./cbc:AccountingCost"),
        "classifications": classifications,
        "origin_country": origin,
        "origin_country_label": readable_country(origin),
        "additional_properties": properties,
    }


def _parse_tax_subtotal(subtotal: etree._Element) -> dict:
    category_node = first_node(subtotal, "./cac:TaxCategory")
    category = first_text(category_node, "./cbc:ID")
    amount = first_node(subtotal, "./cbc:TaxAmount")
    basis = first_node(subtotal, "./cbc:TaxableAmount")
    return {
        "type": first_text(category_node, "./cac:TaxScheme/cbc:ID"),
        "category_code": category,
        "category_label": readable_tax_category(category),
        "category_display": readable_tax_category_display(category),
        "rate": first_text(category_node, "./cbc:Percent"),
        "basis_amount": element_text(basis),
        "basis_label": readable_tax_basis_label(category),
        "basis_currency": attr_value(basis, "currencyID"),
        "tax_amount": element_text(amount),
        "tax_currency": attr_value(amount, "currencyID"),
        "exemption_reason": first_text(category_node, "./cbc:TaxExemptionReason"),
        "exemption_reason_code": first_text(category_node, "./cbc:TaxExemptionReasonCode"),
    }


def _parse_payment_means(node: etree._Element) -> dict:
    code_node = first_node(node, "./cbc:PaymentMeansCode")
    code = element_text(code_node)
    account = first_node(node, "./cac:PayeeFinancialAccount")
    account_id = first_node(account, "./cbc:ID")
    institution = first_node(account, "./cac:FinancialInstitutionBranch")
    institution_id = first_node(institution, "./cbc:ID")
    mandate = first_node(node, "./cac:PaymentMandate")
    debited_account_id = first_node(
        mandate,
        "./cac:PayerFinancialAccount/cbc:ID",
    )
    card = first_node(node, "./cac:CardAccount")
    account_entry = id_entry(account_id)
    institution_entry = id_entry(institution_id)
    debited_account_entry = id_entry(debited_account_id)
    account_scheme = ((account_entry or {}).get("scheme") or "").upper()
    institution_scheme = ((institution_entry or {}).get("scheme") or "").upper()
    debited_account_scheme = ((debited_account_entry or {}).get("scheme") or "").upper()
    return {
        "type_code": code,
        "type_label": readable_payment_means(code),
        "information": attr_value(code_node, "name") or first_text(node, "./cbc:InstructionNote"),
        "account_id": account_entry,
        "iban": (account_entry or {}).get("value") if account_scheme == "IBAN" else None,
        "account_name": first_text(account, "./cbc:Name"),
        "service_provider_id": institution_entry,
        "bic": (institution_entry or {}).get("value") if institution_scheme in {"BIC", "BICFI"} else None,
        "debited_account_id": debited_account_entry,
        "payer_iban": ((debited_account_entry or {}).get("value") if debited_account_scheme == "IBAN" else None),
        "mandate_reference": first_text(mandate, "./cbc:ID"),
        "creditor_id": id_entry(
            first_node(
                mandate,
                "./cac:PayerParty/cac:PartyIdentification/cbc:ID",
            )
        ),
        "card_account": first_text(card, "./cbc:PrimaryAccountNumberID"),
        "card_holder_name": first_text(card, "./cbc:HolderName"),
        "payment_id": first_text(node, "./cbc:PaymentID"),
    }


def parse_ubl(root: etree._Element) -> dict[str, Any]:
    root_kind = local_name(root)
    profile_id = first_text(root, "./cbc:CustomizationID")
    business_process_id = first_text(root, "./cbc:ProfileID")
    issue_date = parse_xsd_date_value(first_text(root, "./cbc:IssueDate"))
    due_date = parse_xsd_date_value(first_text(root, "./cbc:DueDate"))
    tax_point_date = parse_xsd_date_value(first_text(root, "./cbc:TaxPointDate"))
    delivery_date = parse_xsd_date_value(
        first_text(root, "./cac:Delivery/cbc:ActualDeliveryDate")
        or first_text(root, "./cac:Delivery/cbc:LatestDeliveryDate")
    )
    currency = first_text(root, "./cbc:DocumentCurrencyCode")
    tax_currency = first_text(root, "./cbc:TaxCurrencyCode")
    type_code = first_text(root, "./cbc:InvoiceTypeCode") or first_text(root, "./cbc:CreditNoteTypeCode")
    invoice_period_node = first_node(root, "./cac:InvoicePeriod")
    invoice_period = _parse_period(invoice_period_node)
    tax_point_date_code = first_text(invoice_period_node, "./cbc:DescriptionCode")

    payment_means_nodes = nodes(root, "./cac:PaymentMeans")
    if root_kind == "CreditNote" and due_date is None:
        for payment_means_node in payment_means_nodes:
            due_date = parse_xsd_date_value(first_text(payment_means_node, "./cbc:PaymentDueDate"))
            if due_date is not None:
                break

    lines = [_parse_line(item, root_kind) for item in nodes(root, "./cac:InvoiceLine | ./cac:CreditNoteLine")]

    taxes: list[dict] = []
    tax_total_amounts: list[etree._Element] = []
    for tax_total in nodes(root, "./cac:TaxTotal"):
        tax_amount = first_node(tax_total, "./cbc:TaxAmount")
        if tax_amount is not None:
            tax_total_amounts.append(tax_amount)
        subtotals = nodes(tax_total, "./cac:TaxSubtotal")
        amount_currency = attr_value(tax_amount, "currencyID")
        if subtotals and (currency is None or amount_currency == currency):
            taxes.extend(_parse_tax_subtotal(item) for item in subtotals)

    monetary = first_node(root, "./cac:LegalMonetaryTotal")
    document_tax_total = (
        next(
            (amount for amount in tax_total_amounts if attr_value(amount, "currencyID") == currency),
            None,
        )
        if currency is not None
        else None
    )
    accounting_tax_total = (
        next(
            (amount for amount in tax_total_amounts if attr_value(amount, "currencyID") == tax_currency),
            None,
        )
        if tax_currency is not None
        else None
    )

    line_total = first_node(monetary, "./cbc:LineExtensionAmount")
    allowance_total = first_node(monetary, "./cbc:AllowanceTotalAmount")
    charge_total = first_node(monetary, "./cbc:ChargeTotalAmount")
    tax_basis_total = first_node(monetary, "./cbc:TaxExclusiveAmount")
    grand_total = first_node(monetary, "./cbc:TaxInclusiveAmount")
    prepaid_amount = first_node(monetary, "./cbc:PrepaidAmount")
    rounding_amount = first_node(monetary, "./cbc:PayableRoundingAmount")
    due_payable_amount = first_node(monetary, "./cbc:PayableAmount")
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

    payment_terms: list[dict[str, str | None]] = []
    for term in nodes(root, "./cac:PaymentTerms"):
        term_due = parse_xsd_date_value(first_text(term, "./cbc:PaymentDueDate"))
        due_date = due_date or term_due
        partial_payment = first_node(term, "./cbc:Amount")
        payment_terms.append(
            {
                "description": first_text(term, "./cbc:Note"),
                "due_date": term_due,
                "direct_debit_mandate_id": None,
                "partial_payment_amount": element_text(partial_payment),
                "partial_payment_currency": attr_value(partial_payment, "currencyID"),
            }
        )

    payment_means = [_parse_payment_means(item) for item in payment_means_nodes]

    header_allowances_charges = [_parse_allowance_charge(item) for item in nodes(root, "./cac:AllowanceCharge")]

    preceding_invoice_documents: list[dict[str, str | None]] = []
    for reference in nodes(root, "./cac:BillingReference/cac:InvoiceDocumentReference"):
        reference_id = first_text(reference, "./cbc:ID")
        if reference_id is None:
            continue
        preceding_invoice_documents.append(
            {
                "id": reference_id,
                "issue_date": parse_xsd_date_value(first_text(reference, "./cbc:IssueDate")),
            }
        )

    references: dict[str, Any] = {
        "buyer_order": first_text(root, "./cac:OrderReference/cbc:ID"),
        "seller_order": first_text(root, "./cac:OrderReference/cbc:SalesOrderID"),
        "contract": first_text(root, "./cac:ContractDocumentReference/cbc:ID"),
        "tender": first_text(root, "./cac:OriginatorDocumentReference/cbc:ID"),
        "project": first_text(root, "./cac:ProjectReference/cbc:ID"),
        "buyer_accounting_reference": first_text(root, "./cbc:AccountingCost"),
        "invoiced_object": None,
        "preceding_invoices": [item["id"] for item in preceding_invoice_documents],
        "preceding_invoice_documents": preceding_invoice_documents,
        "additional_documents": [],
    }
    for ref in nodes(root, "./cac:AdditionalDocumentReference"):
        reference_type_code = first_text(ref, "./cbc:DocumentTypeCode")
        if reference_type_code == "130":
            if references["invoiced_object"] is None:
                references["invoiced_object"] = id_entry(first_node(ref, "./cbc:ID"))
            continue
        attachment = first_node(ref, "./cac:Attachment/cbc:EmbeddedDocumentBinaryObject")
        references["additional_documents"].append(
            {
                "id": id_entry(first_node(ref, "./cbc:ID")),
                "type_code": reference_type_code,
                "name": first_text(ref, "./cbc:DocumentType"),
                "description": first_text(ref, "./cbc:DocumentDescription"),
                "attachment_filename": attr_value(attachment, "filename"),
                "attachment_mime": attr_value(attachment, "mimeCode"),
                "external_uri": first_text(ref, "./cac:Attachment/cac:ExternalReference/cbc:URI"),
            }
        )

    seller = _parse_party(first_node(root, "./cac:AccountingSupplierParty"))
    buyer = _parse_party(first_node(root, "./cac:AccountingCustomerParty"))
    payee = _parse_party(first_node(root, "./cac:PayeeParty"))
    invoicee = _parse_party(None)
    seller_tax_representative = _parse_party(first_node(root, "./cac:TaxRepresentativeParty"))
    delivery = first_node(root, "./cac:Delivery")
    delivery_location = first_node(delivery, "./cac:DeliveryLocation")
    delivery_location_id = first_node(delivery_location, "./cbc:ID")
    delivery_address = _parse_address(first_node(delivery_location, "./cac:Address"))
    ship_to = _parse_party(first_node(delivery, "./cac:DeliveryParty"))

    notes = _parse_document_notes(root)
    format_name = "OASIS UBL 2.1 Invoice" if root_kind == "Invoice" else "OASIS UBL 2.1 CreditNote"

    document = document_meta(
        syntax="UBL",
        format_name=format_name,
        profile_id=profile_id,
        document_id=first_text(root, "./cbc:ID"),
        type_code=type_code,
        issue_date=issue_date,
        due_date=due_date,
        tax_point_date=tax_point_date,
        delivery_date=delivery_date,
        currency=currency,
        buyer_reference=first_text(root, "./cbc:BuyerReference"),
        notes=[],
        root_kind=root_kind,
    )
    document["tax_currency"] = tax_currency
    document["tax_point_date_code"] = tax_point_date_code
    document["notes"] = notes

    return {
        "document": document,
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
            "reference": first_text(root, "./cac:PaymentMeans/cbc:PaymentID"),
            "means": payment_means,
            "terms": payment_terms,
        },
        "references": references,
        "invoice_period": invoice_period,
        "header_allowances_charges": header_allowances_charges,
        "delivery": {
            "date": delivery_date,
            "location_id": id_entry(delivery_location_id),
            "address": delivery_address,
            "despatch_advice_reference": first_text(root, "./cac:DespatchDocumentReference/cbc:ID"),
            "receiving_advice_reference": first_text(root, "./cac:ReceiptDocumentReference/cbc:ID"),
        },
        "profile": {
            "id": profile_id,
            "name": profile_name(profile_id),
            "business_process_id": business_process_id,
            "ubl_version": first_text(root, "./cbc:UBLVersionID"),
        },
    }
