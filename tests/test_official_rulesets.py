from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.official_rulesets import (
    FACTURX_EXTENDED,
    FACTURX_EXTENDED_PROFILE_ID,
    RULESET_REGISTRY,
    RULESETS,
    select_official_ruleset,
)
from app.xml_utils import safe_parse_xml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def _select(name: str):
    return select_official_ruleset(safe_parse_xml((FIXTURES / name).read_bytes()))


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


def test_registry_is_keyed_by_root_namespace_and_exact_identifier() -> None:
    assert RULESETS == (FACTURX_EXTENDED,)
    assert set(RULESET_REGISTRY) == {
        (
            "CrossIndustryInvoice",
            "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
            FACTURX_EXTENDED_PROFILE_ID,
        )
    }
    assert FACTURX_EXTENDED.scenario_name == "Factur-X 1.09.2 EXTENDED (CII)"
    scenario_file = (PROJECT_ROOT / "packaging" / "kosit" / "facturx-extended" / "scenarios.xml").read_text(
        encoding="utf-8"
    )
    assert f"<name>{FACTURX_EXTENDED.scenario_name}</name>" in scenario_file
    assert f"text() = '{FACTURX_EXTENDED_PROFILE_ID}'" in scenario_file
    assert len(FACTURX_EXTENDED.known_deviation_rules) == 12


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
        ("ubl-facturx-extended-id.xml", "unsupported-root", False),
        ("cii-category-o.xml", "unknown-identifier", False),
    ],
)
def test_look_alike_identifiers_never_select_a_ruleset(name: str, reason: str, near_miss: bool) -> None:
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
