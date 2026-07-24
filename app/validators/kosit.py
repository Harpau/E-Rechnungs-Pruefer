from __future__ import annotations

import ctypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import Structure, byref, sizeof
from ctypes import c_longlong as _LargeInteger
from ctypes import c_size_t as SIZE_T
from ctypes import c_ulonglong as ULONGLONG
from ctypes import c_void_p as HANDLE
from ctypes.wintypes import BOOL, DWORD, LPCWSTR
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, Thread
from typing import IO, Any

from lxml import etree

from ..desktop_security import API_TOKEN_ENV, SERVICE_MODE_ENV
from ..settings import Settings
from ..windows_acl import WindowsServiceAcl
from ..windows_service_config import ServicePaths
from ..xml_utils import clean_text, local_name, namespace_uri

TECHNICAL_START_PATTERNS = (
    "no main manifest attribute",
    "kein hauptmanifestattribut",
    "unable to access jarfile",
    "invalid or corrupt jarfile",
    "could not find or load main class",
    "could not create the java virtual machine",
    "error opening zip file",
    "a jni error has occurred",
)

_REPORT_END_RE = re.compile(rb"</(?:[A-Za-z_][A-Za-z0-9_.-]*:)?report\s*>", re.IGNORECASE)
_FORMAT_ERROR_PREFIX = b"[Format error!] <"
_FORMAT_ERROR_SUFFIX = b"> with params <"
WINDOWS_SUBPROCESS_CREATION_FLAGS = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if sys.platform == "win32" else 0
_RUNNING_PROCESS_LOCK = Lock()
_RUNNING_PROCESSES: set[subprocess.Popen[bytes]] = set()
_KOSIT_PROCESS_STARTS_ALLOWED = True
_KOSIT_JOB_LOCK = Lock()
_KOSIT_TEMP_DIRECTORY_LOCK = Lock()
_KOSIT_JOB_HANDLE: int | None = None
_PROCESS_LOCK_TIMEOUT_SECONDS = 0.25
_KILL_COMMUNICATE_TIMEOUT_SECONDS = 1.0
MAXIMUM_KOSIT_CONSOLE_BYTES = 2 * 1024 * 1024
MAXIMUM_KOSIT_REPORT_BYTES = 2 * 1024 * 1024
_PIPE_READ_CHUNK_BYTES = 64 * 1024
_PIPE_READER_JOIN_SECONDS = 2.0
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class _IoCounters(Structure):
    _fields_ = (
        ("ReadOperationCount", ULONGLONG),
        ("WriteOperationCount", ULONGLONG),
        ("OtherOperationCount", ULONGLONG),
        ("ReadTransferCount", ULONGLONG),
        ("WriteTransferCount", ULONGLONG),
        ("OtherTransferCount", ULONGLONG),
    )


class _BasicLimitInformation(Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", _LargeInteger),
        ("PerJobUserTimeLimit", _LargeInteger),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", SIZE_T),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    )


class _ExtendedLimitInformation(Structure):
    _fields_ = (
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    )


class _BoundedPipeCapture:
    def __init__(self, stream: IO[bytes], *, maximum_bytes: int) -> None:
        self._stream = stream
        self._maximum_bytes = maximum_bytes
        self._payload = bytearray()
        self._overflow = False
        self._failure: Exception | None = None
        self._thread = Thread(target=self._read, name="kosit-output-reader", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _read(self) -> None:
        try:
            while chunk := self._stream.read(_PIPE_READ_CHUNK_BYTES):
                remaining = self._maximum_bytes - len(self._payload)
                if remaining > 0:
                    self._payload.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self._overflow = True
        except Exception as exc:
            self._failure = exc
        finally:
            try:
                self._stream.close()
            except OSError:
                pass

    def finish(self, *, reject_overflow: bool) -> bytes:
        self._thread.join(_PIPE_READER_JOIN_SECONDS)
        if self._thread.is_alive():
            raise OSError("Die KoSIT-Ausgabepipe konnte nicht begrenzt geschlossen werden.")
        if self._failure is not None:
            raise OSError("Die KoSIT-Ausgabepipe konnte nicht gelesen werden.") from self._failure
        if reject_overflow and self._overflow:
            raise OSError(f"Die KoSIT-Konsolenausgabe überschreitet das Bytebudget von {self._maximum_bytes} Bytes.")
        return bytes(self._payload)

    @property
    def overflowed(self) -> bool:
        return self._overflow


def _windows_kernel32() -> Any:
    if sys.platform != "win32":
        raise OSError("Windows-Job-Objekte sind nur unter Windows verfügbar.")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _windows_last_error() -> int:
    getter = vars(ctypes).get("get_last_error")
    if getter is None:
        raise OSError("Der native Windows-Fehlerstatus ist nicht verfügbar.")
    return int(getter())


def _create_kosit_job() -> int | None:
    if sys.platform != "win32":
        return None
    kernel32 = _windows_kernel32()
    create_job = kernel32.CreateJobObjectW
    create_job.argtypes = [HANDLE, LPCWSTR]
    create_job.restype = HANDLE
    set_information = kernel32.SetInformationJobObject
    set_information.argtypes = [HANDLE, DWORD, HANDLE, DWORD]
    set_information.restype = BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [HANDLE]
    close_handle.restype = BOOL
    job = create_job(None, None)
    if not job:
        raise OSError(_windows_last_error(), "Das KoSIT-Job-Objekt konnte nicht erstellt werden.")
    information = _ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not set_information(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        byref(information),
        sizeof(information),
    ):
        error = _windows_last_error()
        close_handle(job)
        raise OSError(error, "Das KoSIT-Job-Objekt konnte nicht abgesichert werden.")
    return int(job)


def _assign_current_process_to_kosit_job(job: int) -> None:
    kernel32 = _windows_kernel32()
    current_process = kernel32.GetCurrentProcess
    current_process.argtypes = []
    current_process.restype = HANDLE
    assign = kernel32.AssignProcessToJobObject
    assign.argtypes = [HANDLE, HANDLE]
    assign.restype = BOOL
    if not assign(HANDLE(job), current_process()):
        raise OSError(
            _windows_last_error(),
            "Der Dienstprozess konnte nicht dem geschützten KoSIT-Job zugeordnet werden.",
        )


def _close_kosit_job(job: int | None) -> None:
    if job is None:
        return
    kernel32 = _windows_kernel32()
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [HANDLE]
    close_handle.restype = BOOL
    if not close_handle(HANDLE(job)):
        raise OSError(_windows_last_error(), "Das KoSIT-Job-Objekt konnte nicht geschlossen werden.")


def _ensure_kosit_process_job() -> None:
    """Protect service-mode Java trees without capturing desktop browser children."""

    global _KOSIT_JOB_HANDLE
    if sys.platform != "win32" or os.environ.get(SERVICE_MODE_ENV) != "1" or _KOSIT_JOB_HANDLE is not None:
        return
    with _KOSIT_JOB_LOCK:
        if _KOSIT_JOB_HANDLE is not None:
            return
        job = _create_kosit_job()
        if job is None:
            raise OSError("Das Windows-Job-Objekt für KoSIT fehlt.")
        try:
            _assign_current_process_to_kosit_job(job)
        except Exception:
            _close_kosit_job(job)
            raise
        # The handle intentionally remains open for the process lifetime.
        # Children inherit the job at CreateProcess time, so a hard parent
        # termination closes the last handle and kills the entire Java tree.
        _KOSIT_JOB_HANDLE = job


def _acquire_process_lock(timeout: float = _PROCESS_LOCK_TIMEOUT_SECONDS) -> bool:
    return _RUNNING_PROCESS_LOCK.acquire(timeout=max(timeout, 0.0))


def allow_kosit_process_starts() -> None:
    global _KOSIT_PROCESS_STARTS_ALLOWED
    if not _acquire_process_lock():
        raise RuntimeError("Die KoSIT-Prozessverwaltung konnte nicht rechtzeitig freigegeben werden.")
    try:
        _KOSIT_PROCESS_STARTS_ALLOWED = True
    finally:
        _RUNNING_PROCESS_LOCK.release()


def _terminate_unregistered_process(process: subprocess.Popen[bytes]) -> None:
    """Best-effort cleanup for a process that could not be registered safely."""

    try:
        process.kill()
    except OSError:
        return
    try:
        process.wait(timeout=_KILL_COMMUNICATE_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        pass


@contextmanager
def _ephemeral_invoice_file(path: Path, payload: bytes) -> Iterator[None]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if sys.platform == "win32":
        binary_flag = vars(os).get("O_BINARY")
        if isinstance(binary_flag, int):
            flags |= binary_flag
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        try:
            os.close(descriptor)
        finally:
            path.unlink(missing_ok=True)


@contextmanager
def _kosit_temporary_directory() -> Iterator[Path]:
    if sys.platform != "win32" or os.environ.get(SERVICE_MODE_ENV) != "1":
        with tempfile.TemporaryDirectory(prefix="einvoice-kosit-") as temporary:
            yield Path(temporary)
        return

    paths = ServicePaths.from_environment()
    acl = WindowsServiceAcl()
    with _KOSIT_TEMP_DIRECTORY_LOCK:
        # The service data directory is owned by Administrators and grants
        # access to the service SID, not to the shared LocalService identity.
        # Keeping the runtime tree below that verified parent prevents another
        # LocalService-hosted service from renaming and replacing the freshly
        # created directory between creation, verification and first use.
        acl.verify_data_directory(paths.data_directory)
        acl.protect_directory(paths.runtime_directory, allow_local_service_owner=True)
        temporary = paths.runtime_directory / f"einvoice-kosit-{secrets.token_hex(16)}"
        acl.create_protected_directory(
            temporary,
            allow_local_service_owner=True,
        )
    try:
        yield temporary
    finally:
        try:
            shutil.rmtree(temporary)
        finally:
            with _KOSIT_TEMP_DIRECTORY_LOCK:
                try:
                    paths.runtime_directory.rmdir()
                except OSError:
                    # Another validation may still own a sibling. A protected
                    # empty root is harmless and is also removed at the next
                    # service start or uninstall.
                    pass


def _start_kosit_process(
    command: list[str],
    *,
    cwd: str,
    creationflags: int,
) -> subprocess.Popen[bytes]:
    _ensure_kosit_process_job()
    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        creationflags=creationflags,
        env={key: value for key, value in os.environ.items() if key != API_TOKEN_ENV},
    )


def _run_kosit_process(
    command: list[str],
    *,
    capture_output: bool,
    timeout: int,
    check: bool,
    cwd: str,
    creationflags: int,
) -> subprocess.CompletedProcess[bytes]:
    if not capture_output:
        raise ValueError("KoSIT-Prozesse müssen mit begrenzter Ausgabe gestartet werden.")
    global _KOSIT_PROCESS_STARTS_ALLOWED
    if not _acquire_process_lock():
        raise OSError("Die KoSIT-Prozessverwaltung antwortet nicht rechtzeitig.")
    try:
        if not _KOSIT_PROCESS_STARTS_ALLOWED:
            raise OSError("Der Dienst wird beendet; es wird keine neue KoSIT-Prüfung gestartet.")
    finally:
        _RUNNING_PROCESS_LOCK.release()

    process = _start_kosit_process(
        command,
        cwd=cwd,
        creationflags=creationflags,
    )
    if process.stdout is None or process.stderr is None:
        _terminate_unregistered_process(process)
        raise OSError("Die begrenzten KoSIT-Ausgabepipes fehlen.")
    stdout_capture = _BoundedPipeCapture(process.stdout, maximum_bytes=MAXIMUM_KOSIT_CONSOLE_BYTES)
    stderr_capture = _BoundedPipeCapture(process.stderr, maximum_bytes=MAXIMUM_KOSIT_CONSOLE_BYTES)
    if not _acquire_process_lock():
        _terminate_unregistered_process(process)
        raise OSError("Der KoSIT-Prozess konnte nicht rechtzeitig registriert werden.")
    registered = False
    try:
        # Shutdown may have started while Popen was creating the child. Never
        # let such a late child escape the shutdown gate.
        if _KOSIT_PROCESS_STARTS_ALLOWED:
            _RUNNING_PROCESSES.add(process)
            registered = True
    finally:
        _RUNNING_PROCESS_LOCK.release()
    if not registered:
        _terminate_unregistered_process(process)
        raise OSError("Der Dienst wird beendet; die begonnene KoSIT-Prüfung wurde abgebrochen.")

    stdout_capture.start()
    stderr_capture.start()
    try:
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=_KILL_COMMUNICATE_TIMEOUT_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                pass
            stdout = stdout_capture.finish(reject_overflow=False)
            stderr = stderr_capture.finish(reject_overflow=False)
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from exc
        stdout = stdout_capture.finish(reject_overflow=False)
        stderr = stderr_capture.finish(reject_overflow=False)
    finally:
        # A wedged management lock must not turn request or service shutdown
        # into an unbounded wait. A stale completed process is harmless and is
        # ignored by later cancellation snapshots.
        if _acquire_process_lock():
            try:
                _RUNNING_PROCESSES.discard(process)
            finally:
                _RUNNING_PROCESS_LOCK.release()
    completed = subprocess.CompletedProcess(command, return_code, stdout, stderr)
    vars(completed)["_kosit_console_overflow"] = stdout_capture.overflowed or stderr_capture.overflowed
    if check:
        completed.check_returncode()
    return completed


def cancel_running_kosit_processes(timeout: float = 5.0) -> int:
    """Terminate active KoSIT processes during bounded SCM shutdown."""

    global _KOSIT_PROCESS_STARTS_ALLOWED
    # Close the start gate before attempting the lock. A thread blocked in
    # Popen rechecks this flag before registration and cleans up its own child.
    _KOSIT_PROCESS_STARTS_ALLOWED = False
    deadline = time.monotonic() + max(timeout, 0.0)
    lock_timeout = min(_PROCESS_LOCK_TIMEOUT_SECONDS, max(0.0, deadline - time.monotonic()))
    if not _acquire_process_lock(lock_timeout):
        return 0
    try:
        processes = tuple(_RUNNING_PROCESSES)
    finally:
        _RUNNING_PROCESS_LOCK.release()
    for process in processes:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
    for process in processes:
        if process.poll() is not None:
            continue
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            try:
                process.kill()
            except OSError:
                pass
            continue
        try:
            process.wait(timeout=remaining)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
    # Reap killed children only within the caller's original deadline.
    for process in processes:
        if process.poll() is not None:
            continue
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            break
        try:
            process.wait(timeout=remaining)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return len(processes)


@dataclass(slots=True)
class KositValidator:
    settings: Settings

    @staticmethod
    def _jar_main_class(jar_path: Path) -> str | None:
        try:
            with zipfile.ZipFile(jar_path) as archive:
                raw = archive.read("META-INF/MANIFEST.MF")
        except (OSError, KeyError, zipfile.BadZipFile):
            return None

        text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        unfolded: list[str] = []
        for line in text.split("\n"):
            if line.startswith(" ") and unfolded:
                unfolded[-1] += line[1:]
            else:
                unfolded.append(line)
        for line in unfolded:
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() == "main-class":
                return value.strip() or None
        return None

    def configuration_state(self) -> dict[str, Any]:
        jar = self.settings.kosit_validator_jar
        scenarios = self.settings.kosit_scenarios
        problems: list[str] = []
        main_class: str | None = None
        if not self.settings.kosit_enabled:
            problems.append("KoSIT-Anbindung ist deaktiviert.")
        if jar is None:
            problems.append("KOSIT_VALIDATOR_JAR ist nicht gesetzt.")
        elif not jar.is_file():
            problems.append(f"Validator-JAR wurde nicht gefunden: {jar}")
        else:
            main_class = self._jar_main_class(jar)
            if not main_class:
                problems.append(
                    "Validator-JAR ist nicht mit 'java -jar' ausführbar, weil im Manifest die Main-Class fehlt. "
                    "Benötigt wird das offizielle '*-standalone.jar', nicht validator-<Version>.jar."
                )
        if not scenarios:
            problems.append("KOSIT_SCENARIOS ist nicht gesetzt.")
        else:
            for scenario in scenarios:
                if not scenario.is_file():
                    problems.append(f"Szenariokonfiguration wurde nicht gefunden: {scenario}")
        if shutil.which(self.settings.kosit_java_bin) is None:
            problems.append(f"Java wurde nicht gefunden: {self.settings.kosit_java_bin}")
        for repository in self.settings.kosit_repositories:
            if not repository.exists():
                problems.append(f"KoSIT-Ressourcenverzeichnis wurde nicht gefunden: {repository}")
        return {
            "configured": not problems,
            "problems": problems,
            "jar": str(jar) if jar else None,
            "jar_main_class": main_class,
            "scenarios": [str(path) for path in scenarios],
            "repositories": [str(path) for path in self.settings.kosit_repositories],
        }

    @staticmethod
    def _parse_xml_root(payload: bytes | None) -> etree._Element | None:
        if not payload:
            return None
        parser = etree.XMLParser(
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
            recover=False,
            huge_tree=False,
        )
        try:
            return etree.fromstring(payload, parser=parser)
        except (etree.XMLSyntaxError, ValueError):
            return None

    @classmethod
    def _extract_xml_payload(cls, output: bytes) -> bytes | None:
        """Extract one complete KoSIT XML report from mixed console output."""

        if not output:
            return None

        starts = [pos for marker in (b"<?xml", b"<rep:report", b"<report") if (pos := output.find(marker)) >= 0]
        if not starts:
            # Namespace prefixes are not fixed. Look for any prefixed report
            # element instead of treating an arbitrary '<' as XML.
            match = re.search(rb"<[A-Za-z_][A-Za-z0-9_.-]*:report(?:\s|>)", output)
            if not match:
                return None
            starts = [match.start()]

        start = min(starts)
        end_match = _REPORT_END_RE.search(output, start)
        if not end_match:
            return None
        candidate = output[start : end_match.end()].strip()
        root = cls._parse_xml_root(candidate)
        if root is None or local_name(root).lower() not in {"report", "validationreport"}:
            return None
        return candidate

    @classmethod
    def _extract_format_error_payload(cls, stderr: bytes) -> bytes | None:
        """Recover a report wrapped by KoSIT's ``[Format error!]`` output.

        KoSIT 1.6.2's ``--print`` path passes the complete serialized XML to
        ``MessageFormat``. Report content can therefore trigger a formatting
        exception; KoSIT then writes the otherwise valid XML inside a wrapper
        to stderr. Version 1.0.2 no longer uses ``--print``, but this fallback
        also makes existing/custom launchers readable.
        """

        if not stderr:
            return None
        search_from = 0
        recovered: bytes | None = None
        while True:
            wrapper_start = stderr.find(_FORMAT_ERROR_PREFIX, search_from)
            if wrapper_start < 0:
                break
            payload_start = wrapper_start + len(_FORMAT_ERROR_PREFIX)
            wrapper_end = stderr.find(_FORMAT_ERROR_SUFFIX, payload_start)
            if wrapper_end < 0:
                break
            candidate = stderr[payload_start:wrapper_end]
            payload = cls._extract_xml_payload(candidate)
            if payload is not None:
                recovered = payload
            search_from = wrapper_end + len(_FORMAT_ERROR_SUFFIX)
        return recovered

    @classmethod
    def _read_serialized_report(cls, report_directory: Path, invoice_path: Path) -> bytes | None:
        """Read the report KoSIT writes to its output directory."""

        expected = report_directory / f"{invoice_path.stem}-report.xml"
        candidates: list[Path] = []
        if expected.is_file():
            candidates.append(expected)
        try:
            candidates.extend(
                path
                for path in sorted(
                    report_directory.glob("*.xml"),
                    key=lambda item: item.stat().st_mtime_ns,
                    reverse=True,
                )
                if path not in candidates
            )
        except OSError:
            pass

        for candidate in candidates:
            try:
                with candidate.open("rb") as handle:
                    payload = handle.read(MAXIMUM_KOSIT_REPORT_BYTES + 1)
            except OSError:
                continue
            if len(payload) > MAXIMUM_KOSIT_REPORT_BYTES:
                raise OSError(f"Der KoSIT-Bericht überschreitet das Bytebudget von {MAXIMUM_KOSIT_REPORT_BYTES} Bytes.")
            root = cls._parse_xml_root(payload)
            if root is not None and local_name(root).lower() in {"report", "validationreport"}:
                return payload
        return None

    @staticmethod
    def _report_decision(root: etree._Element) -> tuple[bool | None, str | None]:
        # The actual VARL decision is represented by
        # <rep:assessment><rep:accept/> or <rep:reject/>.
        for element in root.iter():
            if not isinstance(element.tag, str) or local_name(element).lower() != "assessment":
                continue
            for descendant in element.iterdescendants():
                if not isinstance(descendant.tag, str):
                    continue
                lname = local_name(descendant).lower()
                if lname == "reject":
                    return False, "reject"
                if lname == "accept":
                    return True, "accept"

        # Compatibility fallback for report variants with an explicit root
        # valid attribute or textual status.
        valid_raw = root.attrib.get("valid")
        if valid_raw is not None:
            value = valid_raw.strip().lower()
            if value in {"true", "1", "yes"}:
                return True, value
            if value in {"false", "0", "no"}:
                return False, value

        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            lname = local_name(element).lower()
            if lname in {"accepted", "acceptrecommendation", "acceptance", "status"}:
                value = (clean_text(element) or "").strip().lower()
                if value in {"true", "yes", "accepted", "accept", "valid", "ok", "success"}:
                    return True, value
                if value in {"false", "no", "rejected", "reject", "invalid", "failed", "error"}:
                    return False, value
        return None, None

    @classmethod
    def _parse_report(
        cls,
        report_bytes: bytes | None,
    ) -> tuple[list[dict[str, Any]], bool | None, str | None, bool]:
        root = cls._parse_xml_root(report_bytes)
        if root is None:
            return [], None, None, False

        root_name = local_name(root).lower()
        root_namespace = (namespace_uri(root) or "").strip().lower()
        is_validator_report = root_name in {"report", "validationreport"} and (
            "validator" in root_namespace or "varl" in root_namespace or root_namespace == ""
        )
        if not is_validator_report:
            return [], None, None, False

        decision, assessment = cls._report_decision(root)
        findings: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for element in root.iter():
            if len(findings) >= 500:
                break
            if not isinstance(element.tag, str):
                continue
            lname = local_name(element).lower()
            attrs = {key.split("}")[-1].lower(): value for key, value in element.attrib.items()}
            severity_raw = (
                attrs.get("severity") or attrs.get("level") or attrs.get("flag") or attrs.get("class") or ""
            ).lower()
            interesting_name = any(token in lname for token in ("error", "warning", "assert", "message", "notice"))
            interesting_severity = any(
                token in severity_raw for token in ("fatal", "error", "warning", "warn", "info", "information")
            )
            if not interesting_name and not interesting_severity:
                continue

            text = " ".join(" ".join(element.itertext()).split())
            if not text or len(text) < 3:
                continue
            text = text[:2000]
            severity = "info"
            if any(token in severity_raw for token in ("fatal", "error")) or "error" in lname or "failed" in lname:
                severity = "error"
            elif "warn" in severity_raw or "warning" in lname:
                severity = "warning"
            rule_id = (
                attrs.get("id") or attrs.get("test") or attrs.get("rule") or attrs.get("code") or local_name(element)
            )
            key = (rule_id, text)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                {
                    "id": rule_id[:200],
                    "severity": severity,
                    "title": "KoSIT-Prüfmeldung",
                    "message": text,
                    "location": attrs.get("location") or attrs.get("xpath") or attrs.get("context"),
                    "actual": None,
                    "expected": None,
                    "source": "KoSIT Validator",
                }
            )
        return findings, decision, assessment, True

    @staticmethod
    def _not_executed(
        state: dict[str, Any],
        *,
        summary: str,
        message: str | None = None,
        finding_id: str = "KOSIT-CONFIG",
        exit_code: int | None = None,
        technical_output: str | None = None,
    ) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        if message:
            findings.append(
                {
                    "id": finding_id,
                    "severity": "warning",
                    "title": "KoSIT-Prüfung wurde nicht ausgeführt",
                    "message": message[:4000],
                    "location": None,
                    "actual": str(exit_code) if exit_code is not None else None,
                    "expected": "Ausführbares Standalone-JAR, gültige KoSIT-Konfiguration und XML-Prüfbericht",
                    "source": "KoSIT-Anbindung",
                }
            )
        return {
            **state,
            "executed": False,
            "accepted": None,
            "exit_code": exit_code,
            "summary": summary,
            "findings": findings,
            "raw_report": None,
            "technical_output": technical_output,
            "report_source": None,
        }

    @staticmethod
    def _looks_like_startup_failure(text: str) -> bool:
        lowered = text.lower()
        return any(pattern in lowered for pattern in TECHNICAL_START_PATTERNS)

    def validate(self, xml_bytes: bytes, filename: str) -> dict[str, Any]:
        state = self.configuration_state()
        if not state["configured"]:
            detail = " ".join(state.get("problems") or [])
            return self._not_executed(
                state,
                summary=f"Offizielle KoSIT-Prüfung ist nicht konfiguriert. {detail}".strip(),
                message=detail or None,
            )

        with _kosit_temporary_directory() as temp_path:
            temp_dir = str(temp_path)
            invoice_path = temp_path / Path(filename).name
            if invoice_path.suffix.lower() != ".xml":
                invoice_path = invoice_path.with_suffix(".xml")

            report_directory = temp_path / "reports"
            report_directory.mkdir()

            command = [
                self.settings.kosit_java_bin,
                "-jar",
                str(self.settings.kosit_validator_jar),
            ]
            for scenario in self.settings.kosit_scenarios:
                command.extend(["-s", str(scenario)])
            for repository in self.settings.kosit_repositories:
                command.extend(["-r", str(repository)])

            # KoSIT serializes a report for every check. Reading that file is
            # more reliable than '-p/--print', whose implementation in KoSIT
            # 1.6.2 can produce a '[Format error!]' wrapper on stderr.
            command.extend(["-o", str(report_directory), str(invoice_path)])

            try:
                with _ephemeral_invoice_file(invoice_path, xml_bytes):
                    completed = _run_kosit_process(
                        command,
                        capture_output=True,
                        timeout=self.settings.kosit_timeout_seconds,
                        check=False,
                        cwd=temp_dir,
                        creationflags=WINDOWS_SUBPROCESS_CREATION_FLAGS,
                    )
            except subprocess.TimeoutExpired:
                return self._not_executed(
                    state,
                    summary=f"KoSIT-Prüfung wurde nach {self.settings.kosit_timeout_seconds} Sekunden abgebrochen.",
                    message="Zeitüberschreitung beim Start oder bei der Ausführung des KoSIT-Validators.",
                    finding_id="KOSIT-TIMEOUT",
                )
            except OSError as exc:
                return self._not_executed(
                    state,
                    summary=f"KoSIT-Validator konnte nicht gestartet werden: {exc}",
                    message=str(exc),
                    finding_id="KOSIT-START",
                )

            try:
                report_payload = self._read_serialized_report(report_directory, invoice_path)
            except OSError as exc:
                return self._not_executed(
                    state,
                    summary="KoSIT-Prüfung lieferte einen zu großen oder nicht sicher lesbaren Prüfbericht.",
                    message=str(exc),
                    finding_id="KOSIT-REPORT",
                    exit_code=completed.returncode,
                )
            report_source: str | None = "file" if report_payload is not None else None
            if report_payload is None:
                report_payload = self._extract_xml_payload(completed.stdout)
                report_source = "stdout" if report_payload is not None else None
            if report_payload is None:
                report_payload = self._extract_format_error_payload(completed.stderr)
                report_source = "stderr-format-error" if report_payload is not None else None
            if report_payload is None:
                report_payload = self._extract_xml_payload(completed.stderr)
                report_source = "stderr" if report_payload is not None else None

            stdout = completed.stdout.decode("utf-8", errors="replace").strip()
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            findings, report_decision, assessment, valid_report = self._parse_report(report_payload)
            technical_output = "\n".join(part for part in (stderr, stdout if not valid_report else "") if part).strip()
            console_overflow = bool(getattr(completed, "_kosit_console_overflow", False))
            if console_overflow and not valid_report:
                overflow_notice = (
                    f"Die KoSIT-Konsolenausgabe überschritt das Bytebudget von "
                    f"{MAXIMUM_KOSIT_CONSOLE_BYTES} Bytes und wurde gekürzt."
                )
                technical_output = "\n".join(part for part in (overflow_notice, technical_output) if part)

            # A Java/configuration failure without a valid report says nothing
            # about the invoice and must never be shown as a rejection.
            if not valid_report:
                diagnostic = technical_output or (
                    f"Der Prozess endete mit Rückgabecode {completed.returncode}, ohne einen auswertbaren "
                    "KoSIT-XML-Bericht zu liefern."
                )
                if self._looks_like_startup_failure(diagnostic):
                    summary = (
                        "KoSIT-Prüfung wurde wegen einer technischen Start- oder "
                        "JAR-Konfigurationsstörung nicht ausgeführt."
                    )
                else:
                    summary = (
                        "KoSIT-Prüfung lieferte keinen auswertbaren XML-Prüfbericht und wurde daher "
                        "nicht als Rechnungsprüfung gewertet."
                    )
                return self._not_executed(
                    state,
                    summary=summary,
                    message=diagnostic,
                    finding_id="KOSIT-EXEC",
                    exit_code=completed.returncode,
                    technical_output=technical_output or None,
                )

            if console_overflow:
                findings.append(
                    {
                        "id": "KOSIT-OUTPUT-TRUNCATED",
                        "severity": "warning",
                        "title": "KoSIT-Konsolenausgabe wurde begrenzt",
                        "message": (
                            "Die KoSIT-Konsolenausgabe überschritt das feste Bytebudget und wurde gekürzt. "
                            "Die ausdrückliche Entscheidung im vollständig gelesenen VARL-Bericht bleibt maßgeblich."
                        ),
                    }
                )

        # The explicit assessment in the VARL XML report is authoritative. The
        # process return code remains a fallback for custom/older reports.
        accepted = report_decision if report_decision is not None else completed.returncode == 0

        raw_report = report_payload.decode("utf-8", errors="replace") if report_payload else None
        if raw_report and len(raw_report) > 2_000_000:
            raw_report = raw_report[:2_000_000] + "\n<!-- Bericht für die Anzeige gekürzt -->"

        summary = (
            "KoSIT-Prüfung erfolgreich: Rechnung wurde akzeptiert."
            if accepted
            else "KoSIT-Prüfung abgeschlossen: Rechnung wurde abgelehnt."
        )
        if assessment:
            summary += f" Bewertung im Bericht: {assessment}."
        if report_source == "stderr-format-error":
            summary += " Der XML-Bericht wurde aus einer KoSIT-Formatfehler-Ausgabe wiederhergestellt."

        exit_code_accepts = completed.returncode == 0
        if report_decision is not None and exit_code_accepts != report_decision:
            findings.append(
                {
                    "id": "KOSIT-RESULT-MISMATCH",
                    "severity": "warning",
                    "title": "KoSIT-Bericht und Prozess-Rückgabecode widersprechen sich",
                    "message": (
                        f"Der XML-Bericht bewertet die Rechnung als {'akzeptiert' if report_decision else 'abgelehnt'}, "
                        f"der Prozess endete jedoch mit Rückgabecode {completed.returncode}. "
                        "Für die Anzeige wurde die ausdrückliche Bewertung im XML-Bericht verwendet."
                    ),
                    "location": None,
                    "actual": str(completed.returncode),
                    "expected": "0 bei Annahme, ungleich 0 bei Ablehnung",
                    "source": "KoSIT Validator",
                }
            )

        if not accepted and not findings:
            findings.append(
                {
                    "id": "KOSIT-REJECT",
                    "severity": "error",
                    "title": "KoSIT-Validator hat die Rechnung abgelehnt",
                    "message": "Der KoSIT-Prüfbericht enthält eine Ablehnungsentscheidung ohne separat extrahierte Einzelmeldung.",
                    "location": None,
                    "actual": str(completed.returncode),
                    "expected": "0 bzw. Annahmeentscheidung im KoSIT-Bericht",
                    "source": "KoSIT Validator",
                }
            )

        clean_technical_output = stderr or None
        if (
            clean_technical_output
            and "[Format error!]" in clean_technical_output
            and report_source == "stderr-format-error"
        ):
            clean_technical_output = None

        return {
            **state,
            "executed": True,
            "accepted": accepted,
            "exit_code": completed.returncode,
            "summary": summary,
            "findings": findings,
            "raw_report": raw_report,
            "technical_output": clean_technical_output,
            "report_source": report_source,
        }
