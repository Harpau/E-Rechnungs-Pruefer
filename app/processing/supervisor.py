"""Trusted bounded broker. All processes and jobs belong to the backend parent."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any, BinaryIO

from ..configuration import settings_from_snapshot
from .budgets import ProcessingBudgets
from .native import child_environment, inherited_file
from .observation import validate_binding, validate_entering, validate_finished
from .protocol import (
    DATA_LIMIT,
    VERSION,
    FrameKind,
    ProtocolError,
    payload_length,
    read_control,
    read_frame,
    read_payload,
    write_control,
    write_frame,
    write_payload,
)
from .result import OperationLimits, result_limit, validate_result_metadata


def relay_payload(source: BinaryIO, destination: BinaryIO, size: object, maximum: int) -> None:
    remaining = payload_length(size, maximum)
    while remaining:
        kind, payload = read_frame(source)
        if kind is not FrameKind.DATA or len(payload) != min(DATA_LIMIT, remaining):
            raise ProtocolError("Ungültige Datensequenz.")
        write_frame(destination, kind, payload)
        remaining -= len(payload)
    if read_frame(source) != (FrameKind.END, b""):
        raise ProtocolError("Datensequenz ohne Abschluss.")
    write_frame(destination, FrameKind.END)


def run_java_launcher(
    incoming: BinaryIO, outgoing: BinaryIO, setup: dict[str, Any], stdout_handle: int, stderr_handle: int
) -> int:
    from .kosit_runtime import prepare_java_command

    budgets = ProcessingBudgets(**setup["budgets"])
    settings = settings_from_snapshot(setup["settings"])
    command = prepare_java_command(settings, Path(setup["temporary_directory"]), budgets)
    if sys.platform != "win32":
        from .posix import apply_limits

        limits = apply_limits(
            memory_headroom=budgets.java_memory_bytes, cpu_seconds=min(360, max(20, settings.kosit_timeout_seconds * 2))
        )
        signal.setitimer(signal.ITIMER_REAL, 0)
    else:
        limits = {"job_memory_bytes": budgets.java_memory_bytes}
    with inherited_file(stdout_handle, "wb") as stdout, inherited_file(stderr_handle, "wb") as stderr:
        write_control(outgoing, {"type": "ready", "role": "java", "protocol": VERSION, "limits": limits})
        if read_control(incoming) != {"type": "go"}:
            raise ProtocolError("KoSIT-Startfreigabe fehlt.")
        if sys.platform == "win32":
            # Atomic inherited Job membership covers the fixed JVM too. The
            # only outer/role job handles remain in the backend parent.
            # Detaching avoids an extra conhost job member; all three standard
            # streams are supplied explicitly and require no console.
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
                env=child_environment(),
                cwd=setup["temporary_directory"],
                creationflags=subprocess.DETACHED_PROCESS,
            )
            return process.wait(timeout=settings.kosit_timeout_seconds + 1)
        os.dup2(stdout.fileno(), 1)
        os.dup2(stderr.fileno(), 2)
        incoming.close()
        outgoing.close()
        os.chdir(setup["temporary_directory"])
        os.execve(command[0], command, child_environment())
    raise RuntimeError("Java wurde nicht gestartet.")


def run_supervisor(incoming: BinaryIO, outgoing: BinaryIO, setup: dict[str, Any], handles: list[int]) -> None:
    from .kosit_runtime import BoundedConsoleCapture, KositRuntimeError, read_execution, write_invoice

    budgets = ProcessingBudgets(**setup["budgets"])
    observation = validate_binding(setup.get("observation"))
    settings = settings_from_snapshot(setup["settings"])
    if len(handles) != (6 if setup["java_enabled"] else 2):
        raise ProtocolError("Das feste Rollenkanalinventar passt nicht zur Konfiguration.")
    if sys.platform != "win32":
        from .posix import apply_limits

        own_limits = apply_limits(memory_headroom=budgets.supervisor_memory_bytes, cpu_seconds=60)
        del own_limits["cpu_seconds"]
    else:
        own_limits = {"job_memory_bytes": budgets.supervisor_memory_bytes}
    startup = True
    phase = "startup"
    with ExitStack() as stack:
        worker_in = stack.enter_context(inherited_file(handles[0], "rb"))
        worker_out = stack.enter_context(inherited_file(handles[1], "wb"))
        try:
            write_control(worker_out, setup)
            worker_ready = read_control(worker_in)
            if worker_ready.get("type") != "ready" or worker_ready.get("role") != "worker":
                raise ProtocolError("Workerstart wurde nicht bestätigt.")
            java_ready = None
            java_out = None
            captures = []
            if setup["java_enabled"]:
                java_in = stack.enter_context(inherited_file(handles[2], "rb"))
                java_out = stack.enter_context(inherited_file(handles[3], "wb"))
                for handle in handles[4:]:
                    stream = stack.enter_context(inherited_file(handle, "rb"))
                    capture = BoundedConsoleCapture(stream)
                    capture.start()
                    captures.append(capture)
                write_control(java_out, setup)
                java_ready = read_control(java_in)
                if java_ready.get("type") != "ready" or java_ready.get("role") != "java":
                    raise ProtocolError("Java-Startprofil wurde nicht bestätigt.")
            if sys.platform != "win32":
                signal.setitimer(signal.ITIMER_REAL, 0)
            write_control(
                outgoing,
                {
                    "type": "ready",
                    "role": "supervisor",
                    "protocol": VERSION,
                    "limits": own_limits,
                    "worker": worker_ready,
                    "java": java_ready,
                    "children": setup["role_pids"],
                },
            )
            request = read_control(incoming)
            phase = "input"
            if set(request) != {"type", "size"} or request["type"] != "input":
                raise ProtocolError("Rechnungseingabe erwartet.")
            write_control(worker_out, request)
            relay_payload(incoming, worker_out, request["size"], settings.max_upload_bytes)
            if read_control(worker_in) != {"type": "input_received"}:
                raise ProtocolError("Rechnungseingang wurde nicht bestätigt.")
            write_control(outgoing, {"type": "input_received"})
            startup = False
            if observation is not None:
                entering = read_control(worker_in)
                validate_entering(entering, observation)
                write_control(outgoing, entering)
            response = read_control(worker_in)
            if response.get("type") == "kosit":
                phase = "java_input"
                if java_out is None or set(response) != {"type", "size"}:
                    raise ProtocolError("Unzulässige KoSIT-Anforderung.")
                xml = read_payload(worker_in, response["size"], maximum=settings.max_upload_bytes)
                write_invoice(Path(setup["temporary_directory"]), xml, maximum_bytes=settings.max_upload_bytes)
                del xml
                write_control(java_out, {"type": "go"})
                write_control(outgoing, {"type": "java_wait"})
                exit_status = read_control(incoming)
                phase = "java_output"
                if (
                    set(exit_status) != {"type", "returncode", "timed_out"}
                    or exit_status["type"] != "java_exit"
                    or type(exit_status["timed_out"]) is not bool
                    or type(exit_status["returncode"]) is not int
                ):
                    raise ProtocolError("Java-Prozessende wurde nicht bestätigt.")
                try:
                    if exit_status["timed_out"]:
                        write_control(worker_out, {"type": "java_failure"})
                    else:
                        captured: list[bytes] = []
                        console_error = None
                        for capture in captures:
                            try:
                                captured.append(capture.finish(timeout=2))
                            except KositRuntimeError as exc:
                                # finish raises this only after the drainer has
                                # ended. Java exit is already parent-confirmed;
                                # a separate safe VARL must remain authoritative.
                                if str(exc) != "console_capture_read_failed":
                                    raise
                                captured.append(b"")
                                console_error = "console_capture_read_failed"
                        execution = read_execution(
                            Path(setup["temporary_directory"]),
                            returncode=exit_status["returncode"],
                            stdout=captured[0],
                            stderr=captured[1],
                            console_overflow=any(capture.overflowed for capture in captures),
                            console_error=console_error,
                        )
                        write_control(worker_out, {"type": "java_result", **execution.metadata()})
                        write_payload(worker_out, execution.stdout)
                        write_payload(worker_out, execution.stderr)
                        for candidate in execution.report_candidates:
                            write_payload(worker_out, candidate)
                except (OSError, KositRuntimeError):
                    write_control(worker_out, {"type": "java_failure"})
                response = read_control(worker_in)
            if observation is not None:
                validate_finished(response, observation)
                write_control(outgoing, response)
                response = read_control(worker_in)
            phase = "result"
            if response.get("type") == "result":
                result = validate_result_metadata(
                    response["result"], operation=setup["operation"], expected_scope=setup["scope"]
                )
                write_control(outgoing, {"type": "result", "result": result})
                relay_payload(
                    worker_in, outgoing, result["body_size"], result_limit(setup["operation"], OperationLimits())
                )
            elif response.get("type") == "error":
                write_control(outgoing, response)
            else:
                raise ProtocolError("Unbekannte Workerantwort.")
            write_control(outgoing, {"type": "complete"})
            # The parent owns/reaps every native process and ends the group.
            incoming.read(1)
        except Exception as exc:
            try:
                write_control(
                    outgoing,
                    {
                        "type": "error",
                        "status": 503 if startup else 500,
                        "error_type": "processing_unavailable_error" if startup else "processing_worker_error",
                        "detail": "Die geschützte Rechnungsverarbeitung konnte nicht abgeschlossen werden.",
                        "diagnostic": {
                            "phase": phase,
                            "class": type(exc).__name__
                            if type(exc).__module__
                            in {"builtins", "app.processing.protocol", "app.processing.kosit_runtime"}
                            else "InternalError",
                        },
                    },
                )
            except Exception:
                pass
