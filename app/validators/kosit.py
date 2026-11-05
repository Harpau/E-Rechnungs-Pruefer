from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import ntpath
import os
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
from ..official_rulesets import OfficialRuleset, RulesetFile
from ..settings import Settings
from ..windows_acl import WindowsServiceAcl
from ..windows_service_config import ServicePaths
from ..xml_utils import local_name

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

_VARL_NAMESPACE = "http://www.xoev.de/de/validator/varl/1"
_SCENARIOS_NAMESPACE = "http://www.xoev.de/de/validator/framework/1/scenarios"
# Bound rule-set runs always use this ASCII file name: KoSIT derives the report
# name from it and percent-encodes non-ASCII characters otherwise.
_BOUND_INVOICE_FILENAME = "rechnung.xml"
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


class _ReportBindingError(Exception):
    """A KoSIT report could not be bound to the expected rule set and document."""

    def __init__(self, finding_id: str, message: str) -> None:
        super().__init__(message)
        self.finding_id = finding_id
        self.message = message


def _extended_windows_path(absolute: str) -> str:
    r"""Return the extended-length form (``\\?\``) of an absolute Windows path.

    Win32 opens a plain path of 260 characters or more only when the machine sets
    ``LongPathsEnabled``; the extended form has no such limit. Java resolves long
    paths itself, so only Python's own reads need it.
    """

    normalized = ntpath.normpath(absolute)
    if normalized.startswith(("\\\\?\\", "\\\\.\\")):
        return normalized
    if normalized.startswith("\\\\"):
        return "\\\\?\\UNC\\" + normalized[2:]
    return "\\\\?\\" + normalized


def _long_path(path: Path) -> Path:
    """Path through which this module reads installation files; unchanged outside Windows."""

    if os.name != "nt":
        return path
    return Path(_extended_windows_path(os.path.abspath(path)))


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    # KoSIT probes System.in.available() at start-up. An inherited stdin that is
    # no pipe (NUL or a closed handle, as on CI runners and in services) makes
    # that probe fail on Windows with "Incorrect function" before any report is
    # written. A private pipe that is closed right away reads as a clean EOF.
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        creationflags=creationflags,
        env={key: value for key, value in os.environ.items() if key != API_TOKEN_ENV},
    )
    stdin = getattr(process, "stdin", None)
    if stdin is not None:
        try:
            stdin.close()
        except OSError:
            pass
    return process


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
    """Runs the KoSIT validator for exactly one bound rule set and binds its VARL report.

    Every run is bound: the rule set names the scenario the report must carry
    and the artefacts that must match their locked size and SHA-256 before Java
    is started. Only the report file KoSIT writes for the fixed invoice name is
    read; console output, the ``valid`` attribute, textual status values and the
    process return code never decide about the invoice.
    """

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

    def _ruleset_paths(self, ruleset: OfficialRuleset | None) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
        if ruleset is None or ruleset.settings_key is None:
            return tuple(self.settings.kosit_scenarios), tuple(self.settings.kosit_repositories)
        scenarios = getattr(self.settings, f"kosit_{ruleset.settings_key}_scenarios", ())
        repositories = getattr(self.settings, f"kosit_{ruleset.settings_key}_repositories", ())
        return tuple(scenarios), tuple(repositories)

    @staticmethod
    def _artefact_problem(path: Path, expected: RulesetFile, kind: str) -> str | None:
        """Compare one artefact with its locked size and SHA-256; ``None`` means it matches."""

        readable = _long_path(path)
        if not readable.is_file():
            return f"{kind} fehlt: {expected.target}"
        try:
            size = readable.stat().st_size
            digest = _sha256_of_file(readable) if size == expected.size else None
        except OSError as exc:
            return f"{kind} {expected.target} ist nicht lesbar: {exc}"
        if size != expected.size:
            return f"{kind} {expected.target} hat {size} statt {expected.size} Bytes."
        if digest != expected.sha256:
            return f"{kind} {expected.target} entspricht nicht der gebundenen SHA-256-Prüfsumme."
        return None

    @classmethod
    def _ruleset_file_problems(
        cls,
        scenarios: tuple[Path, ...],
        repositories: tuple[Path, ...],
        ruleset: OfficialRuleset,
    ) -> list[str]:
        """Fail closed: the scenario file and every bound artefact must exist with their locked size and SHA-256."""

        problems: list[str] = []
        if ruleset.scenario_file is not None:
            if len(scenarios) != 1:
                problems.append(
                    f"Für {ruleset.label} ist genau eine Szenariodatei zulässig, konfiguriert sind {len(scenarios)}."
                )
            elif _long_path(scenarios[0]).is_file():
                # A missing file was already reported by the generic check.
                problem = cls._artefact_problem(scenarios[0], ruleset.scenario_file, "Szenariodatei")
                if problem:
                    problems.append(problem)
        if not repositories:
            problems.append(f"Ressourcenverzeichnis für {ruleset.label} ist nicht eingerichtet.")
            return problems
        if len(repositories) != 1:
            problems.append(
                f"Für {ruleset.label} ist genau ein Ressourcenverzeichnis zulässig, konfiguriert sind {len(repositories)}."
            )
            return problems
        repository = repositories[0]
        for item in ruleset.files:
            problem = cls._artefact_problem(repository / item.target, item, "Regeldatei")
            if problem:
                problems.append(problem)
        if not _long_path(repository / ruleset.report_stylesheet).is_file():
            problems.append(f"Report-Stylesheet fehlt: {ruleset.report_stylesheet}")
        return problems

    def configuration_state(self, ruleset: OfficialRuleset | None = None) -> dict[str, Any]:
        """Describe the installation; with a rule set, every bound artefact is verified."""

        jar = self.settings.kosit_validator_jar
        scenarios, repositories = self._ruleset_paths(ruleset)
        problems: list[str] = []
        main_class: str | None = None
        if not self.settings.kosit_enabled:
            problems.append("KoSIT-Anbindung ist deaktiviert.")
        if jar is None:
            problems.append("KOSIT_VALIDATOR_JAR ist nicht gesetzt.")
        elif not _long_path(jar).is_file():
            problems.append(f"Validator-JAR wurde nicht gefunden: {jar}")
        else:
            main_class = self._jar_main_class(_long_path(jar))
            if not main_class:
                problems.append(
                    "Validator-JAR ist nicht mit 'java -jar' ausführbar, weil im Manifest die Main-Class fehlt. "
                    "Benötigt wird das offizielle '*-standalone.jar', nicht validator-<Version>.jar."
                )
        if not scenarios:
            if ruleset is None or ruleset.settings_key is None:
                problems.append("KOSIT_SCENARIOS ist nicht gesetzt.")
            else:
                problems.append(
                    f"Regelpaket {ruleset.label} ist nicht eingerichtet (scripts/install_kosit.py --force ausführen)."
                )
        else:
            for scenario in scenarios:
                if not _long_path(scenario).is_file():
                    problems.append(f"Szenariokonfiguration wurde nicht gefunden: {scenario}")
        if shutil.which(self.settings.kosit_java_bin) is None:
            problems.append(f"Java wurde nicht gefunden: {self.settings.kosit_java_bin}")
        for repository in repositories:
            if not _long_path(repository).exists():
                problems.append(f"KoSIT-Ressourcenverzeichnis wurde nicht gefunden: {repository}")
        if ruleset is not None and scenarios:
            problems.extend(self._ruleset_file_problems(scenarios, repositories, ruleset))
        return {
            "configured": not problems,
            "problems": problems,
            "jar": str(jar) if jar else None,
            "jar_main_class": main_class,
            "scenarios": [str(path) for path in scenarios],
            "repositories": [str(path) for path in repositories],
            "ruleset": ruleset.id if ruleset else None,
            "rules_version": ruleset.rules_version if ruleset else None,
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

    @staticmethod
    def _bound_findings(matched: etree._Element, ruleset: OfficialRuleset) -> list[dict[str, Any]]:
        """Findings from the ``rep:message`` elements of the matched scenario; the XHTML rendering is ignored."""

        findings: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for message in matched.iter(f"{{{_VARL_NAMESPACE}}}message"):
            if len(findings) >= 500:
                break
            code = (message.get("code") or message.get("id") or "KOSIT-MESSAGE").strip()
            level = (message.get("level") or "").strip().lower()
            severity = "error" if level in {"error", "fatal"} else "warning" if level == "warning" else "info"
            text = " ".join(" ".join(message.itertext()).split())[:2000]
            key = (code, text)
            if not text or key in seen:
                continue
            seen.add(key)
            line = message.get("lineNumber")
            column = message.get("columnNumber")
            findings.append(
                {
                    "id": code[:200],
                    "severity": severity,
                    "title": "KoSIT-Prüfmeldung",
                    "message": text,
                    "location": message.get("xpathLocation"),
                    "line": int(line) if line and line.isdigit() else None,
                    "column": int(column) if column and column.isdigit() else None,
                    "actual": None,
                    "expected": None,
                    "source": "KoSIT Validator",
                    "profile": ruleset.profile_id,
                    "version": ruleset.rules_version,
                }
            )
        return findings

    @classmethod
    def _bind_report(
        cls,
        report_bytes: bytes,
        ruleset: OfficialRuleset,
        xml_bytes: bytes,
    ) -> tuple[bool, list[dict[str, Any]], str]:
        """Bind a VARL report to the expected scenario and the exact document.

        Every deviation raises ``_ReportBindingError``; nothing falls back to the
        ``valid`` attribute, textual status values or the process exit code.
        """

        root = cls._parse_xml_root(report_bytes)
        if root is None:
            raise _ReportBindingError("KOSIT-REPORT", "Der KoSIT-Prüfbericht ist kein wohlgeformtes XML.")
        if root.tag != f"{{{_VARL_NAMESPACE}}}report":
            raise _ReportBindingError(
                "KOSIT-REPORT", "Der KoSIT-Prüfbericht liegt nicht im erwarteten VARL-Namespace vor."
            )
        engine = root.findtext(f"{{{_VARL_NAMESPACE}}}engine/{{{_VARL_NAMESPACE}}}name")
        if not (engine or "").strip():
            raise _ReportBindingError("KOSIT-REPORT", "Der KoSIT-Prüfbericht nennt keine Prüf-Engine.")

        matched = root.find(f"{{{_VARL_NAMESPACE}}}scenarioMatched")
        if root.find(f"{{{_VARL_NAMESPACE}}}noScenarioMatched") is not None or matched is None:
            raise _ReportBindingError(
                "KOSIT-SCENARIO",
                "KoSIT hat kein Prüfszenario angewendet; die Profilkennung passt nicht exakt zur Szenariokonfiguration "
                f"(erwartet wurde {ruleset.scenario_name!r}).",
            )
        scenario_name = matched.findtext(f"{{{_SCENARIOS_NAMESPACE}}}scenario/{{{_SCENARIOS_NAMESPACE}}}name")
        if scenario_name != ruleset.scenario_name:
            raise _ReportBindingError(
                "KOSIT-SCENARIO",
                f"Der Bericht gehört zum Szenario {scenario_name!r}, erwartet wurde {ruleset.scenario_name!r}.",
            )

        hash_prefix = f"{{{_VARL_NAMESPACE}}}documentIdentification/{{{_VARL_NAMESPACE}}}documentHash/"
        algorithm = root.findtext(hash_prefix + f"{{{_VARL_NAMESPACE}}}hashAlgorithm")
        value = root.findtext(hash_prefix + f"{{{_VARL_NAMESPACE}}}hashValue")
        if algorithm is None or value is None:
            raise _ReportBindingError("KOSIT-REPORT", "Der KoSIT-Prüfbericht enthält keinen Dokument-Hash.")
        if algorithm.strip().upper() != "SHA-256":
            raise _ReportBindingError(
                "KOSIT-REPORT", f"Unbekannter Hash-Algorithmus im KoSIT-Prüfbericht: {algorithm.strip()[:50]}"
            )
        try:
            digest = base64.b64decode(value.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _ReportBindingError(
                "KOSIT-REPORT", "Der Dokument-Hash im KoSIT-Prüfbericht ist kein gültiges Base64."
            ) from exc
        if digest != hashlib.sha256(xml_bytes).digest():
            raise _ReportBindingError(
                "KOSIT-REPORT", "Der Dokument-Hash im KoSIT-Prüfbericht gehört nicht zur übergebenen XML."
            )

        assessments = root.findall(f"{{{_VARL_NAMESPACE}}}assessment")
        if len(assessments) != 1:
            raise _ReportBindingError("KOSIT-REPORT", "Der KoSIT-Prüfbericht enthält nicht genau eine Bewertung.")
        decisions = [local_name(child) for child in assessments[0] if isinstance(child.tag, str)]
        if decisions == ["accept"]:
            decision = True
        elif decisions == ["reject"]:
            decision = False
        else:
            raise _ReportBindingError(
                "KOSIT-REPORT", "Die Bewertung im KoSIT-Prüfbericht ist weder eine eindeutige Annahme noch Ablehnung."
            )
        return decision, cls._bound_findings(matched, ruleset), scenario_name

    def _finish_bound_validation(
        self,
        state: dict[str, Any],
        ruleset: OfficialRuleset,
        xml_bytes: bytes,
        completed: subprocess.CompletedProcess[bytes],
        report_path: Path,
    ) -> dict[str, Any]:
        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        console_overflow = bool(getattr(completed, "_kosit_console_overflow", False))
        overflow_notice = (
            f"Die KoSIT-Konsolenausgabe überschritt das Bytebudget von {MAXIMUM_KOSIT_CONSOLE_BYTES} Bytes "
            "und wurde gekürzt."
            if console_overflow
            else ""
        )
        technical_output = "\n".join(part for part in (overflow_notice, stderr, stdout) if part).strip() or None
        try:
            try:
                with report_path.open("rb") as handle:
                    report_payload = handle.read(MAXIMUM_KOSIT_REPORT_BYTES + 1)
            except FileNotFoundError:
                raise _ReportBindingError(
                    "KOSIT-REPORT", "KoSIT hat keinen Prüfbericht für die gebundene Prüfung geschrieben."
                ) from None
            except OSError as exc:
                raise _ReportBindingError("KOSIT-REPORT", f"Der KoSIT-Prüfbericht ist nicht lesbar: {exc}") from exc
            if len(report_payload) > MAXIMUM_KOSIT_REPORT_BYTES:
                raise _ReportBindingError(
                    "KOSIT-REPORT",
                    f"Der KoSIT-Prüfbericht überschreitet das Bytebudget von {MAXIMUM_KOSIT_REPORT_BYTES} Bytes.",
                )
            accepted, findings, scenario_name = self._bind_report(report_payload, ruleset, xml_bytes)
        except _ReportBindingError as exc:
            diagnostic = exc.message
            finding_id = exc.finding_id
            if self._looks_like_startup_failure(technical_output or ""):
                # A Java/JAR failure says nothing about the invoice; the console
                # output is the only evidence and is attached for diagnosis.
                finding_id = "KOSIT-EXEC"
                summary = "KoSIT-Prüfung wurde wegen einer technischen Start- oder JAR-Konfigurationsstörung nicht ausgeführt."
            elif finding_id == "KOSIT-SCENARIO":
                summary = "KoSIT hat kein passendes Prüfszenario angewendet; es liegt keine Rechnungsentscheidung vor."
            else:
                summary = (
                    "KoSIT-Prüfung lieferte keinen an Szenario und Dokument gebundenen Prüfbericht und wurde "
                    "daher nicht als Rechnungsprüfung gewertet."
                )
            if finding_id != "KOSIT-SCENARIO" and technical_output:
                diagnostic = f"{diagnostic} Prozessausgabe: {technical_output[:1500]}"
            return self._not_executed(
                state,
                summary=summary,
                message=diagnostic,
                finding_id=finding_id,
                exit_code=completed.returncode,
                technical_output=technical_output,
            )

        summary = (
            f"KoSIT-Prüfung erfolgreich: Rechnung wurde nach {ruleset.label} akzeptiert."
            if accepted
            else f"KoSIT-Prüfung abgeschlossen: Rechnung wurde nach {ruleset.label} abgelehnt."
        )
        summary += (
            f" Bewertung im Bericht: {'accept' if accepted else 'reject'}."
            f" Prüfszenario: {scenario_name}."
            f" Regelstand: {ruleset.rules_version} ({ruleset.publisher}), KoSIT Validator als Prüfmaschine."
        )
        for note in ruleset.summary_notes:
            summary += f" {note}"
        exit_code_accepts = completed.returncode == 0
        if exit_code_accepts != accepted:
            findings.append(
                {
                    "id": "KOSIT-RESULT-MISMATCH",
                    "severity": "warning",
                    "title": "KoSIT-Bericht und Prozess-Rückgabecode widersprechen sich",
                    "message": (
                        f"Der XML-Bericht bewertet die Rechnung als {'akzeptiert' if accepted else 'abgelehnt'}, "
                        f"der Prozess endete jedoch mit Rückgabecode {completed.returncode}. "
                        "Für die Anzeige wurde die ausdrückliche Bewertung im XML-Bericht verwendet."
                    ),
                    "location": None,
                    "actual": str(completed.returncode),
                    "expected": "0 bei Annahme, ungleich 0 bei Ablehnung",
                    "source": "KoSIT Validator",
                }
            )
        if console_overflow:
            findings.append(
                {
                    "id": "KOSIT-OUTPUT-TRUNCATED",
                    "severity": "warning",
                    "title": "KoSIT-Konsolenausgabe wurde begrenzt",
                    "message": (
                        "Die KoSIT-Konsolenausgabe überschritt das feste Bytebudget und wurde gekürzt. "
                        "Die ausdrückliche Entscheidung im vollständig gelesenen und gebundenen VARL-Bericht "
                        "bleibt maßgeblich."
                    ),
                    "location": None,
                    "actual": None,
                    "expected": None,
                    "source": "KoSIT-Anbindung",
                }
            )
        deviations = sorted({str(item["id"]) for item in findings} & ruleset.known_deviation_rules)
        if deviations:
            # Transparent processing notice; the official finding itself stays untouched.
            findings.append(
                {
                    "id": "KOSIT-RULE-NOTICE",
                    "severity": "info",
                    "title": "Hinweis zu bekannter Regelabweichung",
                    "message": (
                        f"Die Regel(n) {', '.join(deviations)} des Release Factur-X 1.09.2 weichen in bekannter Weise ab: "
                        "Unterpositions-XPath in BR-FXEXT-X-01 und Rundung in BR-FXEXT-CO-10/12/13. FNFE-MPE hat die "
                        "Korrektur fix-FR04 (v1.4.0.04) veröffentlicht, die Übernahme ist für Factur-X 1.10 / ZUGFeRD 2.6 "
                        "angekündigt. Der offizielle Befund bleibt unverändert; dieser Hinweis ist eine Einordnung des Prüfers."
                    ),
                    "location": None,
                    "actual": ", ".join(deviations),
                    "expected": None,
                    "source": "KoSIT-Anbindung",
                    "profile": ruleset.profile_id,
                    "version": ruleset.rules_version,
                }
            )
        if not accepted and not any(item["severity"] == "error" for item in findings):
            findings.append(
                {
                    "id": "KOSIT-REJECT",
                    "severity": "error",
                    "title": "KoSIT-Validator hat die Rechnung abgelehnt",
                    "message": "Der KoSIT-Prüfbericht enthält eine Ablehnungsentscheidung ohne separat extrahierte Einzelmeldung.",
                    "location": None,
                    "actual": str(completed.returncode),
                    "expected": "Annahmeentscheidung im KoSIT-Bericht",
                    "source": "KoSIT Validator",
                    "profile": ruleset.profile_id,
                    "version": ruleset.rules_version,
                }
            )
        raw_report = report_payload.decode("utf-8", errors="replace")
        return {
            **state,
            "executed": True,
            "accepted": accepted,
            "exit_code": completed.returncode,
            "summary": summary,
            "findings": findings,
            "raw_report": raw_report,
            "technical_output": "\n".join(part for part in (overflow_notice, stderr) if part) or None,
            "report_source": "file",
            "scenario": scenario_name,
        }

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
                    "expected": "Ausführbares Standalone-JAR, gültige KoSIT-Konfiguration und gebundener XML-Prüfbericht",
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
            "scenario": None,
        }

    @staticmethod
    def _looks_like_startup_failure(text: str) -> bool:
        lowered = text.lower()
        return any(pattern in lowered for pattern in TECHNICAL_START_PATTERNS)

    def validate(self, xml_bytes: bytes, filename: str, ruleset: OfficialRuleset | None = None) -> dict[str, Any]:
        """Run the bound validation of ``xml_bytes`` for ``ruleset``.

        ``filename`` is the caller's name of the document and is kept for the
        call contract only: KoSIT always receives the fixed name
        ``rechnung.xml``. Without a rule set no process is started, because a
        report could not be bound to an expected scenario.
        """

        state = self.configuration_state(ruleset)
        if ruleset is None:
            return self._not_executed(
                state,
                summary=(
                    "Offizielle KoSIT-Prüfung wurde nicht ausgeführt: Für das Dokument ist kein gebundenes "
                    "Prüfszenario bestimmt."
                ),
                message=(
                    "Ohne Regelsatz aus app/official_rulesets.py startet die Anwendung keinen ungebundenen KoSIT-Lauf, "
                    "weil ein Bericht nicht an Szenario und Dokument gebunden werden könnte."
                ),
                finding_id="KOSIT-SCENARIO",
            )
        if not state["configured"]:
            detail = " ".join(state.get("problems") or [])
            return self._not_executed(
                state,
                summary=f"Offizielle KoSIT-Prüfung ist nicht konfiguriert. {detail}".strip(),
                message=detail or None,
            )

        scenarios, repositories = self._ruleset_paths(ruleset)
        with _kosit_temporary_directory() as temp_path:
            temp_dir = str(temp_path)
            invoice_path = temp_path / _BOUND_INVOICE_FILENAME
            report_directory = temp_path / "reports"
            report_directory.mkdir()

            command = [
                self.settings.kosit_java_bin,
                "-jar",
                str(self.settings.kosit_validator_jar),
            ]
            for scenario in scenarios:
                command.extend(["-s", str(scenario)])
            for repository in repositories:
                command.extend(["-r", str(repository)])

            # KoSIT serializes a report for every check. Reading that file is
            # the only accepted source; '-p/--print' and console output are not
            # used, because they cannot be bound to the document.
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

            # A bound run reads exactly the report KoSIT writes for the fixed
            # file name; console fallbacks and mtime guesses are not accepted.
            return self._finish_bound_validation(
                state,
                ruleset,
                xml_bytes,
                completed,
                report_directory / f"{invoice_path.stem}-report.xml",
            )
