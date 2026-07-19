"""Small, presentation-oriented code lists used by the viewer.

The official validator remains the authoritative source for complete code-list
validation. These maps intentionally focus on common values and readable labels.
"""

from __future__ import annotations

UNIT_CODES: dict[str, str] = {
    "C62": "Einheit",
    "H87": "Stück",
    "DAY": "Tag",
    "HUR": "Stunde",
    "MIN": "Minute",
    "SEC": "Sekunde",
    "MON": "Monat",
    "ANN": "Jahr",
    "KGM": "Kilogramm",
    "GRM": "Gramm",
    "TNE": "Tonne",
    "MTR": "Meter",
    "CMT": "Zentimeter",
    "MMT": "Millimeter",
    "KMT": "Kilometer",
    "MTK": "Quadratmeter",
    "MTQ": "Kubikmeter",
    "LTR": "Liter",
    "MLT": "Milliliter",
    "KWH": "Kilowattstunde",
    "MWH": "Megawattstunde",
    "KWT": "Kilowatt",
    "WTT": "Watt",
    "AMP": "Ampere",
    "VLT": "Volt",
    "CEL": "Grad Celsius",
    "PCE": "Stück",
    "SET": "Satz",
    "PR": "Paar",
    "PK": "Packung",
    "BX": "Schachtel",
    "CT": "Karton",
    "DZN": "Dutzend",
    "EA": "Einzelstück",
    "LS": "Pauschale",
    "XPP": "Palette",
    "XPK": "Packstück",
    "XBG": "Beutel",
    "XBX": "Kiste",
    "XCT": "Karton",
}

PAYMENT_MEANS_CODES: dict[str, str] = {
    "1": "Nicht festgelegt",
    "10": "Barzahlung",
    "20": "Scheck",
    "30": "Überweisung",
    "31": "Debitüberweisung",
    "42": "Zahlung auf Bankkonto",
    "48": "Bankkarte",
    "49": "Lastschrift",
    "57": "Dauerauftrag",
    "58": "SEPA-Überweisung",
    "59": "SEPA-Lastschrift",
    "68": "Online-Zahlungsdienst",
    "97": "Verrechnung zwischen Partnern",
}

DOCUMENT_TYPE_CODES: dict[str, str] = {
    "325": "Pro-forma-Rechnung",
    "326": "Teilrechnung",
    "380": "Rechnung",
    "381": "Gutschrift",
    "383": "Belastungsanzeige",
    "384": "Korrekturrechnung",
    "386": "Vorauszahlungsrechnung",
    "389": "Eigenabrechnung",
    "751": "Rechnungsinformation",
}

TAX_CATEGORY_CODES: dict[str, str] = {
    "S": "Standardsteuersatz",
    "Z": "Nullsteuersatz",
    "E": "Steuerbefreit",
    "AE": "Steuerschuldnerschaft des Leistungsempfängers",
    "O": "Nicht der Umsatzsteuer unterliegend",
    "G": "Steuerfreie Ausfuhr außerhalb der EU",
    "K": "Innergemeinschaftliche Lieferung",
    "L": "IGIC (Kanarische Inseln)",
    "M": "IPSI (Ceuta/Melilla)",
}

COUNTRY_NAMES: dict[str, str] = {
    "AT": "Österreich",
    "BE": "Belgien",
    "BG": "Bulgarien",
    "CH": "Schweiz",
    "CY": "Zypern",
    "CZ": "Tschechien",
    "DE": "Deutschland",
    "DK": "Dänemark",
    "EE": "Estland",
    "ES": "Spanien",
    "FI": "Finnland",
    "FR": "Frankreich",
    "GB": "Vereinigtes Königreich",
    "GR": "Griechenland",
    "HR": "Kroatien",
    "HU": "Ungarn",
    "IE": "Irland",
    "IS": "Island",
    "IT": "Italien",
    "LI": "Liechtenstein",
    "LT": "Litauen",
    "LU": "Luxemburg",
    "LV": "Lettland",
    "MT": "Malta",
    "NL": "Niederlande",
    "NO": "Norwegen",
    "PL": "Polen",
    "PT": "Portugal",
    "RO": "Rumänien",
    "SE": "Schweden",
    "SI": "Slowenien",
    "SK": "Slowakei",
    "US": "Vereinigte Staaten",
}

CURRENCY_NAMES: dict[str, str] = {
    "EUR": "Euro",
    "CHF": "Schweizer Franken",
    "GBP": "Pfund Sterling",
    "USD": "US-Dollar",
    "DKK": "Dänische Krone",
    "NOK": "Norwegische Krone",
    "SEK": "Schwedische Krone",
    "PLN": "Polnischer Złoty",
    "CZK": "Tschechische Krone",
    "HUF": "Ungarischer Forint",
}


def code_label(mapping: dict[str, str], code: str | None) -> str | None:
    if not code:
        return None
    label = mapping.get(code)
    return f"{code} – {label}" if label else code


def unit_label(code: str | None) -> str | None:
    return code_label(UNIT_CODES, code)


def payment_means_label(code: str | None) -> str | None:
    return code_label(PAYMENT_MEANS_CODES, code)


def document_type_label(code: str | None) -> str | None:
    return code_label(DOCUMENT_TYPE_CODES, code)


def tax_category_label(code: str | None) -> str | None:
    return code_label(TAX_CATEGORY_CODES, code)


def tax_category_display(code: str | None) -> str | None:
    """Return the machine code together with its human-readable label."""

    normalized = (code or "").strip().upper()
    return tax_category_label(normalized) if normalized else None


def tax_basis_label(code: str | None) -> str:
    """Use a neutral amount label for transactions outside the VAT scope."""

    normalized = (code or "").strip().upper()
    if normalized == "O":
        return "Nettobetrag dieser Steuerkategorie"
    return "Bemessungsgrundlage"


def country_label(code: str | None) -> str | None:
    return code_label(COUNTRY_NAMES, code)


def currency_label(code: str | None) -> str | None:
    return code_label(CURRENCY_NAMES, code)
