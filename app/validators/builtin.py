from __future__ import annotations

import re
from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from ..document_semantics import PartyReference, derive_document_semantics
from ..document_types import (
    DocumentTypeStatus,
    RootCompatibility,
    UblRoot,
    resolve_document_type,
)
from ..profiles import resolve_profile
from ..xml_utils import InvoiceInputError, clean_text, date_object, money_string, xml_decimal_value

TOLERANCE = Decimal("0.02")
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
BANK_ACCOUNT_PAYMENT_CODES = frozenset({"30", "31", "42", "49", "57", "58", "59"})
XRECHNUNG_PROFILE_PATTERN = re.compile(
    r"(?:^|#)urn:xeinkauf\.de:kosit:xrechnung(?:_[0-9]+(?:\.[0-9]+)*)?$",
    re.IGNORECASE,
)
XRECHNUNG_RECOMMENDED_DOCUMENT_TYPES = frozenset({"326", "380", "381", "384", "389", "875", "876", "877"})
_DECIMAL_LEXICAL_PATTERN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
MAX_DECIMAL_DIGITS = 4_096
MAX_DECIMAL_CONTEXT_PRECISION = (3 * MAX_DECIMAL_DIGITS) + 128
_DECIMAL_CONTEXT_GUARD_DIGITS = 64
_DECIMAL_TOTAL_FIELDS = (
    "line_total",
    "allowance_total",
    "charge_total",
    "tax_basis_total",
    "tax_total",
    "tax_total_accounting",
    "grand_total",
    "prepaid_amount",
    "rounding_amount",
    "due_payable_amount",
)
_DECIMAL_LINE_FIELDS = (
    "quantity",
    "price",
    "base_quantity",
    "line_total",
    "gross_price",
    "price_discount_amount",
    "price_allowance",
    "price_discount_percent",
    "price_allowance_percent",
    "tax_rate",
)
_DECIMAL_ALLOWANCE_FIELDS = ("amount", "basis_amount", "percent", "tax_rate")
_DECIMAL_TAX_FIELDS = ("rate", "basis_amount", "tax_amount")


def _finding(
    rule_id: str,
    severity: str,
    title: str,
    message: str,
    *,
    location: str | None = None,
    actual: Any = None,
    expected: Any = None,
    source: str = "Interne Prüfung",
    reference: str | None = None,
    rule_class: str | None = None,
    profile: str | None = None,
    semantic_reference: list[str] | None = None,
    location_label: str | None = None,
    occurrence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": rule_id,
        "severity": severity,
        "title": title,
        "message": message,
        "location": location,
        "actual": None if actual is None else str(actual),
        "expected": None if expected is None else str(expected),
        "source": source,
    }
    if reference is not None:
        finding["reference"] = reference
    if rule_class is not None:
        finding["rule_class"] = rule_class
    if profile is not None:
        finding["profile"] = profile
    if semantic_reference is not None:
        finding["semantic_reference"] = semantic_reference
    if location_label is not None:
        finding["location_label"] = location_label
    if occurrence is not None:
        finding["occurrence"] = dict(occurrence)
    return finding


def _is_close(left: Decimal, right: Decimal, tolerance: Decimal = TOLERANCE) -> bool:
    return abs(left - right) <= tolerance


def _rounded(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _decimal_value_span(value: Any) -> int:
    if isinstance(value, Decimal):
        if not value.is_finite():
            return 0
        parts = value.as_tuple()
        exponent = int(parts.exponent)
        return max(len(parts.digits), max(value.adjusted() + 1, 0) - min(exponent, 0))
    if isinstance(value, int) and not isinstance(value, bool):
        return len(str(abs(value)))
    if isinstance(value, str) and _DECIMAL_LEXICAL_PATTERN.fullmatch(value.strip()) is not None:
        return sum(character.isdigit() for character in value)
    return 0


def _decimal_operands(analysis: dict[str, Any]) -> Iterator[Any]:
    totals = analysis.get("totals")
    if isinstance(totals, dict):
        for key in _DECIMAL_TOTAL_FIELDS:
            yield totals.get(key)

    for tax in analysis.get("taxes") or []:
        if isinstance(tax, dict):
            for key in _DECIMAL_TAX_FIELDS:
                yield tax.get(key)

    for line in analysis.get("lines") or []:
        if not isinstance(line, dict):
            continue
        for key in _DECIMAL_LINE_FIELDS:
            yield line.get(key)
        for item in line.get("allowances_charges") or []:
            if isinstance(item, dict):
                for key in _DECIMAL_ALLOWANCE_FIELDS:
                    yield item.get(key)

    for item in analysis.get("header_allowances_charges") or []:
        if isinstance(item, dict):
            for key in _DECIMAL_ALLOWANCE_FIELDS:
                yield item.get(key)

    payment = analysis.get("payment")
    if isinstance(payment, dict):
        for term in payment.get("terms") or []:
            if isinstance(term, dict):
                yield term.get("partial_payment_amount")


def _decimal_work_precision(source: dict[str, Any]) -> int:
    """Size a hard-bounded Decimal context for exact fixed-point invoice arithmetic."""
    max_span = 0
    operand_count = 0
    for value in _decimal_operands(source):
        span = _decimal_value_span(value)
        if span == 0:
            continue
        if span > MAX_DECIMAL_DIGITS:
            raise InvoiceInputError(
                f"Ein numerisches Rechnungsfeld überschreitet das zulässige Rechenbudget von "
                f"{MAX_DECIMAL_DIGITS} Dezimalziffern."
            )
        max_span = max(max_span, span)
        operand_count += 1

    carry_digits = len(str(max(operand_count, 1)))
    requested = (3 * max_span) + carry_digits + _DECIMAL_CONTEXT_GUARD_DIGITS
    return min(MAX_DECIMAL_CONTEXT_PRECISION, max(64, requested))


def _optional_decimal(value: Any, default: Decimal) -> Decimal | None:
    if clean_text(value) is None:
        return default
    return xml_decimal_value(value)


def _sum_amounts(items: list[dict], item_type: str) -> tuple[Decimal, bool]:
    total = Decimal("0")
    complete = True
    for item in items:
        if item.get("type") != item_type:
            continue
        amount = xml_decimal_value(item.get("amount"))
        if amount is None:
            complete = False
            continue
        total += amount
    return total, complete


def _iban_valid(value: str) -> bool:
    iban = re.sub(r"\s+", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}", iban):
        return False
    rearranged = iban[4:] + iban[:4]
    remainder = 0
    for char in rearranged:
        digits = char if char.isdigit() else str(ord(char) - 55)
        for digit in digits:
            remainder = (remainder * 10 + int(digit)) % 97
    return remainder == 1


def _has_payment_terms(payment: dict[str, Any]) -> bool:
    for term in payment.get("terms") or []:
        if isinstance(term, dict):
            if clean_text(term.get("description")) is not None:
                return True
        elif clean_text(term) is not None:
            return True
    return False


def _is_known_xrechnung_profile(analysis: dict[str, Any]) -> bool:
    profile = analysis.get("profile")
    if isinstance(profile, dict):
        family = clean_text(profile.get("family"))
        status = clean_text(profile.get("status"))
        if family and status and family.casefold() == "xrechnung" and status.casefold() == "known":
            return True

    document = analysis.get("document")
    candidates = [
        document.get("profile_id") if isinstance(document, dict) else None,
        profile.get("id") if isinstance(profile, dict) else None,
    ]
    return any(
        XRECHNUNG_PROFILE_PATTERN.search(identifier) is not None
        for candidate in candidates
        if (identifier := clean_text(candidate)) is not None
    )


def _declared_identifier(
    means: dict[str, Any],
    *,
    generic_key: str,
    legacy_key: str,
    schemes: set[str],
) -> str | None:
    entry = means.get(generic_key)
    if isinstance(entry, dict):
        scheme = clean_text(entry.get("scheme"))
        if scheme is not None and scheme.upper() in schemes:
            return clean_text(entry.get("value"))
    return clean_text(means.get(legacy_key))


def _expected_payment_recipient_name(
    analysis: dict[str, Any],
    payable: Any,
) -> tuple[str, list[str]] | None:
    document = analysis.get("document")
    document_data = document if isinstance(document, dict) else {}
    profile = analysis.get("profile")
    profile_data = profile if isinstance(profile, dict) else {}
    payee = analysis.get("payee")
    payee_data = payee if isinstance(payee, dict) else {}
    payee_name = clean_text(payee_data.get("name"))
    semantics = derive_document_semantics(
        resolve_document_type(clean_text(document_data.get("type_code"))),
        resolve_profile(clean_text(profile_data.get("id")) or clean_text(document_data.get("profile_id"))),
        payable,
        has_payee=payee_name is not None,
    )
    recipient = semantics.settlement.expected_recipient
    if recipient is PartyReference.PAYEE and payee_name is not None:
        return payee_name, ["BG-10"]
    if recipient is PartyReference.SELLER:
        seller = analysis.get("seller")
        seller_data = seller if isinstance(seller, dict) else {}
        seller_name = clean_text(seller_data.get("name"))
        return (seller_name, ["BG-4"]) if seller_name is not None else None
    if recipient is PartyReference.BUYER:
        buyer = analysis.get("buyer")
        buyer_data = buyer if isinstance(buyer, dict) else {}
        buyer_name = clean_text(buyer_data.get("name"))
        return (buyer_name, ["BG-7"]) if buyer_name is not None else None
    return None


def _semantic_text(*values: Any) -> str:
    return " ".join(str(value) for value in values if value not in (None, "")).casefold()


def _reason_indicates_outside_scope(value: str) -> bool:
    patterns = (
        "nicht im inland steuerbar",
        "nicht steuerbar",
        "nicht der umsatzsteuer unterliegend",
        "outside the scope",
        "not subject to vat",
        "leistungsort außerhalb",
        "leistungsort ausserhalb",
        "place of supply outside",
        "§ 3a",
        "paragraph 3a",
    )
    return any(pattern in value for pattern in patterns)


def _reason_indicates_reverse_charge(value: str) -> bool:
    patterns = (
        "steuerschuldnerschaft des leistungsempfängers",
        "steuerschuldnerschaft des leistungsempfaengers",
        "reverse charge",
    )
    return any(pattern in value for pattern in patterns)


def _reason_indicates_export(value: str) -> bool:
    patterns = ("ausfuhr", "export outside", "export außerhalb", "export ausserhalb")
    return any(pattern in value for pattern in patterns)


def _require(findings: list[dict], value: Any, rule_id: str, title: str, location: str) -> None:
    if value is None or value == "" or value == []:
        findings.append(
            _finding(
                rule_id,
                "error",
                title,
                "Ein für die Verarbeitung wesentliches Rechnungsfeld fehlt.",
                location=location,
            )
        )


def _occurrence_data(
    scope: str,
    json_pointer: str,
    *,
    index: int | None = None,
    identifier: str | None = None,
) -> dict[str, Any]:
    return {
        "scope": scope,
        "index": index,
        "identifier": identifier,
        "json_pointer": json_pointer,
    }


def _validate_date_text(
    findings: list[dict[str, Any]],
    value: Any,
    *,
    title: str,
    location: str,
    json_pointer: str,
    scope: str,
    semantic_reference: list[str] | None = None,
    index: int | None = None,
    identifier: str | None = None,
) -> None:
    text = clean_text(value)
    if text is None or date_object(text) is not None:
        return
    findings.append(
        _finding(
            "FORMAT-DATE-001",
            "error",
            title,
            "Der vorhandene Datumswert ist kein gültiges Kalenderdatum.",
            location=location,
            actual=text,
            expected="gültiges Kalenderdatum im Format JJJJ-MM-TT",
            rule_class="core_precheck",
            semantic_reference=semantic_reference,
            occurrence=_occurrence_data(
                scope,
                json_pointer,
                index=index,
                identifier=identifier,
            ),
        )
    )


def _validate_decimal_text(
    findings: list[dict[str, Any]],
    value: Any,
    *,
    title: str,
    location: str,
    json_pointer: str,
    scope: str,
    semantic_reference: list[str] | None = None,
    index: int | None = None,
    identifier: str | None = None,
) -> None:
    text = clean_text(value)
    if text is None or xml_decimal_value(text) is not None:
        return
    findings.append(
        _finding(
            "FORMAT-DECIMAL-001",
            "error",
            title,
            "Der vorhandene Zahlenwert entspricht keinem gültigen XML-Dezimalwert.",
            location=location,
            actual=text,
            expected="endlicher XML-Schema-Decimalwert ohne Komma oder Exponent",
            rule_class="core_precheck",
            semantic_reference=semantic_reference,
            occurrence=_occurrence_data(
                scope,
                json_pointer,
                index=index,
                identifier=identifier,
            ),
        )
    )


def _validate_typed_values(analysis: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    document = analysis.get("document") or {}
    delivery = analysis.get("delivery") or {}
    document_dates = (
        (
            document.get("issue_date"),
            "Rechnungsdatum ist ungültig",
            "BT-2",
            "/document/issue_date",
            "document",
            ["BT-2"],
        ),
        (
            document.get("due_date"),
            "Fälligkeitsdatum ist ungültig",
            "BT-9",
            "/payment/due_date",
            "payment",
            ["BT-9"],
        ),
        (
            document.get("tax_point_date"),
            "Steuerzeitpunkt ist ungültig",
            "BT-7",
            "/document/tax_point_date",
            "document",
            ["BT-7"],
        ),
        (
            document.get("delivery_date") or delivery.get("date"),
            "Lieferdatum ist ungültig",
            "BT-72",
            "/delivery/actual_date",
            "period",
            ["BT-72"],
        ),
    )
    for value, title, location, pointer, scope, semantic_reference in document_dates:
        _validate_date_text(
            findings,
            value,
            title=title,
            location=location,
            json_pointer=pointer,
            scope=scope,
            semantic_reference=semantic_reference,
        )

    periods = analysis.get("periods") or {}
    invoice_period = analysis.get("invoice_period") or periods.get("invoice") or {}
    delivery_period = periods.get("delivery") or {}
    for period, pointer, label in (
        (invoice_period, "/periods/invoice", "Rechnungszeitraum"),
        (delivery_period, "/periods/delivery", "Lieferzeitraum"),
    ):
        if not isinstance(period, dict):
            continue
        for source_key, target_key, suffix in (
            ("start", "start_date", "Beginn"),
            ("start_date", "start_date", "Beginn"),
            ("end", "end_date", "Ende"),
            ("end_date", "end_date", "Ende"),
        ):
            if source_key not in period:
                continue
            _validate_date_text(
                findings,
                period.get(source_key),
                title=f"{label}: {suffix} ist ungültig",
                location=label,
                json_pointer=f"{pointer}/{target_key}",
                scope="period",
            )

    totals = analysis.get("totals") or {}
    total_fields = (
        ("line_total", "Summe der Rechnungspositionen", "BT-106", "/totals/line_net_total", "total"),
        ("allowance_total", "Summe der Nachlässe", "BT-107", "/totals/allowance_total", "total"),
        ("charge_total", "Summe der Zuschläge", "BT-108", "/totals/charge_total", "total"),
        ("tax_basis_total", "Rechnungsbetrag ohne Umsatzsteuer", "BT-109", "/totals/tax_exclusive_total", "total"),
        ("tax_total", "Umsatzsteuerbetrag", "BT-110", "/tax/totals/document_currency", "tax"),
        (
            "tax_total_accounting",
            "Umsatzsteuerbetrag in Abrechnungswährung",
            "BT-111",
            "/tax/totals/vat_accounting_currency",
            "tax",
        ),
        ("grand_total", "Rechnungsbetrag mit Umsatzsteuer", "BT-112", "/totals/tax_inclusive_total", "total"),
        ("prepaid_amount", "Vorauszahlungsbetrag", "BT-113", "/totals/prepaid_total", "total"),
        ("rounding_amount", "Rundungsbetrag", "BT-114", "/totals/rounding", "total"),
        ("due_payable_amount", "Zahlbetrag", "BT-115", "/totals/payable", "total"),
    )
    for source_key, label, location, pointer, scope in total_fields:
        _validate_decimal_text(
            findings,
            totals.get(source_key),
            title=f"{label} ist ungültig",
            location=location,
            json_pointer=pointer,
            scope=scope,
            semantic_reference=[location],
        )

    for tax_index, tax in enumerate(analysis.get("taxes") or []):
        if not isinstance(tax, dict):
            continue
        tax_identifier = clean_text(tax.get("category_code"))
        for source_key, target_key, label in (
            ("rate", "rate_percent", "Steuersatz"),
            ("basis_amount", "taxable_amount", "Steuerbasis"),
            ("tax_amount", "tax_amount", "Steuerbetrag"),
        ):
            _validate_decimal_text(
                findings,
                tax.get(source_key),
                title=f"Steuergruppe {tax_index + 1}: {label} ist ungültig",
                location=f"Steuergruppe {tax_index + 1}",
                json_pointer=f"/tax/breakdown/{tax_index}/{target_key}",
                scope="tax",
                index=tax_index,
                identifier=tax_identifier,
            )

    for line_index, line in enumerate(analysis.get("lines") or []):
        if not isinstance(line, dict):
            continue
        line_identifier = clean_text(line.get("id"))
        line_location = f"Position {line_identifier or line_index + 1}"
        for value, pointer, label in (
            (line.get("gross_price"), f"/lines/{line_index}/price/gross", "Bruttopreis"),
            (
                line.get("price_discount_amount") or line.get("price_allowance"),
                f"/lines/{line_index}/price/discount/amount",
                "Preisnachlassbetrag",
            ),
            (
                line.get("price_discount_percent") or line.get("price_allowance_percent"),
                f"/lines/{line_index}/price/discount/percentage",
                "Preisnachlasssatz",
            ),
            (line.get("tax_rate"), f"/lines/{line_index}/tax_rate_percent", "Steuersatz"),
        ):
            _validate_decimal_text(
                findings,
                value,
                title=f"{line_location}: {label} ist ungültig",
                location=line_location,
                json_pointer=pointer,
                scope="line",
                index=line_index,
                identifier=line_identifier,
            )
        period = line.get("period") or {}
        if isinstance(period, dict):
            for source_key, target_key, suffix in (
                ("start", "start_date", "Beginn"),
                ("start_date", "start_date", "Beginn"),
                ("end", "end_date", "Ende"),
                ("end_date", "end_date", "Ende"),
            ):
                if source_key not in period:
                    continue
                _validate_date_text(
                    findings,
                    period.get(source_key),
                    title=f"{line_location}: Zeitraum-{suffix} ist ungültig",
                    location=line_location,
                    json_pointer=f"/lines/{line_index}/period/{target_key}",
                    scope="line",
                    index=line_index,
                    identifier=line_identifier,
                )
        for allowance_index, item in enumerate(line.get("allowances_charges") or []):
            if not isinstance(item, dict):
                continue
            for source_key, target_key, label in (
                ("amount", "amount", "Betrag"),
                ("basis_amount", "base_amount", "Basisbetrag"),
                ("percent", "percentage", "Prozentsatz"),
                ("tax_rate", "tax_rate_percent", "Steuersatz"),
            ):
                _validate_decimal_text(
                    findings,
                    item.get(source_key),
                    title=f"{line_location}: {label} des Nachlasses oder Zuschlags ist ungültig",
                    location=line_location,
                    json_pointer=f"/lines/{line_index}/allowances_charges/{allowance_index}/{target_key}",
                    scope="line",
                    index=line_index,
                    identifier=line_identifier,
                )

    for item_index, item in enumerate(analysis.get("header_allowances_charges") or []):
        if not isinstance(item, dict):
            continue
        for source_key, target_key, label in (
            ("amount", "amount", "Betrag"),
            ("basis_amount", "base_amount", "Basisbetrag"),
            ("percent", "percentage", "Prozentsatz"),
            ("tax_rate", "tax_rate_percent", "Steuersatz"),
        ):
            _validate_decimal_text(
                findings,
                item.get(source_key),
                title=f"Nachlass oder Zuschlag {item_index + 1}: {label} ist ungültig",
                location=f"Nachlass oder Zuschlag {item_index + 1}",
                json_pointer=f"/allowances_charges/{item_index}/{target_key}",
                scope="allowance-charge",
                index=item_index,
            )

    payment = analysis.get("payment") or {}
    for term_index, term in enumerate(payment.get("terms") or []):
        if not isinstance(term, dict):
            continue
        _validate_date_text(
            findings,
            term.get("due_date"),
            title=f"Zahlungsbedingung {term_index + 1}: Fälligkeitsdatum ist ungültig",
            location=f"Zahlungsbedingung {term_index + 1}",
            json_pointer=f"/payment/terms/{term_index}/due_date",
            scope="payment",
            index=term_index,
        )
        _validate_decimal_text(
            findings,
            term.get("partial_payment_amount"),
            title=f"Zahlungsbedingung {term_index + 1}: Teilzahlungsbetrag ist ungültig",
            location=f"Zahlungsbedingung {term_index + 1}",
            json_pointer=f"/payment/terms/{term_index}/partial_payment",
            scope="payment",
            index=term_index,
        )

    references = analysis.get("references") or {}
    preceding = references.get("preceding_invoice_documents") or []
    for reference_index, reference in enumerate(preceding):
        if not isinstance(reference, dict):
            continue
        reference_id = clean_text(reference.get("id"))
        _validate_date_text(
            findings,
            reference.get("issue_date"),
            title=f"Vorgängerrechnung {reference_index + 1}: Rechnungsdatum ist ungültig",
            location=f"Vorgängerrechnung {reference_index + 1}",
            json_pointer=f"/references/preceding_invoices/{reference_index}/issue_date",
            scope="reference",
            index=reference_index,
            identifier=reference_id,
        )


def _check_date_order(
    findings: list[dict],
    earlier_value: str | None,
    later_value: str | None,
    rule_id: str,
    title: str,
    message: str,
    location: str,
) -> None:
    earlier = date_object(earlier_value)
    later = date_object(later_value)
    if earlier and later and later < earlier:
        findings.append(
            _finding(
                rule_id,
                "warning",
                title,
                message,
                location=location,
                actual=later_value,
                expected=f"nicht vor {earlier_value}",
            )
        )


def _validate_builtin(analysis: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    document = analysis.get("document", {})
    seller = analysis.get("seller", {})
    buyer = analysis.get("buyer", {})
    lines = analysis.get("lines", [])
    totals = analysis.get("totals", {})
    taxes = analysis.get("taxes", [])
    payment = analysis.get("payment", {})

    _require(findings, document.get("id"), "REQ-001", "Rechnungsnummer fehlt", "BT-1")
    _require(findings, document.get("issue_date"), "REQ-002", "Rechnungsdatum fehlt", "BT-2")
    _require(findings, document.get("type_code"), "REQ-003", "Rechnungsart fehlt", "BT-3")
    _require(findings, document.get("currency"), "REQ-004", "Währung fehlt", "BT-5")
    _require(findings, seller.get("name"), "REQ-005", "Verkäufername fehlt", "BT-27")
    _require(findings, buyer.get("name"), "REQ-006", "Käufername fehlt", "BT-44")
    _require(findings, lines, "REQ-007", "Rechnungspositionen fehlen", "BG-25")
    _require(
        findings,
        totals.get("due_payable_amount"),
        "REQ-008",
        "Zahlbetrag fehlt",
        "BT-115",
    )
    _validate_typed_values(analysis, findings)

    type_code = clean_text(document.get("type_code"))
    root_name = clean_text((analysis.get("technical") or {}).get("root_element"))
    ubl_root = UblRoot.INVOICE if root_name == "Invoice" else UblRoot.CREDIT_NOTE if root_name == "CreditNote" else None
    type_resolution = resolve_document_type(type_code, ubl_root)
    if type_resolution.status is DocumentTypeStatus.UNKNOWN:
        location_label = "Rechnungsartcode (BT-3)"
        findings.append(
            _finding(
                "BR-CL-01",
                "error",
                "Rechnungsartcode ist nicht unterstützt",
                "Der Rechnungsartcode gehört nicht zur mit CEN EN 16931 1.3.15 gebündelten UNTDID-1001-Auswahl.",
                location=location_label,
                actual=type_code,
                expected="ein unterstützter UNTDID-1001-Code",
                source="EN 16931",
                reference="BR-CL-01",
                rule_class="core_precheck",
                semantic_reference=["BT-3"],
                location_label=location_label,
            )
        )
    elif type_resolution.root_compatibility is RootCompatibility.INCOMPATIBLE:
        root_label = f"UBL {root_name}"
        location_label = "Rechnungsartcode (BT-3)"
        findings.append(
            _finding(
                "BR-CL-01",
                "error",
                "Rechnungsartcode passt nicht zum UBL-Wurzelelement",
                f"Der Rechnungsartcode ist gemäß CEN EN 16931 1.3.15 nicht für {root_label} vorgesehen.",
                location=location_label,
                actual=type_code,
                expected=f"ein mit {root_label} kompatibler UNTDID-1001-Code",
                source="EN 16931",
                reference="BR-CL-01",
                rule_class="core_precheck",
                semantic_reference=["BT-3"],
                location_label=location_label,
            )
        )

    if (
        _is_known_xrechnung_profile(analysis)
        and type_code is not None
        and type_code not in XRECHNUNG_RECOMMENDED_DOCUMENT_TYPES
    ):
        location_label = "Rechnungsartcode (BT-3)"
        findings.append(
            _finding(
                "XRECHNUNG-BR-DE-17",
                "warning",
                "Rechnungsartcode ist für XRechnung nicht empfohlen",
                "Für XRechnung sollen ausschließlich die in BR-DE-17 aufgeführten Rechnungsartcodes verwendet werden.",
                location=location_label,
                actual=type_code,
                expected=", ".join(sorted(XRECHNUNG_RECOMMENDED_DOCUMENT_TYPES)),
                source="XRechnung",
                reference="BR-DE-17",
                rule_class="profile_precheck",
                profile="XRechnung",
                semantic_reference=["BT-3"],
                location_label=location_label,
            )
        )

    if not document.get("profile_id"):
        findings.append(
            _finding(
                "PROFILE-001",
                "warning",
                "Profilkennung fehlt",
                "Ohne Profilkennung ist die Zuordnung zu EN 16931, XRechnung, Peppol oder Factur-X erschwert.",
                location="BT-24 / CustomizationID / Guideline ID",
            )
        )

    currency = document.get("currency")
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        findings.append(
            _finding(
                "CODE-001",
                "error",
                "Ungültiges Währungsformat",
                "Der Währungscode muss aus drei Großbuchstaben bestehen.",
                location="BT-5",
                actual=currency,
                expected="ISO-4217-Code, z. B. EUR",
            )
        )

    for role, party, prefix in (("Verkäufer", seller, "BG-4"), ("Käufer", buyer, "BG-7")):
        address = party.get("address") or {}
        country = address.get("country_code")
        if country and not re.fullmatch(r"[A-Z]{2}", country):
            findings.append(
                _finding(
                    f"CODE-{2 if role == 'Verkäufer' else 3:03d}",
                    "warning",
                    f"Ländercode des {role.lower()}s ist auffällig",
                    "Der Ländercode sollte aus zwei Großbuchstaben bestehen.",
                    location=prefix,
                    actual=country,
                    expected="ISO-3166-1-Alpha-2, z. B. DE",
                )
            )
        if not country:
            findings.append(
                _finding(
                    f"ADDR-{1 if role == 'Verkäufer' else 2:03d}",
                    "warning",
                    f"Land des {role.lower()}s fehlt",
                    "Für eine eindeutige Adressierung sollte das Land angegeben sein.",
                    location=prefix,
                )
            )

    _check_date_order(
        findings,
        document.get("issue_date"),
        document.get("due_date"),
        "DATE-001",
        "Fälligkeit liegt vor dem Rechnungsdatum",
        "Das Zahlungsziel liegt zeitlich vor der Ausstellung der Rechnung.",
        "BT-9 / BT-2",
    )
    _check_date_order(
        findings,
        document.get("delivery_date"),
        document.get("due_date"),
        "DATE-002",
        "Fälligkeit liegt vor dem Lieferdatum",
        "Das Zahlungsziel liegt zeitlich vor dem angegebenen Liefer- oder Leistungsdatum.",
        "BT-9 / BT-72",
    )

    line_ids: set[str] = set()
    computed_line_total = Decimal("0")
    computed_line_total_complete = True

    for line_index, line in enumerate(lines):
        display_ordinal = line_index + 1
        line_id = clean_text(line.get("id"))
        location = f"Position {line_id or display_ordinal}"
        line_occurrence = _occurrence_data(
            "line",
            f"/lines/{line_index}",
            index=line_index,
            identifier=line_id,
        )
        if not line_id:
            findings.append(
                _finding(
                    "LINE-001",
                    "error",
                    "Positionsnummer fehlt",
                    "Jede Rechnungsposition benötigt eine Kennung.",
                    location=location,
                    occurrence=line_occurrence,
                )
            )
        elif line_id in line_ids:
            findings.append(
                _finding(
                    "LINE-002",
                    "error",
                    "Positionsnummer ist doppelt",
                    "Positionsnummern müssen innerhalb der Rechnung eindeutig sein.",
                    location=location,
                    actual=line_id,
                    occurrence=line_occurrence,
                )
            )
        else:
            line_ids.add(line_id)

        if not line.get("name") and not line.get("description"):
            findings.append(
                _finding(
                    "LINE-003",
                    "error",
                    "Artikel- oder Leistungsbezeichnung fehlt",
                    "Die Position enthält weder einen Namen noch eine Beschreibung.",
                    location=location,
                    occurrence=line_occurrence,
                )
            )

        quantity = xml_decimal_value(line.get("quantity"))
        price = xml_decimal_value(line.get("price"))
        base = _optional_decimal(line.get("base_quantity"), Decimal("1"))
        line_total = xml_decimal_value(line.get("line_total"))

        if quantity is None:
            findings.append(
                _finding(
                    "LINE-004",
                    "error",
                    "Menge fehlt oder ist ungültig",
                    "Die Positionsmenge ist nicht numerisch auswertbar.",
                    location=location,
                    occurrence=line_occurrence,
                )
            )
        if not line.get("unit_code"):
            findings.append(
                _finding(
                    "LINE-005",
                    "error",
                    "Mengeneinheit fehlt",
                    "Zur Positionsmenge fehlt der unitCode.",
                    location=location,
                    occurrence=line_occurrence,
                )
            )
        if price is None:
            findings.append(
                _finding(
                    "LINE-006",
                    "error",
                    "Nettopreis fehlt oder ist ungültig",
                    "Der Positionspreis ist nicht numerisch auswertbar.",
                    location=location,
                    occurrence=line_occurrence,
                )
            )
        if line_total is None:
            findings.append(
                _finding(
                    "LINE-007",
                    "error",
                    "Positionsnettobetrag fehlt oder ist ungültig",
                    "Der Positionsnettobetrag ist nicht numerisch auswertbar.",
                    location=location,
                    occurrence=line_occurrence,
                )
            )
            computed_line_total_complete = False
        else:
            computed_line_total += line_total

        line_allowances, line_allowances_complete = _sum_amounts(
            line.get("allowances_charges", []),
            "allowance",
        )
        line_charges, line_charges_complete = _sum_amounts(
            line.get("allowances_charges", []),
            "charge",
        )

        if base is None:
            findings.append(
                _finding(
                    "LINE-010",
                    "error",
                    "Preisbasismenge ist ungültig",
                    "Die Preisbasismenge entspricht keinem gültigen XML-Dezimalwert.",
                    location=location,
                    actual=line.get("base_quantity"),
                    expected="endlicher Dezimalwert ohne Komma oder Exponent",
                    occurrence=line_occurrence,
                )
            )
        elif base == 0:
            findings.append(
                _finding(
                    "LINE-008",
                    "error",
                    "Preisbasismenge ist null",
                    "Durch eine Preisbasismenge von null kann der Positionsbetrag nicht berechnet werden.",
                    location=location,
                    occurrence=line_occurrence,
                )
            )
        elif (
            quantity is not None
            and price is not None
            and line_total is not None
            and line_allowances_complete
            and line_charges_complete
        ):
            expected = quantity * price / base
            expected -= line_allowances
            expected += line_charges
            expected = _rounded(expected)
            if not _is_close(line_total, expected):
                findings.append(
                    _finding(
                        "CALC-LINE-001",
                        "error",
                        "Positionsbetrag stimmt rechnerisch nicht",
                        "Menge × Preis ÷ Preisbasismenge abzüglich Nachlässe zuzüglich Zuschläge ergibt einen anderen Betrag.",
                        location=location,
                        actual=money_string(line_total),
                        expected=money_string(expected),
                        occurrence=line_occurrence,
                    )
                )

        if base not in {None, Decimal("0"), Decimal("1")}:
            findings.append(
                _finding(
                    "LINE-009",
                    "info",
                    "Abweichende Preisbasismenge",
                    "Der angegebene Preis gilt nicht für genau eine Einheit und muss bei Berechnungen entsprechend berücksichtigt werden.",
                    location=location,
                    actual=line.get("base_quantity"),
                    occurrence=line_occurrence,
                )
            )

        for amount_currency, field_name in (
            (line.get("price_currency"), "Preiswährung"),
            (line.get("line_currency"), "Positionswährung"),
        ):
            if amount_currency and currency and amount_currency != currency:
                findings.append(
                    _finding(
                        "CURR-001",
                        "warning",
                        f"{field_name} weicht von der Rechnungswährung ab",
                        "Betragswährungen innerhalb der Rechnung sollten konsistent sein oder ausdrücklich als andere Währung ausgewiesen werden.",
                        location=location,
                        actual=amount_currency,
                        expected=currency,
                        occurrence=line_occurrence,
                    )
                )

        category = line.get("tax_category")
        rate = xml_decimal_value(line.get("tax_rate"))
        if not category:
            findings.append(
                _finding(
                    "TAX-LINE-001",
                    "error",
                    "Umsatzsteuerkategorie fehlt",
                    "Die Position enthält keine Umsatzsteuerkategorie.",
                    location=location,
                    occurrence=line_occurrence,
                )
            )
        if category == "S" and (rate is None or rate <= 0):
            findings.append(
                _finding(
                    "TAX-LINE-002",
                    "error",
                    "Standardsteuer ohne positiven Steuersatz",
                    "Für die Steuerkategorie S wird ein positiver Steuersatz erwartet.",
                    location=location,
                    actual=line.get("tax_rate"),
                    occurrence=line_occurrence,
                )
            )
        if category == "O" and rate is not None:
            findings.append(
                _finding(
                    "TAX-LINE-O-001",
                    "error",
                    "Steuersatz ist bei Kategorie O nicht zulässig",
                    "Bei 'Nicht der Umsatzsteuer unterliegend' darf auf Positionsebene kein Umsatzsteuersatz angegeben werden.",
                    location=location,
                    actual=line.get("tax_rate"),
                    expected="kein Steuersatz",
                    occurrence=line_occurrence,
                )
            )
        elif category in {"Z", "E", "AE", "G", "K"} and rate != Decimal("0"):
            findings.append(
                _finding(
                    "TAX-LINE-003",
                    "error",
                    "Steuerkategorie und Steuersatz widersprechen sich",
                    "Für diese Steuerkategorie ist auf Positionsebene ein Steuersatz von 0 erforderlich.",
                    location=location,
                    actual=line.get("tax_rate"),
                    expected="0",
                    occurrence=line_occurrence,
                )
            )

    header_line_total = xml_decimal_value(totals.get("line_total"))
    if (
        computed_line_total_complete
        and header_line_total is not None
        and not _is_close(computed_line_total, header_line_total)
    ):
        findings.append(
            _finding(
                "CALC-HDR-001",
                "error",
                "Summe der Positionen stimmt nicht mit dem Rechnungskopf überein",
                "Die addierten Positionsnettobeträge unterscheiden sich vom ausgewiesenen Positionsnettobetrag.",
                location="BT-106",
                actual=money_string(header_line_total),
                expected=money_string(computed_line_total),
            )
        )

    allowance_total = _optional_decimal(totals.get("allowance_total"), Decimal("0"))
    charge_total = _optional_decimal(totals.get("charge_total"), Decimal("0"))
    tax_basis_total = xml_decimal_value(totals.get("tax_basis_total"))
    if (
        header_line_total is not None
        and allowance_total is not None
        and charge_total is not None
        and tax_basis_total is not None
    ):
        expected_basis = _rounded(header_line_total - allowance_total + charge_total)
        if not _is_close(tax_basis_total, expected_basis):
            findings.append(
                _finding(
                    "CALC-HDR-002",
                    "error",
                    "Steuerbasis stimmt nicht",
                    "Positionssumme abzüglich Nachlässe zuzüglich Zuschläge ergibt eine andere Steuerbasis.",
                    location="BT-109",
                    actual=money_string(tax_basis_total),
                    expected=money_string(expected_basis),
                )
            )

    listed_header_allowances, listed_header_allowances_complete = _sum_amounts(
        analysis.get("header_allowances_charges", []),
        "allowance",
    )
    listed_header_charges, listed_header_charges_complete = _sum_amounts(
        analysis.get("header_allowances_charges", []),
        "charge",
    )
    if (
        totals.get("allowance_total") is not None
        and allowance_total is not None
        and listed_header_allowances_complete
        and not _is_close(allowance_total, listed_header_allowances)
    ):
        findings.append(
            _finding(
                "CALC-HDR-003",
                "warning",
                "Ausgewiesene Nachlasssumme weicht von den Einzelnachlässen ab",
                "Die Summe der im Kopf gefundenen Nachlässe entspricht nicht dem Nachlassgesamtbetrag.",
                location="BT-107 / BG-20",
                actual=money_string(allowance_total),
                expected=money_string(listed_header_allowances),
            )
        )
    if (
        totals.get("charge_total") is not None
        and charge_total is not None
        and listed_header_charges_complete
        and not _is_close(charge_total, listed_header_charges)
    ):
        findings.append(
            _finding(
                "CALC-HDR-004",
                "warning",
                "Ausgewiesene Zuschlagssumme weicht von den Einzelzuschlägen ab",
                "Die Summe der im Kopf gefundenen Zuschläge entspricht nicht dem Zuschlagsgesamtbetrag.",
                location="BT-108 / BG-21",
                actual=money_string(charge_total),
                expected=money_string(listed_header_charges),
            )
        )

    tax_total = xml_decimal_value(totals.get("tax_total"))
    if taxes:
        tax_rows_sum = Decimal("0")
        tax_rows_complete = True
        for index, tax in enumerate(taxes, start=1):
            row_amount = xml_decimal_value(tax.get("tax_amount"))
            basis = xml_decimal_value(tax.get("basis_amount"))
            rate = xml_decimal_value(tax.get("rate"))
            category = tax.get("category_code")
            if row_amount is None:
                tax_rows_complete = False
            else:
                tax_rows_sum += row_amount
            if basis is not None and rate is not None and row_amount is not None and category in {"S", "Z", "L", "M"}:
                expected_tax = _rounded(basis * rate / Decimal("100"))
                if not _is_close(row_amount, expected_tax):
                    findings.append(
                        _finding(
                            "CALC-TAX-001",
                            "error",
                            "Steuerbetrag einer Steuergruppe stimmt nicht",
                            "Steuerbasis × Steuersatz ergibt einen anderen Steuerbetrag.",
                            location=f"Steuergruppe {index}",
                            actual=money_string(row_amount),
                            expected=money_string(expected_tax),
                        )
                    )
            if (
                category in {"E", "AE", "O", "G", "K"}
                and not tax.get("exemption_reason")
                and not tax.get("exemption_reason_code")
            ):
                findings.append(
                    _finding(
                        "TAX-HDR-001",
                        "warning",
                        "Begründung für steuerliche Sonderbehandlung fehlt",
                        "Bei Steuerbefreiung, Reverse Charge oder nicht steuerbaren Umsätzen sollte eine Begründung bzw. ein Code angegeben sein.",
                        location=f"Steuergruppe {index}",
                    )
                )
            if category == "O" and rate is not None:
                findings.append(
                    _finding(
                        "TAX-HDR-O-001",
                        "error",
                        "Steuersatz ist bei Steuerkategorie O nicht zulässig",
                        "Eine Umsatzsteueraufschlüsselung der Kategorie O darf keinen Umsatzsteuersatz enthalten.",
                        location=f"Steuergruppe {index}",
                        actual=tax.get("rate"),
                        expected="kein Steuersatz",
                    )
                )
            elif category in {"Z", "E", "AE", "G", "K"} and rate != Decimal("0"):
                findings.append(
                    _finding(
                        "TAX-HDR-002",
                        "error",
                        "Steuerkategorie und Steuersatz widersprechen sich",
                        "Für diese Steuerkategorie ist in der Umsatzsteueraufschlüsselung ein Steuersatz von 0 erforderlich.",
                        location=f"Steuergruppe {index}",
                        actual=tax.get("rate"),
                        expected="0",
                    )
                )

            if category in {"Z", "E", "AE", "O", "G", "K"} and row_amount not in {None, Decimal("0")}:
                findings.append(
                    _finding(
                        "TAX-HDR-003",
                        "error",
                        "Steuerbetrag muss für diese Steuerkategorie 0 sein",
                        "Die Steueraufschlüsselung weist trotz einer nicht steuerpflichtigen oder mit 0 bewerteten Kategorie einen Steuerbetrag aus.",
                        location=f"Steuergruppe {index}",
                        actual=money_string(row_amount),
                        expected="0,00",
                    )
                )

            reason_text = _semantic_text(tax.get("exemption_reason"), tax.get("exemption_reason_code"))
            if category == "G" and (
                _reason_indicates_outside_scope(reason_text) or _reason_indicates_reverse_charge(reason_text)
            ):
                findings.append(
                    _finding(
                        "TAX-SEM-001",
                        "warning",
                        "Steuerkategorie G widerspricht dem Begründungstext",
                        "Der maschinenlesbare Code G bezeichnet eine Ausfuhr außerhalb der EU. Der Begründungstext beschreibt dagegen eine nicht steuerbare Leistung oder Reverse Charge. Bitte den Geschäftsvorfall und die Kategorie prüfen; für nicht der Umsatzsteuer unterliegende Leistungen ist regelmäßig O vorgesehen.",
                        location=f"Steuergruppe {index}",
                        actual=f"G; {tax.get('exemption_reason') or tax.get('exemption_reason_code')}",
                        expected="inhaltlich übereinstimmende Steuerkategorie und Begründung",
                    )
                )
            if category == "O" and _reason_indicates_export(reason_text):
                findings.append(
                    _finding(
                        "TAX-SEM-002",
                        "warning",
                        "Steuerkategorie O widerspricht einem Ausfuhrhinweis",
                        "Der Code O bezeichnet einen Umsatz außerhalb des Umsatzsteuer-Anwendungsbereichs; der Begründungstext deutet dagegen auf eine Ausfuhr hin.",
                        location=f"Steuergruppe {index}",
                        actual=tax.get("exemption_reason") or tax.get("exemption_reason_code"),
                        expected="inhaltlich übereinstimmende Steuerkategorie und Begründung",
                    )
                )
        if tax_rows_complete and tax_total is not None and not _is_close(tax_total, tax_rows_sum):
            findings.append(
                _finding(
                    "CALC-TAX-002",
                    "error",
                    "Gesamtsteuer stimmt nicht mit den Steuergruppen überein",
                    "Die Summe der Steuerbeträge der einzelnen Steuergruppen weicht vom Gesamtsteuerbetrag ab.",
                    location="BT-110",
                    actual=money_string(tax_total),
                    expected=money_string(tax_rows_sum),
                )
            )

    header_categories = {str(tax.get("category_code")) for tax in taxes if tax.get("category_code")}
    line_categories = {str(line.get("tax_category")) for line in lines if line.get("tax_category")}
    if "O" in header_categories and len(header_categories) > 1:
        findings.append(
            _finding(
                "TAX-O-001",
                "error",
                "Kategorie O darf nicht mit anderen Steuergruppen kombiniert werden",
                "Eine Rechnung mit einer Umsatzsteueraufschlüsselung der Kategorie O darf keine weiteren Umsatzsteueraufschlüsselungen enthalten.",
                location="BG-23",
                actual=", ".join(sorted(header_categories)),
                expected="nur O",
            )
        )
    if "O" in header_categories and any(category != "O" for category in line_categories):
        findings.append(
            _finding(
                "TAX-O-002",
                "error",
                "Positionen passen nicht zur Steuergruppe O",
                "Wenn der Rechnungskopf die Kategorie O verwendet, müssen auch alle Rechnungspositionen dieser Kategorie zugeordnet sein.",
                location="BG-23 / BG-25",
                actual=", ".join(sorted(line_categories)),
                expected="nur O",
            )
        )

    grand_total = xml_decimal_value(totals.get("grand_total"))
    if tax_basis_total is not None and tax_total is not None and grand_total is not None:
        expected_grand = _rounded(tax_basis_total + tax_total)
        if not _is_close(grand_total, expected_grand):
            findings.append(
                _finding(
                    "CALC-HDR-005",
                    "error",
                    "Bruttobetrag stimmt nicht",
                    "Steuerbasis zuzüglich Gesamtsteuer ergibt einen anderen Bruttobetrag.",
                    location="BT-112",
                    actual=money_string(grand_total),
                    expected=money_string(expected_grand),
                )
            )

    prepaid = _optional_decimal(totals.get("prepaid_amount"), Decimal("0"))
    rounding = _optional_decimal(totals.get("rounding_amount"), Decimal("0"))
    payable = xml_decimal_value(totals.get("due_payable_amount"))
    if grand_total is not None and prepaid is not None and rounding is not None and payable is not None:
        expected_payable = _rounded(grand_total - prepaid + rounding)
        if not _is_close(payable, expected_payable):
            findings.append(
                _finding(
                    "CALC-HDR-006",
                    "error",
                    "Zahlbetrag stimmt nicht",
                    "Bruttobetrag abzüglich Vorauszahlungen zuzüglich Rundung ergibt einen anderen Zahlbetrag.",
                    location="BT-115",
                    actual=money_string(payable),
                    expected=money_string(expected_payable),
                )
            )

    payment_means = payment.get("means") or []
    if (
        payable is not None
        and payable > 0
        and clean_text(document.get("due_date")) is None
        and not _has_payment_terms(payment)
    ):
        location_label = "Zahlbetrag (BT-115) / Fälligkeitsdatum (BT-9) / Zahlungsbedingungen (BT-20)"
        findings.append(
            _finding(
                "BR-CO-25",
                "error",
                "Fälligkeitsdatum oder Zahlungsbedingungen fehlen",
                "Bei einem positiven Zahlbetrag muss entweder ein Fälligkeitsdatum oder eine Zahlungsbedingung angegeben sein.",
                location=location_label,
                source="EN 16931",
                reference="BR-CO-25",
                rule_class="core_precheck",
                semantic_reference=["BT-115", "BT-9", "BT-20"],
                location_label=location_label,
            )
        )

    if _is_known_xrechnung_profile(analysis) and not payment_means:
        location_label = "Zahlungsanweisungen (BG-16)"
        findings.append(
            _finding(
                "XRECHNUNG-BR-DE-1",
                "error",
                "Zahlungsanweisungen fehlen",
                "Für ein sicher erkanntes XRechnung-Profil muss mindestens eine Zahlungsanweisung angegeben sein.",
                location=location_label,
                source="XRechnung",
                reference="BR-DE-1",
                rule_class="profile_precheck",
                profile="XRechnung",
                semantic_reference=["BG-16"],
                location_label=location_label,
            )
        )

    for index, means_value in enumerate(payment_means, start=1):
        means = means_value if isinstance(means_value, dict) else {}
        payment_code = clean_text(means.get("type_code"))
        if payment_code is None:
            location_label = f"Zahlungsanweisung {index}: Zahlungsart (BT-81) / Zahlungsanweisungen (BG-16)"
            findings.append(
                _finding(
                    "BR-49",
                    "error",
                    "Zahlungsart fehlt",
                    "Eine vorhandene Zahlungsanweisung muss einen Zahlungsartcode enthalten.",
                    location=location_label,
                    source="EN 16931",
                    reference="BR-49",
                    rule_class="core_precheck",
                    semantic_reference=["BG-16", "BT-81"],
                    location_label=location_label,
                )
            )

        if payment_code not in BANK_ACCOUNT_PAYMENT_CODES:
            continue

        iban = _declared_identifier(
            means,
            generic_key="account_id",
            legacy_key="iban",
            schemes={"IBAN"},
        )
        bic = _declared_identifier(
            means,
            generic_key="service_provider_id",
            legacy_key="bic",
            schemes={"BIC", "BICFI"},
        )
        if iban and not _iban_valid(iban):
            findings.append(
                _finding(
                    "PAY-002",
                    "error",
                    "IBAN ist formal ungültig",
                    "Die IBAN besteht die Modulo-97-Prüfung nicht oder hat ein ungültiges Format.",
                    location=f"Zahlungsweg {index}",
                    actual=iban,
                )
            )
        if bic and not re.fullmatch(r"[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?", bic.replace(" ", "").upper()):
            findings.append(
                _finding(
                    "PAY-003",
                    "warning",
                    "BIC ist formal auffällig",
                    "Eine BIC besteht üblicherweise aus acht oder elf Zeichen.",
                    location=f"Zahlungsweg {index}",
                    actual=bic,
                )
            )
        account_name = clean_text(means.get("account_name"))
        expected_recipient = _expected_payment_recipient_name(analysis, totals.get("due_payable_amount"))
        if (
            account_name is not None
            and expected_recipient is not None
            and account_name.casefold() != expected_recipient[0].casefold()
        ):
            location_label = f"Zahlungsanweisung {index}: erwarteter Zahlungsempfänger"
            findings.append(
                _finding(
                    "PAY-004",
                    "info",
                    "Kontoinhaber weicht vom erwarteten Zahlungsempfänger ab",
                    "Die Bezeichnung kann eine zulässige Kurzform oder ein zulässiger Drittempfänger sein, sollte aber bei Bedarf geprüft werden.",
                    location=location_label,
                    actual=means.get("account_name"),
                    expected=expected_recipient[0],
                    rule_class="plausibility",
                    semantic_reference=["BG-16", *expected_recipient[1]],
                    location_label=location_label,
                )
            )

    technical = analysis.get("technical", {})
    if technical.get("truncated"):
        time_limited = technical.get("limit_reason") == "time"
        findings.append(
            _finding(
                "TECH-001",
                "warning",
                (
                    "Zeitbudget der technischen Feldliste wurde erreicht"
                    if time_limited
                    else "Technische Feldliste wurde an der Zeilengrenze beendet"
                ),
                (
                    "Die Erzeugung der technischen Feldliste hat ihr konfiguriertes Zeitbudget ausgeschöpft. "
                    "Das Roh-XML bleibt vollständig erhalten."
                    if time_limited
                    else "Das XML enthält mehr darstellbare Felder als die konfigurierte Zeilengrenze zulässt. "
                    "Das Roh-XML bleibt vollständig erhalten."
                ),
                location="Technischer Anhang",
            )
        )

    if not findings:
        findings.append(
            _finding(
                "CHECK-000",
                "info",
                "Keine Auffälligkeiten in der internen Prüfung",
                "Alle implementierten Pflichtfeld-, Format-, Datums- und Rechenprüfungen waren unauffällig.",
            )
        )

    findings.sort(key=lambda item: (SEVERITY_ORDER.get(item["severity"], 99), item["id"], item.get("location") or ""))
    counts = {
        severity: sum(1 for item in findings if item["severity"] == severity)
        for severity in ("error", "warning", "info")
    }
    status = "invalid" if counts["error"] else "warning" if counts["warning"] else "ok"
    return {
        "status": status,
        "counts": counts,
        "findings": findings,
        "scope": (
            "Interne Plausibilitäts-, Pflichtfeld-, Format-, Datums- und Rechenprüfung. "
            "Sie ersetzt keine vollständige XSD-/Schematron-Konformitätsprüfung."
        ),
    }


def validate_builtin(analysis: dict[str, Any]) -> dict[str, Any]:
    precision = _decimal_work_precision(analysis)
    with localcontext() as context:
        context.prec = precision
        return _validate_builtin(analysis)
