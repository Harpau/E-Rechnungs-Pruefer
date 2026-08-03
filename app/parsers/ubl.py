from __future__ import annotations

import re
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


def _coalesce_node(*values: etree._Element | None) -> etree._Element | None:
    for value in values:
        if value is not None:
            return value
    return None


def _append_unique(entries: list[dict], entry: dict | None) -> None:
    if not entry:
        return
    key = (entry.get("value"), entry.get("scheme"))
    if key not in {(item.get("value"), item.get("scheme")) for item in entries}:
        entries.append(entry)


def _parse_address(address: etree._Element | None) -> dict | None:
    if address is None:
        return None
    country_code = first_text(address, "./*[local-name()='Country']/*[local-name()='IdentificationCode']")
    additional_lines = all_text(address, "./*[local-name()='AddressLine']/*[local-name()='Line']")
    return {
        "line1": first_text(address, "./*[local-name()='StreetName']"),
        "line2": first_text(address, "./*[local-name()='AdditionalStreetName']"),
        "line3": additional_lines[0] if additional_lines else None,
        "postcode": first_text(address, "./*[local-name()='PostalZone']"),
        "city": first_text(address, "./*[local-name()='CityName']"),
        "subdivision": first_text(address, "./*[local-name()='CountrySubentity']"),
        "country_code": country_code,
        "country": readable_country(country_code),
    }


def _parse_party(wrapper: etree._Element | None) -> dict:
    result = empty_party()
    if wrapper is None:
        return result
    party = first_node(wrapper, "./*[local-name()='Party']")
    if party is None:
        party = wrapper

    legal_entity = first_node(party, "./*[local-name()='PartyLegalEntity']")
    party_name = first_node(party, "./*[local-name()='PartyName']")
    result["name"] = first_text(legal_entity, "./*[local-name()='RegistrationName']") or first_text(
        party_name, "./*[local-name()='Name']"
    )
    result["trading_name"] = first_text(party_name, "./*[local-name()='Name']")
    result["description"] = first_text(legal_entity, "./*[local-name()='CompanyLegalForm']")

    for node in party.xpath("./*[local-name()='PartyIdentification']/*[local-name()='ID']"):
        if isinstance(node, etree._Element):
            _append_unique(result["ids"], id_entry(node))
    company_id = first_node(legal_entity, "./*[local-name()='CompanyID']")
    _append_unique(result["legal_registration_ids"], id_entry(company_id))

    for tax_scheme in party.xpath("./*[local-name()='PartyTaxScheme']"):
        if not isinstance(tax_scheme, etree._Element):
            continue
        company = first_node(tax_scheme, "./*[local-name()='CompanyID']")
        entry = id_entry(company)
        if entry and not entry.get("scheme"):
            entry["scheme"] = first_text(tax_scheme, "./*[local-name()='TaxScheme']/*[local-name()='ID']")
        _append_unique(result["tax_ids"], entry)

    result["endpoint"] = id_entry(first_node(party, "./*[local-name()='EndpointID']"))

    contact = first_node(party, "./*[local-name()='Contact']")
    if contact is not None:
        result["contact"] = {
            "name": first_text(contact, "./*[local-name()='Name']"),
            "department": first_text(contact, "./*[local-name()='Department']"),
            "phone": first_text(contact, "./*[local-name()='Telephone']"),
            "email": first_text(contact, "./*[local-name()='ElectronicMail']"),
        }

    address = _parse_address(first_node(party, "./*[local-name()='PostalAddress']"))
    if address is not None:
        result["address"] = address
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
    amount_node = first_node(node, "./*[local-name()='Amount']")
    basis_node = first_node(node, "./*[local-name()='BaseAmount']")
    tax_category = first_node(node, "./*[local-name()='TaxCategory']")
    category = first_text(tax_category, "./*[local-name()='ID']")
    return {
        "type": item_type,
        "type_label": type_label,
        "indicator_raw": indicator_raw,
        "amount": clean_text(amount_node),
        "currency": attr_value(amount_node, "currencyID"),
        "basis_amount": clean_text(basis_node),
        "basis_currency": attr_value(basis_node, "currencyID"),
        "percent": first_text(node, "./*[local-name()='MultiplierFactorNumeric']"),
        "reason": first_text(node, "./*[local-name()='AllowanceChargeReason']"),
        "reason_code": first_text(node, "./*[local-name()='AllowanceChargeReasonCode']"),
        "tax_category": category,
        "tax_category_label": readable_tax_category(category),
        "tax_category_display": readable_tax_category_display(category),
        "tax_rate": first_text(tax_category, "./*[local-name()='Percent']"),
        "tax_type": first_text(tax_category, "./*[local-name()='TaxScheme']/*[local-name()='ID']"),
    }


def _parse_period(node: etree._Element | None) -> dict | None:
    if node is None:
        return None
    start = parse_date_value(first_text(node, "./*[local-name()='StartDate']"))
    end = parse_date_value(first_text(node, "./*[local-name()='EndDate']"))
    description = first_text(node, "./*[local-name()='Description']")
    if not any((start, end, description)):
        return None
    return {"start": start, "end": end, "description": description}


_NOTE_SUBJECT_PREFIX = re.compile(r"^#([A-Z]{3})#")


def _parse_document_notes(root: etree._Element) -> list[dict[str, str | None]]:
    notes: list[dict[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    for raw_text in all_text(root, "./*[local-name()='Note']"):
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
    quantity = first_node(line, f"./*[local-name()='{quantity_name}']")
    if quantity is None:
        quantity = _coalesce_node(
            first_node(line, "./*[local-name()='InvoicedQuantity']"),
            first_node(line, "./*[local-name()='CreditedQuantity']"),
        )
    line_total = first_node(line, "./*[local-name()='LineExtensionAmount']")
    item = first_node(line, "./*[local-name()='Item']")
    price = first_node(line, "./*[local-name()='Price']")
    price_amount = first_node(price, "./*[local-name()='PriceAmount']")
    base_quantity = first_node(price, "./*[local-name()='BaseQuantity']")
    gross_price_amount = first_node(price, "./*[local-name()='GrossPrice']/*[local-name()='PriceAmount']")
    if gross_price_amount is None:
        gross_price_amount = first_node(price, "./*[local-name()='GrossPrice']/*[local-name()='Amount']")
    price_allowance = None
    if price is not None:
        for candidate in price.xpath("./*[local-name()='AllowanceCharge']"):
            if not isinstance(candidate, etree._Element):
                continue
            indicator = (first_text(candidate, "./*[local-name()='ChargeIndicator']") or "").casefold()
            if indicator in {"false", "0"}:
                price_allowance = candidate
                break
    price_allowance_amount = first_node(price_allowance, "./*[local-name()='Amount']")
    price_allowance_base = first_node(price_allowance, "./*[local-name()='BaseAmount']")
    if gross_price_amount is None:
        gross_price_amount = price_allowance_base
    tax = first_node(item, "./*[local-name()='ClassifiedTaxCategory']")
    category = first_text(tax, "./*[local-name()='ID']")
    standard_id = first_node(item, "./*[local-name()='StandardItemIdentification']/*[local-name()='ID']")

    classifications: list[dict[str, str | None]] = []
    if item is not None:
        for classification in item.xpath("./*[local-name()='CommodityClassification']"):
            if not isinstance(classification, etree._Element):
                continue
            code_node = first_node(classification, "./*[local-name()='ItemClassificationCode']")
            classifications.append(
                {
                    "code": clean_text(code_node),
                    "scheme": attr_value(code_node, "listID"),
                    "version": attr_value(code_node, "listVersionID"),
                    "name": attr_value(code_node, "name"),
                }
            )

    properties: list[dict[str, str | None]] = []
    if item is not None:
        for prop in item.xpath("./*[local-name()='AdditionalItemProperty']"):
            if not isinstance(prop, etree._Element):
                continue
            properties.append(
                {
                    "name": first_text(prop, "./*[local-name()='Name']"),
                    "value": first_text(prop, "./*[local-name()='Value']"),
                }
            )

    allowances_charges = [
        _parse_allowance_charge(item_node)
        for item_node in line.xpath("./*[local-name()='AllowanceCharge']")
        if isinstance(item_node, etree._Element)
    ]

    origin = first_text(item, "./*[local-name()='OriginCountry']/*[local-name()='IdentificationCode']")
    notes = unique_nonempty(
        all_text(line, "./*[local-name()='Note']") + all_text(item, "./*[local-name()='Description']")
    )

    return {
        "id": first_text(line, "./*[local-name()='ID']"),
        "name": first_text(item, "./*[local-name()='Name']"),
        "description": first_text(item, "./*[local-name()='Description']"),
        "seller_item_id": first_text(item, "./*[local-name()='SellersItemIdentification']/*[local-name()='ID']"),
        "buyer_item_id": first_text(item, "./*[local-name()='BuyersItemIdentification']/*[local-name()='ID']"),
        "standard_item_id": clean_text(standard_id),
        "standard_item_scheme": attr_value(standard_id, "schemeID"),
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
            "./*[local-name()='MultiplierFactorNumeric']",
        ),
        "base_quantity": clean_text(base_quantity) or "1",
        "base_unit_code": attr_value(base_quantity, "unitCode") or attr_value(quantity, "unitCode"),
        "base_unit_label": readable_unit(attr_value(base_quantity, "unitCode") or attr_value(quantity, "unitCode")),
        "line_total": clean_text(line_total),
        "line_currency": attr_value(line_total, "currencyID"),
        "tax_category": category,
        "tax_category_label": readable_tax_category(category),
        "tax_category_display": readable_tax_category_display(category),
        "tax_rate": first_text(tax, "./*[local-name()='Percent']"),
        "tax_type": first_text(tax, "./*[local-name()='TaxScheme']/*[local-name()='ID']"),
        "allowances_charges": allowances_charges,
        "notes": notes,
        "period": _parse_period(first_node(line, "./*[local-name()='InvoicePeriod']")),
        "order_line_reference": first_text(line, "./*[local-name()='OrderLineReference']/*[local-name()='LineID']"),
        "object_identifier": id_entry(first_node(line, "./*[local-name()='DocumentReference']/*[local-name()='ID']")),
        "accounting_cost": first_text(line, "./*[local-name()='AccountingCost']"),
        "classifications": classifications,
        "origin_country": origin,
        "origin_country_label": readable_country(origin),
        "additional_properties": properties,
    }


def _parse_tax_subtotal(subtotal: etree._Element) -> dict:
    category_node = first_node(subtotal, "./*[local-name()='TaxCategory']")
    category = first_text(category_node, "./*[local-name()='ID']")
    amount = first_node(subtotal, "./*[local-name()='TaxAmount']")
    basis = first_node(subtotal, "./*[local-name()='TaxableAmount']")
    return {
        "type": first_text(category_node, "./*[local-name()='TaxScheme']/*[local-name()='ID']"),
        "category_code": category,
        "category_label": readable_tax_category(category),
        "category_display": readable_tax_category_display(category),
        "rate": first_text(category_node, "./*[local-name()='Percent']"),
        "basis_amount": clean_text(basis),
        "basis_label": readable_tax_basis_label(category),
        "basis_currency": attr_value(basis, "currencyID"),
        "tax_amount": clean_text(amount),
        "tax_currency": attr_value(amount, "currencyID"),
        "exemption_reason": first_text(category_node, "./*[local-name()='TaxExemptionReason']"),
        "exemption_reason_code": first_text(category_node, "./*[local-name()='TaxExemptionReasonCode']"),
    }


def _parse_payment_means(node: etree._Element) -> dict:
    code_node = first_node(node, "./*[local-name()='PaymentMeansCode']")
    code = clean_text(code_node)
    account = first_node(node, "./*[local-name()='PayeeFinancialAccount']")
    account_id = first_node(account, "./*[local-name()='ID']")
    institution = first_node(account, "./*[local-name()='FinancialInstitutionBranch']")
    institution_id = first_node(institution, "./*[local-name()='ID']")
    mandate = first_node(node, "./*[local-name()='PaymentMandate']")
    debited_account_id = first_node(
        mandate,
        "./*[local-name()='PayerFinancialAccount']/*[local-name()='ID']",
    )
    card = first_node(node, "./*[local-name()='CardAccount']")
    account_entry = id_entry(account_id)
    institution_entry = id_entry(institution_id)
    debited_account_entry = id_entry(debited_account_id)
    account_scheme = ((account_entry or {}).get("scheme") or "").upper()
    institution_scheme = ((institution_entry or {}).get("scheme") or "").upper()
    debited_account_scheme = ((debited_account_entry or {}).get("scheme") or "").upper()
    return {
        "type_code": code,
        "type_label": readable_payment_means(code),
        "information": attr_value(code_node, "name") or first_text(node, "./*[local-name()='InstructionNote']"),
        "account_id": account_entry,
        "iban": (account_entry or {}).get("value") if account_scheme == "IBAN" else None,
        "account_name": first_text(account, "./*[local-name()='Name']"),
        "service_provider_id": institution_entry,
        "bic": (institution_entry or {}).get("value") if institution_scheme in {"BIC", "BICFI"} else None,
        "debited_account_id": debited_account_entry,
        "payer_iban": ((debited_account_entry or {}).get("value") if debited_account_scheme == "IBAN" else None),
        "mandate_reference": first_text(mandate, "./*[local-name()='ID']"),
        "creditor_id": id_entry(
            first_node(
                mandate,
                "./*[local-name()='PayerParty']/*[local-name()='PartyIdentification']/*[local-name()='ID']",
            )
        ),
        "card_account": first_text(card, "./*[local-name()='PrimaryAccountNumberID']"),
        "card_holder_name": first_text(card, "./*[local-name()='HolderName']"),
        "payment_id": first_text(node, "./*[local-name()='PaymentID']"),
    }


def parse_ubl(root: etree._Element) -> dict[str, Any]:
    root_kind = local_name(root)
    profile_id = first_text(root, "./*[local-name()='CustomizationID']")
    business_process_id = first_text(root, "./*[local-name()='ProfileID']")
    issue_date = parse_date_value(first_text(root, "./*[local-name()='IssueDate']"))
    due_date = parse_date_value(first_text(root, "./*[local-name()='DueDate']"))
    tax_point_date = parse_date_value(first_text(root, "./*[local-name()='TaxPointDate']"))
    delivery_date = parse_date_value(
        first_text(root, "./*[local-name()='Delivery']/*[local-name()='ActualDeliveryDate']")
        or first_text(root, "./*[local-name()='Delivery']/*[local-name()='LatestDeliveryDate']")
    )
    currency = first_text(root, "./*[local-name()='DocumentCurrencyCode']")
    tax_currency = first_text(root, "./*[local-name()='TaxCurrencyCode']")
    type_code = first_text(root, "./*[local-name()='InvoiceTypeCode']") or first_text(
        root, "./*[local-name()='CreditNoteTypeCode']"
    )
    invoice_period_node = first_node(root, "./*[local-name()='InvoicePeriod']")
    invoice_period = _parse_period(invoice_period_node)
    tax_point_date_code = first_text(invoice_period_node, "./*[local-name()='DescriptionCode']")

    payment_means_nodes = [
        item for item in root.xpath("./*[local-name()='PaymentMeans']") if isinstance(item, etree._Element)
    ]
    if root_kind == "CreditNote" and due_date is None:
        for payment_means_node in payment_means_nodes:
            due_date = parse_date_value(first_text(payment_means_node, "./*[local-name()='PaymentDueDate']"))
            if due_date is not None:
                break

    lines = [
        _parse_line(item, root_kind)
        for item in root.xpath("./*[local-name()='InvoiceLine'] | ./*[local-name()='CreditNoteLine']")
        if isinstance(item, etree._Element)
    ]

    taxes: list[dict] = []
    tax_total_amounts: list[etree._Element] = []
    for tax_total in root.xpath("./*[local-name()='TaxTotal']"):
        if not isinstance(tax_total, etree._Element):
            continue
        tax_amount = first_node(tax_total, "./*[local-name()='TaxAmount']")
        if tax_amount is not None:
            tax_total_amounts.append(tax_amount)
        subtotals = [
            item for item in tax_total.xpath("./*[local-name()='TaxSubtotal']") if isinstance(item, etree._Element)
        ]
        amount_currency = attr_value(tax_amount, "currencyID")
        if subtotals and (currency is None or amount_currency == currency):
            taxes.extend(_parse_tax_subtotal(item) for item in subtotals)

    monetary = first_node(root, "./*[local-name()='LegalMonetaryTotal']")
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

    line_total = first_node(monetary, "./*[local-name()='LineExtensionAmount']")
    allowance_total = first_node(monetary, "./*[local-name()='AllowanceTotalAmount']")
    charge_total = first_node(monetary, "./*[local-name()='ChargeTotalAmount']")
    tax_basis_total = first_node(monetary, "./*[local-name()='TaxExclusiveAmount']")
    grand_total = first_node(monetary, "./*[local-name()='TaxInclusiveAmount']")
    prepaid_amount = first_node(monetary, "./*[local-name()='PrepaidAmount']")
    rounding_amount = first_node(monetary, "./*[local-name()='PayableRoundingAmount']")
    due_payable_amount = first_node(monetary, "./*[local-name()='PayableAmount']")
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

    payment_terms: list[dict[str, str | None]] = []
    for term in root.xpath("./*[local-name()='PaymentTerms']"):
        if not isinstance(term, etree._Element):
            continue
        term_due = parse_date_value(first_text(term, "./*[local-name()='PaymentDueDate']"))
        due_date = due_date or term_due
        partial_payment = first_node(term, "./*[local-name()='Amount']")
        payment_terms.append(
            {
                "description": first_text(term, "./*[local-name()='Note']"),
                "due_date": term_due,
                "direct_debit_mandate_id": None,
                "partial_payment_amount": clean_text(partial_payment),
                "partial_payment_currency": attr_value(partial_payment, "currencyID"),
            }
        )

    payment_means = [_parse_payment_means(item) for item in payment_means_nodes]

    header_allowances_charges = [
        _parse_allowance_charge(item)
        for item in root.xpath("./*[local-name()='AllowanceCharge']")
        if isinstance(item, etree._Element)
    ]

    preceding_invoice_documents: list[dict[str, str | None]] = []
    for reference in root.xpath("./*[local-name()='BillingReference']/*[local-name()='InvoiceDocumentReference']"):
        if not isinstance(reference, etree._Element):
            continue
        reference_id = first_text(reference, "./*[local-name()='ID']")
        if reference_id is None:
            continue
        preceding_invoice_documents.append(
            {
                "id": reference_id,
                "issue_date": parse_date_value(first_text(reference, "./*[local-name()='IssueDate']")),
            }
        )

    references: dict[str, Any] = {
        "buyer_order": first_text(root, "./*[local-name()='OrderReference']/*[local-name()='ID']"),
        "seller_order": first_text(root, "./*[local-name()='OrderReference']/*[local-name()='SalesOrderID']"),
        "contract": first_text(root, "./*[local-name()='ContractDocumentReference']/*[local-name()='ID']"),
        "tender": first_text(root, "./*[local-name()='OriginatorDocumentReference']/*[local-name()='ID']"),
        "project": first_text(root, "./*[local-name()='ProjectReference']/*[local-name()='ID']"),
        "buyer_accounting_reference": first_text(root, "./*[local-name()='AccountingCost']"),
        "invoiced_object": None,
        "preceding_invoices": [item["id"] for item in preceding_invoice_documents],
        "preceding_invoice_documents": preceding_invoice_documents,
        "additional_documents": [],
    }
    for ref in root.xpath("./*[local-name()='AdditionalDocumentReference']"):
        if not isinstance(ref, etree._Element):
            continue
        reference_type_code = first_text(ref, "./*[local-name()='DocumentTypeCode']")
        if reference_type_code == "130":
            if references["invoiced_object"] is None:
                references["invoiced_object"] = id_entry(first_node(ref, "./*[local-name()='ID']"))
            continue
        attachment = first_node(ref, "./*[local-name()='Attachment']/*[local-name()='EmbeddedDocumentBinaryObject']")
        references["additional_documents"].append(
            {
                "id": id_entry(first_node(ref, "./*[local-name()='ID']")),
                "type_code": reference_type_code,
                "name": first_text(ref, "./*[local-name()='DocumentType']"),
                "description": first_text(ref, "./*[local-name()='DocumentDescription']"),
                "attachment_filename": attr_value(attachment, "filename"),
                "attachment_mime": attr_value(attachment, "mimeCode"),
                "external_uri": first_text(
                    ref, "./*[local-name()='Attachment']/*[local-name()='ExternalReference']/*[local-name()='URI']"
                ),
            }
        )

    seller = _parse_party(first_node(root, "./*[local-name()='AccountingSupplierParty']"))
    buyer = _parse_party(first_node(root, "./*[local-name()='AccountingCustomerParty']"))
    payee = _parse_party(first_node(root, "./*[local-name()='PayeeParty']"))
    invoicee = _parse_party(None)
    seller_tax_representative = _parse_party(first_node(root, "./*[local-name()='TaxRepresentativeParty']"))
    delivery = first_node(root, "./*[local-name()='Delivery']")
    delivery_location = first_node(delivery, "./*[local-name()='DeliveryLocation']")
    delivery_location_id = first_node(delivery_location, "./*[local-name()='ID']")
    delivery_address = _parse_address(first_node(delivery_location, "./*[local-name()='Address']"))
    ship_to = _parse_party(first_node(delivery, "./*[local-name()='DeliveryParty']"))

    notes = _parse_document_notes(root)
    format_name = "OASIS UBL 2.1 Invoice" if root_kind == "Invoice" else "OASIS UBL 2.1 CreditNote"

    document = document_meta(
        syntax="UBL",
        format_name=format_name,
        profile_id=profile_id,
        document_id=first_text(root, "./*[local-name()='ID']"),
        type_code=type_code,
        issue_date=issue_date,
        due_date=due_date,
        tax_point_date=tax_point_date,
        delivery_date=delivery_date,
        currency=currency,
        buyer_reference=first_text(root, "./*[local-name()='BuyerReference']"),
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
            "reference": first_text(root, "./*[local-name()='PaymentMeans']/*[local-name()='PaymentID']"),
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
            "despatch_advice_reference": first_text(
                root, "./*[local-name()='DespatchDocumentReference']/*[local-name()='ID']"
            ),
            "receiving_advice_reference": first_text(
                root, "./*[local-name()='ReceiptDocumentReference']/*[local-name()='ID']"
            ),
        },
        "profile": {
            "id": profile_id,
            "name": profile_name(profile_id),
            "business_process_id": business_process_id,
            "ubl_version": first_text(root, "./*[local-name()='UBLVersionID']"),
        },
    }
