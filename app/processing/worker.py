"""Invoice parsing role. Imports here must never start the web application."""

from __future__ import annotations

import sys
from typing import Any, BinaryIO

from .budgets import ProcessingBudgets
from .protocol import ProtocolError, read_control, read_payload, write_control, write_payload


def run_worker(incoming: BinaryIO, outgoing: BinaryIO, setup: dict[str, Any]) -> None:
    from ..configuration import settings_from_snapshot

    budgets = ProcessingBudgets(**setup["budgets"])
    if sys.platform != "win32":
        from .posix import apply_limits

        apply_limits(memory_headroom=budgets.python_start_memory_bytes, cpu_seconds=30)

    # All expensive trusted imports precede the smaller runtime profile and READY.
    from ..source import ProcessingLimitError
    from ..validators.kosit import KositValidator
    from ..xml_utils import InvoiceInputError
    from .operations import execute_operation

    settings = settings_from_snapshot(setup["settings"])
    if sys.platform != "win32":
        from .posix import seal_runtime_memory

        limits = seal_runtime_memory(memory_headroom=budgets.python_memory_bytes)
    else:
        # The creation-time worker Job is already active. Lower its commit limit
        # after imports through the parent before opening the input gate.
        limits = {"job_memory_bytes": budgets.python_start_memory_bytes}
    write_control(outgoing, {"type": "ready", "role": "worker", "protocol": 1, "limits": limits})
    request = read_control(incoming)
    if set(request) != {"type", "size"} or request["type"] != "input":
        raise ProtocolError("Rechnungseingabe erwartet.")
    data = read_payload(incoming, request["size"], maximum=settings.max_upload_bytes)
    write_control(outgoing, {"type": "input_received"})
    validator = KositValidator(settings)
    state = setup["official_state"]
    java_requested = False

    def official_validation(xml: bytes, filename: str) -> dict[str, Any]:
        nonlocal java_requested
        del filename
        if java_requested:
            raise ProtocolError("Mehrfache KoSIT-Anforderung.")
        java_requested = True
        if not state["configured"]:
            return validator._not_executed(state, summary="KoSIT ist nicht vollständig konfiguriert.")
        write_control(outgoing, {"type": "kosit", "size": len(xml)})
        write_payload(outgoing, xml)
        result = read_control(incoming)
        if result.get("type") == "java_failure":
            return validator._not_executed(
                state,
                summary="Die offizielle Prüfung konnte technisch nicht abgeschlossen werden.",
                message="Die begrenzte KoSIT-Verarbeitung wurde unterbrochen.",
                finding_id="KOSIT-EXEC",
            )
        if result.get("type") != "java_result":
            raise ProtocolError("KoSIT-Ergebnis erwartet.")
        returncode, console_overflow = result.get("returncode"), result.get("console_overflow")
        console_error = result.get("console_error")
        if (
            type(returncode) is not int
            or type(console_overflow) is not bool
            or (
                console_error is not None
                and (type(console_error) is not str or console_error != "console_capture_read_failed")
            )
        ):
            raise ProtocolError("Ungültiger KoSIT-Prozessstatus.")
        stdout = read_payload(incoming, result["stdout_size"], maximum=2 * 1024**2)
        stderr = read_payload(incoming, result["stderr_size"], maximum=2 * 1024**2)
        sizes = result.get("report_sizes")
        if not isinstance(sizes, list) or len(sizes) > 8:
            raise ProtocolError("Ungültige KoSIT-Berichtsliste.")
        candidates: list[bytes] = []
        remaining = 2 * 1024**2
        for size in sizes:
            candidate = read_payload(incoming, size, maximum=remaining)
            remaining -= len(candidate)
            candidates.append(candidate)
        if result.get("report_error"):
            return validator._not_executed(
                state,
                summary="Der offizielle Prüfbericht konnte technisch nicht vollständig gelesen werden.",
                message="Das KoSIT-Ausgabebudget oder der geschützte Dateizugriff wurde verletzt.",
                finding_id="KOSIT-EXEC",
            )
        selected = None
        from ..xml_utils import local_name

        for candidate in candidates:
            report = validator._parse_xml_root(candidate)
            if report is not None and local_name(report).lower() in {"report", "validationreport"}:
                selected = candidate
                break
        return validator.evaluate_execution(
            state,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            report_payload=selected,
            console_overflow=console_overflow,
            console_error=console_error,
        )

    try:
        if sys.platform != "win32":
            import signal

            signal.setitimer(signal.ITIMER_REAL, 0)
        result = execute_operation(
            setup["operation"],
            data,
            setup["filename"],
            setup["media_type"],
            app_settings=settings,
            official=setup["official"],
            scope=setup["scope"],
            official_validator=official_validation,
            official_state=state,
        )
        write_control(outgoing, {"type": "result", "result": result.metadata()})
        write_payload(outgoing, result.body)
    except (MemoryError, ProcessingLimitError) as exc:
        write_control(
            outgoing,
            {
                "type": "error",
                "status": 422,
                "error_type": "processing_limit_error",
                "detail": "Die Rechnung überschreitet das zulässige Verarbeitungsbudget.",
                "diagnostic": {"phase": "operation", "class": type(exc).__name__},
            },
        )
    except InvoiceInputError as exc:
        write_control(
            outgoing, {"type": "error", "status": 422, "error_type": "invoice_input_error", "detail": str(exc)[:4096]}
        )
    except Exception:
        write_control(
            outgoing,
            {
                "type": "error",
                "status": 500,
                "error_type": "processing_worker_error",
                "detail": "Die geschützte Rechnungsverarbeitung wurde unterbrochen.",
            },
        )
