"""Bounded synthetic native-manager calibration; never a VM/install controller.

Each fixed case runs in a fresh harness process. Its independent outer deadline
terminates that process, so the product's parent-death bindings must also run.
A forced stop is always failed evidence, never a successful limit proof. Only
repository examples and generated synthetic bytes are accepted as inputs.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import ctypes
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zlib
from dataclasses import asdict
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LARGE_LINES = 128
CASES = {"small": 15, "xml_25mib": 60, "cii_large": 60, "ubl_large": 60, "hybrid_pdf": 60, "parallel": 60}
NS = {
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}


def rss_bytes(value: int, platform: str) -> int:
    if type(value) is not int or value < 0 or platform not in {"darwin", "linux"}:
        raise ValueError("Unknown native RSS unit or invalid counter")
    return value if platform == "darwin" else value * 1024


def windows_memory_counters(handle: int, *, query: Any = None) -> dict[str, Any]:
    """Read process counters only; never duplicate or inspect a product job handle.

    Microsoft PROCESS_MEMORY_COUNTERS_EX: all nine SIZE_T fields are bytes.
    PeakPagefileUsage is lifetime peak private commit, not disk bytes or AS.
    """

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_uint32), ("page_faults", ctypes.c_uint32)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak_working_set",
                "working_set",
                "peak_paged_pool",
                "paged_pool",
                "peak_nonpaged_pool",
                "nonpaged_pool",
                "pagefile",
                "peak_commit",
                "private",
            )
        ]

    if query is None:
        if sys.platform != "win32":
            raise OSError("Windows process counters unavailable on this platform")
        library = ctypes.WinDLL("kernel32", use_last_error=True)
        query = library.K32GetProcessMemoryInfo
        query.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        query.restype = ctypes.c_int
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not query(handle, ctypes.byref(counters), counters.cb):
        raise OSError("GetProcessMemoryInfo did not return bound process counters")
    return {
        "status": "observed",
        "method": "K32GetProcessMemoryInfo",
        "unit": "bytes",
        "limit_verified": False,
        "peak_working_set_bytes": int(counters.peak_working_set),
        "peak_private_commit_bytes": int(counters.peak_commit),
        "sample_working_set_bytes": int(counters.working_set),
        "sample_private_commit_bytes": int(counters.private),
        "coverage": "kernel lifetime peak through this query; not a job limit or virtual-address-space value",
    }


def observe_process(process: Any, record: dict[str, Any], *, platform: str) -> None:
    """Observe only the normal close/reap of a directly created child."""
    record.update({"pid": process.pid, "status": "unavailable_no_final_observation", "unit": "bytes"})
    if platform == "win32":
        original_close = process.close

        def close() -> None:
            with process._lock:
                if process._handle:
                    try:
                        record.update(windows_memory_counters(process._handle))
                    except Exception as exc:
                        record.update({"status": "unavailable_final_query", "error_class": type(exc).__name__})
                original_close()

        process.close = close
    elif platform in {"darwin", "linux"}:
        original_wait = process._try_wait
        wait4 = getattr(os, "wait4", None)
        if wait4 is None:
            raise OSError("Native POSIX reaping counters are unavailable")

        def native_wait(flags: int) -> tuple[int, int]:
            try:
                pid, status, usage = wait4(process.pid, flags)
            except ChildProcessError:
                record["status"] = "unavailable_already_reaped"
                return cast(tuple[int, int], original_wait(flags))
            if pid == process.pid:
                try:
                    record.update(
                        {
                            "status": "observed",
                            "method": "wait4 at normal Popen reaping",
                            "limit_verified": False,
                            "peak_rss_bytes": rss_bytes(usage.ru_maxrss, platform),
                            "user_cpu_seconds": usage.ru_utime,
                            "system_cpu_seconds": usage.ru_stime,
                            "coverage": "kernel lifetime peak RSS; not a virtual-address-space limit or simultaneous total",
                        }
                    )
                except Exception as exc:
                    record.update({"status": "unavailable_metric_validation", "error_class": type(exc).__name__})
            return pid, status

        process._try_wait = native_wait


def observe_ready(event: Any, record: dict[str, Any], *, started: float) -> None:
    original_set = event.set

    def ready() -> None:
        record.setdefault("ready_seconds", time.monotonic() - started)
        original_set()

    event.set = ready


def harness_memory() -> dict[str, Any]:
    try:
        if sys.platform == "win32":
            value = windows_memory_counters(-1)
        else:
            import resource

            value = {
                "status": "observed",
                "method": "getrusage(RUSAGE_SELF)",
                "unit": "bytes",
                "peak_rss_bytes": rss_bytes(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, sys.platform),
            }
        return {
            **value,
            "scope": "test harness including synthetic input construction and output verification; not production frontend RSS",
        }
    except (OSError, ValueError) as exc:
        return {"status": "unavailable", "error_class": type(exc).__name__}


def maximum_xml() -> bytes:
    prefix, suffix = b'<?xml version="1.0"?>\n<synthetic>', b"</synthetic>"
    return prefix + b" " * (25 * 1024**2 - len(prefix) - len(suffix)) + suffix


def _set(root: ET.Element, path: str, value: str) -> None:
    target = root.find(path, NS)
    if target is None:
        raise ValueError("The bound synthetic example structure changed")
    target.text = value


def large_example(syntax: str) -> bytes:
    if syntax not in {"cii", "ubl"}:
        raise ValueError("Only fixed repository examples are permitted")
    root = ET.fromstring((ROOT / "app" / "examples" / f"{syntax}-rechnung-demo.xml").read_bytes())
    if syntax == "cii":
        transaction = root.find("rsm:SupplyChainTradeTransaction", NS)
        assert transaction is not None
        lines = transaction.findall("ram:IncludedSupplyChainTradeLineItem", NS)
        template = copy.deepcopy(lines[0])
        for line in lines:
            transaction.remove(line)
        for index in range(1, LARGE_LINES + 1):
            line = copy.deepcopy(template)
            _set(line, ".//ram:LineID", str(index))
            _set(line, ".//ram:ChargeAmount", "100.00")
            _set(line, ".//ram:BilledQuantity", "1")
            _set(line, ".//ram:LineTotalAmount", "100.00")
            transaction.insert(index - 1, line)
        settlement = transaction.find("ram:ApplicableHeaderTradeSettlement", NS)
        assert settlement is not None
        for name, value in {
            "CalculatedAmount": "2432.00",
            "BasisAmount": "12800.00",
            "LineTotalAmount": "12800.00",
            "TaxBasisTotalAmount": "12800.00",
            "TaxTotalAmount": "2432.00",
            "GrandTotalAmount": "15232.00",
            "DuePayableAmount": "15232.00",
        }.items():
            _set(settlement, f".//ram:{name}", value)
    else:
        lines = root.findall("cac:InvoiceLine", NS)
        template = copy.deepcopy(lines[0])
        for line in lines:
            root.remove(line)
        for index in range(1, LARGE_LINES + 1):
            line = copy.deepcopy(template)
            _set(line, "cbc:ID", str(index))
            root.append(line)
        _set(root, "cac:TaxTotal/cbc:TaxAmount", "2432.00")
        _set(root, "cac:TaxTotal/cac:TaxSubtotal/cbc:TaxAmount", "2432.00")
        _set(root, "cac:TaxTotal/cac:TaxSubtotal/cbc:TaxableAmount", "12800.00")
        for name in ("LineExtensionAmount", "TaxExclusiveAmount", "TaxInclusiveAmount", "PayableAmount"):
            _set(
                root,
                f"cac:LegalMonetaryTotal/cbc:{name}",
                "12800.00" if name in {"LineExtensionAmount", "TaxExclusiveAmount"} else "15232.00",
            )
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def hybrid_example() -> tuple[bytes, bytes, dict[str, int]]:
    """Fixed synthetic hybrid container; this is not a PDF/A certification case."""
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject

    source = large_example("cii")
    end = source.index(b"?>") + 2
    marker = b"SYNTHETIC_HYBRID_CALIBRATION "
    comment_size = 2 * 1024**2 - len(source) - 7
    comment = marker + b" " * (comment_size - len(marker))
    xml = source[:end] + b"<!--" + comment + b"-->" + source[end:]
    assert len(xml) == 2 * 1024**2
    compressed = zlib.compress(xml)
    encoded = compressed.hex().encode("ascii") + b">"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    attachment = writer.add_attachment("factur-x.xml", encoded)
    files = cast(DictionaryObject, attachment.pdf_object["/EF"])
    embedded = cast(DictionaryObject, files["/F"])
    embedded[NameObject("/Filter")] = ArrayObject([NameObject("/ASCIIHexDecode"), NameObject("/FlateDecode")])
    output = BytesIO()
    writer.write(output)
    pdf = output.getvalue()
    return (
        pdf,
        xml,
        {
            "xml_bytes": len(xml),
            "pdf_bytes": len(pdf),
            "ascii_hex_input_bytes": len(encoded),
            "after_ascii_hex_bytes": len(compressed),
            "after_flate_bytes": len(xml),
        },
    )


def validate_output(
    operation: str, source: bytes, output: bytes, media_type: str, expected_lines: int | None
) -> dict[str, Any]:
    record: dict[str, Any] = {"output_size": len(output), "output_sha256": hashlib.sha256(output).hexdigest()}
    expected_media = {
        "export_xml": "application/xml",
        "analyze": "application/json",
        "report_html": "text/html",
        "report_pdf": "application/pdf",
    }
    if media_type != expected_media[operation] or not output:
        raise ValueError("Unexpected native result envelope")
    if operation == "export_xml":
        if output != source:
            raise ValueError("Native XML export changed original bytes")
        record["byte_identical"] = True
    elif operation == "analyze":
        analysis = json.loads(output)
        if analysis.get("schema_version") != 2 or len(analysis["lines"]) != expected_lines:
            raise ValueError("Native analysis did not preserve synthetic line count")
        record["line_count"] = len(analysis["lines"])
    elif operation == "report_pdf":
        if not output.startswith(b"%PDF-") or b"%%EOF" not in output[-1024:]:
            raise ValueError("Native PDF output is incomplete")
    elif b"<html" not in output[:1024].lower() or b"</html>" not in output[-1024:].lower():
        raise ValueError("Native HTML output is incomplete")
    return record


def successful_child(report: dict[str, Any], *, returncode: int, forced_stop: str | None) -> bool:
    return (
        returncode == 0
        and forced_stop is None
        and report.get("passed") is True
        and report.get("cleanup_confirmed") is True
        and report.get("active_after") == 0
    )


def write_json(path: Path, report: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(report, output, indent=2, ensure_ascii=True, allow_nan=False)
        output.write("\n")


async def run_case(case: str, raw_directory: Path) -> dict[str, Any]:
    from app.configuration import Settings
    from app.processing import manager as manager_module
    from app.processing.manager import ProcessingManager
    from app.upload_ingress import Operation, ReceivedUpload, UploadOptions

    manager = ProcessingManager()
    raw_directory.mkdir(mode=0o700)
    operations = ("analyze", "report_html", "report_pdf")
    specs: list[tuple[str, str, bytes, int | None]]
    embedded_xml: bytes | None = None
    hybrid_sizes: dict[str, int] | None = None
    if case == "small":
        specs = [
            (operation, "cii", (ROOT / "app/examples/cii-rechnung-demo.xml").read_bytes(), 6)
            for operation in ("export_xml", "analyze")
        ]
    elif case == "xml_25mib":
        specs = [("export_xml", "synthetic", maximum_xml(), None)]
    elif case in {"cii_large", "ubl_large"}:
        syntax = case[:3]
        payload = large_example(syntax)
        specs = [(operation, syntax, payload, LARGE_LINES) for operation in operations]
    elif case == "hybrid_pdf":
        payload, embedded_xml, hybrid_sizes = hybrid_example()
        specs = [(operation, "cii", payload, LARGE_LINES) for operation in ("export_xml", *operations)]
    elif case == "parallel":
        specs = [
            ("report_html", "cii", large_example("cii"), LARGE_LINES),
            ("report_pdf", "ubl", large_example("ubl"), LARGE_LINES),
        ]
    else:
        raise ValueError("Unknown fixed case")
    records: list[dict[str, Any]] = []
    memory: list[dict[str, Any]] = []
    held = []
    report: dict[str, Any] = {
        "case": case,
        "budgets": asdict(manager.budgets),
        "passed": False,
        "jobs": records,
        "peak_acquired_leases": 0,
        "memory_before_jobs": harness_memory(),
        "memory_roles": memory,
        "cold_start_scope": "fresh role processes; OS/filesystem/antivirus caches are not flushed",
        "hybrid_filter_sizes": hybrid_sizes,
        "hybrid_scope": "synthetic blank-page PDF with embedded CII; no PDF/A certification" if hybrid_sizes else None,
    }

    async def one(index: int, spec: tuple[Any, ...], lease: Any) -> None:
        operation, syntax, payload, lines = spec
        upload = ReceivedUpload(
            cast(Operation, operation),
            f"synthetic-{syntax}.pdf" if embedded_xml is not None else f"synthetic-{syntax}.xml",
            "application/pdf" if embedded_xml is not None else "application/xml",
            UploadOptions(False, "complete"),
            memoryview(payload),
        )
        started = time.monotonic()
        record: dict[str, Any] = {
            "operation": operation,
            "syntax": syntax,
            "scope": "complete",
            "input_size": len(payload),
            "input_sha256": hashlib.sha256(payload).hexdigest(),
        }
        records.append(record)
        observe_ready(lease.ready, record, started=started)
        try:
            result = await lease.run(upload, Settings())
            output = b"".join(result.chunks)
            record.update(
                validate_output(
                    operation, embedded_xml if embedded_xml is not None else payload, output, result.media_type, lines
                )
            )
            if operation.startswith("report_") and result.headers.get("X-Einvoice-Report-Scope") != "complete":
                raise ValueError("Report scope changed")
            extension = {"analyze": "json", "export_xml": "xml", "report_html": "html", "report_pdf": "pdf"}[operation]
            artifact = raw_directory / f"{index:02d}-{syntax}-{operation}.{extension}"
            with artifact.open("xb") as target:
                target.write(output)
            record.update({"artifact": str(artifact), "headers": result.headers, "passed": True})
        except BaseException as exc:
            record.update({"passed": False, "error_class": type(exc).__name__})
            if exc.__cause__ is not None:
                record["cause_class"] = type(exc.__cause__).__name__
                record["cause_detail"] = str(exc.__cause__)[:1024]
            diagnostic = getattr(exc, "diagnostic", None)
            if isinstance(diagnostic, dict):
                record["diagnostic"] = diagnostic
            for key in ("status", "error_type", "detail"):
                value = getattr(exc, key, None)
                if type(value) in (int, str):
                    record[f"error_{key}"] = value
            raise
        finally:
            upload.close()
            lease.release()
            record.update(
                {
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                    "tree_cleaned": lease.tree.cleaned,
                    "lease_released": lease.released,
                    "poisoned": lease.poisoned,
                    "supervisor_pid": lease.tree.supervisor.pid if lease.tree.supervisor else None,
                    "watchdog_pid": lease.tree.watchdog.pid if lease.tree.watchdog else None,
                    "ready_confirmed": lease.ready.is_set(),
                    "validated_ready_manifest": lease.ready_manifest,
                    "role_returncodes": {role: child.process.returncode for role, child in lease.tree.roles.items()},
                }
            )

    async def jobs() -> None:
        if case == "parallel":
            for _ in specs:
                lease = manager.try_acquire()
                if lease is None:
                    raise ValueError("Two fixed leases could not be acquired")
                held.append(lease)
            report["peak_acquired_leases"] = manager.active_count
            third = manager.try_acquire()
            if third is not None:
                held.append(third)
                raise ValueError("Third lease bypassed capacity")
            report["third_lease_rejected"] = True
            outcomes = await asyncio.gather(
                *(one(index, spec, held[index]) for index, spec in enumerate(specs)), return_exceptions=True
            )
            if any(isinstance(value, BaseException) for value in outcomes):
                raise ValueError("At least one parallel job failed")
        else:
            for index, spec in enumerate(specs):
                lease = manager.try_acquire()
                if lease is None:
                    raise ValueError("Fixed job could not acquire capacity")
                held.append(lease)
                report["peak_acquired_leases"] = max(report["peak_acquired_leases"], manager.active_count)
                await one(index, spec, lease)

    original_spawn = manager_module.spawn_role

    def measured_spawn(role: str, *args: Any, **kwargs: Any) -> Any:
        child = original_spawn(role, *args, **kwargs)
        item: dict[str, Any] = {"role": role}
        memory.append(item)
        try:
            observe_process(child.process, item, platform=sys.platform)
        except Exception as exc:
            # Instrumentation cannot hide a created child from its real owner.
            item.update({"status": "unavailable_observer_setup", "error_class": type(exc).__name__})
        return child

    manager_module.spawn_role = measured_spawn
    try:
        await asyncio.wait_for(jobs(), timeout=CASES[case] - 5)
        report["passed"] = True
    except BaseException as exc:
        report.update({"passed": False, "error_class": type(exc).__name__})
    finally:
        try:
            manager.shutdown()
        finally:
            manager_module.spawn_role = original_spawn
        for lease in held:
            lease.release()
        report["active_after"] = manager.active_count
        report["cleanup_confirmed"] = all(
            lease.tree.cleaned and lease.released and not lease.poisoned for lease in held
        )
        report["ambient_settings_imported"] = "app.settings" in sys.modules
        report["memory_after_jobs"] = harness_memory()
        report["memory_observation_complete"] = bool(memory) and all(item["status"] == "observed" for item in memory)
        if report["ambient_settings_imported"] or not report["cleanup_confirmed"] or manager.active_count:
            report["passed"] = False
    return report


def run_bounded(case: str, raw_directory: Path) -> dict[str, Any]:
    from app.processing.native import child_environment, python_executable

    process = subprocess.Popen(
        [
            python_executable(),
            "-I",
            str(Path(__file__).resolve()),
            "--child-case",
            case,
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

    def drain(stream: BinaryIO, target: bytearray) -> None:
        try:
            while chunk := stream.read(64 * 1024):
                remaining = 64 * 1024 - len(target)
                target.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
                    process.kill()
        finally:
            stream.close()

    started = time.monotonic()
    forced_stop = None
    readers: list[threading.Thread] = []
    child_waited = False
    try:
        assert process.stdout is not None and process.stderr is not None
        for stream, target in ((process.stdout, output), (process.stderr, errors)):
            reader = threading.Thread(target=drain, args=(stream, target), daemon=True)
            reader.start()
            readers.append(reader)
        try:
            process.wait(timeout=CASES[case])
            child_waited = True
        except subprocess.TimeoutExpired:
            forced_stop = "outer_deadline"
    finally:
        # Also runs for KeyboardInterrupt and unexpected wrapper failures.
        # Popen owns only this harness PID; never infer or kill a foreign group.
        if not child_waited:
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                forced_stop = "harness_cleanup_unconfirmed"
        for reader in readers:
            reader.join(timeout=1)
    if overflow.is_set() or any(reader.is_alive() for reader in readers):
        forced_stop = "diagnostic_overflow_or_open_pipe"
    for suffix, raw in ((".stdout.txt", output), (".stderr.txt", errors)):
        with raw_directory.with_suffix(suffix).open("xb") as evidence:
            evidence.write(raw)
    try:
        report = json.loads(output)
        if not isinstance(report, dict):
            raise ValueError("Missing case report")
    except (ValueError, UnicodeError):
        report = {"passed": False, "error_class": "InvalidCaseReport"}
    return {
        "case": case,
        "returncode": process.returncode,
        "forced_stop": forced_stop,
        "outer_seconds": CASES[case],
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "passed": successful_child(report, returncode=process.returncode, forced_stop=forced_stop),
        "report": report,
        "stderr": errors.decode("utf-8", "replace"),
        "stdout_sha256": hashlib.sha256(output).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", choices=tuple(CASES))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child-case", choices=tuple(CASES), help=argparse.SUPPRESS)
    parser.add_argument("--raw-directory", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if arguments.child_case:
        if arguments.raw_directory is None or not arguments.raw_directory.is_absolute():
            parser.error("Fixed child requires an absolute evidence directory")
        report = asyncio.run(run_case(arguments.child_case, arguments.raw_directory))
        print(json.dumps(report, separators=(",", ":"), allow_nan=False))
        return 0 if report["passed"] else 1
    if arguments.output is None:
        parser.error("--output is required")
    output = arguments.output.resolve()
    if output.exists():
        parser.error("Existing evidence must not be overwritten")
    raw_root = output.with_suffix("")
    raw_root.mkdir(mode=0o700)
    cases = arguments.case or ["small", "xml_25mib"]
    if len(cases) != len(set(cases)):
        parser.error("Duplicate cases are not allowed")
    sources = sorted((ROOT / "app/processing").glob("*.py")) + [
        Path(__file__).resolve(),
        ROOT / "app/configuration.py",
        ROOT / "app/source.py",
        ROOT / "app/upload_ingress.py",
    ]
    report = {
        "schema_version": 1,
        "synthetic_only": True,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
        "executable": sys.executable,
        "source_binding_scope": "Selected processing/configuration/input modules; complete workspace binding belongs to the controller",
        "selected_source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources
        },
        "cases": [run_bounded(case, raw_root / case) for case in cases],
    }
    report["passed"] = all(item["passed"] for item in report["cases"])
    write_json(output, report)
    print(json.dumps({"passed": report["passed"], "evidence": str(output)}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
