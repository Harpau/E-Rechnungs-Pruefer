"""Versioned document-type semantics for the bundled CEN validation rules.

The registry deliberately follows the locally bundled CEN EN 16931 validation
artefacts version 1.3.15. In particular, UBL codes 502 and 503 remain assigned
to ``Invoice`` here. Updating this module to a newer CEN allocation must happen
together with an explicit validator-version update.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

REGISTRY_VERSION: Final = "CEN-EN16931-validation-1.3.15"

CEN_UBL_INVOICE_CODES: Final[frozenset[str]] = frozenset(
    """
    71 80 81 82 84 102 130 202 203 204 211 218 219 295 325 326 331 380 382
    383 384 385 386 387 388 389 390 393 394 395 456 457 471 472 473 500 501
    502 503 527 553 575 623 633 751 780 817 870 875 876 877 935
    """.split()
)

CEN_UBL_CREDIT_NOTE_CODES: Final[frozenset[str]] = frozenset("81 83 261 262 296 308 381 396 420 458 532".split())


class UblRoot(StrEnum):
    INVOICE = "Invoice"
    CREDIT_NOTE = "CreditNote"


class DocumentTypeStatus(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    MISSING = "missing"


class DocumentFamily(StrEnum):
    INVOICE = "invoice"
    CREDIT_NOTE = "credit_note"
    DEBIT_NOTE = "debit_note"
    CORRECTIVE_INVOICE = "corrective_invoice"
    PREPAYMENT_INVOICE = "prepayment_invoice"
    PAYMENT_REQUEST = "payment_request"
    PRO_FORMA = "pro_forma"
    INFORMATION = "information"
    CLAIM = "claim"
    UNKNOWN = "unknown"


class BasePolarity(StrEnum):
    """Accounting orientation for a positive amount.

    ``DEBIT`` increases the buyer/debtor obligation, ``CREDIT`` reduces it.
    It does not assert that money is or will actually be transferred.
    """

    DEBIT = "debit"
    CREDIT = "credit"
    UNDETERMINED = "undetermined"


class SettlementRelevance(StrEnum):
    DETERMINISTIC = "deterministic"
    NON_SETTLEMENT = "non_settlement"
    UNDETERMINED = "undetermined"


class IssuanceMode(StrEnum):
    SUPPLIER_ISSUED = "supplier_issued"
    SELF_BILLING = "self_billing"
    UNDETERMINED = "undetermined"


class RootCompatibility(StrEnum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    NOT_APPLICABLE = "not_applicable"
    UNDETERMINED = "undetermined"


@dataclass(frozen=True, slots=True)
class DocumentTypeInfo:
    code: str
    label_de: str
    family: DocumentFamily
    base_polarity: BasePolarity
    settlement_relevance: SettlementRelevance
    issuance_mode: IssuanceMode
    allowed_ubl_roots: frozenset[UblRoot]
    tags: frozenset[str] = frozenset()
    source_version: str = REGISTRY_VERSION


@dataclass(frozen=True, slots=True)
class DocumentTypeResolution:
    code: str | None
    status: DocumentTypeStatus
    info: DocumentTypeInfo | None
    ubl_root: UblRoot | None
    root_compatibility: RootCompatibility

    def to_dict(self) -> dict[str, object]:
        info = self.info
        return {
            "code": self.code,
            "status": self.status.value,
            "label": info.label_de if info else None,
            "family": info.family.value if info else DocumentFamily.UNKNOWN.value,
            "base_polarity": info.base_polarity.value if info else BasePolarity.UNDETERMINED.value,
            "settlement_relevance": (
                info.settlement_relevance.value if info else SettlementRelevance.UNDETERMINED.value
            ),
            "issuance_mode": info.issuance_mode.value if info else IssuanceMode.UNDETERMINED.value,
            "allowed_ubl_roots": sorted(root.value for root in info.allowed_ubl_roots) if info else [],
            "ubl_root": self.ubl_root.value if self.ubl_root else None,
            "root_compatibility": self.root_compatibility.value,
            "registry_version": REGISTRY_VERSION,
        }


def _roots(code: str) -> frozenset[UblRoot]:
    roots: set[UblRoot] = set()
    if code in CEN_UBL_INVOICE_CODES:
        roots.add(UblRoot.INVOICE)
    if code in CEN_UBL_CREDIT_NOTE_CODES:
        roots.add(UblRoot.CREDIT_NOTE)
    return frozenset(roots)


def _entry(
    code: str,
    label_de: str,
    family: DocumentFamily,
    base_polarity: BasePolarity,
    settlement_relevance: SettlementRelevance,
    issuance_mode: IssuanceMode,
    *tags: str,
) -> DocumentTypeInfo:
    return DocumentTypeInfo(
        code=code,
        label_de=label_de,
        family=family,
        base_polarity=base_polarity,
        settlement_relevance=settlement_relevance,
        issuance_mode=issuance_mode,
        allowed_ubl_roots=_roots(code),
        tags=frozenset(tags),
    )


_D = BasePolarity.DEBIT
_C = BasePolarity.CREDIT
_U = BasePolarity.UNDETERMINED
_DET = SettlementRelevance.DETERMINISTIC
_NON = SettlementRelevance.NON_SETTLEMENT
_UND = SettlementRelevance.UNDETERMINED
_SUP = IssuanceMode.SUPPLIER_ISSUED
_SELF = IssuanceMode.SELF_BILLING
_MODE_UND = IssuanceMode.UNDETERMINED

_DOCUMENT_TYPES: dict[str, DocumentTypeInfo] = {
    "71": _entry("71", "Zahlungsaufforderung", DocumentFamily.PAYMENT_REQUEST, _D, _DET, _SUP),
    "80": _entry("80", "Belastungsanzeige für Waren oder Dienstleistungen", DocumentFamily.DEBIT_NOTE, _D, _DET, _SUP),
    "81": _entry("81", "Gutschrift für Waren oder Dienstleistungen", DocumentFamily.CREDIT_NOTE, _C, _DET, _SUP),
    "82": _entry("82", "Verbrauchsabrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "83": _entry("83", "Gutschrift für finanzielle Anpassungen", DocumentFamily.CREDIT_NOTE, _C, _DET, _SUP),
    "84": _entry("84", "Belastungsanzeige für finanzielle Anpassungen", DocumentFamily.DEBIT_NOTE, _D, _DET, _SUP),
    "102": _entry("102", "Steuermitteilung", DocumentFamily.INFORMATION, _U, _NON, _MODE_UND),
    "130": _entry("130", "Fakturadatenblatt", DocumentFamily.INFORMATION, _U, _NON, _MODE_UND),
    "202": _entry("202", "Direkte Zahlungsbewertung", DocumentFamily.PAYMENT_REQUEST, _D, _DET, _MODE_UND),
    "203": _entry("203", "Vorläufige Zahlungsbewertung", DocumentFamily.PAYMENT_REQUEST, _D, _DET, _MODE_UND),
    "204": _entry("204", "Zahlungsbewertung", DocumentFamily.PAYMENT_REQUEST, _D, _DET, _MODE_UND),
    "211": _entry("211", "Zwischenantrag auf Zahlung", DocumentFamily.PAYMENT_REQUEST, _D, _DET, _SUP),
    "218": _entry(
        "218", "Abschließende Zahlungsanforderung nach Fertigstellung", DocumentFamily.PAYMENT_REQUEST, _D, _DET, _SUP
    ),
    "219": _entry(
        "219", "Zahlungsanforderung für fertiggestellte Einheiten", DocumentFamily.PAYMENT_REQUEST, _D, _DET, _SUP
    ),
    "261": _entry("261", "Selbst ausgestellte Gutschrift", DocumentFamily.CREDIT_NOTE, _C, _DET, _SELF, "self_billing"),
    "262": _entry("262", "Sammelgutschrift für Waren und Dienstleistungen", DocumentFamily.CREDIT_NOTE, _C, _DET, _SUP),
    "295": _entry("295", "Preisanpassungsrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "296": _entry("296", "Gutschrift zur Preisanpassung", DocumentFamily.CREDIT_NOTE, _C, _DET, _SUP),
    "308": _entry("308", "Delkredere-Gutschrift", DocumentFamily.CREDIT_NOTE, _C, _DET, _SUP),
    "325": _entry("325", "Pro-forma-Rechnung", DocumentFamily.PRO_FORMA, _U, _NON, _SUP, "pro_forma"),
    "326": _entry("326", "Teilrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP, "partial"),
    "331": _entry("331", "Handelsrechnung mit Packliste", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "380": _entry("380", "Handelsrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "381": _entry("381", "Gutschrift", DocumentFamily.CREDIT_NOTE, _C, _DET, _SUP),
    "382": _entry("382", "Provisionsabrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "383": _entry("383", "Belastungsanzeige", DocumentFamily.DEBIT_NOTE, _D, _DET, _SUP),
    "384": _entry("384", "Korrekturrechnung", DocumentFamily.CORRECTIVE_INVOICE, _D, _DET, _SUP, "correction"),
    "385": _entry("385", "Sammelrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "386": _entry("386", "Vorauszahlungsrechnung", DocumentFamily.PREPAYMENT_INVOICE, _D, _DET, _SUP, "prepayment"),
    "387": _entry("387", "Mietrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "388": _entry("388", "Steuerrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "389": _entry("389", "Eigenabrechnung", DocumentFamily.INVOICE, _D, _DET, _SELF, "self_billing"),
    "390": _entry("390", "Delkredere-Rechnung", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "393": _entry("393", "Factoring-Rechnung", DocumentFamily.INVOICE, _D, _DET, _SUP, "factoring"),
    "394": _entry("394", "Leasingrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "395": _entry("395", "Konsignationsrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "396": _entry("396", "Factoring-Gutschrift", DocumentFamily.CREDIT_NOTE, _C, _DET, _SUP, "factoring"),
    "420": _entry("420", "OCR-Zahlungsgutschrift", DocumentFamily.CREDIT_NOTE, _C, _DET, _SUP),
    "456": _entry("456", "Belastungsanzeige", DocumentFamily.DEBIT_NOTE, _D, _DET, _MODE_UND),
    "457": _entry("457", "Stornierung einer Belastung", DocumentFamily.CREDIT_NOTE, _C, _DET, _MODE_UND, "reversal"),
    "458": _entry("458", "Stornierung einer Gutschrift", DocumentFamily.DEBIT_NOTE, _D, _DET, _MODE_UND, "reversal"),
    "471": _entry(
        "471",
        "Selbst ausgestellte Korrekturrechnung",
        DocumentFamily.CORRECTIVE_INVOICE,
        _D,
        _DET,
        _SELF,
        "self_billing",
        "correction",
    ),
    "472": _entry(
        "472",
        "Factoring-Korrekturrechnung",
        DocumentFamily.CORRECTIVE_INVOICE,
        _D,
        _DET,
        _SUP,
        "factoring",
        "correction",
    ),
    "473": _entry(
        "473",
        "Selbst ausgestellte Factoring-Korrekturrechnung",
        DocumentFamily.CORRECTIVE_INVOICE,
        _D,
        _DET,
        _SELF,
        "self_billing",
        "factoring",
        "correction",
    ),
    "500": _entry(
        "500",
        "Selbst ausgestellte Vorauszahlungsrechnung",
        DocumentFamily.PREPAYMENT_INVOICE,
        _D,
        _DET,
        _SELF,
        "self_billing",
        "prepayment",
    ),
    "501": _entry(
        "501",
        "Selbst ausgestellte Factoring-Rechnung",
        DocumentFamily.INVOICE,
        _D,
        _DET,
        _SELF,
        "self_billing",
        "factoring",
    ),
    "502": _entry(
        "502",
        "Selbst ausgestellte Factoring-Gutschrift",
        DocumentFamily.CREDIT_NOTE,
        _C,
        _DET,
        _SELF,
        "self_billing",
        "factoring",
    ),
    "503": _entry("503", "Vorauszahlungsgutschrift", DocumentFamily.CREDIT_NOTE, _C, _DET, _SUP, "prepayment"),
    "527": _entry(
        "527", "Selbst ausgestellte Belastungsanzeige", DocumentFamily.DEBIT_NOTE, _D, _DET, _SELF, "self_billing"
    ),
    "532": _entry("532", "Spediteurgutschrift", DocumentFamily.CREDIT_NOTE, _C, _DET, _SUP),
    "553": _entry(
        "553", "Abweichungsbericht zu einer Spediteursrechnung", DocumentFamily.INFORMATION, _U, _NON, _MODE_UND
    ),
    "575": _entry("575", "Versicherungsrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "623": _entry("623", "Spediteursrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "633": _entry("633", "Hafengebührenabrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "751": _entry(
        "751", "Rechnungsinformation für Buchhaltungszwecke", DocumentFamily.INFORMATION, _U, _NON, _MODE_UND
    ),
    "780": _entry("780", "Frachtrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "817": _entry("817", "Schadens- oder Anspruchsmitteilung", DocumentFamily.CLAIM, _U, _UND, _MODE_UND),
    "870": _entry("870", "Konsularrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP),
    "875": _entry(
        "875", "Abschlagsrechnung im Bauwesen", DocumentFamily.INVOICE, _D, _DET, _SUP, "construction", "partial"
    ),
    "876": _entry("876", "Teilschlussrechnung im Bauwesen", DocumentFamily.INVOICE, _D, _DET, _SUP, "construction"),
    "877": _entry("877", "Schlussrechnung im Bauwesen", DocumentFamily.INVOICE, _D, _DET, _SUP, "construction"),
    "935": _entry("935", "Zollrechnung", DocumentFamily.INVOICE, _D, _DET, _SUP, "customs"),
}

DOCUMENT_TYPE_REGISTRY: Final[Mapping[str, DocumentTypeInfo]] = MappingProxyType(_DOCUMENT_TYPES)


def resolve_document_type(
    code: str | None,
    ubl_root: UblRoot | str | None = None,
) -> DocumentTypeResolution:
    normalized_code = (code or "").strip() or None
    normalized_root = UblRoot(ubl_root) if ubl_root is not None else None
    info = DOCUMENT_TYPE_REGISTRY.get(normalized_code) if normalized_code else None

    if normalized_code is None:
        status = DocumentTypeStatus.MISSING
    elif info is None:
        status = DocumentTypeStatus.UNKNOWN
    else:
        status = DocumentTypeStatus.KNOWN

    if normalized_root is None:
        compatibility = RootCompatibility.NOT_APPLICABLE
    elif info is None:
        compatibility = RootCompatibility.UNDETERMINED
    elif normalized_root in info.allowed_ubl_roots:
        compatibility = RootCompatibility.COMPATIBLE
    else:
        compatibility = RootCompatibility.INCOMPATIBLE

    return DocumentTypeResolution(
        code=normalized_code,
        status=status,
        info=info,
        ubl_root=normalized_root,
        root_compatibility=compatibility,
    )
