from __future__ import annotations

import pytest

from app.document_types import IssuanceMode
from app.profiles import (
    InternalSemanticCapability,
    OfficialValidationCapability,
    ProfileFamily,
    ProfileStatus,
    resolve_profile,
)


@pytest.mark.parametrize(
    ("identifier", "family", "label", "official"),
    [
        (
            "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0",
            ProfileFamily.XRECHNUNG,
            "XRechnung",
            OfficialValidationCapability.BUNDLED,
        ),
        (
            "urn:cen.eu:en16931:2017",
            ProfileFamily.EN16931,
            "EN 16931",
            OfficialValidationCapability.BUNDLED,
        ),
        (
            "urn:fdc:peppol.eu:2017:poacc:billing:3.0",
            ProfileFamily.PEPPOL_BILLING,
            "Peppol BIS Billing 3.0",
            OfficialValidationCapability.NOT_BUNDLED,
        ),
        (
            "urn:factur-x.eu:1p0:basic",
            ProfileFamily.FACTUR_X,
            "Factur-X",
            OfficialValidationCapability.NOT_BUNDLED,
        ),
        (
            "urn:ferd:zugferd:2p0:comfort",
            ProfileFamily.ZUGFERD,
            "ZUGFeRD",
            OfficialValidationCapability.NOT_BUNDLED,
        ),
    ],
)
def test_known_profiles_expose_family_and_capabilities(
    identifier: str,
    family: ProfileFamily,
    label: str,
    official: OfficialValidationCapability,
) -> None:
    result = resolve_profile(identifier)

    assert result.identifier == identifier
    assert result.status is ProfileStatus.KNOWN
    assert result.family is family
    assert result.label == label
    assert result.capabilities.internal_semantics is InternalSemanticCapability.SUPPORTED
    assert result.capabilities.official_validation is official


@pytest.mark.parametrize(
    "identifier",
    [
        "urn:fdc:peppol.eu:2017:poacc:selfbilling:3.0",
        "urn:example:PEPPOL:SELF-BILLING:3.0",
        "urn:example:poacc:self_billing:3.0",
    ],
)
def test_peppol_self_billing_wins_over_general_peppol_match(identifier: str) -> None:
    result = resolve_profile(identifier)

    assert result.family is ProfileFamily.PEPPOL_SELF_BILLING
    assert result.label == "Peppol BIS Self-Billing 3.0"
    assert result.issuance_mode is IssuanceMode.SELF_BILLING
    assert result.capabilities.official_validation is OfficialValidationCapability.NOT_BUNDLED
    assert result.capabilities.document_type_policy == "peppol-self-billing-3.0"


def test_regular_peppol_profile_is_supplier_issued() -> None:
    result = resolve_profile("urn:example:poacc:billing:3.0")

    assert result.family is ProfileFamily.PEPPOL_BILLING
    assert result.issuance_mode is IssuanceMode.SUPPLIER_ISSUED


def test_unknown_profile_is_preserved_with_partial_internal_capability() -> None:
    result = resolve_profile("  urn:example:custom-profile  ")

    assert result.identifier == "urn:example:custom-profile"
    assert result.status is ProfileStatus.UNKNOWN
    assert result.family is ProfileFamily.CUSTOM
    assert result.issuance_mode is IssuanceMode.UNDETERMINED
    assert result.capabilities.internal_semantics is InternalSemanticCapability.PARTIAL
    assert result.capabilities.official_validation is OfficialValidationCapability.UNKNOWN
    assert result.to_dict()["status"] == "unknown"


def test_xrechnung_like_custom_identifier_is_not_treated_as_bundled_xrechnung() -> None:
    result = resolve_profile("urn:example:xrechnung-like")

    assert result.status is ProfileStatus.UNKNOWN
    assert result.family is ProfileFamily.CUSTOM
    assert result.capabilities.official_validation is OfficialValidationCapability.UNKNOWN


@pytest.mark.parametrize("identifier", [None, "", "  "])
def test_missing_profile_is_explicitly_missing(identifier: str | None) -> None:
    result = resolve_profile(identifier)

    assert result.identifier is None
    assert result.status is ProfileStatus.MISSING
    assert result.family is ProfileFamily.UNSPECIFIED
    assert result.label == "Nicht angegeben"
    assert result.issuance_mode is IssuanceMode.UNDETERMINED
    assert result.capabilities.internal_semantics is InternalSemanticCapability.PARTIAL
    assert result.capabilities.official_validation is OfficialValidationCapability.UNKNOWN
