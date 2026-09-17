"""Safety contracts for the fixed, document-free Darwin startup diagnostic."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "processing_watchdog_probe", Path(__file__).resolve().parents[1] / "scripts/processing_watchdog_probe.py"
)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def report():
    return {
        "case": "inherited",
        "identity": {"pid": 50, "ppid": 20, "pgid": 0, "sid": 0},
        "ready": True,
        "cleanup_confirmed": True,
        "diagnostics": [{"stage": "limits_applied"}, {"stage": "send_ready"}],
    }


@pytest.mark.parametrize("field", ["ready", "cleanup_confirmed"])
def test_missing_proof_never_passes(field):
    value = report()
    value[field] = False
    assert not probe.case_passes(value, "inherited", {"pgid": 0, "sid": 0})


@pytest.mark.parametrize("field", ["pgid", "sid"])
def test_inherited_probe_must_not_silently_change_context(field):
    value = report()
    value["identity"][field] = 50
    assert not probe.case_passes(value, "inherited", {"pgid": 0, "sid": 0})


def test_valid_zero_inherited_context_and_new_session_are_distinct():
    value = report()
    assert probe.case_passes(value, "inherited", {"pgid": 0, "sid": 0})
    value["case"] = "new_session"
    assert not probe.case_passes(value, "new_session", {"pgid": 0, "sid": 0})
    value["identity"].update(pgid=50, sid=50)
    assert probe.case_passes(value, "new_session", {"pgid": 0, "sid": 0})


@pytest.mark.parametrize("context", [{}, {"pid": True, "ppid": 20, "pgid": 0, "sid": 0}])
def test_missing_or_wrongly_typed_context_never_passes(context):
    value = report()
    value["identity"] = context
    assert not probe.case_passes(value, "inherited", {"pgid": 0, "sid": 0})
    value["case"] = "new_session"
    assert not probe.case_passes(value, "new_session", {"pgid": 0, "sid": 0})


def test_bounded_reader_rejects_output_before_accumulating_more(monkeypatch):
    monkeypatch.setattr(probe.select, "select", lambda *_a: ([3], [], []))
    monkeypatch.setattr(probe.os, "read", lambda _fd, size: b"x" * size)
    with pytest.raises(probe.ProbeError, match="output limit"):
        probe.read_bounded(3, 5, probe.time.monotonic() + 1)


def test_bounded_reader_never_waits_past_deadline(monkeypatch):
    monkeypatch.setattr(probe.select, "select", lambda *_a: pytest.fail("expired read waited"))
    with pytest.raises(probe.ProbeError, match="deadline"):
        probe.read_bounded(3, 5, probe.time.monotonic() - 1)


def test_unknown_case_never_starts_process(monkeypatch):
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *_a, **_k: pytest.fail("unexpected spawn"))
    with pytest.raises(probe.ProbeError):
        probe.run_bounded("other")


def test_existing_report_prevents_any_native_action(tmp_path, monkeypatch):
    output = tmp_path / "evidence.json"
    output.write_text("keep")
    monkeypatch.setattr(probe, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(probe, "run_bounded", lambda *_a: pytest.fail("unexpected spawn"))
    with pytest.raises(FileExistsError):
        probe.main(["--output", str(output)])
    assert output.read_text() == "keep"


def test_error_record_never_contains_exception_message_or_traceback():
    error = OSError(24, "synthetic secret /private/example")
    record = probe.error_record(error)
    assert record == {"error_type": "OSError", "errno": 24}
    assert "secret" not in json.dumps(record)


def test_diagnostic_output_is_bounded_before_write(monkeypatch):
    written = []
    monkeypatch.setattr(probe.os, "write", lambda fd, data: written.append(data) or len(data))
    emitter = probe.DiagnosticEmitter(9)
    emitter.emit({"stage": "test"})
    with pytest.raises(probe.ProbeError):
        emitter.emit({"stage": "x" * probe.MAX_DIAGNOSTIC_BYTES})
    assert len(written) == 1


def test_unconfirmed_first_cleanup_stops_comparison(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(probe, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(probe, "identity", lambda: {"pid": 20, "ppid": 10, "pgid": 0, "sid": 0})

    def bounded(case):
        calls.append(case)
        return {"case": case, "cleanup_confirmed": False}

    monkeypatch.setattr(probe, "run_bounded", bounded)
    output = tmp_path / "evidence.json"
    assert probe.main(["--output", str(output)]) == 1
    assert calls == ["inherited"]
    assert json.loads(output.read_text())["passed"] is False


@pytest.mark.skipif(sys.platform != "darwin", reason="Native kqueue startup diagnostic")
def test_native_probe_retains_inherited_context_and_cleans_both_contexts(tmp_path):
    output = tmp_path / "native.json"
    completed = subprocess.run(
        [sys.executable, str(Path(probe.__file__).resolve()), "--output", str(output)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=40,
        check=False,
    )
    result = json.loads(output.read_text())
    assert completed.returncode == 0, result
    assert result["passed"] is True
    assert [entry["case"] for entry in result["cases"]] == ["inherited", "new_session"]
    for entry in result["cases"]:
        assert entry["ready"] and entry["cleanup_confirmed"]
        assert all(code == -9 for code in entry["child_exit_codes"].values())
        applied = next(item for item in entry["diagnostics"] if item["stage"] == "limits_applied")
        assert applied["proof"]["address_space_bytes"] - applied["proof"]["baseline_as_bytes"] == 64 * 1024**2
        assert applied["rlimits"]["nofile"] == [256, 256]
