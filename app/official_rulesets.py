"""Registry of official rule sets bound to exact profile identifiers.

A rule set is selected only for an exact combination of root element, root
namespace and the raw text of the profile identifier as it stands in the XML.
The selection deliberately mirrors the ``text() = '…'`` match of the KoSIT
scenario and is even stricter: an identifier split or followed by a comment,
surrounded by whitespace, or present more than once never selects a rule set.
Such documents stay ``unsupported`` instead of being sent to a validator whose
report could not be bound to the expected scenario.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from lxml import etree

from .parsers.namespaces import CII_ROOT_NAMESPACE
from .xml_utils import local_name, namespace_uri

CII_RAM_NAMESPACE: Final = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
FACTURX_EXTENDED_PROFILE_ID: Final = "urn:cen.eu:en16931:2017#conformant#urn:factur-x.eu:1p0:extended"


@dataclass(frozen=True, slots=True)
class RulesetFile:
    """One artefact of a rule set with its locked size and SHA-256."""

    target: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class OfficialRuleset:
    id: str
    label: str
    publisher: str
    rules_version: str
    profile_id: str
    root_local_name: str
    root_namespace: str
    scenario_name: str
    settings_key: str
    lock_component: str
    source_tag: str
    source_commit: str
    files: tuple[RulesetFile, ...]
    known_deviation_rules: frozenset[str]

    @property
    def registry_key(self) -> tuple[str, str, str]:
        return (self.root_local_name, self.root_namespace, self.profile_id)


FACTURX_EXTENDED: Final = OfficialRuleset(
    id="facturx-extended-1.09.2",
    label="Factur-X 1.09.2 EXTENDED",
    publisher="FNFE-MPE / FeRD",
    rules_version="Factur-X 1.09.2 EXTENDED / FNFE-MPE France_RFE v1.4.0.03 (a63e6b5)",
    profile_id=FACTURX_EXTENDED_PROFILE_ID,
    root_local_name="CrossIndustryInvoice",
    root_namespace=CII_ROOT_NAMESPACE,
    scenario_name="Factur-X 1.09.2 EXTENDED (CII)",
    settings_key="facturx_extended",
    lock_component="facturx_extended",
    source_tag="v1.4.0.03",
    source_commit="a63e6b538bdaff460f17c38dc1bc5455cf1ba35a",
    files=(
        RulesetFile(
            "resources/xsd/Factur-X_EXTENDED.xsd",
            1629,
            "2accff44dbdc405f38fd35d789ff5e6262396940356ed362009509cd024556c6",
        ),
        RulesetFile(
            "resources/xsd/Factur-X_1.09.2_EXTENDED_urn_un_unece_uncefact_data_standard_QualifiedDataType_100.xsd",
            5232,
            "ece13000d4f99c97ef09b909e9f8646d45415c46fd24d19d3c9b771c72ae2b34",
        ),
        RulesetFile(
            "resources/xsd/Factur-X_1.09.2_EXTENDED_urn_un_unece_uncefact_data_standard_"
            "ReusableAggregateBusinessInformationEntity_100.xsd",
            30025,
            "83cef3c48a6c83149733ffdced362e230e9ae482f62acdfb8c0fb3a919452fdb",
        ),
        RulesetFile(
            "resources/xsd/Factur-X_1.09.2_EXTENDED_urn_un_unece_uncefact_data_standard_UnqualifiedDataType_100.xsd",
            3329,
            "c6c9f7b3f36c821999f20d7b7fff30db64112bc7b2a8f132e3d048d859855d00",
        ),
        RulesetFile(
            "resources/xslt/FACTUR-X_EXTENDED.xslt",
            1706705,
            "52b44e77be20de05ee090eec0c4bbe69df239b608aefcaca2e24ffb1a0e50a6e",
        ),
        RulesetFile(
            "resources/xslt/FACTUR-X_EXTENDED_codedb.xml",
            266730,
            "6e23aedbba0e3b32b3b1211d145186193215f56f755201555306c42327e75fbc",
        ),
        RulesetFile(
            "resources/schematron/FACTUR-X_EXTENDED.sch",
            633844,
            "5b9039e31f86e90f1a34925e84d3fad206efa3ca5639bb98a0cefd3661d3e36a",
        ),
        RulesetFile(
            "LICENSE",
            11357,
            "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
        ),
    ),
    # Rules of release 1.09.2 with a known deviation corrected in FNFE fix-FR04
    # (sub-line XPath in BR-FXEXT-X-01, rounding in BR-FXEXT-CO-10/12/13); the
    # correction is announced for Factur-X 1.10 / ZUGFeRD 2.6.
    known_deviation_rules=frozenset(
        {
            "BR-FXEXT-S-01",
            "BR-FXEXT-Z-01",
            "BR-FXEXT-E-01",
            "BR-FXEXT-AE-01",
            "BR-FXEXT-IC-01",
            "BR-FXEXT-G-01",
            "BR-FXEXT-O-01",
            "BR-FXEXT-AF-01",
            "BR-FXEXT-AG-01",
            "BR-FXEXT-CO-10",
            "BR-FXEXT-CO-12",
            "BR-FXEXT-CO-13",
        }
    ),
)

RULESETS: Final[tuple[OfficialRuleset, ...]] = (FACTURX_EXTENDED,)
RULESET_REGISTRY: Final[Mapping[tuple[str, str, str], OfficialRuleset]] = MappingProxyType(
    {ruleset.registry_key: ruleset for ruleset in RULESETS}
)
_KNOWN_PROFILE_IDS: Final[frozenset[str]] = frozenset(ruleset.profile_id for ruleset in RULESETS)


@dataclass(frozen=True, slots=True)
class RulesetSelection:
    """Result of the exact selection, including why nothing was selected."""

    ruleset: OfficialRuleset | None
    raw_identifier: str | None
    normalized_identifier: str | None
    candidate_count: int
    reason: str | None

    @property
    def near_miss(self) -> bool:
        """True when only whitespace or comments separate the identifier from a known rule set."""

        return (
            self.ruleset is None
            and self.candidate_count == 1
            and self.normalized_identifier in _KNOWN_PROFILE_IDS
            and self.reason in {"mixed-content", "unknown-identifier"}
        )


def select_official_ruleset(root: etree._Element) -> RulesetSelection:
    """Select a rule set from the parsed original tree, before any normalisation."""

    root_name = local_name(root)
    root_namespace = namespace_uri(root) or ""
    if root_name != "CrossIndustryInvoice" or root_namespace != CII_ROOT_NAMESPACE:
        return RulesetSelection(None, None, None, 0, "unsupported-root")

    candidates = root.findall(
        f"{{{CII_ROOT_NAMESPACE}}}ExchangedDocumentContext/"
        f"{{{CII_RAM_NAMESPACE}}}GuidelineSpecifiedDocumentContextParameter/"
        f"{{{CII_RAM_NAMESPACE}}}ID"
    )
    if not candidates:
        return RulesetSelection(None, None, None, 0, "no-identifier")
    if len(candidates) != 1:
        return RulesetSelection(None, None, None, len(candidates), "ambiguous")

    element = candidates[0]
    normalized = "".join(element.itertext()).strip() or None
    if len(element):
        # Comments, processing instructions or child elements inside the
        # identifier: KoSIT compares single text nodes, the application refuses.
        return RulesetSelection(None, element.text, normalized, 1, "mixed-content")
    raw = element.text
    if raw is None:
        return RulesetSelection(None, None, None, 1, "no-identifier")
    ruleset = RULESET_REGISTRY.get((root_name, root_namespace, raw))
    if ruleset is None:
        return RulesetSelection(None, raw, normalized, 1, "unknown-identifier")
    return RulesetSelection(ruleset, raw, normalized, 1, None)


__all__ = [
    "CII_RAM_NAMESPACE",
    "FACTURX_EXTENDED",
    "FACTURX_EXTENDED_PROFILE_ID",
    "RULESETS",
    "RULESET_REGISTRY",
    "OfficialRuleset",
    "RulesetFile",
    "RulesetSelection",
    "select_official_ruleset",
]
