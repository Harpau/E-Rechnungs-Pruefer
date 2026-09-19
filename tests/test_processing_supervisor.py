from __future__ import annotations

import io
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app.configuration import Settings, settings_to_snapshot
from app.processing import supervisor, worker
from app.processing.budgets import ProcessingBudgets
from app.processing.protocol import read_control, read_payload, write_control, write_payload
from app.processing.result import OperationResult
from app.validators.kosit import KositValidator


class OpenBuffer(io.BytesIO):
    def __exit__(self, *args):
        return None


class FailedConsole(OpenBuffer):
    def read(self, size=-1):
        raise OSError("synthetic console read failure")


@pytest.mark.parametrize("returncode", [0, 7])
def test_windows_java_launcher_uses_detached_default_after_go_with_explicit_streams(tmp_path, monkeypatch, returncode):
    incoming, outgoing = OpenBuffer(), OpenBuffer()
    stdout, stderr = OpenBuffer(), OpenBuffer()
    write_control(incoming, {"type": "go"})
    incoming.seek(0)
    command = [r"C:\Synthetic\java.exe", "-Xmx512m", "-jar", r"C:\Synthetic\validator.jar"]
    settings = Settings(kosit_timeout_seconds=11)
    budgets = ProcessingBudgets()
    setup = {
        "settings": settings_to_snapshot(settings),
        "budgets": asdict(budgets),
        "temporary_directory": str(tmp_path),
    }
    environment = {"SystemRoot": r"C:\Windows"}
    events = []

    def prepare(observed_settings, directory, observed_budgets):
        assert observed_settings == settings and directory == tmp_path and observed_budgets == budgets
        return command

    def popen(actual_command, **options):
        assert incoming.tell() == len(incoming.getvalue()), "the fixed GO must be consumed before CreateProcess"
        assert read_control(io.BytesIO(outgoing.getvalue())) == {
            "type": "ready",
            "role": "java",
            "protocol": 2,
            "limits": {"job_memory_bytes": budgets.java_memory_bytes},
        }
        assert actual_command == command
        assert options == {
            "stdin": -3,
            "stdout": stdout,
            "stderr": stderr,
            "close_fds": True,
            "env": environment,
            "cwd": str(tmp_path),
            "creationflags": 0x00000008,
        }
        events.append("spawn")

        def wait(*, timeout):
            assert timeout == 12, "keep the existing configured Java wait bound"
            events.append("wait")
            return returncode

        return SimpleNamespace(wait=wait)

    monkeypatch.setattr("app.processing.kosit_runtime.prepare_java_command", prepare)
    monkeypatch.setattr(supervisor, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(supervisor, "inherited_file", lambda handle, _mode: {40: stdout, 41: stderr}[handle])
    monkeypatch.setattr(supervisor, "child_environment", lambda: environment)
    monkeypatch.setattr(
        supervisor,
        "subprocess",
        SimpleNamespace(Popen=popen, DEVNULL=-3, CREATE_NO_WINDOW=0x08000000, DETACHED_PROCESS=0x00000008),
    )
    assert supervisor.run_java_launcher(incoming, outgoing, setup, 40, 41) == returncode
    assert events == ["spawn", "wait"]


def _varl(decision: str) -> bytes:
    return (
        '<rep:report xmlns:rep="http://www.xoev.de/de/validator/varl/1" valid="true">'
        f"<rep:assessment><rep:{decision}/></rep:assessment></rep:report>"
    ).encode()


def _run_completed_java(tmp_path, monkeypatch, *, report, timed_out=False, returncode=0):
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    reports = job / "reports"
    reports.mkdir(mode=0o700)
    if report is not None:
        (reports / "invoice-report.xml").write_bytes(report)
    worker_in, worker_out, java_in, java_out = (OpenBuffer() for _ in range(4))
    parent_in, parent_out = OpenBuffer(), OpenBuffer()
    xml = b"<synthetic/>"
    write_control(worker_in, {"type": "ready", "role": "worker"})
    write_control(worker_in, {"type": "input_received"})
    write_control(worker_in, {"type": "kosit", "size": len(xml)})
    write_payload(worker_in, xml)
    write_control(
        worker_in,
        {
            "type": "result",
            "result": {"status_code": 200, "body_size": 2, "media_type": "application/json", "headers": {}},
        },
    )
    write_payload(worker_in, b"{}")
    write_control(java_in, {"type": "ready", "role": "java"})
    write_control(parent_in, {"type": "input", "size": len(xml)})
    write_payload(parent_in, xml)
    write_control(parent_in, {"type": "java_exit", "returncode": returncode, "timed_out": timed_out})
    for stream in (worker_in, java_in, parent_in):
        stream.seek(0)
    streams = [worker_in, worker_out, java_in, java_out, FailedConsole(), OpenBuffer(b"healthy stderr")]
    monkeypatch.setattr(supervisor, "inherited_file", lambda handle, _mode: streams[handle])
    # This pure broker test must never change the test process's POSIX limits.
    monkeypatch.setattr(supervisor, "sys", SimpleNamespace(platform="win32"))
    setup = {
        "settings": settings_to_snapshot(Settings()),
        "budgets": asdict(ProcessingBudgets()),
        "java_enabled": True,
        "temporary_directory": str(job),
        "role_pids": [123, 124],
        "operation": "analyze",
        "scope": "complete",
    }
    supervisor.run_supervisor(parent_in, parent_out, setup, list(range(6)))
    worker_out.seek(0)
    assert read_control(worker_out) == setup
    assert read_control(worker_out) == {"type": "input", "size": len(xml)}
    assert read_payload(worker_out, len(xml), maximum=1024) == xml
    message = read_control(worker_out)
    if message["type"] == "java_failure":
        return message, None, None, ()
    stdout = read_payload(worker_out, message["stdout_size"], maximum=2 * 1024**2)
    stderr = read_payload(worker_out, message["stderr_size"], maximum=2 * 1024**2)
    candidates = tuple(read_payload(worker_out, size, maximum=2 * 1024**2) for size in message["report_sizes"])
    return message, stdout, stderr, candidates


@pytest.mark.parametrize("decision,returncode,accepted", [("accept", 9, True), ("reject", 0, False)])
def test_console_read_error_keeps_safe_file_varl_authoritative(tmp_path, monkeypatch, decision, returncode, accepted):
    report = _varl(decision)
    message, stdout, stderr, candidates = _run_completed_java(
        tmp_path, monkeypatch, report=report, returncode=returncode
    )
    assert message["type"] == "java_result"
    assert message["console_error"] == "console_capture_read_failed"
    assert message["console_overflow"] is False
    assert candidates == (report,) and stdout == b"" and stderr == b"healthy stderr"
    result = KositValidator(Settings()).evaluate_execution(
        {"configured": True},
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        report_payload=candidates[0],
        console_error=message["console_error"],
    )
    assert result["executed"] is True and result["accepted"] is accepted
    assert result["report_source"] == "file"
    warnings = {finding["id"] for finding in result["findings"] if finding["severity"] == "warning"}
    assert {"KOSIT-CONSOLE-INCOMPLETE", "KOSIT-RESULT-MISMATCH"} <= warnings
    assert "KOSIT-OUTPUT-TRUNCATED" not in warnings


def test_java_timeout_does_not_promote_an_existing_report(tmp_path, monkeypatch):
    message, *_ = _run_completed_java(tmp_path, monkeypatch, report=_varl("accept"), timed_out=True)
    assert message == {"type": "java_failure"}


def test_console_read_error_does_not_bypass_report_byte_budget(tmp_path, monkeypatch):
    message, _stdout, _stderr, candidates = _run_completed_java(tmp_path, monkeypatch, report=b"x" * (2 * 1024**2 + 1))
    assert message["type"] == "java_result"
    assert message["report_error"] == "report_bytes_exceeded"
    assert candidates == ()


def test_console_read_error_without_valid_report_is_technical(tmp_path, monkeypatch):
    message, stdout, stderr, candidates = _run_completed_java(tmp_path, monkeypatch, report=None)
    assert message["type"] == "java_result" and not candidates
    result = KositValidator(Settings()).evaluate_execution(
        {"configured": True},
        returncode=0,
        stdout=stdout,
        stderr=stderr,
        report_payload=None,
        console_error=message["console_error"],
    )
    assert result["executed"] is False and result["accepted"] is None
    assert "nicht vollständig gelesen" in result["technical_output"]


@pytest.mark.parametrize("value", [True, 1, [], {}, "unknown", "console_capture_not_closed"])
def test_validator_rejects_unknown_or_mistyped_console_error_codes(value):
    with pytest.raises(ValueError):
        KositValidator(Settings()).evaluate_execution(
            {"configured": True},
            returncode=0,
            stdout=b"",
            stderr=b"",
            report_payload=_varl("accept"),
            console_error=value,
        )


@pytest.mark.parametrize("console_error", [None, "console_capture_read_failed", True, "unknown", {}, []])
def test_worker_transports_only_exact_console_error_code_to_varl_evaluation(monkeypatch, console_error):
    incoming, outgoing = io.BytesIO(), io.BytesIO()
    xml, report = b"<synthetic/>", _varl("accept")
    write_control(incoming, {"type": "input", "size": len(xml)})
    write_payload(incoming, xml)
    write_control(
        incoming,
        {
            "type": "java_result",
            "returncode": 0,
            "stdout_size": 0,
            "stderr_size": 0,
            "report_sizes": [len(report)],
            "console_overflow": False,
            "report_error": None,
            "console_error": console_error,
        },
    )
    for payload in (b"", b"", report):
        write_payload(incoming, payload)
    incoming.seek(0)

    def execute(_operation, data, filename, _media_type, **options):
        result = options["official_validator"](data, filename)
        return OperationResult(json.dumps(result).encode(), "application/json", {})

    monkeypatch.setattr("app.processing.operations.execute_operation", execute)
    monkeypatch.setattr(worker, "sys", SimpleNamespace(platform="win32"))
    worker.run_worker(
        incoming,
        outgoing,
        {
            "settings": settings_to_snapshot(Settings()),
            "budgets": asdict(ProcessingBudgets()),
            "official_state": {"configured": True},
            "operation": "analyze",
            "filename": "synthetic.xml",
            "media_type": "application/xml",
            "official": True,
            "scope": "complete",
        },
    )
    outgoing.seek(0)
    assert read_control(outgoing)["role"] == "worker"
    assert read_control(outgoing) == {"type": "input_received"}
    assert read_control(outgoing) == {"type": "kosit", "size": len(xml)}
    assert read_payload(outgoing, len(xml), maximum=1024) == xml
    envelope = read_control(outgoing)
    if console_error is None or console_error == "console_capture_read_failed":
        assert envelope["type"] == "result"
        result = json.loads(read_payload(outgoing, envelope["result"]["body_size"], maximum=65536))
        assert result["executed"] is True and result["accepted"] is True
        assert any(item["id"] == "KOSIT-CONSOLE-INCOMPLETE" for item in result["findings"]) is bool(console_error)
    else:
        assert envelope["type"] == "error" and envelope["status"] == 500


def test_unfinished_console_capture_remains_technical_even_with_report(tmp_path, monkeypatch):
    from app.processing.kosit_runtime import KositRuntimeError

    class UnfinishedCapture:
        overflowed = False

        def __init__(self, _stream):
            pass

        def start(self):
            pass

        def finish(self, *, timeout):
            raise KositRuntimeError("console_capture_not_closed")

    monkeypatch.setattr("app.processing.kosit_runtime.BoundedConsoleCapture", UnfinishedCapture)
    message, *_ = _run_completed_java(tmp_path, monkeypatch, report=_varl("accept"))
    assert message == {"type": "java_failure"}
