"""Bound Factur-X EXTENDED validation path of the KoSIT integration.

The KoSIT process is replaced by a fake that writes synthetic VARL reports from
``tests/fixtures``; the rule-set artefacts are tiny stand-ins whose sizes and
hashes are bound into a test copy of the rule set.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.official_rulesets import FACTURX_EXTENDED, OfficialRuleset, RulesetFile
from app.settings import Settings
from app.validators import kosit as kosit_module
from app.validators.kosit import KositValidator

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MAIN_CLASS = "de.kosit.validationtool.cmd.CommandLineApplication"
ARTEFACTS = {
    "resources/xsd/Factur-X_EXTENDED.xsd": b"<xs:schema/>",
    "resources/xslt/FACTUR-X_EXTENDED.xslt": b"<xsl:stylesheet/>",
    "resources/xslt/FACTUR-X_EXTENDED_codedb.xml": b"<codedb/>",
    "LICENSE": b"Apache License\n",
}


def _write_jar(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", f"Manifest-Version: 1.0\r\nMain-Class: {MAIN_CLASS}\r\n\r\n")


def _ruleset_for(repository: Path) -> OfficialRuleset:
    files = []
    for target, payload in ARTEFACTS.items():
        path = repository / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        files.append(RulesetFile(target, len(payload), hashlib.sha256(payload).hexdigest()))
    (repository / "scenarios.xml").write_text("<scenarios/>", encoding="utf-8")
    report = repository / "resources" / "report" / "default-report.xsl"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("<xsl:stylesheet/>", encoding="utf-8")
    return replace(FACTURX_EXTENDED, files=tuple(files))


def _settings(tmp_path: Path, *, extended: bool = True) -> tuple[Settings, Path]:
    jar = tmp_path / "validator-1.6.3-standalone.jar"
    _write_jar(jar)
    legacy_scenarios = tmp_path / "xrechnung" / "scenarios.xml"
    legacy_scenarios.parent.mkdir(parents=True, exist_ok=True)
    legacy_scenarios.write_text("<scenarios/>", encoding="utf-8")
    repository = tmp_path / "facturx-extended"
    repository.mkdir(exist_ok=True)
    return (
        Settings(
            kosit_enabled=True,
            kosit_java_bin=sys.executable,
            kosit_validator_jar=jar,
            kosit_scenarios=(legacy_scenarios,),
            kosit_repositories=(legacy_scenarios.parent,),
            kosit_facturx_extended_scenarios=(repository / "scenarios.xml",) if extended else (),
            kosit_facturx_extended_repositories=(repository,) if extended else (),
            kosit_timeout_seconds=5,
        ),
        repository,
    )


def _fake_run(
    report: bytes | None,
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    observed: dict[str, Any] | None = None,
) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    def fake(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        command = [str(value) for value in args[0]]
        if observed is not None:
            observed["command"] = command
        output_directory = Path(command[command.index("-o") + 1])
        invoice_path = Path(command[-1])
        if observed is not None:
            observed["invoice_bytes"] = invoice_path.read_bytes()
        if report is not None:
            (output_directory / f"{invoice_path.stem}-report.xml").write_bytes(report)
        return subprocess.CompletedProcess(args=args[0], returncode=returncode, stdout=stdout, stderr=b"")

    return fake


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_bound_run_uses_the_ruleset_paths_and_a_fixed_file_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, repository = _settings(tmp_path)
    ruleset = _ruleset_for(repository)
    observed: dict[str, Any] = {}
    monkeypatch.setattr(
        kosit_module, "_run_kosit_process", _fake_run(_fixture("kosit-report-extended-accept.xml"), observed=observed)
    )
    invoice = _fixture("cii-facturx-extended-valid.xml")

    result = KositValidator(settings).validate(invoice, "Rechnung_Zähler.xml", ruleset=ruleset)

    command = observed["command"]
    assert command[command.index("-s") + 1] == str(repository / "scenarios.xml")
    assert command[command.index("-r") + 1] == str(repository)
    assert str(settings.kosit_scenarios[0]) not in command
    assert Path(command[-1]).name == "rechnung.xml"
    assert observed["invoice_bytes"] == invoice
    assert result["executed"] is True
    assert result["accepted"] is True
    assert result["configured"] is True
    assert result["ruleset"] == "facturx-extended-1.09.2"
    assert result["scenario"] == "Factur-X 1.09.2 EXTENDED (CII)"
    assert result["report_source"] == "file"
    assert result["findings"] == []
    assert "Factur-X 1.09.2 EXTENDED" in result["summary"]
    assert ruleset.rules_version in result["summary"]
    assert "nicht die PDF-Konformität" in result["summary"]


@pytest.mark.parametrize(
    ("report", "invoice", "returncode", "executed", "accepted", "finding_ids", "not_executed_id"),
    [
        ("kosit-report-extended-accept.xml", "cii-facturx-extended-valid.xml", 0, True, True, set(), None),
        (
            "kosit-report-extended-accept-warning.xml",
            "cii-facturx-extended-valid.xml",
            0,
            True,
            True,
            {"BR-FXEXT-E-08rev"},
            None,
        ),
        (
            "kosit-report-extended-reject.xml",
            "cii-facturx-extended-schematron-error.xml",
            1,
            True,
            False,
            {"BR-FXEXT-CO-15", "BR-FXEXT-CO-16"},
            None,
        ),
        (
            "kosit-report-extended-reject-xsd.xml",
            "cii-facturx-extended-xsd-error.xml",
            1,
            True,
            False,
            {"cvc-complex-type.2.4.a"},
            None,
        ),
        (
            "kosit-report-fixfr04-rule.xml",
            "cii-facturx-extended-valid.xml",
            1,
            True,
            False,
            {"BR-FXEXT-CO-10", "KOSIT-RULE-NOTICE"},
            None,
        ),
        (
            "kosit-report-noscenario-reject.xml",
            "cii-facturx-extended-id-whitespace.xml",
            1,
            False,
            None,
            set(),
            "KOSIT-SCENARIO",
        ),
        ("kosit-report-wrong-scenario.xml", "cii-facturx-extended-valid.xml", 0, False, None, set(), "KOSIT-SCENARIO"),
        ("kosit-report-wrong-hash.xml", "cii-facturx-extended-valid.xml", 0, False, None, set(), "KOSIT-REPORT"),
        ("kosit-report-missing-hash.xml", "cii-facturx-extended-valid.xml", 0, False, None, set(), "KOSIT-REPORT"),
        ("kosit-report-unknown-algorithm.xml", "cii-facturx-extended-valid.xml", 0, False, None, set(), "KOSIT-REPORT"),
        ("kosit-report-invalid-base64.xml", "cii-facturx-extended-valid.xml", 0, False, None, set(), "KOSIT-REPORT"),
        (
            "kosit-report-missing-assessment.xml",
            "cii-facturx-extended-valid.xml",
            0,
            False,
            None,
            set(),
            "KOSIT-REPORT",
        ),
        ("kosit-report-double-assessment.xml", "cii-facturx-extended-valid.xml", 0, False, None, set(), "KOSIT-REPORT"),
        (
            "kosit-report-contradictory-assessment.xml",
            "cii-facturx-extended-valid.xml",
            0,
            False,
            None,
            set(),
            "KOSIT-REPORT",
        ),
        ("kosit-report-wrong-namespace.xml", "cii-facturx-extended-valid.xml", 0, False, None, set(), "KOSIT-REPORT"),
    ],
)
def test_bound_run_accepts_only_reports_bound_to_scenario_and_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report: str,
    invoice: str,
    returncode: int,
    executed: bool,
    accepted: bool | None,
    finding_ids: set[str],
    not_executed_id: str | None,
) -> None:
    settings, repository = _settings(tmp_path)
    ruleset = _ruleset_for(repository)
    monkeypatch.setattr(kosit_module, "_run_kosit_process", _fake_run(_fixture(report), returncode=returncode))

    result = KositValidator(settings).validate(_fixture(invoice), "rechnung.xml", ruleset=ruleset)

    assert result["executed"] is executed
    assert result["accepted"] is accepted
    ids = {item["id"] for item in result["findings"]}
    if executed:
        assert ids == finding_ids
        for item in result["findings"]:
            assert item["version"] == ruleset.rules_version
            assert item["profile"] == ruleset.profile_id
            assert item["severity"] in {"error", "warning", "info"}
        assert result["raw_report"] is not None
        if "KOSIT-RULE-NOTICE" in ids:
            notice = next(item for item in result["findings"] if item["id"] == "KOSIT-RULE-NOTICE")
            assert notice["severity"] == "info"
            assert notice["source"] == "KoSIT-Anbindung"
            assert "fix-FR04" in notice["message"] and "BR-FXEXT-CO-10" in notice["actual"]
        else:
            assert all(item["id"] not in ruleset.known_deviation_rules for item in result["findings"])
    else:
        assert ids == {not_executed_id}
        assert result["raw_report"] is None
        assert result["findings"][0]["severity"] == "warning"


def test_bound_run_keeps_line_and_column_of_schema_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, repository = _settings(tmp_path)
    ruleset = _ruleset_for(repository)
    monkeypatch.setattr(
        kosit_module, "_run_kosit_process", _fake_run(_fixture("kosit-report-extended-reject-xsd.xml"), returncode=1)
    )

    result = KositValidator(settings).validate(
        _fixture("cii-facturx-extended-xsd-error.xml"), "rechnung.xml", ruleset=ruleset
    )

    (finding,) = result["findings"]
    assert finding["id"] == "cvc-complex-type.2.4.a"
    assert finding["line"] == 13 and finding["column"] == 41
    assert finding["severity"] == "error"


def test_bound_run_requires_the_report_file_and_ignores_console_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, repository = _settings(tmp_path)
    ruleset = _ruleset_for(repository)
    console_report = _fixture("kosit-report-extended-accept.xml")
    monkeypatch.setattr(kosit_module, "_run_kosit_process", _fake_run(None, stdout=console_report))

    result = KositValidator(settings).validate(
        _fixture("cii-facturx-extended-valid.xml"), "rechnung.xml", ruleset=ruleset
    )

    assert result["executed"] is False
    assert result["accepted"] is None
    assert [item["id"] for item in result["findings"]] == ["KOSIT-REPORT"]
    assert "keinen Prüfbericht" in result["findings"][0]["message"]


def test_bound_run_reports_exit_code_mismatch_as_warning_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, repository = _settings(tmp_path)
    ruleset = _ruleset_for(repository)
    monkeypatch.setattr(
        kosit_module, "_run_kosit_process", _fake_run(_fixture("kosit-report-extended-accept.xml"), returncode=3)
    )

    result = KositValidator(settings).validate(
        _fixture("cii-facturx-extended-valid.xml"), "rechnung.xml", ruleset=ruleset
    )

    assert result["accepted"] is True
    assert [item["id"] for item in result["findings"]] == ["KOSIT-RESULT-MISMATCH"]
    assert result["findings"][0]["severity"] == "warning"


def test_bound_run_rejection_without_error_messages_gets_a_reject_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, repository = _settings(tmp_path)
    ruleset = _ruleset_for(repository)
    report = (
        _fixture("kosit-report-extended-accept.xml")
        .replace(b"<rep:accept>", b"<rep:reject>")
        .replace(b"</rep:accept>", b"</rep:reject>")
    )
    monkeypatch.setattr(kosit_module, "_run_kosit_process", _fake_run(report, returncode=1))

    result = KositValidator(settings).validate(
        _fixture("cii-facturx-extended-valid.xml"), "rechnung.xml", ruleset=ruleset
    )

    assert result["accepted"] is False
    assert [item["id"] for item in result["findings"]] == ["KOSIT-REJECT"]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda repo: (repo / "resources/xslt/FACTUR-X_EXTENDED.xslt").unlink(), "Regeldatei fehlt"),
        (lambda repo: (repo / "resources/xslt/FACTUR-X_EXTENDED.xslt").write_bytes(b"<xsl:xtylesheet/>"), "SHA-256"),
        (
            lambda repo: (repo / "resources/xslt/FACTUR-X_EXTENDED.xslt").write_bytes(
                b"<xsl:stylesheet></xsl:stylesheet>"
            ),
            "Bytes",
        ),
        (lambda repo: (repo / "resources/report/default-report.xsl").unlink(), "Report-Stylesheet"),
        (lambda repo: (repo / "scenarios.xml").unlink(), "Szenariokonfiguration"),
    ],
)
def test_precheck_fails_closed_on_missing_or_tampered_artefacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[Path], object],
    expected: str,
) -> None:
    settings, repository = _settings(tmp_path)
    ruleset = _ruleset_for(repository)
    mutate(repository)

    def never(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("KoSIT darf bei fehlgeschlagener Vorprüfung nicht gestartet werden")

    monkeypatch.setattr(kosit_module, "_run_kosit_process", never)
    state = KositValidator(settings).configuration_state(ruleset)
    result = KositValidator(settings).validate(
        _fixture("cii-facturx-extended-valid.xml"), "rechnung.xml", ruleset=ruleset
    )

    assert state["configured"] is False
    assert any(expected in problem for problem in state["problems"])
    assert result["executed"] is False
    assert result["configured"] is False
    assert [item["id"] for item in result["findings"]] == ["KOSIT-CONFIG"]
    # The legacy XRechnung path stays configured and unaffected.
    assert KositValidator(settings).configuration_state()["configured"] is True


def test_missing_extended_configuration_does_not_affect_the_legacy_path(tmp_path: Path) -> None:
    settings, repository = _settings(tmp_path, extended=False)
    ruleset = _ruleset_for(repository)

    state = KositValidator(settings).configuration_state(ruleset)

    assert state["configured"] is False
    assert any("nicht eingerichtet" in problem for problem in state["problems"])
    assert state["ruleset"] == "facturx-extended-1.09.2"
    assert state["rules_version"] == ruleset.rules_version
    legacy = KositValidator(settings).configuration_state()
    assert legacy["configured"] is True
    assert legacy["ruleset"] is None


def test_legacy_path_treats_no_scenario_as_no_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, _repository = _settings(tmp_path, extended=False)
    monkeypatch.setattr(
        kosit_module, "_run_kosit_process", _fake_run(_fixture("kosit-report-noscenario-reject.xml"), returncode=1)
    )

    result = KositValidator(settings).validate(_fixture("cii-facturx-extended-id-whitespace.xml"), "rechnung.xml")

    assert result["executed"] is False
    assert result["accepted"] is None
    assert [item["id"] for item in result["findings"]] == ["KOSIT-SCENARIO"]
    assert "kein passendes Prüfszenario" in result["summary"]
