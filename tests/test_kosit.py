from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

from app.settings import Settings
from app.validators import kosit as kosit_module
from app.validators.kosit import KositValidator


def _write_jar(path: Path, main_class: str | None) -> None:
    manifest = "Manifest-Version: 1.0\r\n"
    if main_class:
        manifest += f"Main-Class: {main_class}\r\n"
    manifest += "\r\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", manifest)
        archive.writestr("placeholder.txt", "test")


def _settings(tmp_path: Path, jar: Path) -> Settings:
    scenarios = tmp_path / "scenarios.xml"
    scenarios.write_text("<scenarios/>", encoding="utf-8")
    repository = tmp_path / "repository"
    repository.mkdir()
    return Settings(
        kosit_enabled=True,
        kosit_java_bin=sys.executable,
        kosit_validator_jar=jar,
        kosit_scenarios=(scenarios,),
        kosit_repositories=(repository,),
        kosit_timeout_seconds=5,
    )


def test_library_jar_without_main_class_is_not_configured(tmp_path):
    jar = tmp_path / "validator-1.6.2.jar"
    _write_jar(jar, None)
    state = KositValidator(_settings(tmp_path, jar)).configuration_state()
    assert state["configured"] is False
    assert any("Main-Class" in problem for problem in state["problems"])


def test_manifest_start_error_is_not_reported_as_invoice_rejection(tmp_path, monkeypatch):
    jar = tmp_path / "validator-1.6.2-standalone.jar"
    _write_jar(jar, "de.kosit.validationtool.cmd.CommandLineApplication")
    validator = KositValidator(_settings(tmp_path, jar))

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout=b"",
            stderr=b"kein Hauptmanifestattribut, in validator-1.6.2.jar",
        )

    monkeypatch.setattr("app.validators.kosit.subprocess.run", fake_run)
    result = validator.validate(b"<invoice/>", "invoice.xml")

    assert result["executed"] is False
    assert result["accepted"] is None
    assert result["exit_code"] == 1
    assert result["findings"][0]["id"] == "KOSIT-EXEC"
    assert result["findings"][0]["severity"] == "warning"
    assert "abgelehnt" not in result["findings"][0]["title"].lower()


def test_real_kosit_reject_report_is_reported_as_rejection(tmp_path, monkeypatch):
    jar = tmp_path / "validator-1.6.2-standalone.jar"
    _write_jar(jar, "de.kosit.validationtool.cmd.CommandLineApplication")
    validator = KositValidator(_settings(tmp_path, jar))
    report = b"""<?xml version="1.0" encoding="UTF-8"?>
<rep:report xmlns:rep="http://www.xoev.de/de/validator/varl/1" valid="false">
  <rep:assessment><rep:reject/></rep:assessment>
  <svrl:failed-assert xmlns:svrl="http://purl.oclc.org/dsdl/svrl" id="BR-TEST" flag="fatal" location="/Invoice">
    <svrl:text>Testfehler</svrl:text>
  </svrl:failed-assert>
</rep:report>"""

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=1, stdout=report, stderr=b"")

    monkeypatch.setattr("app.validators.kosit.subprocess.run", fake_run)
    result = validator.validate(b"<invoice/>", "invoice.xml")

    assert result["executed"] is True
    assert result["accepted"] is False
    assert any(item["id"] == "BR-TEST" for item in result["findings"])


def test_kosit_accept_report_is_reported_as_accepted(tmp_path, monkeypatch):
    jar = tmp_path / "validator-1.6.2-standalone.jar"
    _write_jar(jar, "de.kosit.validationtool.cmd.CommandLineApplication")
    validator = KositValidator(_settings(tmp_path, jar))
    report = b"""<?xml version="1.0" encoding="UTF-8"?>
<rep:report xmlns:rep="http://www.xoev.de/de/validator/varl/1" valid="true">
  <rep:assessment><rep:accept/></rep:assessment>
</rep:report>"""

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout=report, stderr=b"")

    monkeypatch.setattr("app.validators.kosit.subprocess.run", fake_run)
    result = validator.validate(b"<invoice/>", "invoice.xml")

    assert result["executed"] is True
    assert result["accepted"] is True


def test_windows_kosit_process_is_started_without_console(tmp_path, monkeypatch):
    jar = tmp_path / "validator-1.6.2-standalone.jar"
    _write_jar(jar, "de.kosit.validationtool.cmd.CommandLineApplication")
    validator = KositValidator(_settings(tmp_path, jar))
    observed: dict[str, int] = {}

    def fake_run(*args, **kwargs):
        observed["creationflags"] = kwargs["creationflags"]
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(kosit_module.subprocess, "run", fake_run)

    validator.validate(b"<invoice/>", "invoice.xml")

    expected = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if sys.platform == "win32" else 0
    if sys.platform == "win32":
        assert expected != 0
    assert kosit_module.WINDOWS_SUBPROCESS_CREATION_FLAGS == expected
    assert observed["creationflags"] == expected


def test_installer_selects_only_standalone_jar(tmp_path):
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "install_kosit.py"
    spec = importlib.util.spec_from_file_location("kosit_installer_test", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    release = {
        "tag_name": "v1.6.2",
        "assets": [
            {"name": "validator-1.6.2.jar", "size": 1},
            {"name": "validator-1.6.2.zip", "size": 100},
            {"name": "validator-1.6.2-standalone.jar", "size": 10},
        ],
    }
    assert module.choose_validator_asset(release)["name"] == "validator-1.6.2-standalone.jar"

    normal = tmp_path / "validator-1.6.2.jar"
    standalone = tmp_path / "validator-1.6.2-standalone.jar"
    _write_jar(normal, None)
    _write_jar(standalone, "de.kosit.validationtool.cmd.CommandLineApplication")
    assert module.find_validator_jar(tmp_path) == standalone


def test_serialized_report_file_avoids_print_format_error(tmp_path, monkeypatch):
    jar = tmp_path / "validator-1.6.2-standalone.jar"
    _write_jar(jar, "de.kosit.validationtool.cmd.CommandLineApplication")
    validator = KositValidator(_settings(tmp_path, jar))
    report = b"""<?xml version="1.0" encoding="UTF-8"?>
<rep:report xmlns:rep="http://www.xoev.de/de/validator/varl/1" valid="true">
  <rep:engine><rep:name>KoSIT Validator 1.6.2</rep:name></rep:engine>
  <rep:scenarioMatched/>
  <rep:assessment><rep:accept/></rep:assessment>
</rep:report>"""
    observed: dict[str, list[str]] = {}

    def fake_run(*args, **kwargs):
        command = [str(value) for value in args[0]]
        observed["command"] = command
        output_directory = Path(command[command.index("-o") + 1])
        invoice_path = Path(command[-1])
        (output_directory / f"{invoice_path.stem}-report.xml").write_bytes(report)
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=b"KoSIT Validator 1.6.2\nProcessing completed",
            stderr=b"",
        )

    monkeypatch.setattr("app.validators.kosit.subprocess.run", fake_run)
    result = validator.validate(b"<invoice/>", "invoice.xml")

    assert "-p" not in observed["command"]
    assert "--print" not in observed["command"]
    assert result["executed"] is True
    assert result["accepted"] is True
    assert result["report_source"] == "file"
    assert 'valid="true"' in result["raw_report"]


def test_format_error_wrapped_report_on_stderr_is_recovered(tmp_path, monkeypatch):
    jar = tmp_path / "validator-1.6.2-standalone.jar"
    _write_jar(jar, "de.kosit.validationtool.cmd.CommandLineApplication")
    validator = KositValidator(_settings(tmp_path, jar))
    report = b"""<?xml version="1.0" encoding="UTF-8"?>
<rep:report xmlns:rep="http://www.xoev.de/de/validator/varl/1" valid="true">
  <rep:assessment><rep:accept/></rep:assessment>
</rep:report>"""
    wrapped = b"[Format error!] <" + report + b"> with params <[Ljava.lang.Object;@123456>"

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout=b"", stderr=wrapped)

    monkeypatch.setattr("app.validators.kosit.subprocess.run", fake_run)
    result = validator.validate(b"<invoice/>", "invoice.xml")

    assert result["executed"] is True
    assert result["accepted"] is True
    assert result["exit_code"] == 0
    assert result["report_source"] == "stderr-format-error"
    assert "wiederhergestellt" in result["summary"]
    assert result["technical_output"] is None


def test_xml_assessment_is_authoritative_when_exit_code_differs(tmp_path, monkeypatch):
    jar = tmp_path / "validator-1.6.2-standalone.jar"
    _write_jar(jar, "de.kosit.validationtool.cmd.CommandLineApplication")
    validator = KositValidator(_settings(tmp_path, jar))
    report = b"""<?xml version="1.0" encoding="UTF-8"?>
<rep:report xmlns:rep="http://www.xoev.de/de/validator/varl/1" valid="true">
  <rep:assessment><rep:accept/></rep:assessment>
</rep:report>"""

    def fake_run(*args, **kwargs):
        command = [str(value) for value in args[0]]
        output_directory = Path(command[command.index("-o") + 1])
        invoice_path = Path(command[-1])
        (output_directory / f"{invoice_path.stem}-report.xml").write_bytes(report)
        return subprocess.CompletedProcess(args=args[0], returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr("app.validators.kosit.subprocess.run", fake_run)
    result = validator.validate(b"<invoice/>", "invoice.xml")

    assert result["executed"] is True
    assert result["accepted"] is True
    assert any(item["id"] == "KOSIT-RESULT-MISMATCH" for item in result["findings"])
