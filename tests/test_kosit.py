from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import zipfile
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.desktop_security import API_TOKEN_ENV
from app.settings import Settings
from app.validators import kosit as kosit_module
from app.validators.kosit import KositValidator


def test_service_shutdown_gate_prevents_late_kosit_process_start(monkeypatch):
    monkeypatch.setattr(kosit_module.subprocess, "Popen", lambda *_args, **_kwargs: None)
    kosit_module.cancel_running_kosit_processes(0)

    try:
        with pytest.raises(OSError, match="Dienst wird beendet"):
            kosit_module._run_kosit_process(
                ["java"],
                capture_output=True,
                timeout=1,
                check=False,
                cwd=".",
                creationflags=0,
            )
    finally:
        kosit_module.allow_kosit_process_starts()


def test_process_timeout_uses_bounded_communicate_after_kill(monkeypatch):
    process = Mock(returncode=None)
    process.stdout = io.BytesIO(b"teil")
    process.stderr = io.BytesIO(b"fehler")
    process.wait.side_effect = [subprocess.TimeoutExpired(["java"], 3), 1]
    monkeypatch.setattr(kosit_module, "_RUNNING_PROCESS_LOCK", Lock())
    monkeypatch.setattr(kosit_module, "_RUNNING_PROCESSES", set())
    monkeypatch.setattr(kosit_module.subprocess, "Popen", Mock(return_value=process))
    kosit_module.allow_kosit_process_starts()

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        kosit_module._run_kosit_process(
            ["java"],
            capture_output=True,
            timeout=3,
            check=False,
            cwd=".",
            creationflags=0,
        )

    assert raised.value.output == b"teil"
    assert raised.value.stderr == b"fehler"
    process.kill.assert_called_once_with()
    assert process.wait.call_args_list[1].kwargs == {"timeout": kosit_module._KILL_COMMUNICATE_TIMEOUT_SECONDS}
    assert not kosit_module._RUNNING_PROCESSES


def test_process_output_budget_is_enforced_while_draining_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(returncode=0)
    process.stdout = io.BytesIO(b"x" * (kosit_module.MAXIMUM_KOSIT_CONSOLE_BYTES + 1))
    process.stderr = io.BytesIO()
    process.wait.return_value = 0
    monkeypatch.setattr(kosit_module, "_RUNNING_PROCESS_LOCK", Lock())
    monkeypatch.setattr(kosit_module, "_RUNNING_PROCESSES", set())
    monkeypatch.setattr(kosit_module.subprocess, "Popen", Mock(return_value=process))
    kosit_module.allow_kosit_process_starts()

    completed = kosit_module._run_kosit_process(
        ["java"],
        capture_output=True,
        timeout=3,
        check=False,
        cwd=".",
        creationflags=0,
    )

    assert len(completed.stdout) == kosit_module.MAXIMUM_KOSIT_CONSOLE_BYTES
    assert vars(completed)["_kosit_console_overflow"] is True
    assert not kosit_module._RUNNING_PROCESSES


def test_kosit_subprocess_never_inherits_api_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock(returncode=0)
    process.stdout = io.BytesIO()
    process.stderr = io.BytesIO()
    process.wait.return_value = 0
    popen = Mock(return_value=process)
    monkeypatch.setattr(kosit_module, "_RUNNING_PROCESS_LOCK", Lock())
    monkeypatch.setattr(kosit_module, "_RUNNING_PROCESSES", set())
    monkeypatch.setattr(kosit_module.subprocess, "Popen", popen)
    monkeypatch.setenv(API_TOKEN_ENV, "s" * 43)
    monkeypatch.setenv("KOSIT_TEST_PRESERVED", "yes")
    kosit_module.allow_kosit_process_starts()

    kosit_module._run_kosit_process(
        ["java"],
        capture_output=True,
        timeout=3,
        check=False,
        cwd=".",
        creationflags=0,
    )

    child_environment = popen.call_args.kwargs["env"]
    assert API_TOKEN_ENV not in child_environment
    assert child_environment["KOSIT_TEST_PRESERVED"] == "yes"


def test_kosit_process_job_is_established_before_child_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    process = Mock(returncode=0)
    process.stdout = io.BytesIO()
    process.stderr = io.BytesIO()
    process.wait.side_effect = lambda **_kwargs: events.append("wait") or 0
    monkeypatch.setattr(kosit_module, "_RUNNING_PROCESS_LOCK", Lock())
    monkeypatch.setattr(kosit_module, "_RUNNING_PROCESSES", set())
    monkeypatch.setattr(kosit_module, "_ensure_kosit_process_job", lambda: events.append("ensure-job"))
    monkeypatch.setattr(
        kosit_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: events.append("popen") or process,
    )
    kosit_module.allow_kosit_process_starts()

    kosit_module._run_kosit_process(
        ["java"],
        capture_output=True,
        timeout=3,
        check=False,
        cwd=".",
        creationflags=0,
    )

    assert events == ["ensure-job", "popen", "wait"]


def test_kosit_parent_job_assignment_failure_closes_job_before_any_child_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close = Mock()
    popen = Mock()
    monkeypatch.setattr(kosit_module.sys, "platform", "win32")
    monkeypatch.setenv(kosit_module.SERVICE_MODE_ENV, "1")
    monkeypatch.setattr(kosit_module, "_KOSIT_JOB_HANDLE", None)
    monkeypatch.setattr(kosit_module, "_KOSIT_JOB_LOCK", Lock())
    monkeypatch.setattr(kosit_module, "_create_kosit_job", Mock(return_value=42))
    monkeypatch.setattr(
        kosit_module,
        "_assign_current_process_to_kosit_job",
        Mock(side_effect=OSError("assignment failed")),
    )
    monkeypatch.setattr(kosit_module, "_close_kosit_job", close)
    monkeypatch.setattr(kosit_module.subprocess, "Popen", popen)

    with pytest.raises(OSError, match="assignment failed"):
        kosit_module._start_kosit_process(["java"], cwd=".", creationflags=0)

    close.assert_called_once_with(42)
    popen.assert_not_called()


def test_desktop_mode_does_not_join_job_that_would_capture_later_browser_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_job = Mock()
    monkeypatch.setattr(kosit_module.sys, "platform", "win32")
    monkeypatch.delenv(kosit_module.SERVICE_MODE_ENV, raising=False)
    monkeypatch.setattr(kosit_module, "_KOSIT_JOB_HANDLE", None)
    monkeypatch.setattr(kosit_module, "_create_kosit_job", create_job)

    kosit_module._ensure_kosit_process_job()

    create_job.assert_not_called()


def test_windows_invoice_temp_file_remains_shareable_and_is_explicitly_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "invoice.xml"
    handle = Mock()
    handle.__enter__ = Mock(return_value=handle)
    handle.__exit__ = Mock(return_value=False)
    handle.fileno.return_value = 41
    native_open = Mock(return_value=41)
    native_close = Mock()
    monkeypatch.setattr(kosit_module.sys, "platform", "win32")
    monkeypatch.setattr(kosit_module.os, "O_BINARY", 0x80, raising=False)
    monkeypatch.setattr(kosit_module.os, "open", native_open)
    monkeypatch.setattr(kosit_module.os, "fdopen", Mock(return_value=handle))
    monkeypatch.setattr(kosit_module.os, "fsync", Mock())
    monkeypatch.setattr(kosit_module.os, "close", native_close)
    unlink = Mock()
    monkeypatch.setattr(Path, "unlink", unlink)

    with kosit_module._ephemeral_invoice_file(path, b"<Invoice/>"):
        pass

    flags = kosit_module.os.O_WRONLY | kosit_module.os.O_CREAT | kosit_module.os.O_EXCL | 0x80
    native_open.assert_called_once_with(path, flags, 0o600)
    handle.write.assert_called_once_with(b"<Invoice/>")
    native_close.assert_called_once_with(41)
    unlink.assert_called_once_with(missing_ok=True)


def test_invoice_temp_file_can_be_read_by_a_real_child_process_and_is_removed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invoice.xml"
    payload = b"<Invoice>share-test</Invoice>"

    with kosit_module._ephemeral_invoice_file(path, payload):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import pathlib,sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())",
                str(path),
            ],
            check=True,
            capture_output=True,
        )
        assert completed.stdout == payload

    assert not path.exists()


def test_service_kosit_temp_tree_is_created_atomically_with_service_acl_and_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acl = Mock()
    data_directory = tmp_path / "service-data"
    data_directory.mkdir()
    paths = SimpleNamespace(
        data_directory=data_directory,
        runtime_directory=data_directory / "runtime",
    )

    def create_protected_directory(path: Path, *, allow_local_service_owner: bool) -> None:
        assert allow_local_service_owner is True
        path.mkdir()

    def protect_directory(path: Path, *, allow_local_service_owner: bool) -> None:
        assert allow_local_service_owner is True
        path.mkdir()

    acl.create_protected_directory.side_effect = create_protected_directory
    acl.protect_directory.side_effect = protect_directory
    monkeypatch.setattr(kosit_module.sys, "platform", "win32")
    monkeypatch.setenv(kosit_module.SERVICE_MODE_ENV, "1")
    monkeypatch.setattr(kosit_module.secrets, "token_hex", lambda _length: "a" * 32)
    monkeypatch.setattr(kosit_module, "WindowsServiceAcl", Mock(return_value=acl))
    monkeypatch.setattr(
        kosit_module.ServicePaths,
        "from_environment",
        Mock(return_value=paths),
    )

    with kosit_module._kosit_temporary_directory() as temporary:
        expected = paths.runtime_directory / f"einvoice-kosit-{'a' * 32}"
        assert temporary == expected
        assert temporary.is_dir()
        (temporary / "invoice.xml").write_bytes(b"<Invoice/>")

    acl.verify_data_directory.assert_called_once_with(data_directory)
    acl.protect_directory.assert_called_once_with(
        paths.runtime_directory,
        allow_local_service_owner=True,
    )
    acl.create_protected_directory.assert_called_once_with(
        expected,
        allow_local_service_owner=True,
    )
    assert not expected.exists()
    assert not paths.runtime_directory.exists()


def test_shutdown_during_process_creation_kills_late_child(monkeypatch):
    process = Mock()
    process.wait.return_value = 0

    def start_after_shutdown_begins(*_args, **_kwargs):
        kosit_module.cancel_running_kosit_processes(0)
        return process

    monkeypatch.setattr(kosit_module, "_RUNNING_PROCESS_LOCK", Lock())
    monkeypatch.setattr(kosit_module, "_RUNNING_PROCESSES", set())
    monkeypatch.setattr(kosit_module.subprocess, "Popen", start_after_shutdown_begins)
    kosit_module.allow_kosit_process_starts()

    try:
        with pytest.raises(OSError, match="begonnene KoSIT-Prüfung wurde abgebrochen"):
            kosit_module._run_kosit_process(
                ["java"],
                capture_output=True,
                timeout=3,
                check=False,
                cwd=".",
                creationflags=0,
            )
    finally:
        kosit_module.allow_kosit_process_starts()

    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=kosit_module._KILL_COMMUNICATE_TIMEOUT_SECONDS)


def test_shutdown_lock_contention_is_bounded_and_closes_start_gate(monkeypatch):
    observed_timeouts: list[float] = []

    class BusyLock:
        def acquire(self, *, timeout):
            observed_timeouts.append(timeout)
            return False

        def release(self):
            raise AssertionError("Eine nicht erworbene Sperre darf nicht freigegeben werden.")

    monkeypatch.setattr(kosit_module, "_RUNNING_PROCESS_LOCK", BusyLock())
    monkeypatch.setattr(kosit_module, "_KOSIT_PROCESS_STARTS_ALLOWED", True)

    assert kosit_module.cancel_running_kosit_processes(0.05) == 0
    assert kosit_module._KOSIT_PROCESS_STARTS_ALLOWED is False
    assert len(observed_timeouts) == 1
    assert 0 <= observed_timeouts[0] <= 0.05


def test_shutdown_process_waits_and_kills_only_within_deadline(monkeypatch):
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired(["java"], 0.05)
    monkeypatch.setattr(kosit_module, "_RUNNING_PROCESS_LOCK", Lock())
    monkeypatch.setattr(kosit_module, "_RUNNING_PROCESSES", {process})

    try:
        assert kosit_module.cancel_running_kosit_processes(0.05) == 1
    finally:
        monkeypatch.setattr(kosit_module, "_RUNNING_PROCESSES", set())
        kosit_module.allow_kosit_process_starts()

    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert process.wait.call_count <= 2
    assert all(0 <= call.kwargs["timeout"] <= 0.05 for call in process.wait.call_args_list)


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

    monkeypatch.setattr("app.validators.kosit._run_kosit_process", fake_run)
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

    monkeypatch.setattr("app.validators.kosit._run_kosit_process", fake_run)
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

    monkeypatch.setattr("app.validators.kosit._run_kosit_process", fake_run)
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

    monkeypatch.setattr(kosit_module, "_run_kosit_process", fake_run)

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

    monkeypatch.setattr("app.validators.kosit._run_kosit_process", fake_run)
    result = validator.validate(b"<invoice/>", "invoice.xml")

    assert "-p" not in observed["command"]
    assert "--print" not in observed["command"]
    assert result["executed"] is True
    assert result["accepted"] is True
    assert result["report_source"] == "file"
    assert 'valid="true"' in result["raw_report"]


def test_valid_serialized_report_remains_authoritative_after_console_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jar = tmp_path / "validator-1.6.2-standalone.jar"
    _write_jar(jar, "de.kosit.validationtool.cmd.CommandLineApplication")
    validator = KositValidator(_settings(tmp_path, jar))
    report = b"""<?xml version="1.0" encoding="UTF-8"?>
<rep:report xmlns:rep="http://www.xoev.de/de/validator/varl/1" valid="true">
  <rep:assessment><rep:accept/></rep:assessment>
</rep:report>"""

    def fake_run(*args, **_kwargs):
        command = [str(value) for value in args[0]]
        output_directory = Path(command[command.index("-o") + 1])
        invoice_path = Path(command[-1])
        (output_directory / f"{invoice_path.stem}-report.xml").write_bytes(report)
        completed = subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=b"x" * kosit_module.MAXIMUM_KOSIT_CONSOLE_BYTES,
            stderr=b"",
        )
        vars(completed)["_kosit_console_overflow"] = True
        return completed

    monkeypatch.setattr(kosit_module, "_run_kosit_process", fake_run)

    result = validator.validate(b"<invoice/>", "invoice.xml")

    assert result["executed"] is True
    assert result["accepted"] is True
    assert result["report_source"] == "file"
    assert any(item["id"] == "KOSIT-OUTPUT-TRUNCATED" for item in result["findings"])


def test_serialized_report_reader_rejects_oversized_file_before_xml_parse(
    tmp_path: Path,
) -> None:
    report_directory = tmp_path / "reports"
    report_directory.mkdir()
    invoice_path = tmp_path / "invoice.xml"
    report = report_directory / "invoice-report.xml"
    report.write_bytes(b"x" * (kosit_module.MAXIMUM_KOSIT_REPORT_BYTES + 1))

    with pytest.raises(OSError, match="Bytebudget"):
        KositValidator._read_serialized_report(report_directory, invoice_path)


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

    monkeypatch.setattr("app.validators.kosit._run_kosit_process", fake_run)
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

    monkeypatch.setattr("app.validators.kosit._run_kosit_process", fake_run)
    result = validator.validate(b"<invoice/>", "invoice.xml")

    assert result["executed"] is True
    assert result["accepted"] is True
    assert any(item["id"] == "KOSIT-RESULT-MISMATCH" for item in result["findings"])
