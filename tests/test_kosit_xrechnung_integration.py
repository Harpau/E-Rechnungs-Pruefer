"""Integration suite: real KoSIT validator with the locked XRechnung configuration.

Without ``EINVOICE_KOSIT_INTEGRATION=1`` the whole module is skipped. With the
variable set, every missing, incomplete or tampered component and a missing
Java runtime is a test failure, never a skip: a green build must not hide an
XRechnung or EN 16931 validation that never ran. The suite uses the
application's own settings, so it validates exactly the installation the
application would use (``vendor/kosit`` and ``runtime/java`` or the ``KOSIT_*``
environment).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from app.analyzer import analyze_bytes
from app.component_versions import KOSIT_COMPONENT_VERSIONS
from app.official_rulesets import (
    EN16931_CII,
    EN16931_PROFILE_ID,
    EN16931_RULES_VERSION,
    EN16931_UBL_CREDITNOTE,
    EN16931_UBL_INVOICE,
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
    XRECHNUNG_RULES_VERSION,
    XRECHNUNG_RULESETS,
    OfficialRuleset,
)
from app.settings import Settings
from app.settings import settings as app_settings
from app.validators.kosit import KositValidator

ACTIVE = os.environ.get("EINVOICE_KOSIT_INTEGRATION", "").strip() == "1"
pytestmark = [pytest.mark.kosit_integration] + (
    [] if ACTIVE else [pytest.mark.skip(reason="EINVOICE_KOSIT_INTEGRATION=1 ist nicht gesetzt")]
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
EXAMPLES = PROJECT_ROOT / "app" / "examples"
VARL = "http://www.xoev.de/de/validator/varl/1"
SCENARIOS = "http://www.xoev.de/de/validator/framework/1/scenarios"
BASE_FIXTURES = {
    "CrossIndustryInvoice": "xrechnung-3.0-cii-valid.xml",
    "Invoice": "xrechnung-3.0-ubl-invoice-valid.xml",
    "CreditNote": "xrechnung-3.0-ubl-creditnote-valid.xml",
}


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _document(ruleset: OfficialRuleset) -> bytes:
    """The synthetic fixture of the rule set's root element carrying the rule set's exact identifier."""

    payload = _fixture(BASE_FIXTURES[ruleset.root_local_name])
    assert payload.count(XRECHNUNG_3_0_PROFILE_ID.encode()) == 1
    return payload.replace(XRECHNUNG_3_0_PROFILE_ID.encode(), ruleset.profile_id.encode())


def _with_identifier(root_local_name: str, identifier_xml: str) -> bytes:
    payload = _fixture(BASE_FIXTURES[root_local_name]).decode("utf-8")
    original = (
        f"<ram:ID>{XRECHNUNG_3_0_PROFILE_ID}</ram:ID>"
        if root_local_name == "CrossIndustryInvoice"
        else f"<cbc:CustomizationID>{XRECHNUNG_3_0_PROFILE_ID}</cbc:CustomizationID>"
    )
    assert payload.count(original) == 1
    return payload.replace(original, identifier_xml).encode("utf-8")


def _analysis(payload: bytes, name: str, *, settings: Settings = app_settings) -> dict[str, Any]:
    return analyze_bytes(payload, name, run_official_validation=True, app_settings=settings)


def _ids(findings: list[dict[str, Any]]) -> set[str]:
    return {item["rule"]["id"] for item in findings}


def _why(result: dict[str, Any]) -> str:
    """Diagnostic text for assertion messages: what the official run actually reported."""

    official = result["assessment"]["official"]
    processing = result["assessment"]["processing"]
    lines = [
        f"official.status={official['status']} executed={official['executed']} configured={official['configured']}"
    ]
    lines.append(f"scenario={official.get('scenario')!r} summary={official.get('summary')!r}")
    for item in official["findings"] + processing["findings"]:
        lines.append(f"{item['origin']} {item['rule']['id']}: {item['rule']['message'][:600]}")
    technical = official.get("technical_output")
    if technical:
        lines.append(f"technical_output={technical[:1500]!r}")
    return "\n".join(lines)


def _why_raw(result: dict[str, Any]) -> str:
    lines = [f"executed={result.get('executed')} accepted={result.get('accepted')} exit_code={result.get('exit_code')}"]
    lines.append(f"scenario={result.get('scenario')!r} summary={result.get('summary')!r}")
    for item in result.get("findings", []):
        lines.append(f"{item['id']}: {str(item.get('message'))[:600]}")
    if result.get("technical_output"):
        lines.append(f"technical_output={str(result['technical_output'])[:1500]!r}")
    return "\n".join(lines)


def _report_root(official: dict[str, Any]) -> etree._Element:
    return etree.fromstring(official["raw_report"].encode("utf-8"))


def _report_hash(official: dict[str, Any]) -> bytes:
    value = _report_root(official).findtext(
        f"{{{VARL}}}documentIdentification/{{{VARL}}}documentHash/{{{VARL}}}hashValue"
    )
    assert value is not None
    return base64.b64decode(value)


def _report_scenario(official: dict[str, Any]) -> str | None:
    return _report_root(official).findtext(f"{{{VARL}}}scenarioMatched/{{{SCENARIOS}}}scenario/{{{SCENARIOS}}}name")


@pytest.fixture(scope="module")
def installation() -> Settings:
    validator = KositValidator(app_settings)
    problems: list[str] = []
    for ruleset in XRECHNUNG_RULESETS:
        state = validator.configuration_state(ruleset)
        problems += [f"{ruleset.id}: {item}" for item in state["problems"]]
    if problems:
        pytest.fail(
            "Die Integrationssuite ist aktiviert, aber die XRechnung-Konfiguration ist unvollständig:\n"
            + "\n".join(problems)
        )
    jar = app_settings.kosit_validator_jar
    assert jar is not None and KOSIT_COMPONENT_VERSIONS["validator"] in jar.name, (
        f"Installiertes JAR {jar} entspricht nicht der gebundenen Validator-Version."
    )
    return app_settings


@pytest.fixture()
def configuration_copy(installation: Settings, tmp_path: Path) -> Callable[[], tuple[Settings, Path]]:
    """A private copy of the installed XRechnung configuration that tests may tamper with."""

    def make() -> tuple[Settings, Path]:
        source = installation.kosit_repositories[0]
        target = tmp_path / "xrechnung"
        shutil.copytree(source, target)
        settings = replace(installation, kosit_scenarios=(target / "scenarios.xml",), kosit_repositories=(target,))
        return settings, target

    return make


def test_installed_configuration_matches_the_registry(installation: Settings) -> None:
    repository = installation.kosit_repositories[0]
    (scenarios,) = installation.kosit_scenarios
    assert scenarios.stat().st_size == XRECHNUNG_CONFIGURATION_SCENARIO_FILE.size
    assert hashlib.sha256(scenarios.read_bytes()).hexdigest() == XRECHNUNG_CONFIGURATION_SCENARIO_FILE.sha256
    for item in XRECHNUNG_CONFIGURATION_FILES:
        path = repository / item.target
        assert path.stat().st_size == item.size, item.target
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item.sha256, item.target

    root = etree.parse(str(scenarios)).getroot()
    scenario_elements = root.findall(f"{{{SCENARIOS}}}scenario")
    names = [element.findtext(f"{{{SCENARIOS}}}name") for element in scenario_elements]
    assert names == [ruleset.scenario_name for ruleset in XRECHNUNG_RULESETS]
    registered_targets = {item.target for item in XRECHNUNG_CONFIGURATION_FILES}
    root_prefix = {
        "CrossIndustryInvoice": "/rsm:CrossIndustryInvoice",
        "Invoice": "/invoice:Invoice",
        "CreditNote": "/creditnote:CreditNote",
    }
    for ruleset, element in zip(XRECHNUNG_RULESETS, scenario_elements, strict=True):
        match = " ".join((element.findtext(f"{{{SCENARIOS}}}match") or "").split())
        assert f"'{ruleset.profile_id}'" in match, ruleset.id
        assert match.startswith(f"exists({root_prefix[ruleset.root_local_name]}"), ruleset.id
        assert element.findtext(f"{{{SCENARIOS}}}acceptMatch") == "/rep:report/rep:assessment[1]/rep:accept[1]"
        for resource in element.iter(f"{{{SCENARIOS}}}resource"):
            assert resource.findtext(f"{{{SCENARIOS}}}location") in registered_targets, ruleset.id
    text = scenarios.read_text(encoding="utf-8")
    for component in ("xrechnung", "cen_en16931", "xrechnung_schematron"):
        assert KOSIT_COMPONENT_VERSIONS[component] in text


@pytest.mark.parametrize(
    "ruleset",
    [XRECHNUNG_3_0_CII, XRECHNUNG_3_0_UBL_INVOICE, XRECHNUNG_3_0_UBL_CREDITNOTE],
    ids=lambda ruleset: ruleset.id,
)
def test_xrechnung_documents_are_accepted_with_bound_scenario_and_hash(
    installation: Settings, ruleset: OfficialRuleset
) -> None:
    payload = _document(ruleset)
    result = _analysis(payload, BASE_FIXTURES[ruleset.root_local_name])

    official = result["assessment"]["official"]
    assert official["status"] == "accepted", _why(result)
    assert official["executed"] is True
    assert official["report_source"] == "file"
    assert official["scenario"] == ruleset.scenario_name
    assert f"Prüfszenario: {ruleset.scenario_name}." in official["summary"]
    assert XRECHNUNG_RULES_VERSION in official["summary"]
    assert not [item for item in official["findings"] if item["severity"] == "error"], _why(result)
    assert result["capabilities"]["official_validation"] == "bundled"
    assert result["assessment"]["processing"]["status"] == "complete", _why(result)
    assert _report_scenario(official) == ruleset.scenario_name
    assert _report_hash(official) == hashlib.sha256(payload).digest()


@pytest.mark.parametrize(
    ("name", "payload", "ruleset"),
    [
        ("cii-rechnung-demo.xml", (EXAMPLES / "cii-rechnung-demo.xml").read_bytes(), EN16931_CII),
        ("ubl-rechnung-demo.xml", (EXAMPLES / "ubl-rechnung-demo.xml").read_bytes(), EN16931_UBL_INVOICE),
        ("en16931-ubl-creditnote.xml", _document(EN16931_UBL_CREDITNOTE), EN16931_UBL_CREDITNOTE),
    ],
    ids=["en16931-cii-example", "en16931-ubl-example", "en16931-ubl-creditnote"],
)
def test_en16931_documents_are_accepted_with_bound_scenario_and_hash(
    installation: Settings, name: str, payload: bytes, ruleset: OfficialRuleset
) -> None:
    result = _analysis(payload, name)

    official = result["assessment"]["official"]
    assert official["status"] == "accepted", _why(result)
    assert official["scenario"] == ruleset.scenario_name
    assert EN16931_RULES_VERSION in official["summary"]
    assert "XRechnung-Schematron" not in official["summary"]
    assert "EXTENDED" not in official["summary"]
    assert result["capabilities"]["official_validation"] == "bundled"
    assert _report_scenario(official) == ruleset.scenario_name
    assert _report_hash(official) == hashlib.sha256(payload).digest()


@pytest.mark.parametrize(
    "ruleset",
    [XRECHNUNG_3_0_EXTENSION_CII, XRECHNUNG_3_0_EXTENSION_UBL_INVOICE],
    ids=lambda ruleset: ruleset.id,
)
def test_extension_identifiers_bind_the_extension_scenarios(installation: Settings, ruleset: OfficialRuleset) -> None:
    payload = _document(ruleset)
    result = _analysis(payload, "extension.xml")

    official = result["assessment"]["official"]
    assert official["status"] == "accepted", _why(result)
    assert official["scenario"] == ruleset.scenario_name
    assert _report_scenario(official) == ruleset.scenario_name
    assert _report_hash(official) == hashlib.sha256(payload).digest()


@pytest.mark.parametrize(
    "ruleset",
    [XRECHNUNG_3_0_CVD_CII, XRECHNUNG_3_0_CVD_UBL_INVOICE, XRECHNUNG_3_0_CVD_UBL_CREDITNOTE],
    ids=lambda ruleset: ruleset.id,
)
def test_cvd_identifiers_bind_the_cvd_scenarios_and_report_their_rules(
    installation: Settings, ruleset: OfficialRuleset
) -> None:
    # The synthetic documents carry no contract or tender reference, so the CVD
    # rules reject them; the binding and the rule identifiers are what matters.
    payload = _document(ruleset)
    result = _analysis(payload, "cvd.xml")

    official = result["assessment"]["official"]
    assert official["status"] == "rejected", _why(result)
    assert official["scenario"] == ruleset.scenario_name
    assert {"BR-DE-CVD-01", "BR-DE-CVD-02"} <= _ids(official["findings"]), _why(result)
    assert _report_scenario(official) == ruleset.scenario_name
    assert _report_hash(official) == hashlib.sha256(payload).digest()
    assert result["assessment"]["processing"]["status"] == "complete"


def test_schematron_rejection_carries_the_rule_identifier_from_the_code_attribute(installation: Settings) -> None:
    payload = _fixture("xrechnung-3.0-ubl-invoice-br-de-6.xml")
    result = _analysis(payload, "br-de-6.xml")

    official = result["assessment"]["official"]
    assert official["status"] == "rejected", _why(result)
    assert official["scenario"] == XRECHNUNG_3_0_UBL_INVOICE.scenario_name
    assert "BR-DE-6" in _ids(official["findings"]), _why(result)
    finding = next(item for item in official["findings"] if item["rule"]["id"] == "BR-DE-6")
    assert finding["severity"] == "error"
    assert finding["rule"]["version"] == XRECHNUNG_RULES_VERSION
    assert finding["rule"]["profile"] == XRECHNUNG_3_0_PROFILE_ID
    assert finding["xml_location"] is not None and finding["xml_location"]["path"]
    assert all(item["severity"] != "error" or item["rule"]["id"] == "BR-DE-6" for item in official["findings"])
    assert result["assessment"]["processing"]["status"] == "complete", _why(result)
    assert _report_hash(official) == hashlib.sha256(payload).digest()


def test_a_report_of_another_scenario_is_no_decision(installation: Settings) -> None:
    # Bypass the selector on purpose: KoSIT applies the credit-note scenario,
    # the caller expected the invoice scenario.
    result = KositValidator(installation).validate(
        _document(XRECHNUNG_3_0_UBL_CREDITNOTE), "rechnung.xml", ruleset=XRECHNUNG_3_0_UBL_INVOICE
    )

    assert result["executed"] is False, _why_raw(result)
    assert result["accepted"] is None
    assert result["scenario"] is None
    assert [item["id"] for item in result["findings"]] == ["KOSIT-SCENARIO"], _why_raw(result)
    message = result["findings"][0]["message"]
    assert "'EN16931 XRechnung (UBL CreditNote)'" in message and "'EN16931 XRechnung (UBL Invoice)'" in message


def test_a_document_without_matching_scenario_is_no_decision(installation: Settings) -> None:
    # The configuration has no Extension scenario for credit notes: KoSIT answers with noScenarioMatched.
    payload = _with_identifier(
        "CreditNote", f"<cbc:CustomizationID>{XRECHNUNG_3_0_EXTENSION_PROFILE_ID}</cbc:CustomizationID>"
    )
    result = KositValidator(installation).validate(payload, "rechnung.xml", ruleset=XRECHNUNG_3_0_UBL_CREDITNOTE)

    assert result["executed"] is False, _why_raw(result)
    assert result["accepted"] is None
    assert [item["id"] for item in result["findings"]] == ["KOSIT-SCENARIO"], _why_raw(result)
    assert "kein Prüfszenario angewendet" in result["findings"][0]["message"]


@pytest.mark.parametrize(
    ("root_local_name", "identifier_xml"),
    [
        ("CrossIndustryInvoice", f"<ram:ID> {XRECHNUNG_3_0_PROFILE_ID} </ram:ID>"),
        ("Invoice", f"<cbc:CustomizationID>{XRECHNUNG_3_0_PROFILE_ID}<!-- Ende --></cbc:CustomizationID>"),
        ("Invoice", f"<cbc:CustomizationID>{XRECHNUNG_3_0_PROFILE_ID.upper()}</cbc:CustomizationID>"),
        (
            "Invoice",
            "<cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_2.3</cbc:CustomizationID>",
        ),
        (
            "CrossIndustryInvoice",
            "<ram:ID>urn:cen.eu:en16931:2017#conformant#urn:xeinkauf.de:kosit:xrechnung_3.0</ram:ID>",
        ),
        (
            "Invoice",
            f"<cbc:CustomizationID>{XRECHNUNG_3_0_PROFILE_ID}</cbc:CustomizationID>"
            f"<cbc:CustomizationID>{EN16931_PROFILE_ID}</cbc:CustomizationID>",
        ),
        ("CreditNote", f"<cbc:CustomizationID>{XRECHNUNG_3_0_EXTENSION_PROFILE_ID}</cbc:CustomizationID>"),
        ("CreditNote", f"<cbc:CustomizationID>{XRECHNUNG_3_0_CVD_PROFILE_ID} </cbc:CustomizationID>"),
    ],
    ids=[
        "cii-whitespace",
        "ubl-comment",
        "ubl-case",
        "ubl-2.3",
        "cii-conformant",
        "ubl-duplicate",
        "creditnote-extension",
        "creditnote-cvd-space",
    ],
)
def test_look_alike_identifiers_never_unlock_a_bound_validation(
    installation: Settings, root_local_name: str, identifier_xml: str
) -> None:
    result = _analysis(_with_identifier(root_local_name, identifier_xml), "look-alike.xml")

    official = result["assessment"]["official"]
    assert official["status"] == "unsupported", _why(result)
    assert official["executed"] is False
    assert official["scenario"] is None
    assert result["capabilities"]["official_validation"] == "not-bundled"
    assert result["assessment"]["processing"]["status"] == "complete"


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (
            lambda repo: (repo / "scenarios.xml").write_text(
                (repo / "scenarios.xml")
                .read_text(encoding="utf-8")
                .replace("<name>EN16931 XRechnung (CII)</name>", "<name>Anderes Szenario</name>"),
                encoding="utf-8",
            ),
            "Szenariodatei",
        ),
        (
            lambda repo: (repo / "resources/xrechnung/3.0.2/xsl/XRechnung-CII-validation.xsl").write_bytes(
                (repo / "resources/xrechnung/3.0.2/xsl/XRechnung-CII-validation.xsl").read_bytes()[::-1]
            ),
            "SHA-256",
        ),
        (lambda repo: (repo / "resources/ubl/2.1/xsd/maindoc/UBL-CreditNote-2.1.xsd").unlink(), "Regeldatei fehlt"),
    ],
    ids=["renamed-scenario", "tampered-schematron", "missing-schema"],
)
def test_tampered_configuration_makes_the_validation_unavailable(
    configuration_copy: Callable[[], tuple[Settings, Path]],
    mutate: Callable[[Path], object],
    problem: str,
) -> None:
    settings, repository = configuration_copy()
    mutate(repository)

    state = KositValidator(settings).configuration_state(XRECHNUNG_3_0_CII)
    assert state["configured"] is False
    assert any(problem in item for item in state["problems"]), state["problems"]

    payload = _document(XRECHNUNG_3_0_CII)
    result = _analysis(payload, "valid.xml", settings=settings)
    official = result["assessment"]["official"]
    assert official["status"] == "unavailable", _why(result)
    assert official["executed"] is False
    assert _ids(result["assessment"]["processing"]["findings"]) == {"KOSIT-CONFIG"}
    assert result["capabilities"]["official_validation"] == "unavailable"


def test_a_second_scenario_file_is_refused_for_bound_runs(
    configuration_copy: Callable[[], tuple[Settings, Path]],
) -> None:
    settings, repository = configuration_copy()
    settings = replace(settings, kosit_scenarios=(repository / "scenarios.xml", repository / "scenarios.xml"))

    state = KositValidator(settings).configuration_state(XRECHNUNG_3_0_UBL_INVOICE)

    assert state["configured"] is False
    assert any("genau eine Szenariodatei" in item for item in state["problems"]), state["problems"]


def test_original_bytes_reach_kosit_and_leave_the_xml_export_unchanged(
    installation: Settings, pdf_bytes_factory
) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    payload = _fixture("xrechnung-3.0-cii-valid.xml")
    pdf = pdf_bytes_factory(("xrechnung.xml", payload))
    client = TestClient(app)

    exported = client.post("/api/xml", files={"file": ("rechnung.pdf", pdf, "application/pdf")})
    assert exported.status_code == 200
    assert exported.content == payload

    result = _analysis(pdf, "rechnung.pdf")
    official = result["assessment"]["official"]
    assert official["status"] == "accepted", _why(result)
    assert official["scenario"] == XRECHNUNG_3_0_CII.scenario_name
    assert _report_hash(official) == hashlib.sha256(payload).digest()


def test_health_endpoint_reports_the_bound_configuration(installation: Settings) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    payload = TestClient(app).get("/api/health").json()

    assert payload["kosit"]["configured"] is True
    assert payload["kosit"]["xrechnung_configured"] is True
    assert payload["kosit"]["components"]["xrechnung_configuration"] == "2026-08-31"
    assert payload["kosit"]["components"]["xrechnung"] == "3.0.2"


def test_installed_stylesheets_load_nothing_outside_the_repository(installation: Settings) -> None:
    repository = installation.kosit_repositories[0]
    for item in XRECHNUNG_CONFIGURATION_FILES:
        if not item.target.endswith(".xsl"):
            continue
        text = (repository / item.target).read_text(encoding="utf-8", errors="replace")
        includes = re.findall(r"<xsl:(?:import|include)\s+href=\"([^\"]+)\"", text)
        assert includes == (["./default-report.xsl"] if item.target == "resources/xrechnung-report.xsl" else []), (
            item.target
        )
        for call in re.findall(r"document\(([^)]*)\)", text):
            assert call.startswith("$"), f"Unerwarteter document()-Aufruf in {item.target}: {call}"
        for uri in re.findall(r"https?://[^\s\"'<>]+", text):
            assert not uri.startswith(("file:", "ftp:")), item.target
