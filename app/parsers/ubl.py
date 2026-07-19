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
    result["description"] = first_text(party, "./*[local-name()='MarkCareIndicator']")

    for node in party.xpath("./*[local-name()='PartyIdentification']/*[local-name()='ID']"):
        if isinstance(node, etree._Element):
            _append_unique(result["ids"], id_entry(node))
    company_id = first_node(legal_entity, "./*[local-name()='CompanyID']")
    _append_unique(result["ids"], id_entry(company_id))

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

    address = first_node(party, "./*[local-name()='PostalAddress']")
    if address is not None:
        country_code = first_text(address, "./*[local-name()='Country']/*[local-name()='IdentificationCode']")
        additional_lines = all_text(address, "./*[local-name()='AddressLine']/*[local-name()='Line']")
        result["address"] = {
            "line1": first_text(address, "./*[local-name()='StreetName']"),
            "line2": first_text(address, "./*[local-name()='AdditionalStreetName']")
            or (additional_lines[0] if additional_lines else None),
            "line3": additional_lines[1] if len(additional_lines) > 1 else None,
            "postcode": first_text(address, "./*[local-name()='PostalZone']"),
            "city": first_text(address, "./*[local-name()='CityName']"),
            "subdivision": first_text(address, "./*[local-name()='CountrySubentity']"),
            "country_code": country_code,
            "country": readable_country(country_code),
        }
    return result


def _parse_allowance_charge(node: etree._Element) -> dict:
    indicator = (first_text(node, "./*[local-name()='ChargeIndicator']") or "").lower()
    is_charge = indicator in {"true", "1", "yes"}
    amount_node = first_node(node, "./*[local-name()='Amount']")
    basis_node = first_node(node, "./*[local-name()='BaseAmount']")
    return {
        "type": "charge" if is_charge else "allowance",
        "type_label": "Zuschlag" if is_charge else "Nachlass",
        "amount": clean_text(amount_node),
        "currency": attr_value(amount_node, "currencyID"),
        "basis_amount": clean_text(basis_node),
        "basis_currency": attr_value(basis_node, "currencyID"),
        "percent": first_text(node, "./*[local-name()='MultiplierFactorNumeric']"),
        "reason": first_text(node, "./*[local-name()='AllowanceChargeReason']"),
        "reason_code": first_text(node, "./*[local-name()='AllowanceChargeReasonCode']"),
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
        "accounting_cost": first_text(line, "./*[local-name()='AccountingCost']"),
        "classifications": classifications,
        "origin_country": origin,
        "origin_country_label": readable_country(origin),
        "additional_properties": properties,
    }


def _parse_tax_subtotal(subtotal: etree._Element, fallback_tax_amount: etree._Element | None = None) -> dict:
    category_node = first_node(subtotal, "./*[local-name()='TaxCategory']")
    category = first_text(category_node, "./*[local-name()='ID']")
    amount = first_node(subtotal, "./*[local-name()='TaxAmount']")
    if amount is None:
        amount = fallback_tax_amount
    basis = first_node(subtotal, "./*[local-name()='TaxableAmount']")
    return {
        "type": first_text(category_node, "./*[local-name()='TaxScheme']/*[local-name()='ID']") or "VAT",
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
    mandate = first_node(node, "./*[local-name()='PaymentMandate']")
    card = first_node(node, "./*[local-name()='CardAccount']")
    return {
        "type_code": code,
        "type_label": readable_payment_means(code),
        "information": attr_value(code_node, "name") or first_text(node, "./*[local-name()='InstructionNote']"),
        "iban": clean_text(account_id),
        "account_name": first_text(account, "./*[local-name()='Name']"),
        "bic": first_text(institution, "./*[local-name()='ID']"),
        "payer_iban": None,
        "mandate_reference": first_text(mandate, "./*[local-name()='ID']"),
        "creditor_id": first_text(
            mandate, "./*[local-name()='PayerParty']/*[local-name()='PartyIdentification']/*[local-name()='ID']"
        ),
        "card_account": first_text(card, "./*[local-name()='PrimaryAccountNumberID']"),
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
    type_code = first_text(root, "./*[local-name()='InvoiceTypeCode']") or first_text(
        root, "./*[local-name()='CreditNoteTypeCode']"
    )

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
        if subtotals:
            taxes.extend(_parse_tax_subtotal(item, tax_amount) for item in subtotals)

    if not taxes and tax_total_amounts:
        first_amount = tax_total_amounts[0]
        taxes.append(
            {
                "type": "VAT",
                "category_code": None,
                "category_label": None,
                "rate": None,
                "basis_amount": None,
                "basis_currency": None,
                "tax_amount": clean_text(first_amount),
                "tax_currency": attr_value(first_amount, "currencyID"),
                "exemption_reason": None,
                "exemption_reason_code": None,
            }
        )

    monetary = first_node(root, "./*[local-name()='LegalMonetaryTotal']")
    tax_total = None
    for amount in tax_total_amounts:
        amount_currency = attr_value(amount, "currencyID")
        if amount_currency == currency or tax_total is None:
            tax_total = clean_text(amount)
        if amount_currency == currency:
            break

    totals = {
        "line_total": first_text(monetary, "./*[local-name()='LineExtensionAmount']"),
        "allowance_total": first_text(monetary, "./*[local-name()='AllowanceTotalAmount']"),
        "charge_total": first_text(monetary, "./*[local-name()='ChargeTotalAmount']"),
        "tax_basis_total": first_text(monetary, "./*[local-name()='TaxExclusiveAmount']"),
        "tax_total": tax_total,
        "grand_total": first_text(monetary, "./*[local-name()='TaxInclusiveAmount']"),
        "prepaid_amount": first_text(monetary, "./*[local-name()='PrepaidAmount']"),
        "rounding_amount": first_text(monetary, "./*[local-name()='PayableRoundingAmount']"),
        "due_payable_amount": first_text(monetary, "./*[local-name()='PayableAmount']"),
        "currency": currency,
    }

    payment_terms: list[dict[str, str | None]] = []
    for term in root.xpath("./*[local-name()='PaymentTerms']"):
        if not isinstance(term, etree._Element):
            continue
        term_due = parse_date_value(first_text(term, "./*[local-name()='PaymentDueDate']"))
        due_date = due_date or term_due
        payment_terms.append(
            {
                "description": first_text(term, "./*[local-name()='Note']"),
                "due_date": term_due,
                "direct_debit_mandate_id": None,
                "partial_payment_amount": first_text(term, "./*[local-name()='Amount']"),
            }
        )

    payment_means = [
        _parse_payment_means(item)
        for item in root.xpath("./*[local-name()='PaymentMeans']")
        if isinstance(item, etree._Element)
    ]

    header_allowances_charges = [
        _parse_allowance_charge(item)
        for item in root.xpath("./*[local-name()='AllowanceCharge']")
        if isinstance(item, etree._Element)
    ]

    references: dict[str, Any] = {
        "buyer_order": first_text(root, "./*[local-name()='OrderReference']/*[local-name()='ID']"),
        "seller_order": first_text(root, "./*[local-name()='OrderReference']/*[local-name()='SalesOrderID']"),
        "contract": first_text(root, "./*[local-name()='ContractDocumentReference']/*[local-name()='ID']"),
        "project": first_text(root, "./*[local-name()='ProjectReference']/*[local-name()='ID']"),
        "preceding_invoices": all_text(
            root,
            "./*[local-name()='BillingReference']/*[local-name()='InvoiceDocumentReference']/*[local-name()='ID']",
        ),
        "additional_documents": [],
    }
    for ref in root.xpath("./*[local-name()='AdditionalDocumentReference']"):
        if not isinstance(ref, etree._Element):
            continue
        attachment = first_node(ref, "./*[local-name()='Attachment']/*[local-name()='EmbeddedDocumentBinaryObject']")
        references["additional_documents"].append(
            {
                "id": first_text(ref, "./*[local-name()='ID']"),
                "type_code": first_text(ref, "./*[local-name()='DocumentTypeCode']"),
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
    invoicee = _parse_party(first_node(root, "./*[local-name()='TaxRepresentativeParty']"))
    ship_to = _parse_party(first_node(root, "./*[local-name()='Delivery']/*[local-name()='DeliveryParty']"))

    notes = unique_nonempty(all_text(root, "./*[local-name()='Note']"))
    format_name = "OASIS UBL 2.1 Invoice" if root_kind == "Invoice" else "OASIS UBL 2.1 CreditNote"

    return {
        "document": document_meta(
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
            notes=notes,
            root_kind=root_kind,
        ),
        "seller": seller,
        "buyer": buyer,
        "payee": payee,
        "invoicee": invoicee,
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
        "header_allowances_charges": header_allowances_charges,
        "delivery": {
            "date": delivery_date,
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
