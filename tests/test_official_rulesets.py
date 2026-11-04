from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.component_versions import KOSIT_COMPONENT_VERSIONS
from app.official_rulesets import (
    EN16931_CII,
    EN16931_PROFILE_ID,
    EN16931_RULES_VERSION,
    EN16931_UBL_CREDITNOTE,
    EN16931_UBL_INVOICE,
    FACTURX_EXTENDED,
    FACTURX_EXTENDED_PROFILE_ID,
    RULESET_REGISTRY,
    RULESETS,
    XRECHNUNG_3_0_CII,
    XRECHNUNG_3_0_CVD_CII,
    XRECHNUNG_3_0_CVD_PROFILE_ID,
    XRECHNUNG_3_0_CVD_UBL_CREDITNOTE,
    XRECHNUNG_3_0_CVD_UBL_INVOICE,
    XRECHNUNG_3_0_EXTENSION_CII,
    XRECHNUNG_3_0_EXTENSION_PROFILE_ID,
    XRECHNUNG_3_0_EXTENSION_UBL_INVOICE,
    XRECHNUNG_3_0_PROFILE_ID,
    XRECHNUNG_3_0_UBL_CREDITNOTE,
    XRECHNUNG_3_0_UBL_INVOICE,
    XRECHNUNG_CONFIGURATION_FILES,
    XRECHNUNG_CONFIGURATION_SCENARIO_FILE,
    XRECHNUNG_REPORT_STYLESHEET,
    XRECHNUNG_RULES_VERSION,
    XRECHNUNG_RULESETS,
    OfficialRuleset,
    select_official_ruleset,
)
from app.xml_utils import safe_parse_xml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
CII_NAMESPACE = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
UBL_INVOICE_NAMESPACE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
UBL_CREDITNOTE_NAMESPACE = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
BASE_FIXTURES = {
    "CrossIndustryInvoice": "xrechnung-3.0-cii-valid.xml",
    "Invoice": "xrechnung-3.0-ubl-invoice-valid.xml",
    "CreditNote": "xrechnung-3.0-ubl-creditnote-valid.xml",
}
# The scenario table of the KoSIT validator configuration XRechnung 2026-08-31.
SCENARIO_TABLE = {
    ("CrossIndustryInvoice", CII_NAMESPACE, XRECHNUNG_3_0_PROFILE_ID): "EN16931 XRechnung (CII)",
    ("CrossIndustryInvoice", CII_NAMESPACE, XRECHNUNG_3_0_EXTENSION_PROFILE_ID): "EN16931 XRechnung Extension (CII)",
    ("CrossIndustryInvoice", CII_NAMESPACE, XRECHNUNG_3_0_CVD_PROFILE_ID): "EN16931 XRechnung CVD (CII)",
    ("Invoice", UBL_INVOICE_NAMESPACE, XRECHNUNG_3_0_PROFILE_ID): "EN16931 XRechnung (UBL Invoice)",
    ("Invoice", UBL_INVOICE_NAMESPACE, XRECHNUNG_3_0_EXTENSION_PROFILE_ID): "EN16931 XRechnung Extension (UBL Invoice)",
    ("Invoice", UBL_INVOICE_NAMESPACE, XRECHNUNG_3_0_CVD_PROFILE_ID): "EN16931 XRechnung CVD (UBL Invoice)",
    ("CreditNote", UBL_CREDITNOTE_NAMESPACE, XRECHNUNG_3_0_PROFILE_ID): "EN16931 XRechnung (UBL CreditNote)",
    ("CreditNote", UBL_CREDITNOTE_NAMESPACE, XRECHNUNG_3_0_CVD_PROFILE_ID): "EN16931 XRechnung CVD (UBL CreditNote)",
    ("CrossIndustryInvoice", CII_NAMESPACE, EN16931_PROFILE_ID): "EN16931 (CII)",
    ("Invoice", UBL_INVOICE_NAMESPACE, EN16931_PROFILE_ID): "EN16931 (UBL Invoice)",
    ("CreditNote", UBL_CREDITNOTE_NAMESPACE, EN16931_PROFILE_ID): "EN16931 (UBL CreditNote)",
}
CONFIGURATION_TARGETS = {
    "resources/cii/16b/xsd/CrossIndustryInvoice_100pD16B.xsd",
    "resources/cii/16b/xsd/CrossIndustryInvoice_QualifiedDataType_100pD16B.xsd",
    "resources/cii/16b/xsd/CrossIndustryInvoice_ReusableAggregateBusinessInformationEntity_100pD16B.xsd",
    "resources/cii/16b/xsd/CrossIndustryInvoice_UnqualifiedDataType_100pD16B.xsd",
    "resources/cii/16b/xsl/EN16931-CII-validation.xsl",
    "resources/default-report.xsl",
    "resources/ubl/2.1/xsd/common/CCTS_CCT_SchemaModule-2.1.xsd",
    "resources/ubl/2.1/xsd/common/UBL-CommonAggregateComponents-2.1.xsd",
    "resources/ubl/2.1/xsd/common/UBL-CommonBasicComponents-2.1.xsd",
    "resources/ubl/2.1/xsd/common/UBL-CommonExtensionComponents-2.1.xsd",
    "resources/ubl/2.1/xsd/common/UBL-CommonSignatureComponents-2.1.xsd",
    "resources/ubl/2.1/xsd/common/UBL-ExtensionContentDataType-2.1.xsd",
    "resources/ubl/2.1/xsd/common/UBL-QualifiedDataTypes-2.1.xsd",
    "resources/ubl/2.1/xsd/common/UBL-SignatureAggregateComponents-2.1.xsd",
    "resources/ubl/2.1/xsd/common/UBL-SignatureBasicComponents-2.1.xsd",
    "resources/ubl/2.1/xsd/common/UBL-UnqualifiedDataTypes-2.1.xsd",
    "resources/ubl/2.1/xsd/common/UBL-XAdESv132-2.1.xsd",
    "resources/ubl/2.1/xsd/common/UBL-XAdESv141-2.1.xsd",
    "resources/ubl/2.1/xsd/common/UBL-xmldsig-core-schema-2.1.xsd",
    "resources/ubl/2.1/xsd/maindoc/UBL-CreditNote-2.1.xsd",
    "resources/ubl/2.1/xsd/maindoc/UBL-Invoice-2.1.xsd",
    "resources/ubl/2.1/xsl/EN16931-UBL-validation.xsl",
    "resources/xrechnung-report.xsl",
    "resources/xrechnung/3.0.2/xsl/XRechnung-CII-validation.xsl",
    "resources/xrechnung/3.0.2/xsl/XRechnung-UBL-validation.xsl",
}


def _select(name: str):
    return select_official_ruleset(safe_parse_xml((FIXTURES / name).read_bytes()))


def _select_variant(root_local_name: str, identifier_xml: str):
    """The synthetic fixture of the root element with its identifier element replaced verbatim."""

    payload = (FIXTURES / BASE_FIXTURES[root_local_name]).read_text(encoding="utf-8")
    if root_local_name == "CrossIndustryInvoice":
        original = f"<ram:ID>{XRECHNUNG_3_0_PROFILE_ID}</ram:ID>"
    else:
        original = f"<cbc:CustomizationID>{XRECHNUNG_3_0_PROFILE_ID}</cbc:CustomizationID>"
    assert payload.count(original) == 1
    return select_official_ruleset(safe_parse_xml(payload.replace(original, identifier_xml).encode("utf-8")))


def _identifier(root_local_name: str, identifier: str) -> str:
    if root_local_name == "CrossIndustryInvoice":
        return f"<ram:ID>{identifier}</ram:ID>"
    return f"<cbc:CustomizationID>{identifier}</cbc:CustomizationID>"


def test_registry_matches_the_kosit_lock() -> None:
    lock = json.loads((PROJECT_ROOT / "packaging" / "kosit" / "components.lock.json").read_text(encoding="utf-8"))
    component = lock["components"]["facturx_extended"]
    locked = {(item["target"], item["size"], item["sha256"]) for item in component["files"]}
    registered = {(item.target, item.size, item.sha256) for item in FACTURX_EXTENDED.files}

    assert registered == locked
    assert FACTURX_EXTENDED.source_commit == component["commit"]
    assert FACTURX_EXTENDED.source_tag == component["tag"]
    assert FACTURX_EXTENDED.source_commit[:7] in lock["standards"]["facturx_extended_source"]
    assert FACTURX_EXTENDED.source_tag in lock["standards"]["facturx_extended_source"]
    assert lock["standards"]["facturx_extended"] == "1.09.2"
    assert FACTURX_EXTENDED.source_commit[:7] in FACTURX_EXTENDED.rules_version
    assert FACTURX_EXTENDED.settings_key == "facturx_extended"
    assert FACTURX_EXTENDED.scenario_file is None
    assert FACTURX_EXTENDED.report_stylesheet == "resources/report/default-report.xsl"
    assert len(FACTURX_EXTENDED.summary_notes) == 2
    assert len(FACTURX_EXTENDED.known_deviation_rules) == 12
    standards = lock["standards"]
    assert XRECHNUNG_RULESETS[0].source_tag == f"v{standards['xrechnung_configuration']}"
    assert lock["components"]["xrechnung"]["filename"].endswith(f"{standards['xrechnung_configuration']}.zip")


def test_xrechnung_rulesets_bind_the_whole_pinned_configuration() -> None:
    targets = [item.target for item in XRECHNUNG_CONFIGURATION_FILES]
    assert set(targets) == CONFIGURATION_TARGETS
    assert len(targets) == len(set(targets)) == 25
    for item in (*XRECHNUNG_CONFIGURATION_FILES, XRECHNUNG_CONFIGURATION_SCENARIO_FILE):
        assert item.size > 0
        assert re.fullmatch(r"[0-9a-f]{64}", item.sha256), item.target
    assert XRECHNUNG_CONFIGURATION_SCENARIO_FILE.target == "scenarios.xml"
    assert XRECHNUNG_REPORT_STYLESHEET in targets
    assert "resources/xrechnung-report.xsl" in targets
    versions = KOSIT_COMPONENT_VERSIONS
    assert XRECHNUNG_RULES_VERSION == (
        f"XRechnung {versions['xrechnung']} / Validator-Konfiguration {versions['xrechnung_configuration']}"
        f" / EN 16931 {versions['cen_en16931']} / XRechnung-Schematron {versions['xrechnung_schematron']}"
    )
    assert EN16931_RULES_VERSION == (
        f"EN 16931 {versions['cen_en16931']} / Validator-Konfiguration {versions['xrechnung_configuration']}"
    )
    for ruleset in XRECHNUNG_RULESETS:
        assert ruleset.lock_component == "xrechnung"
        assert ruleset.settings_key is None
        assert ruleset.publisher == "KoSIT"
        assert ruleset.files is XRECHNUNG_CONFIGURATION_FILES
        assert ruleset.scenario_file is XRECHNUNG_CONFIGURATION_SCENARIO_FILE
        assert ruleset.report_stylesheet == XRECHNUNG_REPORT_STYLESHEET
        assert ruleset.known_deviation_rules == frozenset()
        assert ruleset.summary_notes == ()
        expected_version = EN16931_RULES_VERSION if ruleset.id.startswith("en16931-") else XRECHNUNG_RULES_VERSION
        assert ruleset.rules_version == expected_version


def test_registry_is_keyed_by_root_namespace_and_exact_identifier() -> None:
    assert RULESETS == XRECHNUNG_RULESETS + (FACTURX_EXTENDED,)
    assert len(RULESETS) == 12
    assert len({ruleset.id for ruleset in RULESETS}) == 12
    assert {key: ruleset.scenario_name for key, ruleset in RULESET_REGISTRY.items()} == {
        **SCENARIO_TABLE,
        ("CrossIndustryInvoice", CII_NAMESPACE, FACTURX_EXTENDED_PROFILE_ID): "Factur-X 1.09.2 EXTENDED (CII)",
    }
    # The configuration has no XRechnung Extension scenario for UBL credit notes.
    assert ("CreditNote", UBL_CREDITNOTE_NAMESPACE, XRECHNUNG_3_0_EXTENSION_PROFILE_ID) not in RULESET_REGISTRY
    scenario_file = (PROJECT_ROOT / "packaging" / "kosit" / "facturx-extended" / "scenarios.xml").read_text(
        encoding="utf-8"
    )
    assert f"<name>{FACTURX_EXTENDED.scenario_name}</name>" in scenario_file
    assert f"text() = '{FACTURX_EXTENDED_PROFILE_ID}'" in scenario_file


@pytest.mark.parametrize(
    "name",
    [
        "cii-facturx-extended-valid.xml",
        "cii-facturx-extended-valid-sublines.xml",
        "cii-facturx-extended-xsd-error.xml",
        "cii-facturx-extended-schematron-error.xml",
    ],
)
def test_exact_identifier_selects_the_extended_ruleset(name: str) -> None:
    selection = _select(name)

    assert selection.ruleset is FACTURX_EXTENDED
    assert selection.reason is None
    assert selection.raw_identifier == FACTURX_EXTENDED_PROFILE_ID
    assert selection.candidate_count == 1
    assert selection.near_miss is False


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("xrechnung-3.0-cii-valid.xml", XRECHNUNG_3_0_CII),
        ("xrechnung-3.0-ubl-invoice-valid.xml", XRECHNUNG_3_0_UBL_INVOICE),
        ("xrechnung-3.0-ubl-invoice-br-de-6.xml", XRECHNUNG_3_0_UBL_INVOICE),
        ("xrechnung-3.0-ubl-creditnote-valid.xml", XRECHNUNG_3_0_UBL_CREDITNOTE),
        ("cii-category-o.xml", EN16931_CII),
        ("ubl-category-o.xml", EN16931_UBL_INVOICE),
        ("ubl-credit-note.xml", EN16931_UBL_CREDITNOTE),
    ],
)
def test_exact_xrechnung_and_en16931_identifiers_select_their_scenario(name: str, expected: OfficialRuleset) -> None:
    selection = _select(name)

    assert selection.ruleset is expected
    assert selection.reason is None
    assert selection.raw_identifier == expected.profile_id
    assert selection.near_miss is False


@pytest.mark.parametrize(
    ("root_local_name", "identifier", "expected"),
    [
        ("CrossIndustryInvoice", XRECHNUNG_3_0_EXTENSION_PROFILE_ID, XRECHNUNG_3_0_EXTENSION_CII),
        ("CrossIndustryInvoice", XRECHNUNG_3_0_CVD_PROFILE_ID, XRECHNUNG_3_0_CVD_CII),
        ("CrossIndustryInvoice", EN16931_PROFILE_ID, EN16931_CII),
        ("Invoice", XRECHNUNG_3_0_EXTENSION_PROFILE_ID, XRECHNUNG_3_0_EXTENSION_UBL_INVOICE),
        ("Invoice", XRECHNUNG_3_0_CVD_PROFILE_ID, XRECHNUNG_3_0_CVD_UBL_INVOICE),
        ("Invoice", EN16931_PROFILE_ID, EN16931_UBL_INVOICE),
        ("CreditNote", XRECHNUNG_3_0_CVD_PROFILE_ID, XRECHNUNG_3_0_CVD_UBL_CREDITNOTE),
        ("CreditNote", EN16931_PROFILE_ID, EN16931_UBL_CREDITNOTE),
    ],
)
def test_every_scenario_identifier_selects_the_matching_rule_set(
    root_local_name: str, identifier: str, expected: OfficialRuleset
) -> None:
    selection = _select_variant(root_local_name, _identifier(root_local_name, identifier))

    assert selection.ruleset is expected
    assert expected.scenario_name == SCENARIO_TABLE[(expected.root_local_name, expected.root_namespace, identifier)]


@pytest.mark.parametrize(
    ("root_local_name", "identifier_xml", "reason", "near_miss"),
    [
        ("CrossIndustryInvoice", f"<ram:ID> {XRECHNUNG_3_0_PROFILE_ID} </ram:ID>", "unknown-identifier", True),
        ("CrossIndustryInvoice", f"<ram:ID>{XRECHNUNG_3_0_PROFILE_ID}\n</ram:ID>", "unknown-identifier", True),
        (
            "Invoice",
            f"<cbc:CustomizationID>\n  {XRECHNUNG_3_0_PROFILE_ID}\n</cbc:CustomizationID>",
            "unknown-identifier",
            True,
        ),
        (
            "Invoice",
            "<cbc:CustomizationID>urn:cen.eu:en16931:2017<!-- synthetisch -->#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
            "</cbc:CustomizationID>",
            "mixed-content",
            True,
        ),
        (
            "CreditNote",
            f"<cbc:CustomizationID>{XRECHNUNG_3_0_PROFILE_ID}<!-- Ende --></cbc:CustomizationID>",
            "mixed-content",
            True,
        ),
        (
            "Invoice",
            f"<cbc:CustomizationID>{XRECHNUNG_3_0_PROFILE_ID.upper()}</cbc:CustomizationID>",
            "unknown-identifier",
            False,
        ),
        (
            "Invoice",
            "<cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_2.3</cbc:CustomizationID>",
            "unknown-identifier",
            False,
        ),
        (
            "CrossIndustryInvoice",
            "<ram:ID>urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.1</ram:ID>",
            "unknown-identifier",
            False,
        ),
        (
            "CrossIndustryInvoice",
            "<ram:ID>urn:cen.eu:en16931:2017#conformant#urn:xeinkauf.de:kosit:xrechnung_3.0</ram:ID>",
            "unknown-identifier",
            False,
        ),
        (
            "Invoice",
            "<cbc:CustomizationID>urn:cen.eu:en16931:2017#conformant#urn:xeinkauf.de:kosit:xrechnung_3.0</cbc:CustomizationID>",
            "unknown-identifier",
            False,
        ),
        ("Invoice", "<cbc:CustomizationID>urn:cen.eu:en16931:2017 </cbc:CustomizationID>", "unknown-identifier", True),
        (
            "Invoice",
            "<cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0</cbc:CustomizationID><cbc:CustomizationID>urn:cen.eu:en16931:2017</cbc:CustomizationID>",
            "ambiguous",
            False,
        ),
        (
            "CrossIndustryInvoice",
            f"<ram:ID>{XRECHNUNG_3_0_PROFILE_ID}</ram:ID><ram:ID>{XRECHNUNG_3_0_PROFILE_ID}</ram:ID>",
            "ambiguous",
            False,
        ),
        (
            "Invoice",
            f"<cbc:CustomizationID>{FACTURX_EXTENDED_PROFILE_ID}</cbc:CustomizationID>",
            "unknown-identifier",
            False,
        ),
        (
            "CreditNote",
            f"<cbc:CustomizationID>{XRECHNUNG_3_0_EXTENSION_PROFILE_ID}</cbc:CustomizationID>",
            "unknown-identifier",
            False,
        ),
        (
            "CreditNote",
            f"<cbc:CustomizationID> {XRECHNUNG_3_0_EXTENSION_PROFILE_ID}</cbc:CustomizationID>",
            "unknown-identifier",
            False,
        ),
        ("Invoice", "<cbc:CustomizationID></cbc:CustomizationID>", "no-identifier", False),
        ("Invoice", "", "no-identifier", False),
    ],
    ids=[
        "cii-whitespace",
        "cii-newline",
        "ubl-whitespace",
        "ubl-comment-middle",
        "creditnote-comment-end",
        "ubl-upper-case",
        "ubl-xrechnung-2.3",
        "cii-xrechnung-3.1",
        "cii-conformant",
        "ubl-conformant",
        "ubl-en-trailing-space",
        "ubl-duplicate",
        "cii-duplicate",
        "ubl-with-cii-only-identifier",
        "creditnote-extension",
        "creditnote-extension-whitespace",
        "ubl-empty",
        "ubl-missing",
    ],
)
def test_look_alike_identifiers_never_select_a_ruleset(
    root_local_name: str, identifier_xml: str, reason: str, near_miss: bool
) -> None:
    selection = _select_variant(root_local_name, identifier_xml)

    assert selection.ruleset is None
    assert selection.reason == reason
    assert selection.near_miss is near_miss


@pytest.mark.parametrize(
    ("name", "reason", "near_miss"),
    [
        ("cii-facturx-extended-id-whitespace.xml", "unknown-identifier", True),
        ("cii-facturx-extended-id-comment-middle.xml", "mixed-content", True),
        ("cii-facturx-extended-id-comment-end.xml", "mixed-content", True),
        ("cii-facturx-extended-id-case.xml", "unknown-identifier", False),
        ("cii-facturx-extended-id-compliant.xml", "unknown-identifier", False),
        ("cii-facturx-extended-id-suffix.xml", "unknown-identifier", False),
        ("cii-facturx-extended-id-zugferd2p0.xml", "unknown-identifier", False),
        ("cii-facturx-extended-id-duplicate.xml", "ambiguous", False),
        ("ubl-facturx-extended-id.xml", "unknown-identifier", False),
    ],
)
def test_look_alike_extended_identifiers_never_select_a_ruleset(name: str, reason: str, near_miss: bool) -> None:
    selection = _select(name)

    assert selection.ruleset is None
    assert selection.reason == reason
    assert selection.near_miss is near_miss


def test_whitespace_identifier_keeps_the_raw_text_for_the_internal_finding() -> None:
    selection = _select("cii-facturx-extended-id-whitespace.xml")

    assert selection.raw_identifier is not None
    assert selection.raw_identifier != FACTURX_EXTENDED_PROFILE_ID
    assert selection.raw_identifier.strip() == FACTURX_EXTENDED_PROFILE_ID
    assert selection.normalized_identifier == FACTURX_EXTENDED_PROFILE_ID


def test_missing_identifier_is_reported_as_such() -> None:
    root = safe_parse_xml(
        b'<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100">'
        b"<rsm:ExchangedDocumentContext/></rsm:CrossIndustryInvoice>"
    )
    selection = select_official_ruleset(root)

    assert selection.ruleset is None
    assert selection.reason == "no-identifier"
    assert selection.candidate_count == 0


def test_unsupported_roots_select_nothing() -> None:
    for payload in (
        b'<Invoice xmlns="urn:example:not-ubl"><CustomizationID>urn:cen.eu:en16931:2017</CustomizationID></Invoice>',
        b'<Order xmlns="urn:oasis:names:specification:ubl:schema:xsd:Order-2" '
        b'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">'
        b"<cbc:CustomizationID>urn:cen.eu:en16931:2017</cbc:CustomizationID></Order>",
    ):
        selection = select_official_ruleset(safe_parse_xml(payload))
        assert selection.ruleset is None
        assert selection.reason == "unsupported-root"
        assert selection.near_miss is False
