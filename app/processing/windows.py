"""Creation-time Windows job containment; no invoice parsing or global state.

JOB_LIST assigns the new process before any thread can run. CREATE_SUSPENDED
allows a membership check before ResumeThread; AssignProcessToJobObject is
intentionally never used as a fallback. Existing parent job chains are inherited
by Windows, so the sole outer job handle stays in the controlling parent.

Microsoft contracts:
https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute
https://learn.microsoft.com/windows/win32/procthread/nested-jobs
"""

from __future__ import annotations

import ctypes
import math
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PureWindowsPath
from threading import RLock
from typing import Any

_DWORD = ctypes.c_uint32
_WORD = ctypes.c_uint16
_BOOL = ctypes.c_int32
_HANDLE = ctypes.c_void_p
_SIZE_T = ctypes.c_size_t
_LPVOID = ctypes.c_void_p
_LPCWSTR = ctypes.c_wchar_p
_KILL_ON_CLOSE = 0x2000
_JOB_MEMORY = 0x200
_PROCESS_MEMORY = 0x100
_ACTIVE_PROCESS = 0x8
_EXTENDED_LIMIT_INFORMATION = 9
_BASIC_ACCOUNTING_INFORMATION = 1
_HANDLE_LIST = 0x20002
_JOB_LIST = 0x2000D
_STARTF_USESTDHANDLES = 0x100
_CREATE_SUSPENDED = 0x4
_CREATE_UNICODE_ENVIRONMENT = 0x400
_EXTENDED_STARTUPINFO_PRESENT = 0x80000
_DETACHED_PROCESS = 0x8
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF
_ERROR_INSUFFICIENT_BUFFER = 122


class _FileTime(ctypes.Structure):
    _fields_ = [("low", _DWORD), ("high", _DWORD)]


class _IOCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", _SIZE_T),
        ("MaximumWorkingSetSize", _SIZE_T),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", _SIZE_T),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IOCounters),
        ("ProcessMemoryLimit", _SIZE_T),
        ("JobMemoryLimit", _SIZE_T),
        ("PeakProcessMemoryUsed", _SIZE_T),
        ("PeakJobMemoryUsed", _SIZE_T),
    ]


class _BasicAccounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", _DWORD),
        ("TotalProcesses", _DWORD),
        ("ActiveProcesses", _DWORD),
        ("TotalTerminatedProcesses", _DWORD),
    ]


class _ProcessIds(ctypes.Structure):
    _fields_ = [
        ("NumberOfAssignedProcesses", _DWORD),
        ("NumberOfProcessIdsInList", _DWORD),
        ("ProcessIdList", _SIZE_T * 64),
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", _DWORD),
        ("lpReserved", _LPCWSTR),
        ("lpDesktop", _LPCWSTR),
        ("lpTitle", _LPCWSTR),
        ("dwX", _DWORD),
        ("dwY", _DWORD),
        ("dwXSize", _DWORD),
        ("dwYSize", _DWORD),
        ("dwXCountChars", _DWORD),
        ("dwYCountChars", _DWORD),
        ("dwFillAttribute", _DWORD),
        ("dwFlags", _DWORD),
        ("wShowWindow", _WORD),
        ("cbReserved2", _WORD),
        ("lpReserved2", _LPVOID),
        ("hStdInput", _HANDLE),
        ("hStdOutput", _HANDLE),
        ("hStdError", _HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", _LPVOID)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [("hProcess", _HANDLE), ("hThread", _HANDLE), ("dwProcessId", _DWORD), ("dwThreadId", _DWORD)]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", _DWORD), ("lpSecurityDescriptor", _LPVOID), ("bInheritHandle", _BOOL)]


@dataclass(frozen=True)
class _Limits:
    memory_bytes: int
    process_memory_bytes: int | None
    active_processes: int

    @property
    def flags(self) -> int:
        return _KILL_ON_CLOSE | _JOB_MEMORY | _ACTIVE_PROCESS | (_PROCESS_MEMORY if self.process_memory_bytes else 0)


def _positive(value: int, maximum: int, label: str) -> None:
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError(f"Ungültiger Windows-Prozessparameter: {label}.")


def _handles(values: Sequence[int]) -> tuple[int, ...]:
    result = tuple(values)
    for value in result:
        _positive(value, sys.maxsize, "Handle")
    if len(set(result)) != len(result):
        raise ValueError("Doppelte Windows-Prozesshandles.")
    return result


def _absolute(value: str) -> bool:
    return isinstance(value, str) and "\0" not in value and PureWindowsPath(value).is_absolute()


def _process_parameters(
    command: Sequence[str], environment: Mapping[str, str], cwd: str | None
) -> tuple[str, str, str]:
    if isinstance(command, (str, bytes)) or not command or not _absolute(command[0]):
        raise ValueError("Der Windows-Prozess benötigt einen absoluten Programmpfad.")
    if any(not isinstance(arg, str) or "\0" in arg for arg in command):
        raise ValueError("Ungültige Windows-Prozessargumente.")
    if cwd is not None and not _absolute(cwd):
        raise ValueError("Der Windows-Prozess benötigt ein absolutes Arbeitsverzeichnis.")
    line = subprocess.list2cmdline(command)
    keys: set[str] = set()
    for key, value in environment.items():
        if (
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\0" in key
            or not isinstance(value, str)
            or "\0" in value
            or key.upper() in keys
        ):
            raise ValueError("Ungültige oder mehrdeutige Windows-Prozessumgebung.")
        keys.add(key.upper())
    block = (
        "\0".join(f"{key}={value}" for key, value in sorted(environment.items(), key=lambda pair: pair[0].upper()))
        + "\0\0"
    )
    if len(line.encode("utf-16-le")) // 2 + 1 > 32767 or len(block.encode("utf-16-le")) // 2 > 32767:
        raise ValueError("Windows-Prozessparameter überschreiten die sichere Größenbegrenzung.")
    return command[0], line, block


class _Win32:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows-Prozessbegrenzung ist auf dieser Plattform nicht verfügbar.")
        self.dll: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        declarations = {
            "CreateJobObjectW": ([_LPVOID, _LPCWSTR], _HANDLE),
            "SetInformationJobObject": ([_HANDLE, ctypes.c_int, _LPVOID, _DWORD], _BOOL),
            "QueryInformationJobObject": ([_HANDLE, ctypes.c_int, _LPVOID, _DWORD, _LPVOID], _BOOL),
            "GetHandleInformation": ([_HANDLE, ctypes.POINTER(_DWORD)], _BOOL),
            "CloseHandle": ([_HANDLE], _BOOL),
            "DuplicateHandle": ([_HANDLE, _HANDLE, _HANDLE, ctypes.POINTER(_HANDLE), _DWORD, _BOOL, _DWORD], _BOOL),
            "TerminateJobObject": ([_HANDLE, ctypes.c_uint], _BOOL),
            "AssignProcessToJobObject": ([_HANDLE, _HANDLE], _BOOL),
            "TerminateProcess": ([_HANDLE, ctypes.c_uint], _BOOL),
            "WaitForSingleObject": ([_HANDLE, _DWORD], _DWORD),
            "GetExitCodeProcess": ([_HANDLE, ctypes.POINTER(_DWORD)], _BOOL),
            "GetProcessTimes": ([_HANDLE, *([ctypes.POINTER(_FileTime)] * 4)], _BOOL),
            "IsProcessInJob": ([_HANDLE, _HANDLE, ctypes.POINTER(_BOOL)], _BOOL),
            "ResumeThread": ([_HANDLE], _DWORD),
            "CreateFileW": ([_LPCWSTR, _DWORD, _DWORD, _LPVOID, _DWORD, _DWORD, _HANDLE], _HANDLE),
            "InitializeProcThreadAttributeList": ([_LPVOID, _DWORD, _DWORD, ctypes.POINTER(_SIZE_T)], _BOOL),
            "UpdateProcThreadAttribute": ([_LPVOID, _DWORD, _SIZE_T, _LPVOID, _SIZE_T, _LPVOID, _LPVOID], _BOOL),
            "DeleteProcThreadAttributeList": ([_LPVOID], None),
            "CreateProcessW": (
                [_LPCWSTR, _LPVOID, _LPVOID, _LPVOID, _BOOL, _DWORD, _LPVOID, _LPCWSTR, _LPVOID, _LPVOID],
                _BOOL,
            ),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.dll, name)
            function.argtypes = arguments
            function.restype = result

    @staticmethod
    def _error(operation: str) -> OSError:
        return OSError(int(vars(ctypes)["get_last_error"]()), f"Windows-Prozesssteuerung fehlgeschlagen: {operation}.")

    def _check(self, result: object, operation: str) -> None:
        if not result:
            raise self._error(operation)

    def create_job(self) -> int:
        handle = self.dll.CreateJobObjectW(None, None)  # unnamed and non-inheritable
        self._check(handle, "CreateJobObject")
        return int(handle)

    def is_inheritable(self, handle: int) -> bool:
        flags = _DWORD()
        self._check(self.dll.GetHandleInformation(handle, ctypes.byref(flags)), "GetHandleInformation")
        return bool(flags.value & 1)

    def set_limits(self, handle: int, limits: _Limits) -> None:
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = limits.flags
        info.BasicLimitInformation.ActiveProcessLimit = limits.active_processes
        info.JobMemoryLimit = limits.memory_bytes
        info.ProcessMemoryLimit = limits.process_memory_bytes or 0
        self._check(
            self.dll.SetInformationJobObject(
                handle, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
            ),
            "SetInformationJobObject",
        )

    def get_limits(self, handle: int) -> _Limits:
        info = _ExtendedLimits()
        self._check(
            self.dll.QueryInformationJobObject(
                handle, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info), None
            ),
            "QueryInformationJobObject",
        )
        limits = _Limits(
            int(info.JobMemoryLimit),
            int(info.ProcessMemoryLimit) or None,
            int(info.BasicLimitInformation.ActiveProcessLimit),
        )
        if info.BasicLimitInformation.LimitFlags != limits.flags:
            raise OSError("Unzulässige oder unvollständige Windows-Jobbegrenzung.")
        return limits

    def active_process_count(self, handle: int) -> int:
        info = _BasicAccounting()
        self._check(
            self.dll.QueryInformationJobObject(
                handle, _BASIC_ACCOUNTING_INFORMATION, ctypes.byref(info), ctypes.sizeof(info), None
            ),
            "QueryInformationJobObject",
        )
        return int(info.ActiveProcesses)

    def process_ids(self, handle: int) -> tuple[int, ...]:
        info = _ProcessIds()
        self._check(
            self.dll.QueryInformationJobObject(handle, 3, ctypes.byref(info), ctypes.sizeof(info), None),
            "QueryInformationJobObject(process ids)",
        )
        if not 0 <= info.NumberOfProcessIdsInList <= info.NumberOfAssignedProcesses <= 64:
            raise OSError("Ungültige Windows-Jobprozessliste.")
        result = tuple(int(info.ProcessIdList[index]) for index in range(info.NumberOfProcessIdsInList))
        if any(pid <= 0 for pid in result) or len(set(result)) != len(result):
            raise OSError("Ungültige Windows-Prozessidentität.")
        return result

    def assign_current_process(self, job: int) -> None:
        self._check(self.dll.AssignProcessToJobObject(job, _HANDLE(-1)), "AssignProcessToJobObject(current)")

    def close_handle(self, handle: int) -> None:
        self._check(self.dll.CloseHandle(handle), "CloseHandle")

    def terminate_job(self, handle: int, exit_code: int) -> None:
        self._check(self.dll.TerminateJobObject(handle, exit_code), "TerminateJobObject")

    def terminate_process(self, handle: int, exit_code: int) -> None:
        if not self.dll.TerminateProcess(handle, exit_code) and not self.wait(handle, 0):
            raise self._error("TerminateProcess")

    def wait(self, handle: int, milliseconds: int) -> bool:
        result = self.dll.WaitForSingleObject(handle, milliseconds)
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        raise self._error("WaitForSingleObject")

    def exit_code(self, handle: int) -> int:
        result = _DWORD()
        self._check(self.dll.GetExitCodeProcess(handle, ctypes.byref(result)), "GetExitCodeProcess")
        return int(result.value)

    def creation_time(self, handle: int) -> int:
        created, exited, kernel, user = (_FileTime() for _ in range(4))
        self._check(
            self.dll.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
            ),
            "GetProcessTimes",
        )
        return (int(created.high) << 32) | int(created.low)

    def duplicate_process_handle(self, handle: int) -> int:
        duplicate = _HANDLE()
        self._check(
            self.dll.DuplicateHandle(_HANDLE(-1), handle, _HANDLE(-1), ctypes.byref(duplicate), 0, False, 2),
            "DuplicateHandle",
        )
        return int(duplicate.value or 0)

    def is_in_job(self, process: int, job: int) -> bool:
        value = _BOOL()
        self._check(self.dll.IsProcessInJob(process, job, ctypes.byref(value)), "IsProcessInJob")
        return bool(value.value)

    def resume(self, thread: int) -> None:
        # Exactly the one suspension requested at creation must be released.
        if self.dll.ResumeThread(thread) != 1:
            raise self._error("ResumeThread")

    def null_handle(self) -> int:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
        handle = self.dll.CreateFileW("NUL", 0xC0000000, 3, ctypes.byref(attributes), 3, 0, None)
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise self._error("CreateFile(NUL)")
        return int(handle)

    def create_process(
        self,
        *,
        application: str,
        command_line: str,
        environment: str,
        cwd: str | None,
        jobs: tuple[int, ...],
        handles: tuple[int, ...],
        stdio: tuple[int, int, int],
    ) -> tuple[int, int, int]:
        size = _SIZE_T()
        result = self.dll.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        if (
            result
            or int(vars(ctypes)["get_last_error"]()) != _ERROR_INSUFFICIENT_BUFFER
            or not 0 < size.value <= 1024 * 1024
        ):
            raise self._error("InitializeProcThreadAttributeList(size)")
        storage = ctypes.create_string_buffer(size.value)
        self._check(
            self.dll.InitializeProcThreadAttributeList(storage, 2, 0, ctypes.byref(size)),
            "InitializeProcThreadAttributeList",
        )
        # Both arrays must live through DeleteProcThreadAttributeList.
        job_array = (_HANDLE * len(jobs))(*jobs)
        handle_array = (_HANDLE * len(handles))(*handles)
        try:
            for key, array in ((_JOB_LIST, job_array), (_HANDLE_LIST, handle_array)):
                self._check(
                    self.dll.UpdateProcThreadAttribute(storage, 0, key, array, ctypes.sizeof(array), None, None),
                    "UpdateProcThreadAttribute",
                )
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = stdio
            startup.lpAttributeList = ctypes.cast(storage, _LPVOID)
            info = _ProcessInformation()
            command_buffer = ctypes.create_unicode_buffer(command_line)
            environment_buffer = ctypes.create_unicode_buffer(environment)
            # A hidden console can still add conhost.exe to the job. Detached
            # roles need no console; stdio remains the explicit HANDLE_LIST.
            # https://learn.microsoft.com/windows/win32/procthread/process-creation-flags
            flags = _CREATE_SUSPENDED | _DETACHED_PROCESS | _CREATE_UNICODE_ENVIRONMENT | _EXTENDED_STARTUPINFO_PRESENT
            self._check(
                self.dll.CreateProcessW(
                    application,
                    command_buffer,
                    None,
                    None,
                    True,
                    flags,
                    environment_buffer,
                    cwd,
                    ctypes.byref(startup),
                    ctypes.byref(info),
                ),
                "CreateProcess",
            )
            return int(info.hProcess), int(info.hThread), int(info.dwProcessId)
        finally:
            self.dll.DeleteProcThreadAttributeList(storage)


def _load_api() -> _Win32:
    return _Win32()


class NativeProcess:
    """A process handle, not the owner of its containing job or descendants."""

    def __init__(self, api: _Win32, handle: int, pid: int, command: Sequence[str]) -> None:
        self._api = api
        self._handle = handle
        self.pid = pid
        self.returncode: int | None = None
        self._command = tuple(command)
        self._lock = RLock()

    def _open_handle(self) -> int:
        if not self._handle:
            raise ValueError("Windows-Prozesshandle ist geschlossen.")
        return self._handle

    def creation_time(self) -> int:
        # Read the original owned process object, never reopen an unbound PID.
        with self._lock:
            return self._api.creation_time(self._open_handle())

    def poll(self) -> int | None:
        with self._lock:
            if self.returncode is not None:
                return self.returncode
            handle = self._open_handle()
            if self._api.wait(handle, 0):
                self.returncode = self._api.exit_code(handle)
            return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if timeout is None:
            milliseconds = _INFINITE
        elif type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 86400:
            raise ValueError("Ungültige Windows-Prozesswartefrist.")
        else:
            milliseconds = math.ceil(timeout * 1000)
        with self._lock:
            if self.returncode is not None:
                return self.returncode
            # Wait on a private non-inheritable reference. Do not hold the lock
            # needed by a concurrent kill, and do not let close invalidate the
            # handle being waited upon.
            handle = self._api.duplicate_process_handle(self._open_handle())
        try:
            if not self._api.wait(handle, milliseconds):
                if timeout is None:
                    raise OSError("Die unbegrenzte Windows-Prozesswarteoperation ist fehlgeschlagen.")
                raise subprocess.TimeoutExpired(self._command, timeout)
            code = self._api.exit_code(handle)
            with self._lock:
                self.returncode = code
            return code
        finally:
            self._api.close_handle(handle)

    def kill(self) -> None:
        with self._lock:
            self._api.terminate_process(self._open_handle(), 1)

    def close(self) -> None:
        with self._lock:
            if self._handle:
                self._api.close_handle(self._handle)
                self._handle = 0


class WindowsJob:
    """Non-inheritable kill-on-close job, configured before any spawn.

    memory_bytes is aggregate private commit, not RSS or virtual address space.
    Children inherit the parent's existing job chain. Optional job_handles are
    additional local non-inheritable jobs, in outer-to-inner order; this job is
    appended last. Incompatible nesting fails without a breakaway retry.
    """

    def __init__(self, memory_bytes: int, *, active_processes: int, process_memory_bytes: int | None = None) -> None:
        _positive(memory_bytes, sys.maxsize, "Jobspeicher")
        _positive(active_processes, 64, "Prozessanzahl")
        if process_memory_bytes is not None:
            _positive(process_memory_bytes, memory_bytes, "Prozessspeicher")
        self._lock = RLock()
        self._api = _load_api()
        self._handle = self._api.create_job()
        self._limits = _Limits(memory_bytes, process_memory_bytes, active_processes)
        try:
            if self._api.is_inheritable(self._handle):
                raise OSError("Windows-Jobhandle darf nicht vererbt werden.")
            self._api.set_limits(self._handle, self._limits)
            if self._api.get_limits(self._handle) != self._limits:
                raise OSError("Windows-Jobgrenzen konnten nicht bestätigt werden.")
        except BaseException:
            self.close()
            raise

    @property
    def handle(self) -> int:
        with self._lock:
            if not self._handle:
                raise ValueError("Windows-Jobhandle ist geschlossen.")
            return self._handle

    def spawn(
        self,
        command: Sequence[str],
        environment: Mapping[str, str],
        *,
        inherited_handles: Sequence[int] = (),
        job_handles: Sequence[int] = (),
        stdio_handles: tuple[int, int, int] | None = None,
        cwd: str | None = None,
    ) -> NativeProcess:
        application, command_line, block = _process_parameters(command, environment, cwd)
        with self._lock:
            jobs = _handles((*job_handles, self.handle))
            handles = _handles(inherited_handles)
            if stdio_handles is not None:
                if len(stdio_handles) != 3:
                    raise ValueError("Windows-stdio benötigt genau drei Handles.")
                for handle in stdio_handles:
                    _positive(handle, sys.maxsize, "stdio-Handle")
                handles = tuple(dict.fromkeys((*handles, *stdio_handles)))
            if set(jobs).intersection(handles):
                raise ValueError("Jobhandles dürfen nicht vererbt werden.")
            if any(self._api.is_inheritable(handle) for handle in jobs):
                raise OSError("Windows-Jobhandles müssen unvererbbar sein.")
            if any(not self._api.is_inheritable(handle) for handle in handles):
                raise OSError("Explizite IPC-Handles müssen vererbbar sein.")
            owned_null = self._api.null_handle() if stdio_handles is None else None
            process_handle = thread_handle = 0
            try:
                if owned_null is not None:
                    handles += (owned_null,)
                    stdio_handles = (owned_null, owned_null, owned_null)
                assert stdio_handles is not None
                process_handle, thread_handle, pid = self._api.create_process(
                    application=application,
                    command_line=command_line,
                    environment=block,
                    cwd=cwd,
                    jobs=jobs,
                    handles=handles,
                    stdio=stdio_handles,
                )
                if not all(self._api.is_in_job(process_handle, handle) for handle in jobs):
                    raise OSError("Die Erzeugungsbindung an den Windows-Job fehlt.")
                self._api.resume(thread_handle)
                closing_thread, thread_handle = thread_handle, 0
                self._api.close_handle(closing_thread)
                if owned_null is not None:
                    closing_null, owned_null = owned_null, None
                    self._api.close_handle(closing_null)
                process = NativeProcess(self._api, process_handle, pid, command)
                process_handle = 0  # ownership transferred only after successful resume
                return process
            except BaseException:
                if process_handle:
                    self._api.terminate_process(process_handle, 1)
                    if not self._api.wait(process_handle, 5000):
                        raise OSError("Der fehlgeschlagene Windows-Prozessstart konnte nicht beendet werden.") from None
                raise
            finally:
                close_error: BaseException | None = None
                for cleanup_handle in (thread_handle, process_handle, owned_null):
                    if cleanup_handle:
                        try:
                            self._api.close_handle(cleanup_handle)
                        except BaseException as error:
                            close_error = close_error or error
                if close_error is not None:
                    raise close_error

    def terminate(self, exit_code: int = 1) -> None:
        if type(exit_code) is not int or not 0 <= exit_code <= 0xFFFFFFFF:
            raise ValueError("Ungültiger Windows-Prozessstatus.")
        with self._lock:
            self._api.terminate_job(self.handle, exit_code)

    def active_process_count(self) -> int:
        with self._lock:
            return self._api.active_process_count(self.handle)

    def process_ids(self) -> tuple[int, ...]:
        with self._lock:
            return self._api.process_ids(self.handle)

    def lower_memory_limit(self, memory_bytes: int, process_memory_bytes: int | None = None) -> None:
        """Seal a smaller post-import commit budget before sending invoice input.

        Omitting the process cap preserves an existing cap (clamped down to the
        smaller aggregate cap). A failed Set/Query closes the kill-on-close job.
        """
        with self._lock:
            handle = self.handle
            _positive(memory_bytes, self._limits.memory_bytes, "Jobspeicher")
            if process_memory_bytes is None and self._limits.process_memory_bytes is not None:
                process_memory_bytes = min(memory_bytes, self._limits.process_memory_bytes)
            if process_memory_bytes is not None:
                _positive(
                    process_memory_bytes,
                    min(memory_bytes, self._limits.process_memory_bytes or memory_bytes),
                    "Prozessspeicher",
                )
            limits = _Limits(memory_bytes, process_memory_bytes, self._limits.active_processes)
            try:
                self._api.set_limits(handle, limits)
                if self._api.get_limits(handle) != limits:
                    raise OSError("Die abgesenkten Windows-Speichergrenzen konnten nicht bestätigt werden.")
            except BaseException:
                self.close()
                raise
            self._limits = limits

    def assign_current_process(self) -> None:
        """Lower the trusted supervisor's limits AFTER creating its children.

        This explicitly binds only the calling, already running process. It is
        never a child-spawn fallback. Existing children keep their original job
        chains, so Java does not inherit this supervisor's smaller memory cap.
        """
        with self._lock:
            self._api.assign_current_process(self.handle)
            if not self._api.is_in_job(-1, self.handle):
                raise OSError("Die Windows-Supervisorbindung konnte nicht bestätigt werden.")

    def close(self) -> None:
        with self._lock:
            if self._handle:
                self._api.close_handle(self._handle)
                self._handle = 0

    def __enter__(self) -> WindowsJob:
        _ = self.handle
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
