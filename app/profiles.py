"""Profile recognition and capabilities independent of parser syntax."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from .document_types import IssuanceMode


class ProfileStatus(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    MISSING = "missing"


class ProfileFamily(StrEnum):
    XRECHNUNG = "xrechnung"
    EN16931 = "en16931"
    PEPPOL_BILLING = "peppol_billing"
    PEPPOL_SELF_BILLING = "peppol_self_billing"
    FACTUR_X = "factur_x"
    ZUGFERD = "zugferd"
    CUSTOM = "custom"
    UNSPECIFIED = "unspecified"


class InternalSemanticCapability(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class OfficialValidationCapability(StrEnum):
    BUNDLED = "bundled"
    NOT_BUNDLED = "not_bundled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProfileCapabilities:
    internal_semantics: InternalSemanticCapability
    official_validation: OfficialValidationCapability
    document_type_policy: str | None
    official_rules_version: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "internal_semantics": self.internal_semantics.value,
            "official_validation": self.official_validation.value,
            "document_type_policy": self.document_type_policy,
            "official_rules_version": self.official_rules_version,
        }


@dataclass(frozen=True, slots=True)
class ProfileInfo:
    family: ProfileFamily
    label: str
    issuance_mode: IssuanceMode
    capabilities: ProfileCapabilities


@dataclass(frozen=True, slots=True)
class ProfileResolution:
    identifier: str | None
    status: ProfileStatus
    family: ProfileFamily
    label: str
    issuance_mode: IssuanceMode
    capabilities: ProfileCapabilities

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.identifier,
            "status": self.status.value,
            "family": self.family.value,
            "label": self.label,
            "issuance_mode": self.issuance_mode.value,
            "capabilities": self.capabilities.to_dict(),
        }


_BUNDLED_RULES = "CEN-EN16931-validation-1.3.15 / XRechnung-3.0.2"

_SUPPORTED_BUNDLED = ProfileCapabilities(
    internal_semantics=InternalSemanticCapability.SUPPORTED,
    official_validation=OfficialValidationCapability.BUNDLED,
    document_type_policy="cen-en16931-1.3.15",
    official_rules_version=_BUNDLED_RULES,
)
_SUPPORTED_NOT_BUNDLED = ProfileCapabilities(
    internal_semantics=InternalSemanticCapability.SUPPORTED,
    official_validation=OfficialValidationCapability.NOT_BUNDLED,
    document_type_policy=None,
    official_rules_version=None,
)
_PARTIAL_UNKNOWN = ProfileCapabilities(
    internal_semantics=InternalSemanticCapability.PARTIAL,
    official_validation=OfficialValidationCapability.UNKNOWN,
    document_type_policy=None,
    official_rules_version=None,
)

_PROFILES: dict[ProfileFamily, ProfileInfo] = {
    ProfileFamily.XRECHNUNG: ProfileInfo(
        family=ProfileFamily.XRECHNUNG,
        label="XRechnung",
        issuance_mode=IssuanceMode.UNDETERMINED,
        capabilities=ProfileCapabilities(
            internal_semantics=InternalSemanticCapability.SUPPORTED,
            official_validation=OfficialValidationCapability.BUNDLED,
            document_type_policy="xrechnung-3.0.2",
            official_rules_version=_BUNDLED_RULES,
        ),
    ),
    ProfileFamily.EN16931: ProfileInfo(
        family=ProfileFamily.EN16931,
        label="EN 16931",
        issuance_mode=IssuanceMode.UNDETERMINED,
        capabilities=_SUPPORTED_BUNDLED,
    ),
    ProfileFamily.PEPPOL_BILLING: ProfileInfo(
        family=ProfileFamily.PEPPOL_BILLING,
        label="Peppol BIS Billing 3.0",
        issuance_mode=IssuanceMode.SUPPLIER_ISSUED,
        capabilities=ProfileCapabilities(
            internal_semantics=InternalSemanticCapability.SUPPORTED,
            official_validation=OfficialValidationCapability.NOT_BUNDLED,
            document_type_policy="peppol-billing-3.0",
            official_rules_version=None,
        ),
    ),
    ProfileFamily.PEPPOL_SELF_BILLING: ProfileInfo(
        family=ProfileFamily.PEPPOL_SELF_BILLING,
        label="Peppol BIS Self-Billing 3.0",
        issuance_mode=IssuanceMode.SELF_BILLING,
        capabilities=ProfileCapabilities(
            internal_semantics=InternalSemanticCapability.SUPPORTED,
            official_validation=OfficialValidationCapability.NOT_BUNDLED,
            document_type_policy="peppol-self-billing-3.0",
            official_rules_version=None,
        ),
    ),
    ProfileFamily.FACTUR_X: ProfileInfo(
        family=ProfileFamily.FACTUR_X,
        label="Factur-X",
        issuance_mode=IssuanceMode.UNDETERMINED,
        capabilities=_SUPPORTED_NOT_BUNDLED,
    ),
    ProfileFamily.ZUGFERD: ProfileInfo(
        family=ProfileFamily.ZUGFERD,
        label="ZUGFeRD",
        issuance_mode=IssuanceMode.UNDETERMINED,
        capabilities=_SUPPORTED_NOT_BUNDLED,
    ),
    ProfileFamily.CUSTOM: ProfileInfo(
        family=ProfileFamily.CUSTOM,
        label="Unbekanntes/individuelles Profil",
        issuance_mode=IssuanceMode.UNDETERMINED,
        capabilities=_PARTIAL_UNKNOWN,
    ),
    ProfileFamily.UNSPECIFIED: ProfileInfo(
        family=ProfileFamily.UNSPECIFIED,
        label="Nicht angegeben",
        issuance_mode=IssuanceMode.UNDETERMINED,
        capabilities=_PARTIAL_UNKNOWN,
    ),
}

PROFILE_REGISTRY: Final[Mapping[ProfileFamily, ProfileInfo]] = MappingProxyType(_PROFILES)

_SELF_BILLING_PATTERN = re.compile(r"self[\s_:-]*billing")
_XRECHNUNG_PATTERN = re.compile(
    r"(?:^|#)urn:xeinkauf\.de:kosit:xrechnung(?:_[0-9]+(?:\.[0-9]+)*)?$",
    re.IGNORECASE,
)


def _profile_family(identifier: str) -> ProfileFamily:
    value = identifier.casefold()
    if _XRECHNUNG_PATTERN.search(identifier):
        return ProfileFamily.XRECHNUNG
    if ("peppol" in value or "poacc" in value) and _SELF_BILLING_PATTERN.search(value):
        return ProfileFamily.PEPPOL_SELF_BILLING
    if "peppol" in value or "poacc" in value:
        return ProfileFamily.PEPPOL_BILLING
    if "factur-x" in value or "factur_x" in value:
        return ProfileFamily.FACTUR_X
    if "zugferd" in value:
        return ProfileFamily.ZUGFERD
    if "en16931" in value or "en:16931" in value:
        return ProfileFamily.EN16931
    return ProfileFamily.CUSTOM


def resolve_profile(profile_id: str | None) -> ProfileResolution:
    identifier = (profile_id or "").strip() or None
    if identifier is None:
        status = ProfileStatus.MISSING
        family = ProfileFamily.UNSPECIFIED
    else:
        family = _profile_family(identifier)
        status = ProfileStatus.UNKNOWN if family is ProfileFamily.CUSTOM else ProfileStatus.KNOWN

    info = PROFILE_REGISTRY[family]
    return ProfileResolution(
        identifier=identifier,
        status=status,
        family=family,
        label=info.label,
        issuance_mode=info.issuance_mode,
        capabilities=info.capabilities,
    )
