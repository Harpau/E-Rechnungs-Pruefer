"""Four bounded synthetic real-Java manager cases, with native memory evidence.

No downloads, product installation, arbitrary commands, invoices or limit changes.
Windows job counters are read through the original parent's handle immediately
before its normal close. No additional handle can delay kill-on-close cleanup.
POSIX RSS and Windows private commit are different metrics, never AS allowances.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, BinaryIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.processing_smoke import harness_memory, observe_process, observe_ready, write_json  # noqa: E402

CASES = ("cii_accept", "cii_reject", "ubl_accept", "ubl_reject")
CASE_SECONDS = 90
TOTAL_SECONDS = 360
RESULT_BYTES = 16 * 1024**2
REPORT_BYTES = 128 * 1024
STDERR_BYTES = 64 * 1024


class ProbeError(RuntimeError):
    """Missing or contradictory calibration evidence; never successful evidence."""


def validate_outcome(case: str, output: bytes) -> dict[str, Any]:
    from app.validators.kosit import KositValidator

    if case not in CASES or not 0 < len(output) <= RESULT_BYTES:
        raise ProbeError("Unknown case or output budget exceeded")
    value = json.loads(output)
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 2:
        raise ProbeError("Missing schema-2 analysis")
    assessment = value.get("assessment")
    official = assessment.get("official") if isinstance(assessment, dict) else None
    accepted = case.endswith("accept")
    expected = "accepted" if accepted else "rejected"
    if not isinstance(official, dict) or not all(
        official.get(name) is True for name in ("requested", "configured", "executed")
    ):
        raise ProbeError("Real official execution is not confirmed")
    raw = official.get("raw_report")
    if official.get("status") != expected or official.get("report_source") != "file" or not isinstance(raw, str):
        raise ProbeError("Expected file VARL outcome is missing")
    _, decision, label, valid = KositValidator._parse_report(raw.encode("utf-8"))
    if not valid or decision is not accepted or label != ("accept" if accepted else "reject"):
        raise ProbeError("Authoritative VARL decision disagrees")
    if not accepted and "BR-CL-01" not in raw:
        raise ProbeError("Synthetic invalid document code was not rejected by its expected rule")
    return {
        "executed": True,
        "status": expected,
        "varl_assessment": label,
        "report_source": "file",
        "exit_code": official.get("exit_code"),
        "raw_report_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "output_sha256": hashlib.sha256(output).hexdigest(),
        "output_size": len(output),
    }


def windows_job_counters(job: Any) -> dict[str, Any]:
    """Query the original job; JOBOBJECT_EXTENDED_LIMIT_INFORMATION uses bytes.

    https://learn.microsoft.com/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information
    PeakJobMemoryUsed includes the launcher AND inherited JVM. Worker peaks may
    precede its post-import limit reduction; they are not a runtime-only sample.
    """
    from app.processing.windows import _ExtendedLimits

    value = _ExtendedLimits()
    if not job._api.dll.QueryInformationJobObject(job.handle, 9, ctypes.byref(value), ctypes.sizeof(value), None):
        raise OSError("The original job counters could not be queried")
    expected = job._limits
    limits_verified = (
        value.BasicLimitInformation.LimitFlags == expected.flags
        and value.BasicLimitInformation.ActiveProcessLimit == expected.active_processes
        and value.JobMemoryLimit == expected.memory_bytes
        and value.ProcessMemoryLimit == (expected.process_memory_bytes or 0)
    )
    return {
        "status": "observed",
        "method": "QueryInformationJobObject original parent-owned handle immediately before normal close",
        "unit": "bytes",
        "metric": "private_commit",
        "peak_job_commit_bytes": int(value.PeakJobMemoryUsed),
        "peak_process_commit_bytes": int(value.PeakProcessMemoryUsed),
        "current_job_limit_bytes": int(value.JobMemoryLimit),
        "current_process_limit_bytes": int(value.ProcessMemoryLimit),
        "current_limit_flags": int(value.BasicLimitInformation.LimitFlags),
        "current_active_process_limit": int(value.BasicLimitInformation.ActiveProcessLimit),
        "active_processes_at_close": job.active_process_count(),
        "limits_verified": limits_verified,
        "coverage": "lifetime aggregate peak through normal close; includes larger worker startup profile; nested jobs overlap and must not be summed",
    }


def observe_job_close(job: Any, record: dict[str, Any]) -> None:
    original_close = job.close
    observed = False
    record["status"] = "unavailable_no_final_observation"

    def close() -> None:
        nonlocal observed
        with job._lock:
            try:
                if job._handle and not observed:
                    observed = True
                    try:
                        record.update(windows_job_counters(job))
                    except Exception as exc:
                        record.update({"status": "unavailable", "error_class": type(exc).__name__})
            finally:
                original_close()

    job.close = close


def memory_complete(roles: list[dict[str, Any]], jobs: list[dict[str, Any]], *, platform: str) -> bool:
    names = {"supervisor", "worker", "java"} | ({"watchdog"} if platform == "darwin" else set())
    if len(roles) != len(names) or {item.get("role") for item in roles} != names:
        return False
    metrics = ("peak_working_set_bytes", "peak_private_commit_bytes") if platform == "win32" else ("peak_rss_bytes",)
    if any(
        item.get("status") != "observed" or any(type(item.get(key)) is not int or item[key] <= 0 for key in metrics)
        for item in roles
    ):
        return False
    if platform == "win32":
        return (
            len(jobs) == 4
            and {item.get("role") for item in jobs} == {"outer", "supervisor", "worker", "java"}
            and all(
                item.get("status") == "observed"
                and item.get("limits_verified") is True
                and type(item.get("peak_job_commit_bytes")) is int
                and item["peak_job_commit_bytes"] > 0
                and type(item.get("peak_process_commit_bytes")) is int
                and item["peak_process_commit_bytes"] > 0
                and type(item.get("active_processes_at_close")) is int
                and item["active_processes_at_close"] == 0
                for item in jobs
            )
        )
    return platform in {"linux", "darwin"} and not jobs


def successful_child(report: dict[str, Any], *, returncode: int | None, forced_stop: str | None) -> bool:
    return (
        returncode == 0
        and forced_stop is None
        and report.get("passed") is True
        and report.get("cleanup_confirmed") is True
        and report.get("active_after") == 0
        and report.get("memory_observation_complete") is True
    )


def component_binding(vendor_root: Path, java: Path, archive: Path) -> dict[str, Any]:
    from scripts.test_kosit_components import DEFAULT_LOCK_FILE, sha256_file, verify_components

    if not java.is_absolute() or not java.is_file() or java.is_symlink():
        raise ProbeError("An explicit resolved Java executable is required")
    return {
        **verify_components(vendor_root, archive, DEFAULT_LOCK_FILE),
        "java_path": str(java),
        "java_sha256": sha256_file(java),
    }


async def run_case(case: str, vendor_root: Path, java: Path, archive: Path, raw_directory: Path) -> dict[str, Any]:
    from app.configuration import Settings
    from app.processing import manager as manager_module
    from app.processing.manager import ProcessingManager
    from app.upload_ingress import ReceivedUpload, UploadOptions
    from scripts.test_kosit_components import synthetic_cases

    if case not in CASES:
        raise ProbeError("Unknown fixed case")
    bound = component_binding(vendor_root, java, archive)
    settings = Settings(
        kosit_enabled=True,
        kosit_java_bin=str(java),
        kosit_validator_jar=Path(bound["validator_path"]),
        kosit_scenarios=(Path(bound["configuration_path"]) / "scenarios.xml",),
        kosit_repositories=(Path(bound["configuration_path"]),),
        kosit_timeout_seconds=60,
    )
    payload = synthetic_cases()[case]
    raw_directory.mkdir(mode=0o700)
    manager = ProcessingManager()
    lease: Any = None
    upload = ReceivedUpload(
        "analyze", f"synthetic-{case}.xml", "application/xml", UploadOptions(True, "complete"), memoryview(payload)
    )
    roles: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    children: list[Any] = []
    report: dict[str, Any] = {
        "case": case,
        "passed": False,
        "budgets": asdict(manager.budgets),
        "components": bound,
        "input_sha256": hashlib.sha256(payload).hexdigest(),
        "input_size": len(payload),
        "memory_roles": roles,
        "memory_jobs": jobs,
        "cold_start_scope": "fresh role processes; OS/filesystem caches are not flushed",
        "java_memory_scope": "POSIX exec preserves launcher PID; Windows job peak includes launcher and actual inherited JVM",
        "sum_scope": "role lifetime peaks are not simultaneous; outer and nested Windows job peaks overlap",
    }
    started = time.monotonic()
    original_spawn = manager_module.spawn_role
    outer_observed = False

    async def execute() -> Any:
        nonlocal lease
        # Bind the lease to this task, so a later failed-job shutdown cannot
        # cancel the separate task that is preserving the calibration report.
        lease = manager.try_acquire()
        if lease is None:
            raise ProbeError("Fresh manager could not acquire a lease")
        observe_ready(lease.ready, report, started=started)
        return await lease.run(upload, settings)

    def measured_spawn(role: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal outer_observed
        child = original_spawn(role, *args, **kwargs)
        children.append(child)
        item: dict[str, Any] = {"role": role}
        roles.append(item)
        try:
            observe_process(child.process, item, platform=sys.platform)
            if child.job is not None:
                job_record: dict[str, Any] = {"role": role, "pid": child.pid}
                jobs.append(job_record)
                observe_job_close(child.job, job_record)
            if lease.tree.outer_job is not None and not outer_observed:
                outer_observed = True
                outer_record: dict[str, Any] = {"role": "outer"}
                jobs.append(outer_record)
                observe_job_close(lease.tree.outer_job, outer_record)
        except Exception as exc:
            # Never hide a successfully created child from its actual owner.
            item.update({"status": "unavailable_observer_setup", "error_class": type(exc).__name__})
        return child

    manager_module.spawn_role = measured_spawn
    try:
        result = await asyncio.wait_for(execute(), timeout=75)
        if result.media_type != "application/json" or not 0 < result.body_size <= RESULT_BYTES:
            raise ProbeError("Unexpected result envelope or output budget")
        output = b"".join(result.chunks)
        if len(output) != result.body_size:
            raise ProbeError("Incomplete analysis output")
        with (raw_directory / "analysis.json").open("xb") as stream:
            stream.write(output)
        report["outcome"] = validate_outcome(case, output)
        if component_binding(vendor_root, java, archive) != bound:
            raise ProbeError("Prepared component bytes changed during the case")
        report["passed"] = True
    except BaseException as exc:
        report.update({"passed": False, "error_class": type(exc).__name__})
        diagnostic = getattr(exc, "diagnostic", None)
        if isinstance(diagnostic, dict):
            report["diagnostic"] = diagnostic
    finally:
        upload.close()
        # Completed jobs relinquish their response lease before shutdown. A
        # poisoned lease remains retained and fails; never cancel a completed
        # successful request merely to obtain its already-closed counters.
        if lease is not None:
            lease.release()
        try:
            manager.shutdown()
        except Exception as exc:
            report.update({"passed": False, "cleanup_error_class": type(exc).__name__})
        finally:
            manager_module.spawn_role = original_spawn
        if lease is not None:
            lease.release()
        report.update(
            {
                "active_after": manager.active_count,
                "cleanup_confirmed": lease is not None
                and lease.tree.cleaned
                and lease.released
                and not lease.poisoned
                and "cleanup_error_class" not in report
                and all(child.process.returncode is not None for child in children),
                "poisoned": lease.poisoned if lease is not None else False,
                "validated_ready_manifest": lease.ready_manifest if lease is not None else None,
                "ready_confirmed": lease.ready.is_set() if lease is not None else False,
                "role_returncodes": {str(child.pid): child.process.returncode for child in children},
                "memory_observation_complete": memory_complete(roles, jobs, platform=sys.platform),
                "harness_memory": harness_memory(),
                "ambient_settings_imported": "app.settings" in sys.modules,
                "elapsed_seconds": time.monotonic() - started,
            }
        )
        if (
            not report["cleanup_confirmed"]
            or not report["memory_observation_complete"]
            or report["ambient_settings_imported"]
            or not report["ready_confirmed"]
            or manager.active_count
        ):
            report["passed"] = False
    return report


def run_bounded(
    case: str, vendor_root: Path, java: Path, archive: Path, raw_directory: Path, *, deadline: float
) -> dict[str, Any]:
    from app.processing.native import child_environment, python_executable

    if case not in CASES or deadline <= time.monotonic():
        raise ProbeError("Unknown case or exhausted outer deadline")
    process = subprocess.Popen(
        [
            python_executable(),
            "-I",
            str(Path(__file__).resolve()),
            "--child-case",
            case,
            "--vendor-root",
            str(vendor_root),
            "--java",
            str(java),
            "--config-archive",
            str(archive),
            "--raw-directory",
            str(raw_directory),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_environment(),
        start_new_session=os.name == "posix",
    )
    output, errors = bytearray(), bytearray()
    overflow = threading.Event()
    readers: list[threading.Thread] = []
    waited = False
    forced_stop = None
    started = time.monotonic()

    def drain(stream: BinaryIO, target: bytearray, maximum: int) -> None:
        try:
            while chunk := stream.read(8192):
                remaining = maximum - len(target)
                target.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
                    process.kill()
        except OSError:
            overflow.set()
        finally:
            stream.close()

    try:
        assert process.stdout is not None and process.stderr is not None
        for stream, target, maximum in ((process.stdout, output, REPORT_BYTES), (process.stderr, errors, STDERR_BYTES)):
            reader = threading.Thread(target=drain, args=(stream, target, maximum), daemon=True)
            reader.start()
            readers.append(reader)
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic() - 5))
            waited = True
        except subprocess.TimeoutExpired:
            forced_stop = "outer_deadline"
    finally:
        if not waited:
            try:
                process.kill()
                process.wait(timeout=max(0.001, min(5, deadline - time.monotonic())))
            except (OSError, subprocess.TimeoutExpired):
                forced_stop = "harness_cleanup_unconfirmed"
        for reader in readers:
            reader.join(timeout=max(0, min(1, deadline - time.monotonic())))
    if overflow.is_set() or any(reader.is_alive() for reader in readers):
        forced_stop = "diagnostic_overflow_or_open_pipe"
    if time.monotonic() > deadline:
        forced_stop = "outer_deadline_exceeded"
    for suffix, raw in ((".stdout.txt", output), (".stderr.txt", errors)):
        with raw_directory.with_suffix(suffix).open("xb") as stream:
            stream.write(raw)
    try:
        report = json.loads(output)
        if not isinstance(report, dict):
            raise ValueError("Invalid case report")
    except (ValueError, UnicodeError):
        report = {"passed": False, "error_class": "InvalidCaseReport"}
    return {
        "case": case,
        "returncode": process.returncode,
        "forced_stop": forced_stop,
        "elapsed_seconds": time.monotonic() - started,
        "passed": successful_child(report, returncode=process.returncode, forced_stop=forced_stop),
        "report": report,
        "stdout_sha256": hashlib.sha256(output).hexdigest(),
        "stderr_sha256": hashlib.sha256(errors).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("vendor-root", "java", "config-archive"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child-case", choices=CASES, help=argparse.SUPPRESS)
    parser.add_argument("--raw-directory", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    started = time.monotonic()
    vendor, java, archive = (path.resolve(strict=True) for path in (args.vendor_root, args.java, args.config_archive))
    if args.child_case:
        if args.raw_directory is None or not args.raw_directory.is_absolute():
            parser.error("Fixed child requires an absolute evidence directory")
        report = asyncio.run(run_case(args.child_case, vendor, java, archive, args.raw_directory))
        print(json.dumps(report, separators=(",", ":"), allow_nan=False))
        return 0 if report["passed"] else 1
    if args.output is None:
        parser.error("--output is required")
    output = args.output.resolve()
    if output.exists():
        parser.error("Existing evidence must not be overwritten")
    raw_root = output.with_suffix("")
    raw_root.mkdir(mode=0o700)
    results: list[dict[str, Any]] = []
    report = {
        "schema_version": 1,
        "synthetic_only": True,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
        "executable": sys.executable,
        "case_seconds": CASE_SECONDS,
        "total_seconds": TOTAL_SECONDS,
        "cases": results,
        "source_binding_scope": "selected harness and processing modules; controller must bind complete build/workspace",
        "selected_source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                Path(__file__).resolve(),
                ROOT / "scripts/processing_smoke.py",
                ROOT / "scripts/test_kosit_components.py",
                ROOT / "app/configuration.py",
                *sorted((ROOT / "app/processing").glob("*.py")),
            ]
        },
    }
    try:
        bound = component_binding(vendor, java, archive)
        report["components"] = bound
        for case in CASES:
            deadline = min(started + TOTAL_SECONDS, time.monotonic() + CASE_SECONDS)
            item = run_bounded(case, vendor, java, archive, raw_root / case, deadline=deadline)
            results.append(item)
            if item["report"].get("components") != bound or not item["passed"]:
                item["passed"] = False
                break
        report["passed"] = len(results) == len(CASES) and all(item["passed"] for item in results)
    except Exception as exc:
        report.update({"passed": False, "error_class": type(exc).__name__})
    report["elapsed_seconds"] = time.monotonic() - started
    if report["elapsed_seconds"] > TOTAL_SECONDS:
        report["passed"] = False
    write_json(output, report)
    print(json.dumps({"passed": report["passed"], "evidence": str(output)}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
