from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app.configuration import Settings
from app.processing.budgets import ProcessingBudgets
from app.processing.kosit_runtime import (
    BoundedConsoleCapture,
    KositRuntimeError,
    prepare_java_command,
    read_execution,
    write_invoice,
)


def prepared(tmp_path: Path) -> tuple[Path, Settings]:
    directory = tmp_path / "job"
    directory.mkdir(mode=0o700)
    java = tmp_path / "java"
    java.write_bytes(b"synthetic executable placeholder")
    jar = tmp_path / "validator.jar"
    jar.write_bytes(b"synthetic jar placeholder")
    scenario = tmp_path / "scenarios.xml"
    scenario.write_bytes(b"synthetic configuration placeholder")
    settings = Settings(
        kosit_java_bin=str(java),
        kosit_validator_jar=jar,
        kosit_scenarios=(scenario,),
        kosit_repositories=(tmp_path,),
    )
    prepare_java_command(settings, directory, ProcessingBudgets())
    return directory, settings


def test_command_uses_fixed_names_private_java_tmp_and_bounded_vm_options(tmp_path: Path) -> None:
    directory, settings = prepared(tmp_path)
    other = tmp_path / "second"
    other.mkdir(mode=0o700)
    command = prepare_java_command(settings, other, ProcessingBudgets())
    assert command[0] == settings.kosit_java_bin
    assert "-XX:ActiveProcessorCount=2" in command
    assert "-Xmx512m" in command
    assert f"-Djava.io.tmpdir={other / 'java-tmp'}" in command
    assert command[-3:] == ["-o", str(other / "reports"), str(other / "invoice.xml")]
    assert not (directory / "invoice.xml").exists()


def test_invoice_creation_is_exclusive_and_bounded(tmp_path: Path) -> None:
    directory, _settings = prepared(tmp_path)
    payload = b"<synthetic/>"
    assert write_invoice(directory, payload, maximum_bytes=len(payload)) == directory / "invoice.xml"
    assert (directory / "invoice.xml").read_bytes() == payload
    with pytest.raises(KositRuntimeError):
        write_invoice(directory, b"replacement")
    (directory / "invoice.xml").unlink()
    with pytest.raises(KositRuntimeError):
        write_invoice(directory, payload, maximum_bytes=len(payload) - 1)
    assert not (directory / "invoice.xml").exists()


def test_raw_candidates_preserved_despite_console_overflow_and_exit_disagreement(tmp_path: Path) -> None:
    directory, _settings = prepared(tmp_path)
    expected = b"<synthetic-valid-varl-placeholder/>"
    (directory / "reports" / "z-report.xml").write_bytes(b"not XML; parser worker decides")
    (directory / "reports" / "invoice-report.xml").write_bytes(expected)
    execution = read_execution(directory, 9, b"out", b"err", True)
    assert execution.report_candidates == (expected, b"not XML; parser worker decides")
    assert execution.console_overflow is True
    assert execution.returncode == 9
    assert execution.report_error is None
    assert execution.metadata() == {
        "returncode": 9,
        "stdout_size": 3,
        "stderr_size": 3,
        "report_sizes": [len(expected), len(b"not XML; parser worker decides")],
        "console_overflow": True,
        "report_error": None,
        "console_error": None,
    }


def test_report_total_budget_failure_never_returns_partial_candidates(tmp_path: Path) -> None:
    directory, _settings = prepared(tmp_path)
    (directory / "reports" / "invoice-report.xml").write_bytes(b"x" * (2 * 1024 * 1024))
    (directory / "reports" / "z.xml").write_bytes(b"x")
    result = read_execution(directory, 0, b"", b"", False)
    assert result.report_error == "report_bytes_exceeded"
    assert result.report_candidates == ()


def test_report_count_budget_failure_never_returns_partial_candidates(tmp_path: Path) -> None:
    directory, _settings = prepared(tmp_path)
    for index in range(9):
        (directory / "reports" / f"{index}.xml").write_bytes(b"x")
    result = read_execution(directory, 0, b"", b"", False)
    assert result.report_error == "report_count_exceeded"
    assert result.report_candidates == ()


def test_missing_report_is_distinct_from_unsafe_report(tmp_path: Path) -> None:
    directory, _settings = prepared(tmp_path)
    result = read_execution(directory, 1, b"", b"java error", False)
    assert result.report_candidates == ()
    assert result.report_error is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink/permission semantics")
def test_report_symlink_cannot_read_outside_private_job(tmp_path: Path) -> None:
    directory, _settings = prepared(tmp_path)
    outside = tmp_path / "outside.xml"
    outside.write_bytes(b"must not be returned")
    (directory / "reports" / "invoice-report.xml").symlink_to(outside)
    result = read_execution(directory, 0, b"", b"", False)
    assert result.report_error == "report_unsafe_file"
    assert result.report_candidates == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX private-directory permissions")
def test_world_readable_job_directory_is_rejected(tmp_path: Path) -> None:
    directory, _settings = prepared(tmp_path)
    directory.chmod(0o755)
    with pytest.raises(KositRuntimeError):
        write_invoice(directory, b"x")


def test_capture_drains_after_overflow_and_retains_only_the_limit() -> None:
    capture = BoundedConsoleCapture(io.BytesIO(b"abcdef"), maximum_bytes=3)
    capture.start()
    assert capture.finish(timeout=1) == b"abc"
    assert capture.overflowed


def test_two_pipes_are_drained_concurrently_with_fixed_caps() -> None:
    pairs = [os.pipe(), os.pipe()]
    streams = [os.fdopen(read_fd, "rb", buffering=0) for read_fd, _write_fd in pairs]
    captures = [BoundedConsoleCapture(stream, maximum_bytes=32) for stream in streams]
    payload = b"synthetic" * 4096

    def write(fd: int) -> None:
        with os.fdopen(fd, "wb", buffering=0) as pipe:
            remaining = memoryview(payload)
            while remaining:
                remaining = remaining[pipe.write(remaining) :]

    writers = [threading.Thread(target=write, args=(write_fd,), daemon=True) for _read_fd, write_fd in pairs]
    try:
        for capture in captures:
            capture.start()
        for writer in writers:
            writer.start()
        assert [capture.finish(timeout=2) for capture in captures] == [payload[:32], payload[:32]]
        assert all(capture.overflowed for capture in captures)
        for writer in writers:
            writer.join(timeout=1)
            assert not writer.is_alive()
    finally:
        for stream in streams:
            stream.close()


def test_console_over_cap_does_not_discard_report(tmp_path: Path) -> None:
    directory, _settings = prepared(tmp_path)
    (directory / "reports" / "invoice-report.xml").write_bytes(b"raw report")
    result = read_execution(directory, 0, b"x" * (2 * 1024 * 1024 + 1), b"", False)
    assert len(result.stdout) == 2 * 1024 * 1024
    assert result.console_overflow
    assert result.report_candidates == (b"raw report",)


def test_hardlinked_report_is_rejected(tmp_path: Path) -> None:
    directory, _settings = prepared(tmp_path)
    report = directory / "reports" / "invoice-report.xml"
    report.write_bytes(b"synthetic")
    os.link(report, tmp_path / "alias")
    result = read_execution(directory, 0, b"", b"", False)
    assert result.report_error == "report_unsafe_file"
    assert result.report_candidates == ()


def test_unbounded_non_xml_directory_is_not_scanned(tmp_path: Path) -> None:
    directory, _settings = prepared(tmp_path)
    for index in range(33):
        (directory / "reports" / f"{index}.txt").write_bytes(b"")
    result = read_execution(directory, 0, b"", b"", False)
    assert result.report_error == "report_directory_entries_exceeded"


def test_capture_cannot_wait_forever_for_open_pipe() -> None:
    release = threading.Event()

    class DelayedStream:
        def read(self, _size):
            release.wait(timeout=1)
            return b""

    capture = BoundedConsoleCapture(DelayedStream())
    capture.start()
    try:
        with pytest.raises(KositRuntimeError, match="console_capture_not_closed"):
            capture.finish(timeout=0.01)
    finally:
        release.set()
        assert capture.finish(timeout=1) == b""


def test_import_does_not_load_parsers_or_ambient_settings() -> None:
    code = (
        "import sys; import app.processing.kosit_runtime; "
        "print(any(name in sys.modules for name in ['app.settings','app.main','lxml','pypdf','reportlab']))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert result.stdout.strip() == "False"
