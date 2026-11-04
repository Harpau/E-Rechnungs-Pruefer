"""Bound XRechnung and EN 16931 validation path of the KoSIT integration.

The KoSIT process is replaced by a fake that writes synthetic VARL reports
built for the exact document hash; the configuration artefacts are tiny
stand-ins whose sizes and hashes are bound into test copies of the rule sets.
"""

from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.official_rulesets import (
    EN16931_CII,
    XRECHNUNG_3_0_CII,
    XRECHNUNG_3_0_PROFILE_ID,
    XRECHNUNG_3_0_UBL_CREDITNOTE,
    XRECHNUNG_3_0_UBL_INVOICE,
    XRECHNUNG_CONFIGURATION_FILES,
    XRECHNUNG_RULESETS,
    OfficialRuleset,
    RulesetFile,
)
from app.settings import Settings
from app.validators import kosit as kosit_module
from app.validators.kosit import KositValidator

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MAIN_CLASS = "de.kosit.validationtool.cmd.CommandLineApplication"
VARL = "http://www.xoev.de/de/validator/varl/1"
SCENARIOS = "http://www.xoev.de/de/validator/framework/1/scenarios"
BASE_FIXTURES = {
    "CrossIndustryInvoice": "xrechnung-3.0-cii-valid.xml",
    "Invoice": "xrechnung-3.0-ubl-invoice-valid.xml",
    "CreditNote": "xrechnung-3.0-ubl-creditnote-valid.xml",
}
Binder = Callable[[OfficialRuleset], OfficialRuleset]


def _write_jar(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", f"Manifest-Version: 1.0\r\nMain-Class: {MAIN_CLASS}\r\n\r\n")


def _locked(path: Path, target: str) -> RulesetFile:
    payload = path.read_bytes()
    return RulesetFile(target, len(payload), hashlib.sha256(payload).hexdigest())


def _install(tmp_path: Path, *, scenario_files: int = 1, repositories: int = 1) -> tuple[Settings, Path, Binder]:
    """A fake installation: JAR, scenario file and stand-in artefacts bound by size and SHA-256."""

    jar = tmp_path / "validator-1.6.3-standalone.jar"
    _write_jar(jar)
    repository = tmp_path / "xrechnung"
    repository.mkdir()
    scenarios = repository / "scenarios.xml"
    scenarios.write_bytes(b"<scenarios>synthetische Konfiguration</scenarios>")
    files: list[RulesetFile] = []
    for item in XRECHNUNG_CONFIGURATION_FILES:
        path = repository / item.target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"<!-- Stand-in fuer {item.target} -->".encode())
        files.append(_locked(path, item.target))
    extra_scenarios = tuple(tmp_path / f"weitere-{index}.xml" for index in range(scenario_files - 1))
    for path in extra_scenarios:
        path.write_bytes(b"<scenarios/>")
    extra_repositories = tuple(tmp_path / f"weiteres-repo-{index}" for index in range(repositories - 1))
    for path in extra_repositories:
        path.mkdir()
    settings = Settings(
        kosit_enabled=True,
        kosit_java_bin=sys.executable,
        kosit_validator_jar=jar,
        kosit_scenarios=(scenarios, *extra_scenarios),
        kosit_repositories=(repository, *extra_repositories),
        kosit_facturx_extended_scenarios=(),
        kosit_facturx_extended_repositories=(),
        kosit_timeout_seconds=5,
    )

    def bind(ruleset: OfficialRuleset) -> OfficialRuleset:
        return replace(ruleset, files=tuple(files), scenario_file=_locked(scenarios, "scenarios.xml"))

    return settings, repository, bind


def _fake_run(
    report: bytes | None,
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    overflow: bool = False,
    observed: dict[str, Any] | None = None,
) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    def fake(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        command = [str(value) for value in args[0]]
        if observed is not None:
            observed["command"] = command
            observed["invoice_bytes"] = Path(command[-1]).read_bytes()
        output_directory = Path(command[command.index("-o") + 1])
        if report is not None:
            (output_directory / f"{Path(command[-1]).stem}-report.xml").write_bytes(report)
        completed = subprocess.CompletedProcess(args=args[0], returncode=returncode, stdout=stdout, stderr=stderr)
        vars(completed)["_kosit_console_overflow"] = overflow
        return completed

    return fake


def _never(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    raise AssertionError("KoSIT darf in diesem Fall nicht gestartet werden")


def _document(ruleset: OfficialRuleset) -> bytes:
    """The synthetic fixture of the rule set's root element with the rule set's exact identifier."""

    payload = (FIXTURES / BASE_FIXTURES[ruleset.root_local_name]).read_bytes()
    assert payload.count(XRECHNUNG_3_0_PROFILE_ID.encode()) == 1
    return payload.replace(XRECHNUNG_3_0_PROFILE_ID.encode(), ruleset.profile_id.encode())


def _report(
    scenario_name: str | None,
    document: bytes,
    *,
    decision: str = "accept",
    messages: str = "",
    hash_value: str | None = None,
    algorithm: str = "SHA-256",
    engine: str = "KoSIT Validator 1.6.3",
    omit_hash: bool = False,
    namespace: str = VARL,
) -> bytes:
    """A synthetic VARL report modelled on the KoSIT Validator 1.6.3 output."""

    digest = hash_value if hash_value is not None else base64.b64encode(hashlib.sha256(document).digest()).decode()
    identification = (
        ""
        if omit_hash
        else (
            f"<rep:documentIdentification><rep:documentHash><rep:hashAlgorithm>{algorithm}</rep:hashAlgorithm>"
            f"<rep:hashValue>{digest}</rep:hashValue></rep:documentHash>"
            "<rep:documentReference>rechnung.xml</rep:documentReference></rep:documentIdentification>"
        )
    )
    if scenario_name is None:
        scenario = "<rep:noScenarioMatched/>"
    else:
        scenario = (
            f"<rep:scenarioMatched><s:scenario><s:name>{scenario_name}</s:name>"
            "<s:description><s:p>Synthetisch</s:p></s:description>"
            "<s:acceptMatch>/rep:report/rep:assessment[1]/rep:accept[1]</s:acceptMatch></s:scenario>"
            '<rep:validationStepResult id="val-xsd" valid="true"/>'
            f'<rep:validationStepResult id="val-sch.1" valid="{"true" if decision == "accept" else "false"}">'
            f"{messages}</rep:validationStepResult></rep:scenarioMatched>"
        )
    explanation = (
        '<rep:explanation><html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<p class="error">Nur Darstellung</p></body></html></rep:explanation>'
    )
    assessment = f"<rep:assessment><rep:{decision}>{explanation}</rep:{decision}></rep:assessment>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<rep:report xmlns:rep="{namespace}" xmlns:s="{SCENARIOS}" varlVersion="1.0.0" '
        f'valid="{"true" if decision == "accept" else "false"}">'
        f"<rep:engine><rep:name>{engine}</rep:name></rep:engine>"
        "<rep:timestamp>2026-11-04T12:00:00.000+01:00</rep:timestamp>"
        f"{identification}{scenario}{assessment}</rep:report>"
    ).encode()


def _message(code: str, level: str, text: str, *, line: int | None = None, column: int | None = None) -> str:
    coordinates = ""
    if line is not None:
        coordinates = f' lineNumber="{line}" columnNumber="{column}"'
    return (
        f'<rep:message id="val-sch.1.{code}" level="{level}" code="{code}" xpathLocation="/*:Invoice[1]"{coordinates}>'
        f"{text}</rep:message>"
    )


def _ids(result: dict[str, Any]) -> list[str]:
    return [item["id"] for item in result["findings"]]


@pytest.mark.parametrize("ruleset", XRECHNUNG_RULESETS, ids=[ruleset.id for ruleset in XRECHNUNG_RULESETS])
def test_every_configuration_scenario_binds_its_accept_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ruleset: OfficialRuleset
) -> None:
    settings, repository, bind = _install(tmp_path)
    bound = bind(ruleset)
    document = _document(ruleset)
    observed: dict[str, Any] = {}
    monkeypatch.setattr(
        kosit_module, "_run_kosit_process", _fake_run(_report(ruleset.scenario_name, document), observed=observed)
    )

    result = KositValidator(settings).validate(document, "Rechnung Ä.xml", ruleset=bound)

    command = observed["command"]
    assert command[command.index("-s") + 1] == str(settings.kosit_scenarios[0])
    assert command[command.index("-r") + 1] == str(repository)
    assert "-p" not in command and "--print" not in command
    assert Path(command[-1]).name == "rechnung.xml"
    assert observed["invoice_bytes"] == document
    assert result["executed"] is True
    assert result["accepted"] is True
    assert result["configured"] is True
    assert result["ruleset"] == ruleset.id
    assert result["rules_version"] == ruleset.rules_version
    assert result["scenario"] == ruleset.scenario_name
    assert result["report_source"] == "file"
    assert result["findings"] == []
    assert f"nach {ruleset.label} akzeptiert" in result["summary"]
    assert f"Prüfszenario: {ruleset.scenario_name}." in result["summary"]
    assert f"Regelstand: {ruleset.rules_version} (KoSIT)" in result["summary"]
    assert "PDF-Konformität" not in result["summary"]


def test_a_report_of_another_scenario_of_the_configuration_is_no_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _repository, bind = _install(tmp_path)
    document = _document(XRECHNUNG_3_0_UBL_CREDITNOTE)
    # The report names the UBL Invoice scenario although a credit note was validated.
    report = _report(XRECHNUNG_3_0_UBL_INVOICE.scenario_name, document)
    monkeypatch.setattr(kosit_module, "_run_kosit_process", _fake_run(report))

    result = KositValidator(settings).validate(document, "rechnung.xml", ruleset=bind(XRECHNUNG_3_0_UBL_CREDITNOTE))

    assert result["executed"] is False
    assert result["accepted"] is None
    assert result["scenario"] is None
    assert result["raw_report"] is None
    assert _ids(result) == ["KOSIT-SCENARIO"]
    message = result["findings"][0]["message"]
    assert "'EN16931 XRechnung (UBL Invoice)'" in message
    assert "'EN16931 XRechnung (UBL CreditNote)'" in message
    assert "kein passendes Prüfszenario" in result["summary"]


@pytest.mark.parametrize(
    ("build", "expected_id", "expected_text"),
    [
        (lambda doc: _report(None, doc, decision="reject"), "KOSIT-SCENARIO", "kein Prüfszenario"),
        (lambda doc: _report("EN16931 (CII)", doc), "KOSIT-SCENARIO", "EN16931 XRechnung (CII)"),
        (lambda doc: _report(XRECHNUNG_3_0_CII.scenario_name, doc[::-1]), "KOSIT-REPORT", "gehört nicht"),
        (
            lambda doc: _report(XRECHNUNG_3_0_CII.scenario_name, doc, omit_hash=True),
            "KOSIT-REPORT",
            "keinen Dokument-Hash",
        ),
        (
            lambda doc: _report(XRECHNUNG_3_0_CII.scenario_name, doc, algorithm="SHA-1"),
            "KOSIT-REPORT",
            "Hash-Algorithmus",
        ),
        (lambda doc: _report(XRECHNUNG_3_0_CII.scenario_name, doc, hash_value="***"), "KOSIT-REPORT", "Base64"),
        (lambda doc: _report(XRECHNUNG_3_0_CII.scenario_name, doc, engine=" "), "KOSIT-REPORT", "Prüf-Engine"),
        (
            lambda doc: _report(XRECHNUNG_3_0_CII.scenario_name, doc).replace(
                b"<rep:assessment>", b"<rep:assessment><rep:reject/></rep:assessment><rep:assessment>"
            ),
            "KOSIT-REPORT",
            "nicht genau eine Bewertung",
        ),
        (
            lambda doc: _report(XRECHNUNG_3_0_CII.scenario_name, doc).replace(
                b"<rep:assessment><rep:accept>", b"<rep:assessment><rep:reject/><rep:accept>"
            ),
            "KOSIT-REPORT",
            "weder eine eindeutige Annahme",
        ),
        (
            lambda doc: (
                _report(XRECHNUNG_3_0_CII.scenario_name, doc)
                .replace(b"<rep:assessment>", b"<rep:note>")
                .replace(b"</rep:assessment>", b"</rep:note>")
            ),
            "KOSIT-REPORT",
            "nicht genau eine Bewertung",
        ),
        (
            lambda doc: _report(XRECHNUNG_3_0_CII.scenario_name, doc, namespace="http://example.test/report"),
            "KOSIT-REPORT",
            "VARL-Namespace",
        ),
        (lambda doc: b"<rep:report", "KOSIT-REPORT", "wohlgeformtes"),
        (lambda doc: None, "KOSIT-REPORT", "keinen Prüfbericht"),
    ],
    ids=[
        "no-scenario",
        "wrong-scenario",
        "wrong-hash",
        "missing-hash",
        "unknown-algorithm",
        "invalid-base64",
        "missing-engine",
        "double-assessment",
        "contradictory-assessment",
        "missing-assessment",
        "foreign-namespace",
        "malformed-xml",
        "missing-report-file",
    ],
)
def test_binding_failures_are_indeterminate_whatever_the_return_code_says(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build: Callable[[bytes], bytes | None],
    expected_id: str,
    expected_text: str,
) -> None:
    settings, _repository, bind = _install(tmp_path)
    document = _document(XRECHNUNG_3_0_CII)
    # Return code 0 claims success; it must never turn a broken binding into an acceptance.
    monkeypatch.setattr(kosit_module, "_run_kosit_process", _fake_run(build(document), returncode=0))

    result = KositValidator(settings).validate(document, "rechnung.xml", ruleset=bind(XRECHNUNG_3_0_CII))

    assert result["executed"] is False
    assert result["accepted"] is None
    assert result["raw_report"] is None
    assert result["scenario"] is None
    assert result["exit_code"] == 0
    assert _ids(result) == [expected_id]
    assert result["findings"][0]["severity"] == "warning"
    assert expected_text in result["findings"][0]["message"]


@pytest.mark.parametrize(
    ("decision", "returncode", "accepted"),
    [("accept", 1, True), ("reject", 0, False)],
)
def test_the_bound_assessment_beats_a_contradicting_return_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str, returncode: int, accepted: bool
) -> None:
    settings, _repository, bind = _install(tmp_path)
    document = _document(EN16931_CII)
    report = _report(EN16931_CII.scenario_name, document, decision=decision)
    monkeypatch.setattr(kosit_module, "_run_kosit_process", _fake_run(report, returncode=returncode))

    result = KositValidator(settings).validate(document, "rechnung.xml", ruleset=bind(EN16931_CII))

    assert result["executed"] is True
    assert result["accepted"] is accepted
    assert "KOSIT-RESULT-MISMATCH" in _ids(result)
    mismatch = next(item for item in result["findings"] if item["id"] == "KOSIT-RESULT-MISMATCH")
    assert mismatch["severity"] == "warning"
    assert mismatch["actual"] == str(returncode)
    if not accepted:
        assert "KOSIT-REJECT" in _ids(result)


def test_console_reports_are_ignored_even_with_a_successful_return_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _repository, bind = _install(tmp_path)
    document = _document(XRECHNUNG_3_0_UBL_INVOICE)
    console_report = _report(XRECHNUNG_3_0_UBL_INVOICE.scenario_name, document)
    wrapped = b"[Format error!] <" + console_report + b"> with params <[Ljava.lang.Object;@123456>"
    monkeypatch.setattr(
        kosit_module, "_run_kosit_process", _fake_run(None, returncode=0, stdout=console_report, stderr=wrapped)
    )

    result = KositValidator(settings).validate(document, "rechnung.xml", ruleset=bind(XRECHNUNG_3_0_UBL_INVOICE))

    assert result["executed"] is False
    assert result["accepted"] is None
    assert result["raw_report"] is None
    assert result["report_source"] is None
    assert _ids(result) == ["KOSIT-REPORT"]
    assert "keinen Prüfbericht" in result["findings"][0]["message"]
    assert "[Format error!]" in (result["technical_output"] or "")


def test_schematron_rejection_uses_the_code_attribute_and_keeps_coordinates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _repository, bind = _install(tmp_path)
    document = _document(XRECHNUNG_3_0_UBL_INVOICE)
    messages = _message("BR-DE-6", "error", "[BR-DE-6] BT-42 muss übermittelt werden.", line=20, column=5) + _message(
        "BR-DE-27", "warning", "[BR-DE-27] In BT-42 sollen mindestens drei Ziffern enthalten sein."
    )
    report = _report(XRECHNUNG_3_0_UBL_INVOICE.scenario_name, document, decision="reject", messages=messages)
    monkeypatch.setattr(kosit_module, "_run_kosit_process", _fake_run(report, returncode=1))

    result = KositValidator(settings).validate(document, "rechnung.xml", ruleset=bind(XRECHNUNG_3_0_UBL_INVOICE))

    assert result["executed"] is True
    assert result["accepted"] is False
    assert result["scenario"] == XRECHNUNG_3_0_UBL_INVOICE.scenario_name
    assert _ids(result) == ["BR-DE-6", "BR-DE-27"]
    error, warning = result["findings"]
    assert error["severity"] == "error" and error["line"] == 20 and error["column"] == 5
    assert error["location"] == "/*:Invoice[1]"
    assert error["profile"] == XRECHNUNG_3_0_PROFILE_ID
    assert error["version"] == XRECHNUNG_3_0_UBL_INVOICE.rules_version
    assert warning["severity"] == "warning" and warning["line"] is None
    assert "Nur Darstellung" not in [item["message"] for item in result["findings"]]
    assert "abgelehnt" in result["summary"]


def test_rejection_without_error_messages_gets_a_reject_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _repository, bind = _install(tmp_path)
    document = _document(XRECHNUNG_3_0_CII)
    report = _report(
        XRECHNUNG_3_0_CII.scenario_name,
        document,
        decision="reject",
        messages=_message("BR-DE-TMP-32", "information", "Hinweis ohne Fehler."),
    )
    monkeypatch.setattr(kosit_module, "_run_kosit_process", _fake_run(report, returncode=1))

    result = KositValidator(settings).validate(document, "rechnung.xml", ruleset=bind(XRECHNUNG_3_0_CII))

    assert result["accepted"] is False
    assert _ids(result) == ["BR-DE-TMP-32", "KOSIT-REJECT"]
    assert result["findings"][0]["severity"] == "info"
    assert result["findings"][1]["severity"] == "error"


def test_console_overflow_is_a_processing_warning_beside_the_bound_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _repository, bind = _install(tmp_path)
    document = _document(XRECHNUNG_3_0_CII)
    monkeypatch.setattr(
        kosit_module,
        "_run_kosit_process",
        _fake_run(
            _report(XRECHNUNG_3_0_CII.scenario_name, document),
            stdout=b"x" * kosit_module.MAXIMUM_KOSIT_CONSOLE_BYTES,
            overflow=True,
        ),
    )

    result = KositValidator(settings).validate(document, "rechnung.xml", ruleset=bind(XRECHNUNG_3_0_CII))

    assert result["executed"] is True
    assert result["accepted"] is True
    assert _ids(result) == ["KOSIT-OUTPUT-TRUNCATED"]
    assert result["findings"][0]["source"] == "KoSIT-Anbindung"
    assert (result["technical_output"] or "").startswith("Die KoSIT-Konsolenausgabe überschritt")


def test_startup_failure_without_report_is_a_technical_finding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, _repository, bind = _install(tmp_path)
    document = _document(XRECHNUNG_3_0_CII)
    monkeypatch.setattr(
        kosit_module,
        "_run_kosit_process",
        _fake_run(None, returncode=1, stderr=b"kein Hauptmanifestattribut, in validator-1.6.3.jar"),
    )

    result = KositValidator(settings).validate(document, "rechnung.xml", ruleset=bind(XRECHNUNG_3_0_CII))

    assert result["executed"] is False
    assert result["accepted"] is None
    assert result["exit_code"] == 1
    assert _ids(result) == ["KOSIT-EXEC"]
    assert "Hauptmanifestattribut" in result["findings"][0]["message"]
    assert "Start- oder JAR-Konfigurationsstörung" in result["summary"]
    assert "abgelehnt" not in result["summary"]


def test_oversized_report_file_is_rejected_before_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, _repository, bind = _install(tmp_path)
    document = _document(XRECHNUNG_3_0_CII)
    monkeypatch.setattr(
        kosit_module, "_run_kosit_process", _fake_run(b"x" * (kosit_module.MAXIMUM_KOSIT_REPORT_BYTES + 1))
    )

    result = KositValidator(settings).validate(document, "rechnung.xml", ruleset=bind(XRECHNUNG_3_0_CII))

    assert result["executed"] is False
    assert _ids(result) == ["KOSIT-REPORT"]
    assert "Bytebudget" in result["findings"][0]["message"]


def test_validate_without_ruleset_never_starts_kosit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, _repository, _bind = _install(tmp_path)
    monkeypatch.setattr(kosit_module, "_run_kosit_process", _never)

    result = KositValidator(settings).validate(_document(XRECHNUNG_3_0_CII), "rechnung.xml")

    assert result["executed"] is False
    assert result["accepted"] is None
    assert result["scenario"] is None
    assert _ids(result) == ["KOSIT-SCENARIO"]
    assert "kein gebundenes Prüfszenario" in result["summary"]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda repo: (repo / "scenarios.xml").write_bytes(b"<scenarios>SYNTHETISCHE Konfiguration</scenarios>"),
            "SHA-256",
        ),
        (
            lambda repo: (repo / "scenarios.xml").write_bytes((repo / "scenarios.xml").read_bytes() + b"\n"),
            "Szenariodatei scenarios.xml hat",
        ),
        (
            lambda repo: (repo / "resources/xrechnung/3.0.2/xsl/XRechnung-CII-validation.xsl").unlink(),
            "Regeldatei fehlt",
        ),
        (
            lambda repo: (repo / "resources/ubl/2.1/xsd/common/UBL-CommonBasicComponents-2.1.xsd").write_bytes(
                b"<!-- Stand-in fuer resources/ubl/2.1/xsd/common/UBL-CommonBasicComponents-2.1.xsX -->"
            ),
            "SHA-256",
        ),
        (
            lambda repo: (repo / "resources/cii/16b/xsl/EN16931-CII-validation.xsl").write_bytes(b"<xsl:stylesheet/>"),
            "Bytes",
        ),
        (lambda repo: (repo / "resources/default-report.xsl").unlink(), "Regeldatei fehlt"),
    ],
    ids=["scenario-content", "scenario-size", "missing-stylesheet", "tampered-xsd", "resized-xsl", "missing-report"],
)
def test_precheck_fails_closed_on_tampered_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[Path], object],
    expected: str,
) -> None:
    settings, repository, bind = _install(tmp_path)
    bound = bind(XRECHNUNG_3_0_UBL_INVOICE)
    mutate(repository)
    monkeypatch.setattr(kosit_module, "_run_kosit_process", _never)

    state = KositValidator(settings).configuration_state(bound)
    result = KositValidator(settings).validate(_document(XRECHNUNG_3_0_UBL_INVOICE), "rechnung.xml", ruleset=bound)

    assert state["configured"] is False
    assert any(expected in problem for problem in state["problems"]), state["problems"]
    assert result["executed"] is False
    assert result["configured"] is False
    assert _ids(result) == ["KOSIT-CONFIG"]
    # The basic installation check without a rule set stays satisfied: only the binding fails.
    assert KositValidator(settings).configuration_state()["configured"] is True


@pytest.mark.parametrize(
    ("scenario_files", "repositories", "expected"),
    [(2, 1, "genau eine Szenariodatei"), (1, 2, "genau ein Ressourcenverzeichnis")],
)
def test_bound_runs_require_exactly_one_scenario_file_and_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario_files: int, repositories: int, expected: str
) -> None:
    settings, _repository, bind = _install(tmp_path, scenario_files=scenario_files, repositories=repositories)
    monkeypatch.setattr(kosit_module, "_run_kosit_process", _never)

    state = KositValidator(settings).configuration_state(bind(EN16931_CII))

    assert state["configured"] is False
    assert any(expected in problem for problem in state["problems"]), state["problems"]
    assert KositValidator(settings).configuration_state()["configured"] is True


def test_every_xrechnung_ruleset_uses_the_shared_configuration_paths(tmp_path: Path) -> None:
    settings, repository, bind = _install(tmp_path)

    for ruleset in XRECHNUNG_RULESETS:
        state = KositValidator(settings).configuration_state(bind(ruleset))
        assert state["configured"] is True, state["problems"]
        assert state["scenarios"] == [str(settings.kosit_scenarios[0])]
        assert state["repositories"] == [str(repository)]
        assert state["ruleset"] == ruleset.id
        assert state["rules_version"] == ruleset.rules_version
