"""Regression baseline for the synthetic Factur-X EXTENDED fixtures.

The CII fixtures were validated against the official Factur-X 1.09.2 EXTENDED rules
with KoSIT Validator 1.6.3 on 2026-11-03 (see the plan and the trial-run protocol
under ``local-data/`` of the maintainers). These tests keep the fixtures consistent
and pin the behaviour that must not change when the Extended validation path is
introduced: look-alike identifiers never unlock an official validation.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from lxml import etree

from app.analyzer import analyze_bytes
from app.profiles import ProfileFamily, resolve_profile
from app.settings import Settings
from app.validators.kosit import KositValidator
from app.xml_utils import safe_parse_xml

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXTENDED_URN = "urn:cen.eu:en16931:2017#conformant#urn:factur-x.eu:1p0:extended"
VARL = "http://www.xoev.de/de/validator/varl/1"

CII_FIXTURES = sorted(path.name for path in FIXTURES.glob("cii-facturx-extended-*.xml"))
REPORT_FIXTURES = sorted(path.name for path in FIXTURES.glob("kosit-report-*.xml"))
LOOK_ALIKE_FIXTURES = [
    "cii-facturx-extended-id-whitespace.xml",
    "cii-facturx-extended-id-comment-middle.xml",
    "cii-facturx-extended-id-comment-end.xml",
    "cii-facturx-extended-id-case.xml",
    "cii-facturx-extended-id-compliant.xml",
    "cii-facturx-extended-id-suffix.xml",
    "cii-facturx-extended-id-zugferd2p0.xml",
    "cii-facturx-extended-id-duplicate.xml",
    "ubl-facturx-extended-id.xml",
]
# Which invoice fixture a synthetic report's document hash was computed from.
REPORT_SOURCES = {
    "kosit-report-extended-accept.xml": "cii-facturx-extended-valid.xml",
    "kosit-report-extended-accept-warning.xml": "cii-facturx-extended-valid.xml",
    "kosit-report-extended-reject.xml": "cii-facturx-extended-schematron-error.xml",
    "kosit-report-extended-reject-xsd.xml": "cii-facturx-extended-xsd-error.xml",
    "kosit-report-noscenario-reject.xml": "cii-facturx-extended-id-whitespace.xml",
    "kosit-report-wrong-scenario.xml": "cii-facturx-extended-valid.xml",
    "kosit-report-missing-assessment.xml": "cii-facturx-extended-valid.xml",
    "kosit-report-double-assessment.xml": "cii-facturx-extended-valid.xml",
    "kosit-report-contradictory-assessment.xml": "cii-facturx-extended-valid.xml",
    "kosit-report-wrong-namespace.xml": "cii-facturx-extended-valid.xml",
    "kosit-report-fixfr04-rule.xml": "cii-facturx-extended-valid.xml",
}


def test_every_expected_fixture_exists() -> None:
    assert len(CII_FIXTURES) == 12
    assert len(REPORT_FIXTURES) == 15
    assert (FIXTURES / "ubl-facturx-extended-id.xml").is_file()
    assert set(REPORT_SOURCES) <= set(REPORT_FIXTURES)


@pytest.mark.parametrize("name", CII_FIXTURES)
def test_cii_fixtures_are_synthetic_and_parse_with_the_hardened_parser(name: str) -> None:
    payload = (FIXTURES / name).read_bytes()
    assert b"Synthetisches Beispiel" in payload
    assert b"<!DOCTYPE" not in payload and b"<!ENTITY" not in payload
    root = safe_parse_xml(payload)
    assert root.tag == "{urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100}CrossIndustryInvoice"
    text = payload.decode("utf-8")
    assert "Beispiel Lieferant GmbH" in text and "beispiel-lieferant.example" in text


def test_valid_fixture_carries_the_exact_identifier_and_a_peppol_process() -> None:
    text = (FIXTURES / "cii-facturx-extended-valid.xml").read_text(encoding="utf-8")
    assert f"<ram:ID>{EXTENDED_URN}</ram:ID>" in text
    assert "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0" in text
    sublines = (FIXTURES / "cii-facturx-extended-valid-sublines.xml").read_text(encoding="utf-8")
    assert "<ram:LineStatusReasonCode>GROUP</ram:LineStatusReasonCode>" in sublines
    assert "<ram:ParentLineID>01</ram:ParentLineID>" in sublines


@pytest.mark.parametrize("name", REPORT_FIXTURES)
def test_report_fixtures_are_well_formed_varl_like_documents(name: str) -> None:
    root = etree.fromstring((FIXTURES / name).read_bytes())
    assert etree.QName(root).localname == "report"
    if name != "kosit-report-wrong-namespace.xml":
        assert etree.QName(root).namespace == VARL


@pytest.mark.parametrize(("report", "source"), sorted(REPORT_SOURCES.items()))
def test_report_fixture_hashes_match_their_source_fixture(report: str, source: str) -> None:
    root = etree.fromstring((FIXTURES / report).read_bytes())
    namespace = etree.QName(root).namespace
    value = root.findtext(f"{{{namespace}}}documentIdentification/{{{namespace}}}documentHash/{{{namespace}}}hashValue")
    assert value is not None
    assert base64.b64decode(value) == hashlib.sha256((FIXTURES / source).read_bytes()).digest()


def test_wrong_hash_fixture_does_not_match_any_invoice_fixture() -> None:
    root = etree.fromstring((FIXTURES / "kosit-report-wrong-hash.xml").read_bytes())
    value = root.findtext(f"{{{VARL}}}documentIdentification/{{{VARL}}}documentHash/{{{VARL}}}hashValue")
    assert value is not None
    digests = {hashlib.sha256((FIXTURES / name).read_bytes()).digest() for name in CII_FIXTURES}
    assert base64.b64decode(value) not in digests


@pytest.mark.parametrize("name", LOOK_ALIKE_FIXTURES)
def test_look_alike_identifiers_never_unlock_an_official_validation(name: str) -> None:
    payload = (FIXTURES / name).read_bytes()
    result = analyze_bytes(payload, name, run_official_validation=True)
    official = result["assessment"]["official"]
    assert official["status"] == "unsupported"
    assert official["executed"] is False
    assert official["requested"] is True


@pytest.mark.parametrize(
    ("name", "family"),
    [
        ("cii-facturx-extended-id-case.xml", ProfileFamily.FACTUR_X),
        ("cii-facturx-extended-id-compliant.xml", ProfileFamily.FACTUR_X),
        ("cii-facturx-extended-id-suffix.xml", ProfileFamily.FACTUR_X),
        ("cii-facturx-extended-id-zugferd2p0.xml", ProfileFamily.ZUGFERD),
    ],
)
def test_profile_family_of_look_alike_identifiers_is_preserved(name: str, family: ProfileFamily) -> None:
    result = analyze_bytes((FIXTURES / name).read_bytes(), name, run_official_validation=False)
    assert resolve_profile(result["profile"]["id"]).family is family


def test_exact_identifier_is_bundled_but_unavailable_without_the_extended_rules() -> None:
    payload = (FIXTURES / "cii-facturx-extended-valid.xml").read_bytes()
    result = analyze_bytes(
        payload,
        "cii-facturx-extended-valid.xml",
        run_official_validation=True,
        app_settings=Settings(kosit_enabled=False),
    )

    official = result["assessment"]["official"]
    assert official["status"] == "unavailable"
    assert official["executed"] is False
    assert official["configured"] is False
    assert result["capabilities"]["official_validation"] == "unavailable"
    assert result["assessment"]["processing"]["status"] == "incomplete"
    assert result["profile"]["id"] == EXTENDED_URN
    assert resolve_profile(result["profile"]["id"]).family is ProfileFamily.FACTUR_X


def test_exact_identifier_runs_the_bound_extended_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def fake_validate(self: KositValidator, xml: bytes, filename: str, ruleset=None) -> dict:
        observed["ruleset"] = ruleset
        observed["xml"] = xml
        return {
            "configured": True,
            "problems": [],
            "jar": None,
            "jar_main_class": "x",
            "scenarios": [],
            "repositories": [],
            "ruleset": ruleset.id if ruleset else None,
            "rules_version": ruleset.rules_version if ruleset else None,
            "executed": True,
            "accepted": True,
            "exit_code": 0,
            "summary": "KoSIT-Prüfung erfolgreich: Rechnung wurde nach Factur-X 1.09.2 EXTENDED akzeptiert.",
            "findings": [],
            "raw_report": "<rep:report/>",
            "technical_output": None,
            "report_source": "file",
            "scenario": "Factur-X 1.09.2 EXTENDED (CII)",
        }

    monkeypatch.setattr(KositValidator, "validate", fake_validate)
    payload = (FIXTURES / "cii-facturx-extended-valid.xml").read_bytes()

    result = analyze_bytes(payload, "cii-facturx-extended-valid.xml", run_official_validation=True)

    assert observed["ruleset"] is not None
    assert observed["ruleset"].id == "facturx-extended-1.09.2"
    assert observed["xml"] == payload
    assert result["assessment"]["official"]["status"] == "accepted"
    assert result["assessment"]["official"]["executed"] is True
    assert result["capabilities"]["official_validation"] == "bundled"
    assert result["assessment"]["processing"]["status"] == "complete"


@pytest.mark.parametrize(
    ("name", "expected_ids"),
    [
        ("cii-facturx-extended-id-whitespace.xml", {"PROFILE-002"}),
        ("cii-facturx-extended-id-comment-middle.xml", {"PROFILE-002"}),
        ("cii-facturx-extended-id-comment-end.xml", {"PROFILE-002"}),
        ("cii-facturx-extended-id-duplicate.xml", {"PROFILE-003"}),
        ("cii-facturx-extended-id-case.xml", set()),
        ("cii-facturx-extended-id-compliant.xml", set()),
        ("cii-facturx-extended-valid.xml", set()),
    ],
)
def test_near_miss_identifiers_get_an_internal_warning(name: str, expected_ids: set[str]) -> None:
    result = analyze_bytes((FIXTURES / name).read_bytes(), name, run_official_validation=False)
    internal = result["assessment"]["internal"]
    ids = {item["rule"]["id"] for item in internal["findings"] if item["rule"]["id"].startswith("PROFILE-00")}

    assert ids - {"PROFILE-001"} == expected_ids
    for item in internal["findings"]:
        if item["rule"]["id"] in expected_ids:
            assert item["severity"] == "warning"
            assert item["origin"] == "internal"
            assert item["rule_class"] == "profile_precheck"
