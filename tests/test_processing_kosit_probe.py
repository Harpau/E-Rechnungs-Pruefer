from __future__ import annotations

import io
import json
import threading
from types import SimpleNamespace

import pytest


def _response(accepted=True):
    decision = "accept" if accepted else "reject"
    return {
        "schema_version": 2,
        "assessment": {
            "official": {
                "configured": True,
                "requested": True,
                "executed": True,
                "status": "accepted" if accepted else "rejected",
                "report_source": "file",
                "exit_code": 0 if accepted else 1,
                "raw_report": (
                    '<rep:report xmlns:rep="http://www.xoev.de/de/validator/varl/1" valid="true">'
                    f"<rep:assessment><rep:{decision}/></rep:assessment>"
                    "<synthetic>BR-CL-01</synthetic></rep:report>"
                ),
            }
        },
    }


@pytest.mark.parametrize("case", ["cii_accept", "cii_reject", "ubl_accept", "ubl_reject"])
def test_fixed_outcome_requires_real_varl_and_matching_official_assessment(case):
    from scripts import processing_kosit_probe as probe

    result = probe.validate_outcome(case, json.dumps(_response(case.endswith("accept"))).encode())
    assert result["executed"] is True and result["varl_assessment"] in {"accept", "reject"}
    assert result["raw_report_sha256"] and result["report_source"] == "file"


@pytest.mark.parametrize(
    "field,value",
    [
        ("executed", False),
        ("configured", False),
        ("requested", False),
        ("status", "unavailable"),
        ("report_source", "stdout"),
        ("raw_report", "<unrelated/>"),
    ],
)
def test_missing_or_technical_java_result_never_passes_calibration(field, value):
    from scripts import processing_kosit_probe as probe

    response = _response()
    response["assessment"]["official"][field] = value
    with pytest.raises(probe.ProbeError):
        probe.validate_outcome("cii_accept", json.dumps(response).encode())


def test_case_inventory_and_output_cap_are_fixed():
    from scripts import processing_kosit_probe as probe

    assert probe.CASES == ("cii_accept", "cii_reject", "ubl_accept", "ubl_reject")
    assert probe.CASE_SECONDS == 90 and probe.TOTAL_SECONDS == 360
    with pytest.raises(probe.ProbeError):
        probe.validate_outcome("arbitrary", b"{}")
    with pytest.raises(probe.ProbeError):
        probe.validate_outcome("cii_accept", b" " * (probe.RESULT_BYTES + 1))


def test_original_job_is_queried_and_closed_once_without_duplicate_or_inheritance(monkeypatch):
    from scripts import processing_kosit_probe as probe

    events = []
    job = SimpleNamespace(_lock=threading.RLock(), _handle=42)

    def close():
        events.append(("close", job._handle))
        job._handle = 0

    job.close = close
    monkeypatch.setattr(
        probe,
        "windows_job_counters",
        lambda observed: events.append(("query", observed._handle)) or {"status": "observed"},
    )
    record = {}
    probe.observe_job_close(job, record)
    job.close()
    job.close()
    assert events == [("query", 42), ("close", 42), ("close", 0)]
    assert record["status"] == "observed"


def test_failed_job_query_never_skips_real_close_or_claims_observation(monkeypatch):
    from scripts import processing_kosit_probe as probe

    closed = []
    job = SimpleNamespace(_lock=threading.RLock(), _handle=42, close=lambda: closed.append(42))
    monkeypatch.setattr(probe, "windows_job_counters", lambda _: (_ for _ in ()).throw(OSError("synthetic")))
    record = {}
    probe.observe_job_close(job, record)
    job.close()
    assert closed == [42] and record["status"] == "unavailable"


def test_job_peak_uses_original_kernel_structure_and_current_limits():
    from app.processing import windows
    from scripts import processing_kosit_probe as probe

    def query(handle, kind, output, size, returned):
        assert handle == 42 and kind == 9 and returned is None
        value = output._obj
        value.BasicLimitInformation.LimitFlags = 0x2208
        value.BasicLimitInformation.ActiveProcessLimit = 2
        value.JobMemoryLimit = 4096 * 1024**2
        value.PeakJobMemoryUsed = 700 * 1024**2
        value.PeakProcessMemoryUsed = 680 * 1024**2
        return 1

    job = SimpleNamespace(
        handle=42,
        _api=SimpleNamespace(dll=SimpleNamespace(QueryInformationJobObject=query)),
        _limits=windows._Limits(4096 * 1024**2, None, 2),
        active_process_count=lambda: 0,
    )
    result = probe.windows_job_counters(job)
    assert result["peak_job_commit_bytes"] == 700 * 1024**2
    assert result["current_job_limit_bytes"] == 4096 * 1024**2
    assert result["active_processes_at_close"] == 0
    assert result["unit"] == "bytes" and result["metric"] == "private_commit"


def test_calibration_cannot_pass_when_any_role_memory_observation_is_missing():
    from scripts import processing_kosit_probe as probe

    roles = [{"role": role, "status": "observed", "peak_rss_bytes": 123} for role in ("supervisor", "worker", "java")]
    assert probe.memory_complete(roles, [], platform="linux")
    roles[2]["status"] = "unavailable"
    assert not probe.memory_complete(roles, [], platform="linux")
    assert not probe.memory_complete([], [], platform="linux")


def test_forced_stop_or_missing_memory_cannot_be_calibration_success():
    from scripts import processing_kosit_probe as probe

    report = {"passed": True, "cleanup_confirmed": True, "active_after": 0, "memory_observation_complete": True}
    assert probe.successful_child(report, returncode=0, forced_stop=None)
    assert not probe.successful_child(report, returncode=0, forced_stop="deadline")
    assert not probe.successful_child(report | {"memory_observation_complete": False}, returncode=0, forced_stop=None)


def test_interrupted_outer_wait_still_cleans_only_its_owned_harness(monkeypatch, tmp_path):
    from scripts import processing_kosit_probe as probe

    class Child:
        stdout = io.BytesIO(b"{}")
        stderr = io.BytesIO()
        returncode = None
        killed = False

        def kill(self):
            self.killed = True

        def wait(self, *, timeout):
            if not self.killed:
                raise KeyboardInterrupt
            self.returncode = -9
            return -9

    child = Child()
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: child)
    with pytest.raises(KeyboardInterrupt):
        probe.run_bounded("cii_accept", tmp_path, tmp_path, tmp_path, tmp_path, deadline=probe.time.monotonic() + 90)
    assert child.killed and child.stdout.closed and child.stderr.closed


def _windows_memory():
    roles = [
        {"role": role, "status": "observed", "peak_working_set_bytes": 123, "peak_private_commit_bytes": 456}
        for role in ("supervisor", "worker", "java")
    ]
    jobs = [
        {
            "role": role,
            "status": "observed",
            "limits_verified": True,
            "peak_job_commit_bytes": 789,
            "peak_process_commit_bytes": 456,
            "active_processes_at_close": 0,
        }
        for role in ("outer", "supervisor", "worker", "java")
    ]
    return roles, jobs


@pytest.mark.parametrize(
    "field,value",
    [
        ("peak_job_commit_bytes", 0),
        ("peak_job_commit_bytes", True),
        ("peak_process_commit_bytes", 0),
        ("limits_verified", False),
        ("active_processes_at_close", 1),
        ("active_processes_at_close", False),
        ("status", "unavailable"),
    ],
)
def test_windows_job_calibration_requires_real_peaks_exact_limits_and_empty_jobs(field, value):
    from scripts import processing_kosit_probe as probe

    roles, jobs = _windows_memory()
    assert probe.memory_complete(roles, jobs, platform="win32")
    jobs[-1][field] = value
    assert not probe.memory_complete(roles, jobs, platform="win32")


def test_launcher_metrics_alone_cannot_prove_actual_jvm_memory():
    from scripts import processing_kosit_probe as probe

    roles, jobs = _windows_memory()
    assert not probe.memory_complete(roles, jobs[:-1], platform="win32")
    roles[-1]["peak_private_commit_bytes"] = 0
    assert not probe.memory_complete(roles, jobs, platform="win32")


def test_query_failure_and_changed_kernel_limit_are_not_verified():
    from app.processing import windows
    from scripts import processing_kosit_probe as probe

    job = SimpleNamespace(
        handle=42,
        _api=SimpleNamespace(dll=SimpleNamespace(QueryInformationJobObject=lambda *args: 0)),
        _limits=windows._Limits(1024, None, 2),
        active_process_count=lambda: 0,
    )
    with pytest.raises(OSError):
        probe.windows_job_counters(job)

    def query(handle, kind, output, size, returned):
        value = output._obj
        value.BasicLimitInformation.LimitFlags = 0x2208
        value.BasicLimitInformation.ActiveProcessLimit = 2
        value.JobMemoryLimit = 2048
        value.PeakJobMemoryUsed = 100
        value.PeakProcessMemoryUsed = 80
        return 1

    job._api.dll.QueryInformationJobObject = query
    assert probe.windows_job_counters(job)["limits_verified"] is False


@pytest.mark.parametrize("failure", ["changed_components", "failed_case"])
def test_catalog_stops_after_first_failure_without_running_more_jobs(monkeypatch, tmp_path, failure):
    from scripts import processing_kosit_probe as probe

    bound = {"java_sha256": "synthetic"}
    monkeypatch.setattr(probe, "component_binding", lambda *args: bound)
    calls = []

    def run(case, *args, **kwargs):
        calls.append(case)
        assert kwargs["deadline"] <= probe.time.monotonic() + probe.CASE_SECONDS
        return {
            "passed": failure != "failed_case",
            "report": {"components": {} if failure == "changed_components" else bound},
        }

    monkeypatch.setattr(probe, "run_bounded", run)
    output = tmp_path / "evidence.json"
    result = probe.main(
        [
            "--vendor-root",
            str(tmp_path),
            "--java",
            str(tmp_path),
            "--config-archive",
            str(tmp_path),
            "--output",
            str(output),
        ]
    )
    assert result == 1 and calls == ["cii_accept"]
    assert json.loads(output.read_text())["passed"] is False


def test_case_uses_explicit_settings_and_actual_manager_without_ambient_import(monkeypatch, tmp_path):
    """Synthetic manager double checks wiring, not native Java or OS enforcement."""
    import asyncio

    from app.processing import manager as manager_module
    from app.processing.budgets import ProcessingBudgets
    from scripts import processing_kosit_probe as probe

    events = []
    bound = {"validator_path": str(tmp_path / "validator.jar"), "configuration_path": str(tmp_path)}
    monkeypatch.setattr(probe, "component_binding", lambda *args: bound)
    monkeypatch.setattr(probe, "memory_complete", lambda *args, **kwargs: True)
    monkeypatch.delitem(probe.sys.modules, "app.settings", raising=False)

    class Lease:
        tree = SimpleNamespace(cleaned=True, outer_job=None)
        ready = threading.Event()
        ready_manifest = {"synthetic": "unit test only"}
        released = False
        poisoned = False

        async def run(self, upload, settings):
            assert upload.options.official is True and upload.operation == "analyze"
            assert settings.kosit_enabled is True and settings.kosit_timeout_seconds == 60
            assert settings.kosit_java_bin == str(tmp_path / "java")
            assert settings.kosit_validator_jar == tmp_path / "validator.jar"
            assert settings.kosit_scenarios == (tmp_path / "scenarios.xml",)
            self.ready.set()
            body = json.dumps(_response()).encode()
            return SimpleNamespace(chunks=[body], body_size=len(body), media_type="application/json")

        def release(self):
            self.released = True
            events.append("release")

    lease = Lease()

    class Manager:
        budgets = ProcessingBudgets()

        def try_acquire(self):
            return lease

        @property
        def active_count(self):
            return 0 if lease.released else 1

        def shutdown(self):
            assert lease.released
            events.append("shutdown")

    monkeypatch.setattr(manager_module, "ProcessingManager", Manager)
    result = asyncio.run(probe.run_case("cii_accept", tmp_path, tmp_path / "java", tmp_path, tmp_path / "raw"))
    assert result["passed"] and result["ready_confirmed"]
    assert result["ambient_settings_imported"] is False
    assert events.index("release") < events.index("shutdown")
    assert (tmp_path / "raw/analysis.json").is_file()


def test_outer_overflow_is_bounded_and_failed_even_if_child_claims_success(monkeypatch, tmp_path):
    from scripts import processing_kosit_probe as probe

    class Child:
        stdout = io.BytesIO(b"x" * (probe.REPORT_BYTES + 1))
        stderr = io.BytesIO(b"y" * (probe.STDERR_BYTES + 1))
        returncode = 0
        kills = 0

        def kill(self):
            self.kills += 1

        def wait(self, *, timeout):
            return 0

    child = Child()
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: child)
    result = probe.run_bounded(
        "cii_accept", tmp_path, tmp_path, tmp_path, tmp_path / "case", deadline=probe.time.monotonic() + 90
    )
    assert result["passed"] is False and result["forced_stop"] == "diagnostic_overflow_or_open_pipe"
    assert child.kills >= 1
    assert (tmp_path / "case.stdout.txt").stat().st_size == probe.REPORT_BYTES
    assert (tmp_path / "case.stderr.txt").stat().st_size == probe.STDERR_BYTES
