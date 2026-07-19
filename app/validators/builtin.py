from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..xml_utils import date_object, decimal_value, money_string

TOLERANCE = Decimal("0.02")
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _finding(
    rule_id: str,
    severity: str,
    title: str,
    message: str,
    *,
    location: str | None = None,
    actual: Any = None,
    expected: Any = None,
) -> dict[str, Any]:
    return {
        "id": rule_id,
        "severity": severity,
        "title": title,
        "message": message,
        "location": location,
        "actual": None if actual is None else str(actual),
        "expected": None if expected is None else str(expected),
        "source": "Interne Prüfung",
    }


def _is_close(left: Decimal, right: Decimal, tolerance: Decimal = TOLERANCE) -> bool:
    return abs(left - right) <= tolerance


def _rounded(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _sum_amounts(items: list[dict], item_type: str) -> Decimal:
    total = Decimal("0")
    for item in items:
        if item.get("type") != item_type:
            continue
        amount = decimal_value(item.get("amount"))
        if amount is not None:
            total += amount
    return total


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


def validate_builtin(analysis: dict[str, Any]) -> dict[str, Any]:
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

    for index, line in enumerate(lines, start=1):
        location = f"Position {line.get('id') or index}"
        line_id = line.get("id")
        if not line_id:
            findings.append(
                _finding(
                    "LINE-001",
                    "error",
                    "Positionsnummer fehlt",
                    "Jede Rechnungsposition benötigt eine Kennung.",
                    location=location,
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
                )
            )

        quantity = decimal_value(line.get("quantity"))
        price = decimal_value(line.get("price"))
        base = decimal_value(line.get("base_quantity")) or Decimal("1")
        line_total = decimal_value(line.get("line_total"))

        if quantity is None:
            findings.append(
                _finding(
                    "LINE-004",
                    "error",
                    "Menge fehlt oder ist ungültig",
                    "Die Positionsmenge ist nicht numerisch auswertbar.",
                    location=location,
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
                )
            )
            computed_line_total_complete = False
        else:
            computed_line_total += line_total

        if base == 0:
            findings.append(
                _finding(
                    "LINE-008",
                    "error",
                    "Preisbasismenge ist null",
                    "Durch eine Preisbasismenge von null kann der Positionsbetrag nicht berechnet werden.",
                    location=location,
                )
            )
        elif quantity is not None and price is not None and line_total is not None:
            expected = quantity * price / base
            expected -= _sum_amounts(line.get("allowances_charges", []), "allowance")
            expected += _sum_amounts(line.get("allowances_charges", []), "charge")
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
                    )
                )

        if base != Decimal("1"):
            findings.append(
                _finding(
                    "LINE-009",
                    "info",
                    "Abweichende Preisbasismenge",
                    "Der angegebene Preis gilt nicht für genau eine Einheit; dies wurde bei der Rechenprüfung berücksichtigt.",
                    location=location,
                    actual=line.get("base_quantity"),
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
                    )
                )

        category = line.get("tax_category")
        rate = decimal_value(line.get("tax_rate"))
        if not category:
            findings.append(
                _finding(
                    "TAX-LINE-001",
                    "error",
                    "Umsatzsteuerkategorie fehlt",
                    "Die Position enthält keine Umsatzsteuerkategorie.",
                    location=location,
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
                )
            )

    header_line_total = decimal_value(totals.get("line_total"))
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

    allowance_total = decimal_value(totals.get("allowance_total")) or Decimal("0")
    charge_total = decimal_value(totals.get("charge_total")) or Decimal("0")
    tax_basis_total = decimal_value(totals.get("tax_basis_total"))
    if header_line_total is not None and tax_basis_total is not None:
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

    listed_header_allowances = _sum_amounts(analysis.get("header_allowances_charges", []), "allowance")
    listed_header_charges = _sum_amounts(analysis.get("header_allowances_charges", []), "charge")
    if totals.get("allowance_total") is not None and not _is_close(allowance_total, listed_header_allowances):
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
    if totals.get("charge_total") is not None and not _is_close(charge_total, listed_header_charges):
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

    tax_total = decimal_value(totals.get("tax_total"))
    if taxes:
        tax_rows_sum = Decimal("0")
        tax_rows_complete = True
        for index, tax in enumerate(taxes, start=1):
            row_amount = decimal_value(tax.get("tax_amount"))
            basis = decimal_value(tax.get("basis_amount"))
            rate = decimal_value(tax.get("rate"))
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

    grand_total = decimal_value(totals.get("grand_total"))
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

    prepaid = decimal_value(totals.get("prepaid_amount")) or Decimal("0")
    rounding = decimal_value(totals.get("rounding_amount")) or Decimal("0")
    payable = decimal_value(totals.get("due_payable_amount"))
    if grand_total is not None and payable is not None:
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
    if payable is not None and payable > 0 and not payment_means:
        findings.append(
            _finding(
                "PAY-001",
                "warning",
                "Zahlungsweg fehlt",
                "Bei einem positiven Zahlbetrag ist kein Zahlungsweg angegeben.",
                location="BG-16",
            )
        )

    for index, means in enumerate(payment_means, start=1):
        iban = means.get("iban")
        bic = means.get("bic")
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
        account_name = (means.get("account_name") or "").strip().lower()
        seller_name = (seller.get("name") or "").strip().lower()
        if account_name and seller_name and account_name != seller_name:
            findings.append(
                _finding(
                    "PAY-004",
                    "info",
                    "Kontoinhaber weicht vom vollständigen Verkäufernamen ab",
                    "Die Bezeichnung kann eine zulässige Kurzform sein, sollte aber bei Bedarf geprüft werden.",
                    location=f"Zahlungsweg {index}",
                    actual=means.get("account_name"),
                    expected=seller.get("name"),
                )
            )

    technical = analysis.get("technical", {})
    if technical.get("truncated"):
        findings.append(
            _finding(
                "TECH-001",
                "warning",
                "Technische Feldliste wurde begrenzt",
                "Das XML enthält mehr Felder als die konfigurierte Darstellungsgrenze. Das Roh-XML bleibt vollständig erhalten.",
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
