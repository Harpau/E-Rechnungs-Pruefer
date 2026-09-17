"""Small synthetic Phase-A probes; never starts the application or Java.

All allocations and outputs have fixed independent ceilings. CPU work has an
independent three-second CPU ceiling, an alarm, and an outer process watchdog.
The liveness probes test kernel/pipe primitives with our own synthetic children;
they do not certify a production worker manager or its complete process tree.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import mmap
import os
import platform
import select
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MIB = 1024 * 1024
ALLOCATION_BYTES = 32 * MIB
AS_HEADROOM_BYTES = 8 * MIB
OUTPUT_LIMIT = 16 * 1024
OUTER_SECONDS = 9.0
OPERATION_CASES = {
    f"operation_{syntax}_{operation}": (syntax, operation)
    for syntax in ("cii", "ubl")
    for operation in ("analyze", "export_xml", "report_html", "report_pdf")
}
CASES = (
    "address_space",
    "heap",
    "cpu",
    "cpu_default_signal",
    "cpu_external_deadline",
    "kqueue_exit",
    "pipe_eof",
    "watcher_exit",
    "group_anchor",
    "operations",
    *OPERATION_CASES,
)
EXTERNAL_CPU_SECONDS = 1.5
PYTHON_HEADROOM_BYTES = 768 * MIB
PYTHON_START_HEADROOM_BYTES = 2 * 1024 * MIB
BASELINE_CEILING_BYTES = 64 * 1024 * MIB


class ProbeError(RuntimeError):
    pass


def emit(value: dict[str, Any]) -> None:
    payload = json.dumps(value, sort_keys=True, allow_nan=False)
    if len(payload.encode()) > 2048:
        raise ProbeError("A synthetic probe record exceeds its fixed output limit")
    print(payload, flush=True)


def virtual_bytes() -> int:
    if sys.platform == "darwin":
        # XNU osfmk/mach/task_info.h: MACH_TASK_BASIC_INFO=20, count in
        # natural_t words. Read only this process; no task_for_pid or ps.
        class BasicInfo(ctypes.Structure):
            _fields_ = [
                ("virtual_size", ctypes.c_uint64),
                ("resident_size", ctypes.c_uint64),
                ("resident_size_max", ctypes.c_uint64),
                ("user_seconds", ctypes.c_int32),
                ("user_microseconds", ctypes.c_int32),
                ("system_seconds", ctypes.c_int32),
                ("system_microseconds", ctypes.c_int32),
                ("policy", ctypes.c_int32),
                ("suspend_count", ctypes.c_int32),
            ]

        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        library.mach_task_self.argtypes = []
        library.mach_task_self.restype = ctypes.c_uint32
        library.task_info.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        library.task_info.restype = ctypes.c_int
        info = BasicInfo()
        count = ctypes.c_uint32(ctypes.sizeof(info) // ctypes.sizeof(ctypes.c_uint32))
        result = library.task_info(library.mach_task_self(), 20, ctypes.byref(info), ctypes.byref(count))
        if result != 0 or count.value != 12 or info.virtual_size <= 0:
            raise ProbeError(f"Unexpected own-task Mach result: {result}/{count.value}")
        return int(info.virtual_size)
    if sys.platform.startswith("linux"):
        pages = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[0])
        return pages * os.sysconf("SC_PAGE_SIZE")
    raise ProbeError("Native address-space measurement is unavailable on this platform")


def inventory() -> dict[str, Any]:
    import resource

    return {
        "system": platform.system(),
        "release": platform.release(),
        "kernel": platform.version(),
        "machine": platform.machine(),
        "macos": platform.mac_ver()[0],
        "python": sys.version,
        "executable": sys.executable,
        "base_prefix": sys.base_prefix,
        "uid": os.getuid(),
        "initial_rlimit_as": list(resource.getrlimit(resource.RLIMIT_AS)),
        "initial_rlimit_cpu": list(resource.getrlimit(resource.RLIMIT_CPU)),
    }


def child_safety() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGALRM, signal.SIGXCPU])
    signal.alarm(7)


def address_space_probe(*, heap: bool = False) -> None:
    import resource

    # This control warms the same allocation API and independently caps it at
    # 32 MiB even if the tested kernel limit is ineffective. No page is touched.
    with mmap.mmap(-1, ALLOCATION_BYTES):
        pass
    before = virtual_bytes()
    limit = before + AS_HEADROOM_BYTES
    emit({"phase": "before_limit", "virtual_bytes": before, "limit_bytes": limit})
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (OSError, ValueError) as exc:
        emit({"phase": "memory_result", "limit_set": False, "error": type(exc).__name__, "detail": str(exc)})
        return
    actual = list(resource.getrlimit(resource.RLIMIT_AS))
    with mmap.mmap(-1, MIB):
        pass
    observed = virtual_bytes()
    refused_errno: int | None = None
    try:
        if heap:
            payload = bytearray(ALLOCATION_BYTES)
            del payload
        else:
            with mmap.mmap(-1, ALLOCATION_BYTES):
                pass
    except OSError as exc:
        refused_errno = exc.errno
    except MemoryError:
        refused_errno = errno.ENOMEM
    emit(
        {
            "phase": "memory_result",
            "allocation_kind": "bytearray" if heap else "mmap",
            "limit_set": actual == [limit, limit],
            "actual_limit": actual,
            "virtual_before_attempt": observed,
            "allocation_bytes": ALLOCATION_BYTES,
            "headroom_bytes": limit - observed,
            "control_allocation_passed": True,
            "small_allocation_passed": True,
            "refused_errno": refused_errno,
            "enforced": refused_errno == errno.ENOMEM and observed + ALLOCATION_BYTES > limit,
        }
    )


def cpu_probe(*, ignore_soft_signal: bool = True) -> None:
    import resource

    # Ignore the soft-limit signal deliberately: prove the hard limit rather
    # than mistaking the default SIGXCPU action for hard-limit enforcement.
    signal.signal(signal.SIGXCPU, signal.SIG_IGN if ignore_soft_signal else signal.SIG_DFL)
    resource.setrlimit(resource.RLIMIT_CPU, (1, 2))
    emit({"phase": "cpu_ready", "limits": list(resource.getrlimit(resource.RLIMIT_CPU))})
    start = time.process_time()
    while time.process_time() - start < 3.0:
        sum(range(2000))
    emit({"phase": "cpu_limit_ineffective", "independent_cpu_ceiling_seconds": 3})


def peak_rss_bytes() -> int:
    import resource

    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(raw if sys.platform == "darwin" else raw * 1024)


def operations_probe(selection: tuple[str, str] | None = None) -> None:
    import resource

    baseline = virtual_bytes()
    if baseline > BASELINE_CEILING_BYTES:
        raise ProbeError("Trusted bootstrap baseline exceeds the fixed calibration ceiling")
    start_limit = baseline + PYTHON_START_HEADROOM_BYTES
    resource.setrlimit(resource.RLIMIT_AS, (start_limit, start_limit))
    signal.signal(signal.SIGXCPU, signal.SIG_DFL)
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    emit({"phase": "operation_start_limits", "baseline_virtual_bytes": baseline, "as_limit_bytes": start_limit})
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from app.configuration import Settings
    from app.processing.operations import execute_operation
    from app.processing.result import OperationLimits

    if "app.settings" in sys.modules or "app.main" in sys.modules:
        raise ProbeError("Processing imports unexpectedly loaded environment/web application configuration")
    imported_baseline = virtual_bytes()
    runtime_limit = imported_baseline + PYTHON_HEADROOM_BYTES
    if imported_baseline > BASELINE_CEILING_BYTES or runtime_limit > start_limit:
        raise ProbeError("Trusted processing imports leave insufficient headroom within the fixed start cap")
    resource.setrlimit(resource.RLIMIT_AS, (runtime_limit, runtime_limit))
    emit(
        {
            "phase": "operation_runtime_limits",
            "virtual_bytes": imported_baseline,
            "as_limit_bytes": runtime_limit,
            "peak_rss_bytes": peak_rss_bytes(),
        }
    )
    settings = Settings(kosit_enabled=False)
    limits = OperationLimits(json_bytes=MIB, html_bytes=MIB, pdf_bytes=2 * MIB, xml_bytes=65536)
    operations = ("analyze", "export_xml", "report_html", "report_pdf") if selection is None else (selection[1],)
    syntaxes = ("cii", "ubl") if selection is None else (selection[0],)
    for syntax in syntaxes:
        filename = f"{syntax}-rechnung-demo.xml"
        source = root / "app" / "examples" / filename
        data = source.read_bytes()
        if not 0 < len(data) <= 65536:
            raise ProbeError("Synthetic example exceeds the independent input ceiling")
        for operation in operations:
            started = time.monotonic()
            result = execute_operation(
                operation,
                data,
                filename,
                "application/xml",
                app_settings=settings,
                official=False,
                scope="complete",
                limits=limits,
            )
            if operation == "export_xml" and result.body != data:
                raise ProbeError("Synthetic XML export changed source bytes")
            if operation == "report_pdf" and not result.body.startswith(b"%PDF-"):
                raise ProbeError("Synthetic PDF rendering did not produce a PDF")
            emit(
                {
                    "phase": "operation_result",
                    "operation": operation,
                    "synthetic_fixture": filename,
                    "fixture_sha256": hashlib.sha256(data).hexdigest(),
                    "input_bytes": len(data),
                    "output_bytes": len(result.body),
                    "elapsed_seconds": time.monotonic() - started,
                    "virtual_bytes": virtual_bytes(),
                    "peak_rss_bytes": peak_rss_bytes(),
                }
            )
            del result
    emit(
        {
            "phase": "operations_complete",
            "count": len(operations) * len(syntaxes),
            "env_module_loaded": "app.settings" in sys.modules,
        }
    )


def read_record(stream: Any) -> dict[str, Any]:
    raw = stream.readline(2049)
    if not raw or len(raw) > 2048:
        raise ProbeError("Missing or oversized synthetic control record")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ProbeError("Synthetic record must be an object")
    return value


def command(role: str, *arguments: str) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--child", role, *arguments]


def watcher(control_fd: int, liveness_fd: int) -> None:
    with os.fdopen(control_fd, "rb", closefd=True) as control:
        configuration = read_record(control)
    mode = configuration["mode"]
    target_pid = configuration["target_pid"]
    leaf_pid = configuration["leaf_pid"]
    if mode not in {"kqueue_exit", "pipe_eof", "watcher_exit", "group_anchor"}:
        raise ProbeError("Unknown synthetic watch mode")
    if any(type(pid) is not int or pid <= 1 for pid in (target_pid, leaf_pid)):
        raise ProbeError("Invalid synthetic child identity")
    # The controller owns and does not reap either child before this watcher
    # exits, reserving these exact PIDs even if one becomes a zombie.
    if os.getppid() != configuration["controller_pid"]:
        raise ProbeError("Synthetic controller disappeared during startup")
    if mode == "group_anchor":
        if os.getpgrp() != target_pid or os.getpgrp() == configuration["controller_pgid"]:
            raise ProbeError("Watcher is not an anchor of the exact independent synthetic job group")
    queue = select.kqueue()
    try:
        event = select.kevent(
            target_pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD, fflags=select.KQ_NOTE_EXIT
        )
        queue.control([event], 0, 0)
        os.set_blocking(liveness_fd, False)
        emit({"phase": "watcher_ready", "pid": os.getpid(), "mode": mode})
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            events = queue.control(None, 1, 0.05)
            try:
                eof = os.read(liveness_fd, 1) == b""
            except BlockingIOError:
                eof = False
            kernel_exit = any(item.ident == target_pid and item.fflags & select.KQ_NOTE_EXIT for item in events)
            triggered = kernel_exit if mode in {"kqueue_exit", "group_anchor"} else eof
            if triggered and mode != "watcher_exit":
                if mode == "group_anchor":
                    emit({"phase": "watcher_group_termination", "pgid": os.getpgrp(), "kernel_exit": kernel_exit})
                    # This process itself reserves this PGID until the kill;
                    # no unrelated group can acquire it between check and use.
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                    raise ProbeError("Self-inclusive group kill unexpectedly returned")
                os.kill(leaf_pid, signal.SIGKILL)
                emit({"phase": "watcher_triggered", "kernel_exit": kernel_exit, "pipe_eof": eof, "mode": mode})
                return
        raise ProbeError("Independent watcher deadline expired")
    finally:
        queue.close()
        os.close(liveness_fd)


def stop_owned(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=1.0)


def finish_outer(process: subprocess.Popen[bytes], forced_stop: str | None, started: float) -> str | None:
    if forced_stop is None:
        try:
            process.wait(timeout=max(0.01, OUTER_SECONDS - (time.monotonic() - started)))
        except subprocess.TimeoutExpired:
            forced_stop = "outer_watchdog"
    # Never poll/reap the leader before a forced group kill: its unreaped PID
    # pins the process-group identity and prevents reuse by an unrelated group.
    if forced_stop is not None or process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=2.0)
    return forced_stop


def liveness_probe(mode: str) -> None:
    if sys.platform != "darwin":
        raise ProbeError("These kqueue/EOF peer-watcher probes target macOS")
    # Most cases stay in the outer probe group. The anchor case uses a separate
    # group; each sleeper has an independent six-second lifetime, and every
    # helper a seven-second alarm even if its controlling harness is killed.
    read_fd, write_fd = os.pipe()
    children: list[subprocess.Popen[bytes]] = []
    closed_write = False
    started = time.monotonic()
    try:
        target = subprocess.Popen(
            command("sleeper"),
            pass_fds=(write_fd,),
            stdout=subprocess.DEVNULL,
            process_group=0 if mode == "group_anchor" else None,
        )
        children.append(target)
        os.close(write_fd)
        closed_write = True
        leaf = None
        if mode != "group_anchor":
            leaf = subprocess.Popen(command("sleeper"), stdout=subprocess.DEVNULL)
            children.append(leaf)
        control_read, control_write = os.pipe()
        try:
            observer = subprocess.Popen(
                command("watcher", "--control-fd", str(control_read), "--liveness-fd", str(read_fd)),
                pass_fds=(control_read, read_fd),
                stdout=subprocess.PIPE,
                process_group=target.pid if mode == "group_anchor" else None,
            )
            children.append(observer)
        finally:
            os.close(control_read)
        with os.fdopen(control_write, "wb") as control:
            record = {
                "mode": mode,
                "target_pid": target.pid,
                "leaf_pid": leaf.pid if leaf is not None else target.pid,
                "controller_pid": os.getpid(),
                "controller_pgid": os.getpgrp(),
            }
            control.write(json.dumps(record).encode() + b"\n")
        assert observer.stdout is not None
        ready = read_record(observer.stdout)
        if ready.get("phase") != "watcher_ready":
            raise ProbeError("Watcher did not establish its native registration")
        if mode == "group_anchor":
            # Only after the guardian's READY can a synthetic processing role
            # join the job group. The parent/controller remains outside it.
            leaf = subprocess.Popen(command("sleeper"), stdout=subprocess.DEVNULL, process_group=target.pid)
            children.append(leaf)
        assert leaf is not None
        if mode == "watcher_exit":
            observer.kill()
            observer.wait(timeout=1.0)
            # Parent controller remains alive: it detects watcher loss and
            # stops its own known child, rather than claiming kernel auto-kill.
            leaf.kill()
            trigger = {"phase": "parent_detected_watcher_exit"}
        else:
            target.kill()
            # Do not reap target/leaf before the watcher has used their PIDs.
            trigger = read_record(observer.stdout)
            observer.wait(timeout=1.0)
            expected_phase = "watcher_group_termination" if mode == "group_anchor" else "watcher_triggered"
            expected_code = -signal.SIGKILL if mode == "group_anchor" else 0
            if trigger.get("phase") != expected_phase or observer.returncode != expected_code:
                raise ProbeError("Watcher did not complete the expected cleanup operation")
        leaf.wait(timeout=1.0)
        emit(
            {
                "phase": "liveness_result",
                "mode": mode,
                "target_pid": target.pid,
                "leaf_pid": leaf.pid,
                "watcher_pid": observer.pid,
                "leaf_returncode": leaf.returncode,
                "trigger": trigger,
                "elapsed_seconds": time.monotonic() - started,
                "scope": "synthetic_primitive_not_production_tree",
            }
        )
    finally:
        if not closed_write:
            os.close(write_fd)
        os.close(read_fd)
        for process in reversed(children):
            stop_owned(process)


def run_bounded(case: str) -> dict[str, Any]:
    if case not in CASES:
        raise ProbeError("Unknown public probe case")
    started = time.monotonic()
    process = subprocess.Popen(command(case), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    assert process.stdout is not None
    output = bytearray()
    forced_stop: str | None = None
    wall_deadline = EXTERNAL_CPU_SECONDS if case == "cpu_external_deadline" else OUTER_SECONDS
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map():
                if time.monotonic() - started >= wall_deadline:
                    forced_stop = "planned_wall_deadline" if case == "cpu_external_deadline" else "outer_watchdog"
                    break
                for key, _events in selector.select(timeout=0.1):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if len(output) + len(chunk) > OUTPUT_LIMIT:
                        forced_stop = "outer_output_cap"
                        break
                    output.extend(chunk)
                if forced_stop:
                    break
        finally:
            forced_stop = finish_outer(process, forced_stop, started)
            process.stdout.close()
    records: list[dict[str, Any]] = []
    for line in output.decode("utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return {
        "case": case,
        "returncode": process.returncode,
        "forced_stop": forced_stop,
        "wall_deadline_seconds": wall_deadline,
        "elapsed_seconds": time.monotonic() - started,
        "raw_output": output.decode("utf-8", errors="replace"),
        "records": records,
    }


def evaluate(result: dict[str, Any]) -> bool:
    if result["case"] == "cpu_external_deadline":
        return (
            result.get("forced_stop") == "planned_wall_deadline"
            and result["returncode"] == -signal.SIGKILL
            and result["records"] == [{"phase": "cpu_ready", "limits": [1, 2]}]
        )
    if result.get("forced_stop") is not None:
        return False
    records = result["records"]
    if result["case"] in {"cpu", "cpu_default_signal"}:
        expected_signal = signal.SIGKILL if result["case"] == "cpu" else signal.SIGXCPU
        return result["returncode"] == -expected_signal and records == [{"phase": "cpu_ready", "limits": [1, 2]}]
    if result["returncode"] != 0 or not records:
        return False
    last = records[-1]
    if result["case"] == "operations" or result["case"] in OPERATION_CASES:
        count = 8 if result["case"] == "operations" else 1
        return last == {"phase": "operations_complete", "count": count, "env_module_loaded": False}
    if result["case"] in {"address_space", "heap"}:
        return bool(last.get("limit_set") and last.get("enforced") and last.get("small_allocation_passed"))
    return last.get("phase") == "liveness_result" and last.get("leaf_returncode") == -signal.SIGKILL


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--child", choices=(*CASES, "sleeper", "watcher"), help=argparse.SUPPRESS)
    parser.add_argument("--control-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--liveness-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.name != "posix":
        parser.error("This bounded native probe currently targets POSIX")
    if args.child:
        child_safety()
        if args.child == "address_space":
            address_space_probe()
        elif args.child == "heap":
            address_space_probe(heap=True)
        elif args.child in {"cpu", "cpu_external_deadline"}:
            cpu_probe()
        elif args.child == "cpu_default_signal":
            cpu_probe(ignore_soft_signal=False)
        elif args.child == "operations":
            operations_probe()
        elif args.child in OPERATION_CASES:
            operations_probe(OPERATION_CASES[args.child])
        elif args.child == "sleeper":
            time.sleep(6.0)
        elif args.child == "watcher":
            watcher(args.control_fd, args.liveness_fd)
        else:
            liveness_probe(args.child)
        return 0
    if args.output is None:
        parser.error("--output is required; existing evidence is never overwritten")
    selected = args.case or list(CASES)
    if len(selected) != len(set(selected)):
        parser.error("Each bounded case can be selected only once")
    report: dict[str, Any] = {
        "schema_version": 1,
        "inventory": inventory(),
        "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limits": {
            "max_allocation_bytes": ALLOCATION_BYTES,
            "outer_seconds_per_case": OUTER_SECONDS,
            "output_cap": OUTPUT_LIMIT,
            "operation_baseline_ceiling_bytes": BASELINE_CEILING_BYTES,
            "operation_as_headroom_bytes": PYTHON_HEADROOM_BYTES,
            "operation_start_as_headroom_bytes": PYTHON_START_HEADROOM_BYTES,
            "operation_synthetic_input_cap": 65536,
            "operation_result_cap": 2 * MIB,
        },
        "scope": "phase_a_synthetic_primitives_not_production_activation",
        "cases": [],
    }
    # Exclusive creation reserves the output before running any probe.
    with args.output.open("x", encoding="utf-8") as evidence:
        for case in selected:
            result = run_bounded(case)
            result["passed"] = evaluate(result)
            report["cases"].append(result)
        report["passed"] = all(item["passed"] for item in report["cases"])
        json.dump(report, evidence, indent=2, allow_nan=False)
        evidence.write("\n")
    print(json.dumps({"output": str(args.output), "passed": report["passed"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
