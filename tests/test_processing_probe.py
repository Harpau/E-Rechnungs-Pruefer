from __future__ import annotations

import importlib.util
import json
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.processing import posix

SPEC = importlib.util.spec_from_file_location(
    "processing_probe", Path(__file__).resolve().parents[1] / "scripts/processing_probe.py"
)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@pytest.mark.skipif(os.name == "nt", reason="POSIX probe CLI")
def test_probe_reports_the_shared_arm64_baseline_ceiling_without_changing_allocation_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(posix, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(posix, "platform", SimpleNamespace(machine=lambda: "arm64"), raising=False)
    monkeypatch.setattr(probe, "inventory", lambda: {})
    monkeypatch.setattr(
        probe,
        "run_bounded",
        lambda case: {
            "case": case,
            "returncode": 0,
            "forced_stop": None,
            "records": [
                {"phase": "memory_result", "limit_set": True, "enforced": True, "small_allocation_passed": True}
            ],
        },
    )
    output = tmp_path / "probe.json"
    assert probe.main(["--case", "address_space", "--case", "heap", "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["limits"]["operation_baseline_ceiling_bytes"] == 512 * 1024**3
    assert report["limits"]["max_allocation_bytes"] == 32 * 1024**2
    assert report["limits"]["outer_seconds_per_case"] == 9


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_kqueue_watcher_rejects_other_platforms_before_using_descriptors(monkeypatch, platform):
    monkeypatch.setattr(probe.sys, "platform", platform)
    monkeypatch.setattr(probe.os, "fdopen", lambda *_a, **_k: pytest.fail("Unexpected descriptor access"))
    with pytest.raises(probe.ProbeError, match="macOS"):
        probe.watcher(123, 456)


def cpu_result() -> dict:
    return {
        "case": "cpu",
        "returncode": -9,
        "forced_stop": None,
        "records": [{"phase": "cpu_ready", "limits": [1, 2]}],
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGKILL native-probe classification")
def test_outer_watchdog_kill_does_not_prove_hard_cpu_limit() -> None:
    result = cpu_result()
    assert probe.evaluate(result)
    result["forced_stop"] = "outer_watchdog"
    assert not probe.evaluate(result)


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGKILL native-probe classification")
def test_soft_cpu_signal_does_not_prove_hard_cpu_limit() -> None:
    result = cpu_result()
    result["returncode"] = -signal.SIGXCPU
    assert not probe.evaluate(result)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal native-probe classification")
def test_default_cpu_signal_is_separately_classified() -> None:
    result = cpu_result()
    result["case"] = "cpu_default_signal"
    result["returncode"] = -signal.SIGXCPU
    assert probe.evaluate(result)
    result["case"] = "cpu"
    assert not probe.evaluate(result)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal native-probe classification")
def test_external_wall_kill_does_not_claim_kernel_cpu_kill() -> None:
    result = cpu_result()
    result["case"] = "cpu_external_deadline"
    result["forced_stop"] = "planned_wall_deadline"
    assert probe.evaluate(result)
    result["case"] = "cpu"
    assert not probe.evaluate(result)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup")
def test_forced_group_kill_precedes_any_reaping(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class Process:
        pid = 999999

        def poll(self):
            pytest.fail("A forced-stop leader must not be reaped before group kill")

        def wait(self, *, timeout):
            calls.append("wait")
            return -9

    monkeypatch.setattr(probe.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    assert probe.finish_outer(Process(), "outer_watchdog", time.monotonic()) == "outer_watchdog"
    assert calls == [(999999, signal.SIGKILL), "wait"]


def test_recorded_address_limit_without_enforcement_is_not_pass() -> None:
    result = {
        "case": "address_space",
        "returncode": 0,
        "forced_stop": None,
        "records": [{"phase": "memory_result", "limit_set": True, "enforced": False, "small_allocation_passed": True}],
    }
    assert not probe.evaluate(result)


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGKILL native-probe classification")
def test_sleeper_natural_exit_is_not_liveness_cleanup() -> None:
    result = {
        "case": "pipe_eof",
        "returncode": 0,
        "forced_stop": None,
        "records": [{"phase": "liveness_result", "leaf_returncode": 0}],
    }
    assert not probe.evaluate(result)


def test_unknown_case_never_starts_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *_a, **_k: pytest.fail("Unexpected process start"))
    with pytest.raises(probe.ProbeError, match="Unknown public"):
        probe.run_bounded("unbounded")


@pytest.mark.skipif(os.name == "nt", reason="POSIX probe CLI")
def test_existing_evidence_cannot_be_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "evidence.json"
    output.write_text("previous evidence", encoding="utf-8")
    monkeypatch.setattr(probe, "inventory", lambda: {})
    monkeypatch.setattr(probe, "run_bounded", lambda _case: pytest.fail("Probe must not run"))
    with pytest.raises(FileExistsError):
        probe.main(["--output", str(output), "--case", "address_space"])
    assert output.read_text(encoding="utf-8") == "previous evidence"
