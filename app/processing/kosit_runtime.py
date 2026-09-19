"""Trusted KoSIT transport/files only; never parse XML or load ambient settings.

The lifecycle owner supplies a private, identity/ACL-checked job directory and
starts Java only after its native limits and process-tree bindings are active.
This module does not spawn processes, infer invoice validity, or own cleanup.
"""

from __future__ import annotations

import math
import os
import shutil
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ..configuration import Settings, settings_to_snapshot
from .budgets import ProcessingBudgets

MAXIMUM_CONSOLE_BYTES = 2 * 1024 * 1024
MAXIMUM_REPORT_BYTES = 2 * 1024 * 1024
MAXIMUM_REPORT_CANDIDATES = 8
MAXIMUM_DIRECTORY_ENTRIES = 32
MAXIMUM_INVOICE_BYTES = 25 * 1024 * 1024
MIB = 1024 * 1024


class KositRuntimeError(RuntimeError):
    """A bounded technical transport/configuration error, never an invoice rejection."""


def _private_directory(path: Path) -> None:
    try:
        information = path.lstat()
    except OSError as exc:
        raise KositRuntimeError("private_directory_unavailable") from exc
    reparse = bool(getattr(information, "st_file_attributes", 0) & 0x400)
    if not path.is_absolute() or not stat.S_ISDIR(information.st_mode) or reparse:
        raise KositRuntimeError("private_directory_unsafe")
    if os.name == "posix" and (information.st_uid != vars(os)["getuid"]() or information.st_mode & 0o077):
        raise KositRuntimeError("private_directory_permissions")
    # Windows DACL/ancestor/reparse binding is established by the lifecycle
    # owner before this module receives the directory, not inferred from mode.


def prepare_java_command(settings: Settings, temp_directory: Path, budgets: ProcessingBudgets) -> list[str]:
    """Prepare exactly one fixed invoice/report location and a controlled Java argv."""
    settings_to_snapshot(settings)
    _private_directory(temp_directory)
    heap = budgets.java_heap_bytes
    if type(heap) is not int or heap < MIB or heap % MIB or heap >= budgets.java_memory_bytes:
        raise KositRuntimeError("java_memory_profile_invalid")
    configured_java = Path(settings.kosit_java_bin)
    located = str(configured_java) if configured_java.is_absolute() else shutil.which(settings.kosit_java_bin)
    if located is None or not Path(located).is_file():
        raise KositRuntimeError("java_executable_unavailable")
    java = Path(located).resolve(strict=True)
    jar = settings.kosit_validator_jar
    if (
        not settings.kosit_enabled
        or jar is None
        or not jar.is_file()
        or not settings.kosit_scenarios
        or any(not path.is_file() for path in settings.kosit_scenarios)
        or any(not path.is_dir() for path in settings.kosit_repositories)
    ):
        raise KositRuntimeError("java_components_unavailable")
    reports = temp_directory / "reports"
    java_temp = temp_directory / "java-tmp"
    try:
        reports.mkdir(mode=0o700)
        java_temp.mkdir(mode=0o700)
    except OSError as exc:
        raise KositRuntimeError("java_directory_creation_failed") from exc
    command = [
        str(java),
        "-XX:ActiveProcessorCount=2",
        f"-Xmx{heap // MIB}m",
        "-XX:MaxMetaspaceSize=256m",
        "-XX:MaxDirectMemorySize=64m",
        "-XX:ReservedCodeCacheSize=128m",
        f"-Djava.io.tmpdir={java_temp}",
        "-jar",
        str(jar),
    ]
    for scenario in settings.kosit_scenarios:
        command.extend(["-s", str(scenario)])
    for repository in settings.kosit_repositories:
        command.extend(["-r", str(repository)])
    command.extend(["-o", str(reports), str(temp_directory / "invoice.xml")])
    return command


def write_invoice(temp_directory: Path, xml_bytes: bytes, *, maximum_bytes: int = MAXIMUM_INVOICE_BYTES) -> Path:
    """Create the one fixed input exclusively; no worker-supplied filename is used."""
    if (
        type(maximum_bytes) is not int
        or not 0 < maximum_bytes <= MAXIMUM_INVOICE_BYTES
        or not isinstance(xml_bytes, bytes)
        or not 0 < len(xml_bytes) <= maximum_bytes
    ):
        raise KositRuntimeError("invoice_bytes_exceeded")
    _private_directory(temp_directory)
    path = temp_directory / "invoice.xml"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(xml_bytes)
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        raise KositRuntimeError("invoice_creation_failed") from exc
    return path


class BoundedConsoleCapture:
    """One pipe drainer; start stdout and stderr instances before releasing Java."""

    def __init__(self, stream: BinaryIO, *, maximum_bytes: int = MAXIMUM_CONSOLE_BYTES) -> None:
        if type(maximum_bytes) is not int or not 0 < maximum_bytes <= MAXIMUM_CONSOLE_BYTES:
            raise ValueError("Ungültiges Konsolenbudget.")
        self._stream = stream
        self._maximum = maximum_bytes
        self._payload = bytearray()
        self._error = False
        self._started = False
        self.overflowed = False
        self._thread = threading.Thread(target=self._drain, name="kosit-console-drain", daemon=True)

    def _drain(self) -> None:
        try:
            while chunk := self._stream.read(64 * 1024):
                if not isinstance(chunk, bytes):
                    raise TypeError("Console pipe must return bytes")
                remaining = self._maximum - len(self._payload)
                self._payload.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.overflowed = True
        except Exception:
            self._error = True

    def start(self) -> None:
        if self._started:
            raise KositRuntimeError("console_capture_already_started")
        self._started = True
        self._thread.start()

    def finish(self, *, timeout: float = 1.0) -> bytes:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 5:
            raise ValueError("Ungültige Konsolenabschlussfrist.")
        if not self._started:
            raise KositRuntimeError("console_capture_not_started")
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise KositRuntimeError("console_capture_not_closed")
        if self._error:
            raise KositRuntimeError("console_capture_read_failed")
        return bytes(self._payload)


def _file_identity(information: os.stat_result, *, cross_api: bool = False) -> tuple[int, ...]:
    # Windows path stat infers executable bits from a suffix; fstat does not.
    # Normalize only across APIs, retaining full-mode same-API race checks.
    mode = information.st_mode & ~0o111 if cross_api and os.name == "nt" else information.st_mode
    changed = information.st_ctime_ns
    if cross_api and os.name == "nt" and sys.version_info >= (3, 12):
        # CPython's Windows path stat retains creation time in legacy ctime,
        # whereas fstat can expose ChangeTime there. Compare creation time
        # explicitly across APIs; same-API checks still retain their ctime.
        # Python 3.11 has no birthtime_ns and uses legacy creation-time ctime.
        # https://github.com/python/cpython/blob/v3.14.7/Modules/posixmodule.c
        # https://github.com/python/cpython/blob/v3.14.7/Python/fileutils.c
        birthtime = getattr(information, "st_birthtime_ns", None)
        if type(birthtime) is not int:
            raise KositRuntimeError("report_creation_time_unavailable")
        changed = birthtime
    return (
        information.st_dev,
        information.st_ino,
        mode,
        information.st_size,
        information.st_mtime_ns,
        changed,
        information.st_nlink,
    )


def _read_candidate(path: Path, remaining: int) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or getattr(before, "st_file_attributes", 0) & 0x400:
        raise KositRuntimeError("report_unsafe_file")
    if before.st_size > remaining:
        raise KositRuntimeError("report_bytes_exceeded")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if _file_identity(before, cross_api=True) != _file_identity(opened, cross_api=True):
            raise KositRuntimeError("report_file_changed")
        payload = source.read(remaining + 1)
        if len(payload) > remaining:
            raise KositRuntimeError("report_bytes_exceeded")
        if _file_identity(opened) != _file_identity(os.fstat(source.fileno())):
            raise KositRuntimeError("report_file_changed")
    if _file_identity(before) != _file_identity(path.lstat()) or len(payload) != before.st_size:
        raise KositRuntimeError("report_file_changed")
    return payload


def _read_candidates(temp_directory: Path) -> tuple[bytes, ...]:
    _private_directory(temp_directory)
    reports = temp_directory / "reports"
    _private_directory(reports)
    candidates: list[Path] = []
    with os.scandir(reports) as entries:
        for index, entry in enumerate(entries):
            if index >= MAXIMUM_DIRECTORY_ENTRIES:
                raise KositRuntimeError("report_directory_entries_exceeded")
            if entry.name.endswith(".xml"):
                if len(candidates) >= MAXIMUM_REPORT_CANDIDATES:
                    raise KositRuntimeError("report_count_exceeded")
                candidates.append(reports / entry.name)
    candidates.sort(key=lambda path: (path.name != "invoice-report.xml", path.name))
    result: list[bytes] = []
    remaining = MAXIMUM_REPORT_BYTES
    for candidate in candidates:
        payload = _read_candidate(candidate, remaining)
        result.append(payload)
        remaining -= len(payload)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class JavaExecution:
    returncode: int
    stdout: bytes
    stderr: bytes
    report_candidates: tuple[bytes, ...]
    console_overflow: bool
    report_error: str | None
    console_error: str | None = None

    def metadata(self) -> dict[str, object]:
        return {
            "returncode": self.returncode,
            "stdout_size": len(self.stdout),
            "stderr_size": len(self.stderr),
            "report_sizes": [len(report) for report in self.report_candidates],
            "console_overflow": self.console_overflow,
            "report_error": self.report_error,
            "console_error": self.console_error,
        }


def read_execution(
    temp_directory: Path,
    returncode: int,
    stdout: bytes,
    stderr: bytes,
    console_overflow: bool,
    console_error: str | None = None,
) -> JavaExecution:
    """Read bounded raw candidates after Java exit; the parser worker interprets VARL."""
    if (
        type(returncode) is not int
        or not -(2**31) <= returncode < 2**32
        or not isinstance(stdout, bytes)
        or not isinstance(stderr, bytes)
        or type(console_overflow) is not bool
        or (
            console_error is not None
            and (type(console_error) is not str or console_error != "console_capture_read_failed")
        )
    ):
        raise KositRuntimeError("java_execution_metadata_invalid")
    overflowed = console_overflow or len(stdout) > MAXIMUM_CONSOLE_BYTES or len(stderr) > MAXIMUM_CONSOLE_BYTES
    report_error: str | None = None
    try:
        reports = _read_candidates(temp_directory)
    except KositRuntimeError as exc:
        reports = ()
        report_error = str(exc)
    except OSError:
        reports = ()
        report_error = "report_read_failed"
    return JavaExecution(
        returncode,
        stdout[:MAXIMUM_CONSOLE_BYTES],
        stderr[:MAXIMUM_CONSOLE_BYTES],
        reports,
        overflowed,
        report_error,
        console_error,
    )
