from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.component_versions import KOSIT_COMPONENT_VERSIONS
from app.desktop_security import API_TOKEN_ENV
from app.official_rulesets import XRECHNUNG_3_0_CII, XRECHNUNG_CONFIGURATION_FILES, OfficialRuleset, RulesetFile
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


def test_kosit_process_gets_a_private_closed_stdin_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    """KoSIT probes System.in.available(); an inherited NUL or closed handle breaks that on Windows."""

    process = Mock()
    process.stdout = io.BytesIO(b"")
    process.stderr = io.BytesIO(b"")
    process.wait.return_value = 0
    observed: dict[str, object] = {}

    def popen(command, **kwargs):
        observed.update(kwargs)
        return process

    monkeypatch.setattr(kosit_module, "_RUNNING_PROCESS_LOCK", Lock())
    monkeypatch.setattr(kosit_module, "_RUNNING_PROCESSES", set())
    monkeypatch.setattr(kosit_module.subprocess, "Popen", popen)

    kosit_module._run_kosit_process(["java"], capture_output=True, timeout=5, check=False, cwd=".", creationflags=0)

    assert observed["stdin"] is subprocess.PIPE
    process.stdin.close.assert_called_once_with()


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


def _bound_ruleset(settings: Settings) -> OfficialRuleset:
    """A copy of the XRechnung CII rule set bound to stand-in artefacts of the test installation."""

    def locked(path: Path, target: str) -> RulesetFile:
        payload = path.read_bytes()
        return RulesetFile(target, len(payload), hashlib.sha256(payload).hexdigest())

    repository = settings.kosit_repositories[0]
    files = []
    for item in XRECHNUNG_CONFIGURATION_FILES:
        path = repository / item.target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"<!-- Stand-in fuer {item.target} -->".encode())
        files.append(locked(path, item.target))
    return replace(
        XRECHNUNG_3_0_CII,
        files=tuple(files),
        scenario_file=locked(settings.kosit_scenarios[0], "scenarios.xml"),
    )


def test_library_jar_without_main_class_is_not_configured(tmp_path):
    jar = tmp_path / "validator-1.6.3.jar"
    _write_jar(jar, None)
    state = KositValidator(_settings(tmp_path, jar)).configuration_state()
    assert state["configured"] is False
    assert any("Main-Class" in problem for problem in state["problems"])


def test_manifest_start_error_is_not_reported_as_invoice_rejection(tmp_path, monkeypatch):
    jar = tmp_path / "validator-1.6.3-standalone.jar"
    _write_jar(jar, "de.kosit.validationtool.cmd.CommandLineApplication")
    settings = _settings(tmp_path, jar)
    validator = KositValidator(settings)

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout=b"",
            stderr=b"kein Hauptmanifestattribut, in validator-1.6.3.jar",
        )

    monkeypatch.setattr("app.validators.kosit._run_kosit_process", fake_run)
    result = validator.validate(b"<invoice/>", "invoice.xml", ruleset=_bound_ruleset(settings))

    assert result["executed"] is False
    assert result["accepted"] is None
    assert result["exit_code"] == 1
    assert result["findings"][0]["id"] == "KOSIT-EXEC"
    assert result["findings"][0]["severity"] == "warning"
    assert "abgelehnt" not in result["findings"][0]["title"].lower()
    assert "abgelehnt" not in result["summary"]


def test_windows_kosit_process_is_started_without_console(tmp_path, monkeypatch):
    jar = tmp_path / "validator-1.6.3-standalone.jar"
    _write_jar(jar, "de.kosit.validationtool.cmd.CommandLineApplication")
    settings = _settings(tmp_path, jar)
    validator = KositValidator(settings)
    observed: dict[str, int] = {}

    def fake_run(*args, **kwargs):
        observed["creationflags"] = kwargs["creationflags"]
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(kosit_module, "_run_kosit_process", fake_run)

    validator.validate(b"<invoice/>", "invoice.xml", ruleset=_bound_ruleset(settings))

    expected = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if sys.platform == "win32" else 0
    if sys.platform == "win32":
        assert expected != 0
    assert kosit_module.WINDOWS_SUBPROCESS_CREATION_FLAGS == expected
    assert observed["creationflags"] == expected


def test_installer_uses_locked_components_and_selects_only_standalone_jar(tmp_path):
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "install_kosit.py"
    spec = importlib.util.spec_from_file_location("kosit_installer_test", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    locked = module.load_lock(module.DEFAULT_LOCK_FILE)
    assert locked["components"]["validator"]["version"] == "KoSIT Validator 1.6.3"
    assert locked["components"]["xrechnung"]["version"].endswith("2026-08-31")
    assert locked["standards"] == {
        "xrechnung": "3.0.2",
        "xrechnung_configuration": "2026-08-31",
        "cen_en16931": "1.3.16",
        "xrechnung_schematron": "2.6.0",
        "facturx_extended": "1.09.2",
        "facturx_extended_source": "fnfempe/France_RFE v1.4.0.03 a63e6b5",
    }
    extended = locked["components"]["facturx_extended"]
    assert extended["kind"] == "fileset"
    assert extended["commit"] == "a63e6b538bdaff460f17c38dc1bc5455cf1ba35a"
    assert extended["base_url"].endswith(f"/{extended['commit']}/")
    assert len(extended["files"]) == 8
    assert {item["target"] for item in extended["files"]} >= {
        "resources/xsd/Factur-X_EXTENDED.xsd",
        "resources/xslt/FACTUR-X_EXTENDED.xslt",
        "resources/xslt/FACTUR-X_EXTENDED_codedb.xml",
        "resources/schematron/FACTUR-X_EXTENDED.sch",
        "LICENSE",
    }

    normal = tmp_path / "validator-1.6.3.jar"
    standalone = tmp_path / "validator-1.6.3-standalone.jar"
    _write_jar(normal, None)
    _write_jar(standalone, "de.kosit.validationtool.cmd.CommandLineApplication")
    assert module.find_validator_jar(tmp_path) == standalone


def test_installer_rejects_unknown_lock_schema_and_invalid_digest(tmp_path):
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "install_kosit.py"
    spec = importlib.util.spec_from_file_location("kosit_installer_invalid_lock_test", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    lock_path = tmp_path / "components.lock.json"
    lock_path.write_text(json.dumps({"schema_version": 3, "components": {}}), encoding="utf-8")
    with pytest.raises(module.InstallError, match="Schema-Version"):
        module.load_lock(lock_path)
    lock_path.write_text(json.dumps({"schema_version": 1, "components": {}}), encoding="utf-8")
    with pytest.raises(module.InstallError, match="Schema-Version"):
        module.load_lock(lock_path)

    component = {
        "version": "1",
        "filename": "component.bin",
        "url": "https://example.test/component.bin",
        "sha256": "falsch",
    }
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "components": {"validator": component, "xrechnung": component},
                "standards": {
                    "xrechnung": "3.0.2",
                    "xrechnung_configuration": "2026-08-31",
                    "cen_en16931": "1.3.16",
                    "xrechnung_schematron": "2.6.0",
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(module.InstallError, match="Prüfsumme"):
        module.load_lock(lock_path)


def test_public_kosit_component_versions_match_the_locked_bundle() -> None:
    lock_path = Path(__file__).resolve().parents[1] / "packaging" / "kosit" / "components.lock.json"
    payload = json.loads(lock_path.read_text(encoding="utf-8"))

    assert KOSIT_COMPONENT_VERSIONS == {
        "validator": payload["components"]["validator"]["version"].removeprefix("KoSIT Validator "),
        "xrechnung": payload["standards"]["xrechnung"],
        "xrechnung_configuration": payload["standards"]["xrechnung_configuration"],
        "cen_en16931": payload["standards"]["cen_en16931"],
        "xrechnung_schematron": payload["standards"]["xrechnung_schematron"],
        "facturx_extended": payload["standards"]["facturx_extended"],
        "facturx_extended_source": payload["standards"]["facturx_extended_source"],
    }
