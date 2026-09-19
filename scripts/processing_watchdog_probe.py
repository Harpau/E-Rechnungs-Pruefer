#!/usr/bin/env python3
"""Bounded Darwin startup diagnostic. No documents or production debug hooks.

Both contexts use the real native launcher, bootstrap, limits and watchdog.
Only this helper's watchdog command adds a private, bounded diagnostic pipe.
The inherited case must retain the caller's actual process group and session.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MAX_DIAGNOSTIC_BYTES = 4096
MAX_REPORT_BYTES = 16384
CASE_SECONDS = 8
OUTER_SECONDS = 15
CASES = ("inherited", "new_session")


class ProbeError(RuntimeError):
    """A fixed diagnostic contract failed."""


def error_record(error: BaseException) -> dict[str, object]:
    allowed = {"OSError", "MemoryError", "WatchdogError", "ProbeError", "ProtocolError", "ValueError", "TimeoutError"}
    kind = type(error).__name__
    number = getattr(error, "errno", None)
    return {"error_type": kind if kind in allowed else "other", "errno": number if type(number) is int else None}


def identity() -> dict[str, int]:
    return {"pid": os.getpid(), "ppid": os.getppid(), "pgid": os.getpgrp(), "sid": os.getsid(0)}


def limits() -> dict[str, list[int]]:
    import resource

    return {
        "as": list(resource.getrlimit(resource.RLIMIT_AS)),
        "nofile": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
    }


class DiagnosticEmitter:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.used = 0

    def emit(self, record: dict[str, object]) -> None:
        payload = json.dumps(record, separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"
        if self.used + len(payload) > MAX_DIAGNOSTIC_BYTES:
            raise ProbeError("Diagnostic output limit")
        self.used += len(payload)
        if os.write(self.descriptor, payload) != len(payload):
            raise ProbeError("Incomplete diagnostic record")


def watchdog_child(arguments: list[str]) -> int:
    """Test-only observer around the real bootstrap; no input interpretation."""
    if sys.platform != "darwin" or len(arguments) != 8:
        return 70
    descriptor = int(arguments[-1])
    os.set_inheritable(descriptor, False)
    os.set_blocking(descriptor, False)
    emitter = DiagnosticEmitter(descriptor)
    sys.path.insert(0, str(ROOT))
    from app.processing import bootstrap, posix, watchdog
    from app.processing.native import ROLE_FLAG

    emitted: set[str] = set()

    def observe(name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(function)
        def observed(*args: Any, **kwargs: Any) -> Any:
            first = name not in emitted
            if first:
                emitted.add(name)
                emitter.emit({"stage": name})
            try:
                value = function(*args, **kwargs)
            except BaseException as error:
                emitter.emit({"stage": name + "_failed", **error_record(error)})
                raise
            if name == "apply_limits":
                emitter.emit({"stage": "limits_applied", "proof": value, "rlimits": limits()})
            elif name == "send_ready":
                emitter.emit({"stage": "ready_sent"})
            return value

        return observed

    try:
        emitter.emit(
            {
                "stage": "bootstrap",
                "identity": identity(),
                "pipe_fds": [int(v) for v in arguments[1:3]],
                "rlimits": limits(),
            }
        )
        posix.apply_limits = observe("apply_limits", posix.apply_limits)
        for attribute, stage in (
            ("run_watchdog", "identity_validation"),
            ("_validate_pipes", "pipes"),
            ("_check_parent", "parent_binding"),
            ("_register", "kqueue_registration"),
            ("_send_ready", "send_ready"),
        ):
            setattr(watchdog, attribute, observe(stage, getattr(watchdog, attribute)))
        status = bootstrap.dispatch_if_requested([ROLE_FLAG, "watchdog", *arguments[:-1]])
        emitter.emit({"stage": "bootstrap_returned", "status": status})
        return 70 if status is None else status
    finally:
        os.close(descriptor)


def read_bounded(descriptor: int, maximum: int, deadline: float) -> bytes:
    chunks: list[bytes] = []
    used = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
            raise ProbeError("Bounded read deadline")
        chunk = os.read(descriptor, min(4096, maximum + 1 - used))
        if not chunk:
            return b"".join(chunks)
        used += len(chunk)
        if used > maximum:
            raise ProbeError("Bounded read output limit")
        chunks.append(chunk)


def run_case(case: str) -> dict[str, Any]:
    if sys.platform != "darwin" or case not in CASES:
        raise ProbeError("Unsupported native case")
    sys.path.insert(0, str(ROOT))
    from app.processing import native
    from app.processing.protocol import read_control

    report: dict[str, Any] = {"case": case, "identity": identity(), "ready": False, "cleanup_confirmed": False}
    tree = native.ProcessTree()
    prepared: native.PreparedRole | None = None
    original_command = native.role_command
    diagnostic_read, diagnostic_write = os.pipe()
    writer: int | None = diagnostic_write

    def observed_command(role: str, arguments: Any) -> list[str]:
        if role == "watchdog":
            return [sys.executable, "-I", str(Path(__file__).resolve()), "--watchdog-child", *arguments]
        return original_command(role, arguments)

    def expired(_signum: int, _frame: object) -> None:
        raise ProbeError("Native case deadline")

    previous_alarm = signal.signal(signal.SIGALRM, expired)
    signal.alarm(CASE_SECONDS)
    try:
        # The real supervisor bootstrap requires the two valid broker handles,
        # even though this no-input probe never sends its initial setup frame.
        prepared = native.PreparedRole()
        supervisor = native.spawn_role("supervisor", group=0, extra_fds=prepared.broker_descriptors())
        tree.install(supervisor)
        native.role_command = observed_command
        watcher = native.spawn_role(
            "watchdog",
            group=supervisor.pid,
            arguments=(str(os.getpgrp()), str(supervisor.pid), str(os.getsid(0)), str(time.monotonic() + 5)),
            extra_fds=(diagnostic_write,),
        )
        tree.install_watchdog(watcher)
        writer = None
        os.close(diagnostic_write)
        ready = read_control(watcher.incoming)
        expected = {
            "type": "ready",
            "role": "watchdog",
            "protocol": 2,
            "pid": watcher.pid,
            "supervisor_pid": supervisor.pid,
            "parent_pid": os.getpid(),
        }
        if ready != expected:
            raise ProbeError("Invalid native READY")
        report["ready"] = True
        report["ready_record"] = ready
        report["role_bindings"] = {
            role: {"pid": child.pid, "pgid": os.getpgid(child.pid), "sid": os.getsid(child.pid)}
            for role, child in (("supervisor", supervisor), ("watchdog", watcher))
        }
        if any(
            value["pgid"] != supervisor.pid or value["sid"] != os.getsid(0)
            for value in report["role_bindings"].values()
        ):
            raise ProbeError("Changed native role binding")
    except BaseException as error:
        report["error"] = error_record(error)
    finally:
        native.role_command = original_command
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_alarm)
        try:
            report["cleanup_confirmed"] = tree.cleanup(3)
            report["child_exit_codes"] = {
                role: child.process.returncode
                for role, child in (("supervisor", tree.supervisor), ("watchdog", tree.watchdog))
                if child is not None
            }
        except BaseException as error:
            report["cleanup_error"] = error_record(error)
        if prepared is not None:
            try:
                prepared.close()
            except BaseException as error:
                report["cleanup_confirmed"] = False
                report["cleanup_error"] = error_record(error)
        try:
            if writer is not None:
                os.close(writer)
            raw = read_bounded(diagnostic_read, MAX_DIAGNOSTIC_BYTES, time.monotonic() + 1)
            report["diagnostics"] = [json.loads(line) for line in raw.splitlines()]
        except BaseException as error:
            report["diagnostic_error"] = error_record(error)
        finally:
            os.close(diagnostic_read)
    return report


def run_bounded(case: str) -> dict[str, Any]:
    if case not in CASES:
        raise ProbeError("Unknown case")
    started = time.monotonic()
    with subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve()), "--case", case],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=case == "new_session",
        close_fds=True,
    ) as process:
        try:
            assert process.stdout is not None
            raw = read_bounded(process.stdout.fileno(), MAX_REPORT_BYTES, started + OUTER_SECONDS)
            code = process.wait(timeout=max(0.001, started + OUTER_SECONDS - time.monotonic()))
            value = json.loads(raw)
            if code != 0 or not isinstance(value, dict):
                raise ProbeError("Missing child report")
            value["elapsed_seconds"] = time.monotonic() - started
            return value
        except BaseException as error:
            # Only our held direct process is targeted, never the inherited CI group.
            process.kill()
            process.wait(timeout=2)
            return {"case": case, "cleanup_confirmed": False, "outer_error": error_record(error)}


def case_passes(value: dict[str, Any], case: str, caller: dict[str, int]) -> bool:
    context = value.get("identity", {})
    stages = {record.get("stage") for record in value.get("diagnostics", [])}
    if not isinstance(context, dict) or any(
        type(context.get(key)) is not int or not minimum <= context[key] < 2**31
        for key, minimum in (("pid", 2), ("ppid", 2), ("pgid", 0), ("sid", 0))
    ):
        return False
    if any(key in value for key in ("error", "cleanup_error", "diagnostic_error", "outer_error")):
        return False
    context_matches = (
        context.get("pgid") == caller["pgid"] and context.get("sid") == caller["sid"]
        if case == "inherited"
        else context.get("pgid") == context.get("sid") == context.get("pid")
    )
    return bool(
        value.get("case") == case
        and value.get("ready") is True
        and value.get("cleanup_confirmed") is True
        and context_matches
        and {"limits_applied", "send_ready"} <= stages
    )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    if sys.platform != "darwin":
        raise ProbeError("Native macOS required")
    with args.output.open("x", encoding="utf-8") as destination:
        caller = identity()
        report: dict[str, Any] = {
            "schema_version": 1,
            "scope": "document-free native watchdog startup",
            "caller": caller,
            "bounds": {
                "case_seconds": CASE_SECONDS,
                "outer_seconds": OUTER_SECONDS,
                "max_diagnostic_bytes": MAX_DIAGNOSTIC_BYTES,
                "max_case_report_bytes": MAX_REPORT_BYTES,
            },
            "cases": [],
            "passed": False,
        }
        for case in CASES:
            result = run_bounded(case)
            result["passed"] = case_passes(result, case, caller)
            report["cases"].append(result)
            if result.get("cleanup_confirmed") is not True:
                break
        report["passed"] = len(report["cases"]) == len(CASES) and all(item["passed"] for item in report["cases"])
        json.dump(report, destination, indent=2, allow_nan=False)
        destination.write("\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--watchdog-child"]:
        raise SystemExit(watchdog_child(sys.argv[2:]))
    if len(sys.argv) == 3 and sys.argv[1] == "--case":
        record = json.dumps(run_case(sys.argv[2]), allow_nan=False).encode("ascii")
        if len(record) > MAX_REPORT_BYTES:
            raise SystemExit(70)
        sys.stdout.buffer.write(record)
        raise SystemExit(0)
    raise SystemExit(main())
