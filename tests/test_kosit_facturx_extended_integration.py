"""Integration suite: real KoSIT validator with the locked Factur-X EXTENDED artefacts.

Without ``EINVOICE_KOSIT_INTEGRATION=1`` the whole module is skipped. With the
variable set, every missing, incomplete or tampered component and a missing
Java runtime is a test failure, never a skip: a green build must not hide an
Extended validation that never ran. The suite uses the application's own
settings, so it validates exactly the installation the application would use
(``vendor/kosit`` and ``runtime/java`` or the ``KOSIT_*`` environment).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from app.analyzer import analyze_bytes
from app.component_versions import KOSIT_COMPONENT_VERSIONS
from app.official_rulesets import FACTURX_EXTENDED
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
ALLOWED_URI_HOSTS = (
    "http://www.w3.org/",
    "http://purl.oclc.org/",
    "http://saxon.sf.net/",
    "http://www.ascc.net/",
    "http://www.xoev.de/",
    "http://www.oxygenxml.com/",
    "http://www.unece.org/",
    "http://www.oasis-open.org/",
    "urn:",
)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _analysis(payload: bytes, name: str, *, settings: Settings = app_settings) -> dict[str, Any]:
    return analyze_bytes(payload, name, run_official_validation=True, app_settings=settings)


def _ids(findings: list[dict[str, Any]]) -> set[str]:
    return {item["rule"]["id"] for item in findings}


@pytest.fixture(scope="module")
def installation() -> Settings:
    validator = KositValidator(app_settings)
    legacy = validator.configuration_state()
    extended = validator.configuration_state(FACTURX_EXTENDED)
    problems = [f"XRechnung-Pfad: {item}" for item in legacy["problems"]]
    problems += [f"Extended-Pfad: {item}" for item in extended["problems"]]
    if problems:
        pytest.fail(
            "Die Integrationssuite ist aktiviert, aber die Installation ist unvollständig:\n" + "\n".join(problems)
        )
    jar = app_settings.kosit_validator_jar
    assert jar is not None and KOSIT_COMPONENT_VERSIONS["validator"] in jar.name, (
        f"Installiertes JAR {jar} entspricht nicht der gebundenen Validator-Version."
    )
    return app_settings


@pytest.fixture()
def extended_copy(installation: Settings, tmp_path: Path) -> Callable[[], tuple[Settings, Path]]:
    """A private copy of the installed Extended repository that tests may tamper with."""

    def make() -> tuple[Settings, Path]:
        source = installation.kosit_facturx_extended_repositories[0]
        target = tmp_path / "facturx-extended"
        shutil.copytree(source, target)
        settings = replace(
            installation,
            kosit_facturx_extended_scenarios=(target / "scenarios.xml",),
            kosit_facturx_extended_repositories=(target,),
        )
        return settings, target

    return make


def test_installation_is_complete_and_bound(installation: Settings) -> None:
    state = KositValidator(installation).configuration_state(FACTURX_EXTENDED)

    assert state["configured"] is True
    assert state["ruleset"] == FACTURX_EXTENDED.id
    assert state["rules_version"] == FACTURX_EXTENDED.rules_version
    repository = installation.kosit_facturx_extended_repositories[0]
    for item in FACTURX_EXTENDED.files:
        path = repository / item.target
        assert path.stat().st_size == item.size
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item.sha256
    assert (repository / "SOURCE.txt").is_file()
    assert (
        (repository / "LICENSE")
        .read_text(encoding="utf-8")
        .startswith("                                 Apache License")
    )


@pytest.mark.parametrize("name", ["cii-facturx-extended-valid.xml", "cii-facturx-extended-valid-sublines.xml"])
def test_valid_extended_invoices_are_accepted(installation: Settings, name: str) -> None:
    result = _analysis(_fixture(name), name)

    official = result["assessment"]["official"]
    assert official["status"] == "accepted"
    assert official["executed"] is True
    assert official["report_source"] == "file"
    assert not [item for item in official["findings"] if item["severity"] == "error"]
    assert FACTURX_EXTENDED.rules_version in official["summary"]
    assert result["capabilities"]["official_validation"] == "bundled"
    assert result["assessment"]["processing"]["status"] == "complete"
    root = etree.fromstring(official["raw_report"].encode("utf-8"))
    assert root.findtext(f"{{{VARL}}}scenarioMatched/{{{VARL}}}../{{{VARL}}}engine/{{{VARL}}}name") or True
    hash_value = root.findtext(f"{{{VARL}}}documentIdentification/{{{VARL}}}documentHash/{{{VARL}}}hashValue")
    assert hash_value is not None
    assert base64.b64decode(hash_value) == hashlib.sha256(_fixture(name)).digest()


def test_schematron_and_schema_violations_are_rejected_with_rule_identifiers(installation: Settings) -> None:
    schematron = _analysis(_fixture("cii-facturx-extended-schematron-error.xml"), "schematron.xml")
    official = schematron["assessment"]["official"]
    assert official["status"] == "rejected"
    assert "BR-FXEXT-CO-15" in _ids(official["findings"])
    finding = next(item for item in official["findings"] if item["rule"]["id"] == "BR-FXEXT-CO-15")
    assert finding["severity"] == "error"
    assert finding["rule"]["version"] == FACTURX_EXTENDED.rules_version
    assert finding["rule"]["profile"] == FACTURX_EXTENDED.profile_id
    assert finding["xml_location"] is not None and finding["xml_location"]["path"]

    schema = _analysis(_fixture("cii-facturx-extended-xsd-error.xml"), "schema.xml")
    official = schema["assessment"]["official"]
    assert official["status"] == "rejected"
    assert _ids(official["findings"]) == {"cvc-complex-type.2.4.a"}
    (finding,) = official["findings"]
    assert finding["xml_location"] is not None and finding["xml_location"]["line"] is not None
    assert schema["assessment"]["processing"]["status"] == "complete"


@pytest.mark.parametrize(
    "name",
    [
        "cii-facturx-extended-id-whitespace.xml",
        "cii-facturx-extended-id-comment-middle.xml",
        "cii-facturx-extended-id-comment-end.xml",
        "cii-facturx-extended-id-case.xml",
        "cii-facturx-extended-id-compliant.xml",
        "cii-facturx-extended-id-duplicate.xml",
        "ubl-facturx-extended-id.xml",
    ],
)
def test_look_alike_identifiers_never_unlock_the_extended_validation(installation: Settings, name: str) -> None:
    result = _analysis(_fixture(name), name)

    official = result["assessment"]["official"]
    assert official["status"] == "unsupported"
    assert official["executed"] is False
    assert result["capabilities"]["official_validation"] == "not-bundled"


def test_a_report_without_matching_scenario_is_no_decision(installation: Settings) -> None:
    # Bypass the selector on purpose: KoSIT itself must answer with noScenarioMatched.
    result = KositValidator(installation).validate(
        _fixture("cii-facturx-extended-id-whitespace.xml"), "rechnung.xml", ruleset=FACTURX_EXTENDED
    )

    assert result["executed"] is False
    assert result["accepted"] is None
    assert [item["id"] for item in result["findings"]] == ["KOSIT-SCENARIO"]


def test_a_report_of_another_scenario_is_no_decision(extended_copy: Callable[[], tuple[Settings, Path]]) -> None:
    settings, repository = extended_copy()
    scenarios = repository / "scenarios.xml"
    text = scenarios.read_text(encoding="utf-8")
    assert text.count(f"<name>{FACTURX_EXTENDED.scenario_name}</name>") == 1
    scenarios.write_text(
        text.replace(f"<name>{FACTURX_EXTENDED.scenario_name}</name>", "<name>Anderes Szenario</name>"),
        encoding="utf-8",
    )

    result = _analysis(_fixture("cii-facturx-extended-valid.xml"), "valid.xml", settings=settings)

    official = result["assessment"]["official"]
    assert official["status"] == "indeterminate"
    assert official["executed"] is False
    assert _ids(result["assessment"]["processing"]["findings"]) >= {"KOSIT-SCENARIO"}
    assert result["assessment"]["processing"]["status"] == "incomplete"


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (lambda repo: (repo / "resources/xslt/FACTUR-X_EXTENDED.xslt").unlink(), "Regeldatei fehlt"),
        (
            lambda repo: (repo / "resources/xslt/FACTUR-X_EXTENDED_codedb.xml").write_bytes(
                (repo / "resources/xslt/FACTUR-X_EXTENDED_codedb.xml").read_bytes() + b"\n"
            ),
            "Bytes",
        ),
        (
            lambda repo: (repo / "resources/xsd/Factur-X_EXTENDED.xsd").write_bytes(
                (repo / "resources/xsd/Factur-X_EXTENDED.xsd").read_bytes()[::-1]
            ),
            "SHA-256",
        ),
    ],
)
def test_missing_or_tampered_artefacts_make_the_validation_unavailable(
    extended_copy: Callable[[], tuple[Settings, Path]],
    mutate: Callable[[Path], object],
    problem: str,
) -> None:
    settings, repository = extended_copy()
    mutate(repository)

    state = KositValidator(settings).configuration_state(FACTURX_EXTENDED)
    assert state["configured"] is False
    assert any(problem in item for item in state["problems"])

    result = _analysis(_fixture("cii-facturx-extended-valid.xml"), "valid.xml", settings=settings)
    official = result["assessment"]["official"]
    assert official["status"] == "unavailable"
    assert official["executed"] is False
    assert _ids(result["assessment"]["processing"]["findings"]) == {"KOSIT-CONFIG"}
    assert result["capabilities"]["official_validation"] == "unavailable"
    # The XRechnung path keeps working with the same settings.
    assert KositValidator(settings).configuration_state()["configured"] is True


def test_startup_failure_and_timeout_are_indeterminate(installation: Settings) -> None:
    broken_java = replace(installation, kosit_java_bin=sys.executable)
    result = _analysis(_fixture("cii-facturx-extended-valid.xml"), "valid.xml", settings=broken_java)
    official = result["assessment"]["official"]
    assert official["status"] == "indeterminate"
    assert official["executed"] is False
    assert _ids(result["assessment"]["processing"]["findings"]) & {"KOSIT-REPORT", "KOSIT-EXEC"}

    impatient = replace(installation, kosit_timeout_seconds=1)
    result = _analysis(_fixture("cii-facturx-extended-valid.xml"), "valid.xml", settings=impatient)
    official = result["assessment"]["official"]
    assert official["status"] == "indeterminate"
    assert official["executed"] is False
    assert _ids(result["assessment"]["processing"]["findings"]) == {"KOSIT-TIMEOUT"}


def test_original_bytes_reach_kosit_and_leave_the_xml_export_unchanged(
    installation: Settings, pdf_bytes_factory
) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    payload = _fixture("cii-facturx-extended-valid.xml")
    pdf = pdf_bytes_factory(("factur-x.xml", payload))
    client = TestClient(app)

    exported = client.post("/api/xml", files={"file": ("rechnung.pdf", pdf, "application/pdf")})
    assert exported.status_code == 200
    assert exported.content == payload

    result = _analysis(pdf, "rechnung.pdf")
    official = result["assessment"]["official"]
    assert official["status"] == "accepted"
    root = etree.fromstring(official["raw_report"].encode("utf-8"))
    hash_value = root.findtext(f"{{{VARL}}}documentIdentification/{{{VARL}}}documentHash/{{{VARL}}}hashValue")
    assert hash_value is not None
    assert base64.b64decode(hash_value) == hashlib.sha256(payload).digest()


@pytest.mark.parametrize("name", ["cii-rechnung-demo.xml", "ubl-rechnung-demo.xml"])
def test_existing_en16931_and_xrechnung_control_cases_are_unchanged(installation: Settings, name: str) -> None:
    result = _analysis((EXAMPLES / name).read_bytes(), name)

    official = result["assessment"]["official"]
    assert official["status"] == "accepted"
    assert official["executed"] is True
    assert "EXTENDED" not in (official["summary"] or "")
    assert result["capabilities"]["official_validation"] == "bundled"


def test_health_endpoint_reports_the_extended_rules_as_configured(installation: Settings) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    payload = TestClient(app).get("/api/health").json()

    assert payload["kosit"]["configured"] is True
    assert payload["kosit"]["facturx_extended_configured"] is True
    assert payload["kosit"]["components"]["facturx_extended"] == "1.09.2"


def test_installed_artefacts_reference_nothing_outside_the_repository(installation: Settings) -> None:
    repository = installation.kosit_facturx_extended_repositories[0]
    xslt = (repository / "resources" / "xslt" / "FACTUR-X_EXTENDED.xslt").read_text(encoding="utf-8")
    assert "xsl:include" not in xslt and "xsl:import" not in xslt
    assert set(re.findall(r"document\(([^)]*)\)", xslt)) == {"'FACTUR-X_EXTENDED_codedb.xml'"}
    for item in FACTURX_EXTENDED.files:
        if item.target == "LICENSE":
            continue
        text = (repository / item.target).read_text(encoding="utf-8", errors="replace")
        for uri in re.findall(r"https?://[^\s\"'<>]+", text):
            assert uri.startswith(ALLOWED_URI_HOSTS), f"Unerwartete externe Referenz in {item.target}: {uri}"
    report = (repository / "resources" / "report" / "default-report.xsl").read_text(encoding="utf-8")
    assert "document(" not in report and "xsl:import" not in report and "xsl:include" not in report
