"""Registry of official rule sets bound to exact profile identifiers.

A rule set is selected only for an exact combination of root element, root
namespace and the raw text of the profile identifier as it stands in the XML:
``GuidelineSpecifiedDocumentContextParameter/ID`` for CII, ``cbc:CustomizationID``
for UBL ``Invoice`` and ``CreditNote``. The selection deliberately mirrors the
exact match of the KoSIT scenario and is even stricter: an identifier split or
followed by a comment, surrounded by whitespace, or present more than once never
selects a rule set. Such documents stay ``unsupported`` instead of being sent to
a validator whose report could not be bound to the expected scenario.

Every rule set names the scenario its VARL report must carry and the artefacts
(size and SHA-256) that must be present before KoSIT is started. The eleven
XRechnung and EN 16931 rule sets share the pinned KoSIT validator configuration
(``KOSIT_SCENARIOS``/``KOSIT_REPOSITORIES``); Factur-X EXTENDED uses its own
paths and artefacts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from lxml import etree

from .component_versions import KOSIT_COMPONENT_VERSIONS
from .parsers.namespaces import CII_ROOT_NAMESPACE, UBL_NAMESPACES, UBL_ROOT_NAMESPACES
from .xml_utils import local_name, namespace_uri

CII_RAM_NAMESPACE: Final = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
UBL_CBC_NAMESPACE: Final = UBL_NAMESPACES["cbc"]
EN16931_PROFILE_ID: Final = "urn:cen.eu:en16931:2017"
XRECHNUNG_3_0_PROFILE_ID: Final = "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
XRECHNUNG_3_0_EXTENSION_PROFILE_ID: Final = (
    "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
    "#conformant#urn:xeinkauf.de:kosit:extension:xrechnung_3.0"
)
XRECHNUNG_3_0_CVD_PROFILE_ID: Final = (
    "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
    "#compliant#urn:xeinkauf.de:kosit:xrechnung:cvd_0.9"
)
FACTURX_EXTENDED_PROFILE_ID: Final = "urn:cen.eu:en16931:2017#conformant#urn:factur-x.eu:1p0:extended"


@dataclass(frozen=True, slots=True)
class RulesetFile:
    """One artefact of a rule set with its locked size and SHA-256."""

    target: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True, kw_only=True)
class OfficialRuleset:
    """One official rule set: exact identifier, expected scenario and bound artefacts.

    ``settings_key`` names the ``Settings`` fields ``kosit_<key>_scenarios`` and
    ``kosit_<key>_repositories`` of a rule set with its own installation; ``None``
    uses the shared ``kosit_scenarios``/``kosit_repositories`` paths. ``files``
    and ``report_stylesheet`` are relative to the single repository, while
    ``scenario_file`` binds the configured scenario file itself.
    """

    id: str
    label: str
    publisher: str
    rules_version: str
    profile_id: str
    root_local_name: str
    root_namespace: str
    scenario_name: str
    lock_component: str
    files: tuple[RulesetFile, ...]
    report_stylesheet: str
    settings_key: str | None = None
    scenario_file: RulesetFile | None = None
    source_tag: str | None = None
    source_commit: str | None = None
    summary_notes: tuple[str, ...] = ()
    known_deviation_rules: frozenset[str] = frozenset()

    @property
    def registry_key(self) -> tuple[str, str, str]:
        return (self.root_local_name, self.root_namespace, self.profile_id)


# --- KoSIT validator configuration XRechnung (XRechnung 3.0.2, EN 16931, 2026-08-31) ---

_VERSIONS = KOSIT_COMPONENT_VERSIONS
XRECHNUNG_RULES_VERSION: Final = (
    f"XRechnung {_VERSIONS['xrechnung']} / Validator-Konfiguration {_VERSIONS['xrechnung_configuration']}"
    f" / EN 16931 {_VERSIONS['cen_en16931']} / XRechnung-Schematron {_VERSIONS['xrechnung_schematron']}"
)
EN16931_RULES_VERSION: Final = (
    f"EN 16931 {_VERSIONS['cen_en16931']} / Validator-Konfiguration {_VERSIONS['xrechnung_configuration']}"
)
_KOSIT_PUBLISHER: Final = "KoSIT"
_XRECHNUNG_CONFIGURATION_TAG: Final = f"v{_VERSIONS['xrechnung_configuration']}"
# The scenario file passed with ``-s`` and the no-scenario report stylesheet of
# the configuration. The sizes and SHA-256 values below are those of the ZIP
# pinned in ``packaging/kosit/components.lock.json``; the integration suite
# proves them against a fresh installation.
XRECHNUNG_CONFIGURATION_SCENARIO_FILE: Final = RulesetFile(
    "scenarios.xml", 40562, "3e8161a39dfaa6aea4d990b279aab848be4f4c6169e820f354a159bda7693fb0"
)
XRECHNUNG_REPORT_STYLESHEET: Final = "resources/default-report.xsl"
# Every XML schema, Schematron stylesheet and report stylesheet that the eleven
# scenarios load directly or through ``schemaLocation`` and ``xsl:import``.
XRECHNUNG_CONFIGURATION_FILES: Final[tuple[RulesetFile, ...]] = (
    RulesetFile(
        "resources/cii/16b/xsd/CrossIndustryInvoice_100pD16B.xsd",
        3078,
        "3baf143b90f289d62c5dfb086aa51fa183490a2d0b38a40c6b31dcae0760f2ba",
    ),
    RulesetFile(
        "resources/cii/16b/xsd/CrossIndustryInvoice_QualifiedDataType_100pD16B.xsd",
        36806,
        "6649215914728652e883662bec1d3eab008c215e86a59f3bb417db9723ee8c5c",
    ),
    RulesetFile(
        "resources/cii/16b/xsd/CrossIndustryInvoice_ReusableAggregateBusinessInformationEntity_100pD16B.xsd",
        99402,
        "1c42244298a863bcc0b5196983ff0a5d084fcabe58aea77ef82b242e7cd0ddfd",
    ),
    RulesetFile(
        "resources/cii/16b/xsd/CrossIndustryInvoice_UnqualifiedDataType_100pD16B.xsd",
        6967,
        "04fce6f6f28521886731294be18a4e6bb5cb3003456457d99ad5c4d3e652f497",
    ),
    RulesetFile(
        "resources/cii/16b/xsl/EN16931-CII-validation.xsl",
        934750,
        "0911e927f13f9ae2cdc7f973643f27bf0ad3c5da02483c04a5b8a47eb4822199",
    ),
    RulesetFile(
        "resources/default-report.xsl",
        32477,
        "b40e53d6d170a1c482aedf09cb5d88ebc2e916db096237d6b3341457cd50e2d1",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsd/common/CCTS_CCT_SchemaModule-2.1.xsd",
        45268,
        "dd546e4809df86b6445589f69f0d6c9df162840ae386574ddfc1da7638103e15",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsd/common/UBL-CommonAggregateComponents-2.1.xsd",
        256745,
        "580b5af6f68f7f556bd15945ba0e819cbf561442c81694f9b9468036ebedec4d",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsd/common/UBL-CommonBasicComponents-2.1.xsd",
        219704,
        "a3b349bf92e5cff26e303d2f05b7e00c884dcafcfd9e12755fb80289abf5e22e",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsd/common/UBL-CommonExtensionComponents-2.1.xsd",
        9491,
        "ad7a4e490978adfbcfc5ec0bb20941cf11ac960ccf0c4de8791a7c731a8dbe87",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsd/common/UBL-CommonSignatureComponents-2.1.xsd",
        3824,
        "4fa9e2370100040fe14c43e135ef77e2eb66b21cb8dbfc2ffb8d82ae991fe92e",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsd/common/UBL-ExtensionContentDataType-2.1.xsd",
        4314,
        "fcee77a11870208e6377ea6311b9f2a050bca24bdad8606ea02d71e9f9e72f8d",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsd/common/UBL-QualifiedDataTypes-2.1.xsd",
        3590,
        "7dcb156e610239c97ae70940cf4653b88e48c3595bf5f56a2204a32e2893e6cf",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsd/common/UBL-SignatureAggregateComponents-2.1.xsd",
        5059,
        "9234c2ca48dbfa9a22a786112bb075c5922a305170920eaab1e3c04fa0b7344b",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsd/common/UBL-SignatureBasicComponents-2.1.xsd",
        4016,
        "0fbe2d7afff0c1e11164b8ec83e13f18801021c3c87e390a9d76f9cf862f6a64",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsd/common/UBL-UnqualifiedDataTypes-2.1.xsd",
        27300,
        "09052d406b4293e2a5f9c2bfee6df10ad4d8d5f0b36e24a6349d7f7936d89eb6",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsd/common/UBL-XAdESv132-2.1.xsd",
        21664,
        "a4f726bcf8cc3f7d9ffa4dab99e005535a8e8b60dced1e5d94578d2e05afa96e",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsd/common/UBL-XAdESv141-2.1.xsd",
        1316,
        "1fa4625e9cefcb7a9abb5ac1b64315547450031eece8a55bd584e4ba4b79dbc1",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsd/common/UBL-xmldsig-core-schema-2.1.xsd",
        10750,
        "101909c9f06456d61ddcc4fb982f1d40dc357b439f393b1a2eb46e42acd60809",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsd/maindoc/UBL-CreditNote-2.1.xsd",
        8738,
        "1e117b6c1ab713604b29b0d685442a81a0c78a82575445ee8c43e6050cda7454",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsd/maindoc/UBL-Invoice-2.1.xsd",
        8948,
        "3a5aacd823f0e5b8f25ae7b5191c2002d5333ba351d87de9371ed62dca2b2b0c",
    ),
    RulesetFile(
        "resources/ubl/2.1/xsl/EN16931-UBL-validation.xsl",
        1082235,
        "759f2d2e830c7fd1d5a6e8b4f61e5dbafc7a7805862c4ae1c749eeac6adde698",
    ),
    RulesetFile(
        "resources/xrechnung-report.xsl",
        3457,
        "a2c9fa2272fb14667d70ed62f11c6ad6c7167f12f2abbe1b420915ccefd5af8c",
    ),
    RulesetFile(
        "resources/xrechnung/3.0.2/xsl/XRechnung-CII-validation.xsl",
        189030,
        "ec54f662803a87f527ec606d1320bc404f1657a848af6a76f37f32905e4aac40",
    ),
    RulesetFile(
        "resources/xrechnung/3.0.2/xsl/XRechnung-UBL-validation.xsl",
        175293,
        "e32d831668facbbd5136cc0119bf1dd6f92ac20738aa3b3f021586f5a305c738",
    ),
)


def _configuration_ruleset(
    *,
    ruleset_id: str,
    label: str,
    profile_id: str,
    root_local_name: str,
    scenario_name: str,
    rules_version: str,
) -> OfficialRuleset:
    root_namespace = (
        CII_ROOT_NAMESPACE if root_local_name == "CrossIndustryInvoice" else UBL_ROOT_NAMESPACES[root_local_name]
    )
    return OfficialRuleset(
        id=ruleset_id,
        label=label,
        publisher=_KOSIT_PUBLISHER,
        rules_version=rules_version,
        profile_id=profile_id,
        root_local_name=root_local_name,
        root_namespace=root_namespace,
        scenario_name=scenario_name,
        lock_component="xrechnung",
        files=XRECHNUNG_CONFIGURATION_FILES,
        report_stylesheet=XRECHNUNG_REPORT_STYLESHEET,
        scenario_file=XRECHNUNG_CONFIGURATION_SCENARIO_FILE,
        source_tag=_XRECHNUNG_CONFIGURATION_TAG,
    )


XRECHNUNG_3_0_CII: Final = _configuration_ruleset(
    ruleset_id="xrechnung-3.0.2-cii",
    label="XRechnung 3.0.2 (CII)",
    profile_id=XRECHNUNG_3_0_PROFILE_ID,
    root_local_name="CrossIndustryInvoice",
    scenario_name="EN16931 XRechnung (CII)",
    rules_version=XRECHNUNG_RULES_VERSION,
)
XRECHNUNG_3_0_EXTENSION_CII: Final = _configuration_ruleset(
    ruleset_id="xrechnung-3.0.2-extension-cii",
    label="XRechnung 3.0.2 Extension (CII)",
    profile_id=XRECHNUNG_3_0_EXTENSION_PROFILE_ID,
    root_local_name="CrossIndustryInvoice",
    scenario_name="EN16931 XRechnung Extension (CII)",
    rules_version=XRECHNUNG_RULES_VERSION,
)
XRECHNUNG_3_0_CVD_CII: Final = _configuration_ruleset(
    ruleset_id="xrechnung-3.0.2-cvd-cii",
    label="XRechnung 3.0.2 CVD (CII)",
    profile_id=XRECHNUNG_3_0_CVD_PROFILE_ID,
    root_local_name="CrossIndustryInvoice",
    scenario_name="EN16931 XRechnung CVD (CII)",
    rules_version=XRECHNUNG_RULES_VERSION,
)
XRECHNUNG_3_0_UBL_INVOICE: Final = _configuration_ruleset(
    ruleset_id="xrechnung-3.0.2-ubl-invoice",
    label="XRechnung 3.0.2 (UBL Invoice)",
    profile_id=XRECHNUNG_3_0_PROFILE_ID,
    root_local_name="Invoice",
    scenario_name="EN16931 XRechnung (UBL Invoice)",
    rules_version=XRECHNUNG_RULES_VERSION,
)
XRECHNUNG_3_0_EXTENSION_UBL_INVOICE: Final = _configuration_ruleset(
    ruleset_id="xrechnung-3.0.2-extension-ubl-invoice",
    label="XRechnung 3.0.2 Extension (UBL Invoice)",
    profile_id=XRECHNUNG_3_0_EXTENSION_PROFILE_ID,
    root_local_name="Invoice",
    scenario_name="EN16931 XRechnung Extension (UBL Invoice)",
    rules_version=XRECHNUNG_RULES_VERSION,
)
XRECHNUNG_3_0_CVD_UBL_INVOICE: Final = _configuration_ruleset(
    ruleset_id="xrechnung-3.0.2-cvd-ubl-invoice",
    label="XRechnung 3.0.2 CVD (UBL Invoice)",
    profile_id=XRECHNUNG_3_0_CVD_PROFILE_ID,
    root_local_name="Invoice",
    scenario_name="EN16931 XRechnung CVD (UBL Invoice)",
    rules_version=XRECHNUNG_RULES_VERSION,
)
XRECHNUNG_3_0_UBL_CREDITNOTE: Final = _configuration_ruleset(
    ruleset_id="xrechnung-3.0.2-ubl-creditnote",
    label="XRechnung 3.0.2 (UBL CreditNote)",
    profile_id=XRECHNUNG_3_0_PROFILE_ID,
    root_local_name="CreditNote",
    scenario_name="EN16931 XRechnung (UBL CreditNote)",
    rules_version=XRECHNUNG_RULES_VERSION,
)
XRECHNUNG_3_0_CVD_UBL_CREDITNOTE: Final = _configuration_ruleset(
    ruleset_id="xrechnung-3.0.2-cvd-ubl-creditnote",
    label="XRechnung 3.0.2 CVD (UBL CreditNote)",
    profile_id=XRECHNUNG_3_0_CVD_PROFILE_ID,
    root_local_name="CreditNote",
    scenario_name="EN16931 XRechnung CVD (UBL CreditNote)",
    rules_version=XRECHNUNG_RULES_VERSION,
)
EN16931_CII: Final = _configuration_ruleset(
    ruleset_id="en16931-cii",
    label="EN 16931 (CII)",
    profile_id=EN16931_PROFILE_ID,
    root_local_name="CrossIndustryInvoice",
    scenario_name="EN16931 (CII)",
    rules_version=EN16931_RULES_VERSION,
)
EN16931_UBL_INVOICE: Final = _configuration_ruleset(
    ruleset_id="en16931-ubl-invoice",
    label="EN 16931 (UBL Invoice)",
    profile_id=EN16931_PROFILE_ID,
    root_local_name="Invoice",
    scenario_name="EN16931 (UBL Invoice)",
    rules_version=EN16931_RULES_VERSION,
)
EN16931_UBL_CREDITNOTE: Final = _configuration_ruleset(
    ruleset_id="en16931-ubl-creditnote",
    label="EN 16931 (UBL CreditNote)",
    profile_id=EN16931_PROFILE_ID,
    root_local_name="CreditNote",
    scenario_name="EN16931 (UBL CreditNote)",
    rules_version=EN16931_RULES_VERSION,
)
# In the order of the scenarios in the configuration's ``scenarios.xml``. The
# configuration has no "XRechnung Extension (UBL CreditNote)" scenario; a
# credit note with the extension identifier therefore selects nothing.
XRECHNUNG_RULESETS: Final[tuple[OfficialRuleset, ...]] = (
    XRECHNUNG_3_0_UBL_INVOICE,
    XRECHNUNG_3_0_EXTENSION_UBL_INVOICE,
    XRECHNUNG_3_0_CVD_UBL_INVOICE,
    XRECHNUNG_3_0_UBL_CREDITNOTE,
    XRECHNUNG_3_0_CVD_UBL_CREDITNOTE,
    XRECHNUNG_3_0_CII,
    XRECHNUNG_3_0_EXTENSION_CII,
    XRECHNUNG_3_0_CVD_CII,
    EN16931_UBL_INVOICE,
    EN16931_UBL_CREDITNOTE,
    EN16931_CII,
)


# --- Factur-X 1.09.2 EXTENDED (own installation under vendor/kosit/facturx-extended) ---

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
    report_stylesheet="resources/report/default-report.xsl",
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
    summary_notes=(
        "Der Regelstand ist eine Festlegung des Prüfers, die Kennung nennt keine Version.",
        "Geprüft wurde ausschließlich die XML, nicht die PDF-Konformität.",
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

RULESETS: Final[tuple[OfficialRuleset, ...]] = XRECHNUNG_RULESETS + (FACTURX_EXTENDED,)
RULESET_REGISTRY: Final[Mapping[tuple[str, str, str], OfficialRuleset]] = MappingProxyType(
    {ruleset.registry_key: ruleset for ruleset in RULESETS}
)


@dataclass(frozen=True, slots=True)
class RulesetSelection:
    """Result of the exact selection, including why nothing was selected.

    ``near_miss`` is true when only whitespace or comments separate the
    identifier from a rule set registered for the same root element.
    """

    ruleset: OfficialRuleset | None
    raw_identifier: str | None
    normalized_identifier: str | None
    candidate_count: int
    reason: str | None
    near_miss: bool = False


def _identifier_elements(root: etree._Element, root_name: str, root_namespace: str) -> list[etree._Element] | None:
    """The profile identifier elements of the syntax, or ``None`` for an unsupported root."""

    if root_name == "CrossIndustryInvoice" and root_namespace == CII_ROOT_NAMESPACE:
        return root.findall(
            f"{{{CII_ROOT_NAMESPACE}}}ExchangedDocumentContext/"
            f"{{{CII_RAM_NAMESPACE}}}GuidelineSpecifiedDocumentContextParameter/"
            f"{{{CII_RAM_NAMESPACE}}}ID"
        )
    if root_name in UBL_ROOT_NAMESPACES and UBL_ROOT_NAMESPACES[root_name] == root_namespace:
        return root.findall(f"{{{UBL_CBC_NAMESPACE}}}CustomizationID")
    return None


def select_official_ruleset(root: etree._Element) -> RulesetSelection:
    """Select a rule set from the parsed original tree, before any normalisation."""

    root_name = local_name(root)
    root_namespace = namespace_uri(root) or ""
    candidates = _identifier_elements(root, root_name, root_namespace)
    if candidates is None:
        return RulesetSelection(None, None, None, 0, "unsupported-root")
    if not candidates:
        return RulesetSelection(None, None, None, 0, "no-identifier")
    if len(candidates) != 1:
        return RulesetSelection(None, None, None, len(candidates), "ambiguous")

    element = candidates[0]
    normalized = "".join(element.itertext()).strip() or None
    near_miss = normalized is not None and (root_name, root_namespace, normalized) in RULESET_REGISTRY
    if len(element):
        # Comments, processing instructions or child elements inside the
        # identifier: KoSIT compares single text nodes, the application refuses.
        return RulesetSelection(None, element.text, normalized, 1, "mixed-content", near_miss)
    raw = element.text
    if raw is None:
        return RulesetSelection(None, None, None, 1, "no-identifier")
    ruleset = RULESET_REGISTRY.get((root_name, root_namespace, raw))
    if ruleset is None:
        return RulesetSelection(None, raw, normalized, 1, "unknown-identifier", near_miss)
    return RulesetSelection(ruleset, raw, normalized, 1, None)


__all__ = [
    "CII_RAM_NAMESPACE",
    "EN16931_CII",
    "EN16931_PROFILE_ID",
    "EN16931_RULES_VERSION",
    "EN16931_UBL_CREDITNOTE",
    "EN16931_UBL_INVOICE",
    "FACTURX_EXTENDED",
    "FACTURX_EXTENDED_PROFILE_ID",
    "RULESETS",
    "RULESET_REGISTRY",
    "UBL_CBC_NAMESPACE",
    "XRECHNUNG_3_0_CII",
    "XRECHNUNG_3_0_CVD_CII",
    "XRECHNUNG_3_0_CVD_PROFILE_ID",
    "XRECHNUNG_3_0_CVD_UBL_CREDITNOTE",
    "XRECHNUNG_3_0_CVD_UBL_INVOICE",
    "XRECHNUNG_3_0_EXTENSION_CII",
    "XRECHNUNG_3_0_EXTENSION_PROFILE_ID",
    "XRECHNUNG_3_0_EXTENSION_UBL_INVOICE",
    "XRECHNUNG_3_0_PROFILE_ID",
    "XRECHNUNG_3_0_UBL_CREDITNOTE",
    "XRECHNUNG_3_0_UBL_INVOICE",
    "XRECHNUNG_CONFIGURATION_FILES",
    "XRECHNUNG_CONFIGURATION_SCENARIO_FILE",
    "XRECHNUNG_REPORT_STYLESHEET",
    "XRECHNUNG_RULESETS",
    "XRECHNUNG_RULES_VERSION",
    "OfficialRuleset",
    "RulesetFile",
    "RulesetSelection",
    "select_official_ruleset",
]
