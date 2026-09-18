"""Bounded Windows package lifecycle probes inside an already consumed CI context.

Only fixed synthetic inputs and held, validated kernel handles are used. This
harness never installs, restarts, restores or cleans up a product installation.
A controlled-stop case publishes a bound READY for its existing PS controller.
Missing active-phase evidence is INCONCLUSIVE, never a successful kill test.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import http.client
import importlib
import json
import math
import os
import socket
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import acceptance_context as acceptance  # noqa: E402
from scripts.processing_smoke import large_example, maximum_xml, windows_memory_counters  # noqa: E402

CASES = ("held-responses", "health", "worker-death", "supervisor-death", "parent-death", "controlled-stop", "xml25")
MAX_RESULT = 64 * 1024**2
HELD_XML_BYTES = 25 * 1024**2
HELD_SEND_SECONDS = 25.0  # Five seconds below the unchanged product send deadline.
HELD_OBSERVE_SECONDS = 15.0
_native_ctypes: Any = ctypes
PROCESS_ACCESS = 0x1000 | 0x400 | 0x40 | 0x100000 | 1
OBSERVATION_COUNTER_MAX = 2**31 - 1


class ProbeError(RuntimeError):
    pass


class Inconclusive(ProbeError):
    pass


def failure_details(exc: BaseException) -> dict[str, Any]:
    """Fixed, numeric error evidence; never exception text, argv or handles."""
    result: dict[str, Any] = {"error_class": type(exc).__name__[:80]}
    for name in ("errno", "winerror"):
        value = getattr(exc, name, None)
        if type(value) is int:
            result[name] = value
    details = getattr(exc, "_probe_native", None)
    if isinstance(details, dict):
        result.update(details)
    return result


def native_failure(exc: BaseException, api: str, *, domain: str = "win32", **codes: int) -> BaseException:
    # All callers supply fixed API labels, never native exception messages.
    if not hasattr(exc, "_probe_native"):
        exc.__dict__["_probe_native"] = {"api": api, "domain": domain, **codes}
    return exc


@dataclass(frozen=True)
class PackageBinding:
    parent_pid: int
    parent_created: int
    executable: str
    executable_sha256: str
    owner_sid: str
    service_sid: str | None
    port: int
    mode: str


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    created: int
    image: str
    owner_sid: str
    groups: tuple[str, ...]
    parent_pid: int
    argv: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        # Never emit command lines, pipe handles, token paths or token values.
        return {key: value for key, value in asdict(self).items() if key != "argv"}


def new_input_observation(identity: ProcessIdentity) -> dict[str, Any]:
    def counters() -> dict[str, Any]:
        return {
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "last_seconds": None,
            "max_seconds": None,
            "counter_saturated": False,
        }

    return {
        "source": {"pid": identity.pid, "created": identity.created, "role": "worker", "channel": "input"},
        "scope": "existing calls only; measured durations are not individual call deadlines",
        "duplicate": counters(),
        "peek": counters(),
        "marker_seen": False,
    }


def _observation_start(observation: dict[str, Any] | None) -> float | None:
    if observation is not None:
        try:
            return time.monotonic()
        except BaseException:
            pass
    return None


def _record_observation_call(
    observation: dict[str, Any] | None, name: str, started: float | None, success: bool
) -> None:
    if observation is None:
        return
    counter = observation[name]
    for key in ("attempts", "successes" if success else "failures"):
        if counter[key] == OBSERVATION_COUNTER_MAX:
            counter["counter_saturated"] = True
        else:
            counter[key] += 1
    try:
        duration = time.monotonic() - started if started is not None else None
        if duration is None or not math.isfinite(duration) or duration < 0:
            raise ValueError("Invalid diagnostic clock")
        counter["last_seconds"] = duration
        counter["max_seconds"] = max(counter["max_seconds"] or 0.0, duration)
    except BaseException:
        # A clock/diagnostic failure must not replace the native API failure.
        counter["last_seconds"] = None
        counter["timing_unavailable"] = True


class _ObjectBasicInformation(ctypes.Structure):
    # Documented PUBLIC_OBJECT_BASIC_INFORMATION, not private object names.
    # https://learn.microsoft.com/windows/win32/api/winternl/nf-winternl-ntqueryobject
    _fields_ = [
        ("Attributes", ctypes.c_uint32),
        ("GrantedAccess", ctypes.c_uint32),
        ("HandleCount", ctypes.c_uint32),
        ("PointerCount", ctypes.c_uint32),
        ("Reserved", ctypes.c_uint32 * 10),
    ]


def _same_path(a: str, b: str) -> bool:
    return PureWindowsPath(a) == PureWindowsPath(b)


def _identity_matches(p: ProcessIdentity, b: PackageBinding) -> None:
    if not _same_path(p.image, b.executable) or p.owner_sid != b.owner_sid:
        raise ProbeError("Executable or user SID differs from the bound package.")
    if b.service_sid is not None and b.service_sid not in p.groups:
        raise ProbeError("Enabled service SID missing from process token.")


def validate_parent(p: ProcessIdentity, b: PackageBinding) -> None:
    _identity_matches(p, b)
    if p.pid != b.parent_pid or p.created != b.parent_created:
        raise ProbeError("Parent PID/creation identity differs.")
    if "--einvoice-processing" in p.argv:
        raise ProbeError("The bound parent is a processing role.")


def role_of(p: ProcessIdentity, b: PackageBinding) -> str:
    _identity_matches(p, b)
    a = p.argv
    if p.parent_pid != b.parent_pid or p.created <= b.parent_created or len(a) < 6:
        raise ProbeError("Role is not a new direct child of the bound parent.")
    role = a[2]
    if not _same_path(a[0], b.executable) or a[1] != "--einvoice-processing" or a[3] != str(b.parent_pid):
        raise ProbeError("Role command does not bind the expected parent.")
    expected = {"worker": 6, "supervisor": 8}
    if (
        role not in expected
        or len(a) != expected[role]
        or any(not s.isascii() or not s.isdecimal() or int(s) <= 0 for s in a[4:])
    ):
        raise ProbeError("Unexpected role or private handle contract.")
    if len(set(a[4:])) != len(a[4:]):
        raise ProbeError("Repeated private pipe handle.")
    return role


def validate_role_counts(counts: dict[str, int], jobs: int) -> None:
    if counts != {"supervisor": jobs, "worker": jobs}:
        raise ProbeError("Complete direct role inventory was not bound.")


def require_active(*, marker_seen: bool, requests_done: bool) -> None:
    if not marker_seen or requests_done:
        raise Inconclusive("Active synthetic input after READY could not be observed without a race.")


def validate_health(samples: list[float]) -> None:
    if len(samples) < 3 or any(not math.isfinite(s) or not 0 <= s < 1 for s in samples):
        raise ProbeError("Health response did not remain below one second for every sample.")


def processing_fixture(nonce: str) -> tuple[bytes, bytes]:
    if len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
        raise ValueError("Fixed hexadecimal nonce required.")
    marker = ("SYNTHETIC_PROCESSING_" + nonce).encode("ascii")
    source = large_example("cii")
    end = source.index(b"?>") + 2
    needed = 8 * 1024**2 - len(source) - 7
    comment = (marker + b" ") * (needed // (len(marker) + 1))
    comment += b" " * (needed - len(comment))
    payload = source[:end] + b"<!--" + comment + b"-->" + source[end:]
    assert len(payload) == 8 * 1024**2
    return payload, marker


def multipart(payload: bytes, *, export: bool) -> tuple[str, bytes]:
    boundary = "einvoice-processing-" + uuid4().hex
    fields = (
        ""
        if export
        else "".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
            for key, value in [("official", "false"), ("scope", "readable")]
        )
    )
    prefix = (
        fields
        + f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="synthetic-processing.xml"\r\nContent-Type: application/xml\r\n\r\n'
    ).encode("ascii")
    return f"multipart/form-data; boundary={boundary}", prefix + payload + f"\r\n--{boundary}--\r\n".encode("ascii")


def write_new_json(path: Path, value: Any) -> None:
    acceptance._exclusive(path, acceptance.canonical(value) + b"\n")


class WindowsAPI:
    """Direct APIs: no shell, PID kill, process injection, or consuming pipe read."""

    k: Any
    n: Any
    s: Any
    ip: Any
    security: Any

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise ProbeError("Native package probes require Windows.")
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.n = ctypes.WinDLL("ntdll")
        self.s = ctypes.WinDLL("shell32", use_last_error=True)
        self.ip = ctypes.WinDLL("iphlpapi", use_last_error=True)
        self.security = importlib.import_module("win32security")
        self.cleanup_diagnostics: list[dict[str, Any]] = []
        H, D, P = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p
        self._define(self.k, "OpenProcess", [D, ctypes.c_int, D], H)
        self._define(self.k, "CloseHandle", [H], ctypes.c_int)
        self._define(self.k, "GetProcessTimes", [H, P, P, P, P], ctypes.c_int)
        self._define(self.k, "QueryFullProcessImageNameW", [H, D, P, P], ctypes.c_int)
        self._define(self.k, "WaitForSingleObject", [H, D], D)
        self._define(self.k, "TerminateProcess", [H, D], ctypes.c_int)
        self._define(self.k, "CreateToolhelp32Snapshot", [D, D], H)
        self._define(self.k, "Process32FirstW", [H, P], ctypes.c_int)
        self._define(self.k, "Process32NextW", [H, P], ctypes.c_int)
        self._define(self.k, "DuplicateHandle", [H, H, H, P, D, ctypes.c_int, D], ctypes.c_int)
        self._define(self.k, "GetCurrentProcess", [], H)
        self._define(self.k, "PeekNamedPipe", [H, P, D, P, P, P], ctypes.c_int)
        self._define(self.k, "LocalFree", [H], H)
        self._define(self.k, "QueryPerformanceCounter", [P], ctypes.c_int)
        self._define(self.k, "QueryPerformanceFrequency", [P], ctypes.c_int)
        self._define(self.n, "NtQueryInformationProcess", [H, D, P, D, P], ctypes.c_int32)
        self._last_ntstatus: Any = None
        try:
            self._define(self.n, "NtQueryObject", [H, D, P, D, P], ctypes.c_int32)
        except (AttributeError, OSError):
            pass
        try:
            # Resolve before observation, never on the native failure path.
            self._define(self.n, "RtlGetLastNtStatus", [], ctypes.c_int32)
            self._last_ntstatus = self.n.RtlGetLastNtStatus
        except (AttributeError, OSError):
            pass
        self._define(self.s, "CommandLineToArgvW", [ctypes.c_wchar_p, P], P)
        self._define(self.ip, "GetExtendedTcpTable", [P, P, ctypes.c_int, D, D, D], D)

    @staticmethod
    def _define(lib: Any, name: str, args: list[Any], result: Any) -> None:
        f = getattr(lib, name)
        f.argtypes = args
        f.restype = result

    @staticmethod
    def _check(value: Any, api: str) -> None:
        if not value:
            code = _native_ctypes.get_last_error()
            raise native_failure(_native_ctypes.WinError(code), api, winerror=code)

    @contextmanager
    def owned(self, close: Callable[[], None], api: str = "CloseHandle") -> Iterator[None]:
        """Attempt one close; preserve a primary failure if cleanup also fails."""
        primary = False
        try:
            yield
        except BaseException:
            primary = True
            raise
        finally:
            try:
                close()
            except BaseException as exc:
                if not hasattr(self, "cleanup_diagnostics"):
                    self.cleanup_diagnostics = []
                if len(self.cleanup_diagnostics) < 8:
                    self.cleanup_diagnostics.append(failure_details(native_failure(exc, api)))
                if not primary:
                    raise

    def close(self, handle: int) -> None:
        self._check(self.k.CloseHandle(handle), "CloseHandle")

    def alive(self, handle: int) -> bool:
        status = self.k.WaitForSingleObject(handle, 0)
        if status not in (0, 258):
            code = _native_ctypes.get_last_error()
            raise native_failure(ProbeError("Kernel process wait failed."), "WaitForSingleObject", winerror=code)
        return status == 258

    def terminate(self, handle: int) -> None:
        self._check(self.k.TerminateProcess(handle, 71), "TerminateProcess")

    def children(self, parent: int) -> list[int]:
        class Entry(ctypes.Structure):
            _fields_ = [
                ("size", ctypes.c_uint32),
                ("usage", ctypes.c_uint32),
                ("pid", ctypes.c_uint32),
                ("heap", ctypes.c_size_t),
                ("module", ctypes.c_uint32),
                ("threads", ctypes.c_uint32),
                ("parent", ctypes.c_uint32),
                ("priority", ctypes.c_int32),
                ("flags", ctypes.c_uint32),
                ("exe", ctypes.c_wchar * 260),
            ]

        h = self.k.CreateToolhelp32Snapshot(2, 0)
        if h == ctypes.c_void_p(-1).value:
            code = _native_ctypes.get_last_error()
            raise native_failure(ProbeError("Process snapshot failed."), "CreateToolhelp32Snapshot", winerror=code)
        result = []
        with self.owned(lambda: self.close(h)):
            e = Entry()
            e.size = ctypes.sizeof(e)
            available = self.k.Process32FirstW(h, ctypes.byref(e))
            while available:
                if e.parent == parent:
                    result.append(int(e.pid))
                available = self.k.Process32NextW(h, ctypes.byref(e))
            code = _native_ctypes.get_last_error()
            if code != 18:
                raise native_failure(ProbeError("Incomplete process snapshot."), "Process32Enumeration", winerror=code)
        return result

    def _argv(self, handle: int) -> tuple[str, ...]:
        class UString(ctypes.Structure):
            _fields_ = [("length", ctypes.c_uint16), ("maximum", ctypes.c_uint16), ("buffer", ctypes.c_void_p)]

        raw = ctypes.create_string_buffer(65536)
        needed = ctypes.c_uint32()
        status = self.n.NtQueryInformationProcess(handle, 60, raw, len(raw), ctypes.byref(needed))
        if status != 0:
            raise native_failure(
                ProbeError("Kernel command line could not be bound."),
                "NtQueryInformationProcess.CommandLine",
                domain="ntstatus",
                ntstatus=status & 0xFFFFFFFF,
            )
        text = UString.from_buffer(raw)
        address = int(text.buffer or 0)
        if text.length % 2 or not ctypes.addressof(raw) <= address <= ctypes.addressof(raw) + len(raw) - text.length:
            raise ProbeError("Invalid native command line envelope.")
        command = ctypes.wstring_at(address, text.length // 2)
        count = ctypes.c_int()
        argv = self.s.CommandLineToArgvW(command, ctypes.byref(count))
        self._check(argv, "CommandLineToArgvW")
        with self.owned(lambda: self._check(not self.k.LocalFree(argv), "LocalFree"), "LocalFree"):
            if not 1 <= count.value <= 16:
                raise ProbeError("Unexpected process arguments.")
            values = ctypes.cast(argv, ctypes.POINTER(ctypes.c_wchar_p))
            return tuple(values[i] for i in range(count.value))

    def kernel_parent(self, handle: int, pid: int, expected_parent: int) -> int:
        class BasicInformation(ctypes.Structure):
            _fields_ = [
                ("exit_status", ctypes.c_int32),
                ("peb", ctypes.c_void_p),
                ("affinity", ctypes.c_size_t),
                ("priority", ctypes.c_int32),
                ("pid", ctypes.c_size_t),
                ("parent", ctypes.c_size_t),
            ]

        info = BasicInformation()
        returned = ctypes.c_uint32()
        status = self.n.NtQueryInformationProcess(
            handle, 0, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned)
        )
        if status != 0:
            raise native_failure(
                ProbeError("Native parent identity could not be queried."),
                "NtQueryInformationProcess.BasicInformation",
                domain="ntstatus",
                ntstatus=status & 0xFFFFFFFF,
            )
        if returned.value != ctypes.sizeof(info):
            raise ProbeError("Native parent identity could not be queried.")
        if info.pid != pid:
            raise ProbeError("Held process PID differs from snapshot.")
        if expected_parent and info.parent != expected_parent:
            raise ProbeError("Native parent differs from snapshot.")
        return int(info.parent)

    def qpc(self) -> tuple[int, int]:
        ticks, frequency = ctypes.c_int64(), ctypes.c_int64()
        self._check(self.k.QueryPerformanceCounter(ctypes.byref(ticks)), "QueryPerformanceCounter")
        self._check(self.k.QueryPerformanceFrequency(ctypes.byref(frequency)), "QueryPerformanceFrequency")
        if ticks.value <= 0 or frequency.value <= 0:
            raise ProbeError("Native monotonic clock invalid.")
        return ticks.value, frequency.value

    def memory(self, handle: int) -> dict[str, Any]:
        return windows_memory_counters(handle)

    def process_access(self, handle: int) -> dict[str, Any]:
        """One fixed-size query of an already bound process, never a retry/open."""
        result: dict[str, Any] = {
            "status": "unavailable",
            "api": "NtQueryObject.ObjectBasicInformation",
            "requested_access": PROCESS_ACCESS,
            "structure_size": ctypes.sizeof(_ObjectBasicInformation),
        }
        try:
            if result["structure_size"] != 56:
                return result
            info = _ObjectBasicInformation()
            returned = ctypes.c_uint32()
            status = self.n.NtQueryObject(handle, 0, ctypes.byref(info), 56, ctypes.byref(returned))
            result.update(ntstatus=status & 0xFFFFFFFF, returned_size=returned.value)
            if status == 0 and returned.value == 56:
                result.update(
                    status="observed",
                    granted_access=int(info.GrantedAccess),
                    dup_handle_granted=bool(info.GrantedAccess & 0x40),
                )
        except BaseException as exc:
            result["failure"] = failure_details(exc)
        return result

    def _correlated_status(self) -> dict[str, Any]:
        # DuplicateHandle documents GetLastError only. This optional last-NT
        # value can be stale or overwritten during ctypes/Python return; it is
        # never treated as the proven originating status or a PASS condition.
        result: dict[str, Any] = {"status": "unavailable", "origin_guaranteed": False}
        try:
            query = getattr(self, "_last_ntstatus", None)
            if query is not None:
                value = query()
                if type(value) is int and -(2**31) <= value < 2**32:
                    result.update(status="observed", value=value & 0xFFFFFFFF)
        except BaseException as exc:
            result["failure"] = failure_details(exc)
        return result

    def _security_call(self, name: str, *args: Any) -> Any:
        try:
            return getattr(self.security, name)(*args)
        except Exception as exc:
            native_failure(exc, name)
            raise

    def open(self, pid: int, parent_pid: int) -> tuple[int, ProcessIdentity]:
        # QUERY_LIMITED_INFORMATION | QUERY_INFORMATION | DUP_HANDLE | SYNCHRONIZE | TERMINATE
        h = self.k.OpenProcess(PROCESS_ACCESS, False, pid)
        self._check(h, "OpenProcess")
        try:
            times = [ctypes.c_uint64() for _ in range(4)]
            self._check(self.k.GetProcessTimes(h, *(ctypes.byref(t) for t in times)), "GetProcessTimes")
            path = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_uint32(len(path))
            self._check(self.k.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size)), "QueryFullProcessImageNameW")
            token = self._security_call("OpenProcessToken", h, 8)
            with self.owned(token.Close, "CloseTokenHandle"):
                owner = self._security_call(
                    "ConvertSidToStringSid",
                    self._security_call("GetTokenInformation", token, self.security.TokenUser)[0],
                )
                groups = tuple(
                    self._security_call("ConvertSidToStringSid", sid)
                    for sid, attributes in self._security_call("GetTokenInformation", token, self.security.TokenGroups)
                    if attributes & 4
                )
            actual_parent = self.kernel_parent(h, pid, parent_pid)
            identity = ProcessIdentity(pid, times[0].value, path.value, owner, groups, actual_parent, self._argv(h))
            if not self.alive(h):
                raise Inconclusive("Process exited before identity was bound.")
            return int(h), identity
        except BaseException:
            with self.owned(lambda: self.close(h)):
                raise

    def peek_marker(
        self, handle: int, input_handle: int, marker: bytes, observation: dict[str, Any] | None = None
    ) -> bool:
        duplicate = ctypes.c_void_p()
        current_process = self.k.GetCurrentProcess()
        started = _observation_start(observation)
        succeeded = self.k.DuplicateHandle(handle, input_handle, current_process, ctypes.byref(duplicate), 0, False, 2)
        if not succeeded:
            code = _native_ctypes.get_last_error()
            correlated = self._correlated_status()
            # End-clock uses native QPC on Windows: capture both statuses first.
            _record_observation_call(observation, "duplicate", started, False)
            if observation is not None:
                observation["last_native_failure"] = {
                    "api": "DuplicateHandle",
                    "winerror": code,
                    "correlated_last_ntstatus": correlated,
                }
            raise native_failure(_native_ctypes.WinError(code), "DuplicateHandle", winerror=code)
        _record_observation_call(observation, "duplicate", started, True)
        with self.owned(lambda: self.close(int(duplicate.value or 0))):
            raw = ctypes.create_string_buffer(65536)
            read = ctypes.c_uint32()
            available = ctypes.c_uint32()
            started = _observation_start(observation)
            succeeded = self.k.PeekNamedPipe(
                duplicate, raw, len(raw), ctypes.byref(read), ctypes.byref(available), None
            )
            if not succeeded:
                code = _native_ctypes.get_last_error()
                correlated = self._correlated_status()
                _record_observation_call(observation, "peek", started, False)
                if observation is not None:
                    observation["last_native_failure"] = {
                        "api": "PeekNamedPipe",
                        "winerror": code,
                        "correlated_last_ntstatus": correlated,
                    }
                if code in (109, 232, 233):
                    return False
                raise native_failure(
                    ProbeError("Bound input pipe cannot be observed safely."), "PeekNamedPipe", winerror=code
                )
            _record_observation_call(observation, "peek", started, True)
            found = marker in raw.raw[: read.value]
            if observation is not None:
                observation["marker_seen"] = observation["marker_seen"] or found
            return found

    def listener(self, port: int, parent: int) -> None:
        size = ctypes.c_uint32(256 * 1024)
        raw = ctypes.create_string_buffer(size.value)
        status = self.ip.GetExtendedTcpTable(raw, ctypes.byref(size), False, 2, 3, 0)
        if status != 0:
            raise native_failure(
                ProbeError("Listening port identity could not be obtained."), "GetExtendedTcpTable", winerror=status
            )
        count = ctypes.c_uint32.from_buffer(raw).value
        if count > (len(raw) - 4) // 24:
            raise ProbeError("Invalid TCP table.")
        rows = (ctypes.c_uint32 * (count * 6)).from_buffer(raw, 4)
        matches = []
        for index in range(count):
            row = list(rows[index * 6 : (index + 1) * 6])
            if socket.ntohs(row[2] & 0xFFFF) == port:
                matches.append(row)
        if not matches or any(row[1] != 0x0100007F or row[5] != parent for row in matches):
            raise ProbeError("Loopback port belongs to a different or wildcard listener.")


def context_binding(action: str) -> dict[str, Any]:
    root = Path(os.environ.get("EINVOICE_ACCEPTANCE_ROOT", ""))
    state = acceptance.verify(root)
    if state["blocked"] or state["controller"] != os.environ.get("EINVOICE_ACCEPTANCE_CONTROLLER"):
        raise ProbeError("Active acceptance controller does not match.")
    if state["binding"] != acceptance.ci_binding(state["binding"]["version"]):
        raise ProbeError("Actual CI checkout/run/attempt differs.")
    active = [v for v in state["contexts"].values() if v["status"] == "RUNNING"]
    if len(active) != 1 or active[0]["action"] != action:
        raise ProbeError("Exactly one consumed package context is required.")
    context = active[0]
    now = datetime.now(UTC)
    if (
        not acceptance.parse_timestamp(context["consumed_at_utc"])
        <= now
        < acceptance.parse_timestamp(context["expires_at_utc"])
    ):
        raise ProbeError("Consumed package context is stale.")
    script = acceptance._file(Path(__file__))
    if script not in context["artifacts"]:
        raise ProbeError("Processing harness is not artifact-bound in the consumed context.")
    return {"binding": state["binding"], "context_id": context["id"], "action": action, "harness": script}


class Request:
    def __init__(self, port: int, token: str, payload: bytes, *, export: bool = False) -> None:
        self.done = threading.Event()
        self.record: dict[str, Any] = {}
        self.error: BaseException | None = None
        self.connection = http.client.HTTPConnection("127.0.0.1", port, timeout=45)
        self._transport_socket: socket.socket | None = None
        self.thread = threading.Thread(target=self._run, args=(token, payload, export), daemon=True)

    def _run(self, token: str, payload: bytes, export: bool) -> None:
        try:
            media, body = multipart(payload, export=export)
            self.connection.request(
                "POST",
                "/api/xml" if export else "/api/report/pdf",
                body,
                {"Authorization": "Bearer " + token, "Content-Type": media},
            )
            del body
            self._transport_socket = self.connection.sock
            response = self.connection.getresponse()
            self._before_read(response, payload)
            digest = hashlib.sha256()
            size = 0
            prefix = b""
            tail = b""
            error_prefix = b""
            while chunk := self._read_chunk(response):
                if size + len(chunk) > MAX_RESULT:
                    raise ProbeError("Response exceeded the fixed test output bound.")
                if export and response.status == 200 and payload[size : size + len(chunk)] != chunk:
                    raise ProbeError("XML export differs from original bytes.")
                size += len(chunk)
                digest.update(chunk)
                prefix = (prefix + chunk)[:8]
                tail = (tail + chunk)[-1024:]
                if response.status == 503:
                    error_prefix = (error_prefix + chunk)[:4097]
            error_type = None
            if response.status == 503 and len(error_prefix) <= 4096:
                try:
                    envelope = json.loads(error_prefix)
                    candidate = envelope.get("type") if isinstance(envelope, dict) else None
                    if isinstance(candidate, str) and len(candidate) <= 80:
                        error_type = candidate
                except (ValueError, UnicodeError):
                    pass
            self.record = {
                "status": response.status,
                "bytes": size,
                "sha256": digest.hexdigest(),
                "media_type": response.getheader("Content-Type"),
                "pdf_markers": prefix.startswith(b"%PDF-") and b"%%EOF" in tail,
                "byte_identical": export and response.status == 200 and size == len(payload),
                "error_type": error_type,
            }
        except BaseException as exc:
            self.error = exc
        finally:
            self.connection.close()
            self.done.set()

    def start(self) -> None:
        self.thread.start()

    def _before_read(self, response: http.client.HTTPResponse, payload: bytes) -> None:
        pass

    def _read_chunk(self, response: http.client.HTTPResponse) -> bytes:
        return response.read(65536)

    def abort(self) -> None:
        # Keep the owned socket object, even if HTTPConnection detached it for
        # a close-delimited response. Never act on a reused numeric descriptor.
        channel = self._transport_socket or self.connection.sock
        if channel is not None:
            try:
                channel.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.connection.close()

    def close(self) -> None:
        self.connection.close()


class HeldHTTPConnection(http.client.HTTPConnection):
    def __init__(self, port: int) -> None:
        super().__init__("127.0.0.1", port, timeout=20)
        self.receive_buffer_bytes = 0

    def connect(self) -> None:
        channel = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            channel.settimeout(20)
            channel.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
            self.receive_buffer_bytes = channel.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
            if not 0 < self.receive_buffer_bytes <= 256 * 1024:
                raise ProbeError("The fixed receive buffer could not be established.")
            channel.connect(("127.0.0.1", self.port))
            self.sock = channel
        except BaseException:
            channel.close()
            raise


class HeldResponseRequest(Request):
    connection: HeldHTTPConnection

    def __init__(self, port: int, token: str, payload: bytes) -> None:
        if len(payload) != HELD_XML_BYTES:
            raise ProbeError("Only the fixed 25MiB XML response case is permitted.")
        super().__init__(port, token, payload, export=True)
        self.connection = HeldHTTPConnection(port)
        self.header_ready = threading.Event()
        self.release_reading = threading.Event()
        self.aborted = threading.Event()
        self.header_received_at = 0.0
        self.header_record: dict[str, Any] = {}
        self.body_reads = 0
        self.drain_deadline = 0.0

    def _before_read(self, response: http.client.HTTPResponse, payload: bytes) -> None:
        lengths = [value for key, value in response.getheaders() if key.lower() == "content-length"]
        if response.status != 200 or lengths != [str(len(payload))]:
            raise ProbeError("Complete maximum XML response headers missing.")
        self.header_record = {"status": response.status, "content_length": len(payload)}
        self.header_received_at = time.monotonic()
        self.drain_deadline = self.header_received_at + HELD_SEND_SECONDS
        self.header_ready.set()
        if not self.release_reading.wait(HELD_OBSERVE_SECONDS) or self.aborted.is_set():
            raise ProbeError("The bounded held-response observation was aborted or expired.")

    def allow_reading(self, deadline: float) -> None:
        if not self.header_ready.is_set() or not time.monotonic() < deadline <= self.drain_deadline:
            raise ProbeError("The common response deadline cannot be extended.")
        self.drain_deadline = deadline
        self.release_reading.set()

    def _read_chunk(self, response: http.client.HTTPResponse) -> bytes:
        remaining = self.drain_deadline - time.monotonic()
        if remaining <= 0 or self.aborted.is_set() or self._transport_socket is None:
            raise ProbeError("The fixed response-drain deadline expired.")
        self._transport_socket.settimeout(min(3.0, remaining))
        self.body_reads += 1
        chunk = response.read1(65536)
        if time.monotonic() >= self.drain_deadline:
            raise ProbeError("The fixed response-drain deadline expired.")
        return chunk

    def close(self) -> None:
        self.aborted.set()
        self.release_reading.set()
        self.abort()


def held_response_deadline(requests: list[HeldResponseRequest]) -> float:
    if len(requests) != 2 or any(
        not request.header_ready.is_set()
        or request.done.is_set()
        or request.error is not None
        or request.body_reads != 0
        or request.release_reading.is_set()
        or request.header_record != {"status": 200, "content_length": HELD_XML_BYTES}
        for request in requests
    ):
        raise Inconclusive("Both complete response bodies are not simultaneously held.")
    first = min(request.header_received_at for request in requests)
    last = max(request.header_received_at for request in requests)
    if not 0 < first <= last <= time.monotonic() < first + HELD_OBSERVE_SECONDS or last - first >= 10:
        raise Inconclusive("The shared response observation window expired.")
    return first + HELD_SEND_SECONDS


def validate_held_capacity(record: dict[str, Any], elapsed: float) -> None:
    if record.get("status") != 503 or record.get("error_type") != "analysis_capacity_error" or not 0 <= elapsed < 1:
        raise ProbeError("The third request did not prove two held leases within one second.")


def validate_held_memory(metrics: dict[str, Any]) -> None:
    observations = metrics.get("memory_observations", [])
    if [item.get("phase") for item in observations] != ["bound", "both-held", "before-close"]:
        raise ProbeError("Three bound backend memory observations are required.")
    previous_peaks = (0, 0)
    for item in observations:
        fields = (
            "sample_working_set_bytes",
            "peak_working_set_bytes",
            "sample_private_commit_bytes",
            "peak_private_commit_bytes",
        )
        if (
            item.get("status") != "observed"
            or item.get("unit") != "bytes"
            or item.get("method") != "K32GetProcessMemoryInfo"
            or any(type(item.get(name)) is not int or item[name] <= 0 for name in fields)
        ):
            raise ProbeError("Actual backend byte counters are unavailable.")
        peaks = (item[fields[1]], item[fields[3]])
        if (
            any(peak < prior for peak, prior in zip(peaks, previous_peaks, strict=True))
            or item[fields[0]] > peaks[0]
            or item[fields[2]] > peaks[1]
        ):
            raise ProbeError("Backend lifetime peak counters are inconsistent.")
        previous_peaks = peaks


def health(port: int) -> float:
    started = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        conn.request("GET", "/api/health")
        response = conn.getresponse()
        raw = response.read(16385)
        if response.status != 200 or len(raw) > 16384 or json.loads(raw).get("status") != "ok":
            raise ProbeError("Health response invalid.")
    finally:
        conn.close()
    return time.monotonic() - started


def wait_ended(api: WindowsAPI, handles: list[int], seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while any(api.alive(h) for h in handles):
        if time.monotonic() >= deadline:
            raise ProbeError("Bound process exit was not confirmed within five seconds.")
        time.sleep(0.01)


def validate_stop_receipt(receipt: Any, report: dict[str, Any], clock: tuple[int, int]) -> tuple[int, int]:
    fixed = {
        "schema_version": 1,
        "action": "stop-bound-parent",
        **{key: report[key] for key in ("nonce", "package", "controller", "ready_sha256")},
    }
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {*fixed, "qpc_ticks", "qpc_frequency"}
        or any(receipt[key] != value for key, value in fixed.items())
    ):
        raise ProbeError("Stop receipt is not bound to this READY, parent and context.")
    ticks, frequency = receipt["qpc_ticks"], receipt["qpc_frequency"]
    if (
        type(ticks) is not int
        or type(frequency) is not int
        or frequency != clock[1]
        or frequency != report["qpc_frequency"]
        or not report["ready_qpc_ticks"] <= ticks <= clock[0]
    ):
        raise ProbeError("Stop receipt native clock differs or is out of sequence.")
    return ticks, frequency


def wait_ended_qpc(api: WindowsAPI, handles: list[int], *, start: int, frequency: int, seconds: int) -> float:
    while True:
        ended = not any(api.alive(handle) for handle in handles)
        now, actual_frequency = api.qpc()
        if actual_frequency != frequency or now < start or now - start > seconds * frequency:
            raise ProbeError("Process exit was not observed within the bound action deadline.")
        if ended:
            return (now - start) / frequency
        time.sleep(0.01)


def controlled_stop(api: WindowsAPI, output: Path, report: dict[str, Any], parent: int, roles: list[int]) -> None:
    path = output / "stop-action.json"
    deadline = time.monotonic() + 15
    while True:
        try:
            raw = acceptance._read(path)
            if len(raw) > 16384:
                raise ProbeError("Stop receipt exceeds fixed bound.")
            receipt = json.loads(raw)
            break
        except (FileNotFoundError, json.JSONDecodeError):
            if time.monotonic() >= deadline:
                raise ProbeError("Bound stop-action receipt did not arrive.") from None
            time.sleep(0.01)
    start, frequency = validate_stop_receipt(receipt, report, api.qpc())
    report["stop_action_sha256"] = hashlib.sha256(raw).hexdigest()
    report["role_exit_after_stop_seconds"] = wait_ended_qpc(api, roles, start=start, frequency=frequency, seconds=5)
    report["parent_exit_after_stop_seconds"] = wait_ended_qpc(
        api, [parent], start=start, frequency=frequency, seconds=10
    )


def verify_only(args: argparse.Namespace, api: WindowsAPI | None = None) -> dict[str, Any]:
    if not args.confirm_isolated_environment:
        raise ProbeError("Explicit isolated context required.")
    controller = context_binding("desktop" if args.mode == "desktop" else "service-recovery")
    if acceptance._file(Path(args.executable))["sha256"] != args.executable_sha256:
        raise ProbeError("Installed executable bytes differ.")
    result: dict[str, Any] = {"status": "PASS", "controller": controller}
    if args.verify_only == "parent":
        api = api or WindowsAPI()
        b = PackageBinding(
            args.parent_pid,
            args.parent_created,
            str(Path(args.executable).absolute()),
            args.executable_sha256,
            args.owner_sid,
            args.service_sid,
            args.port,
            args.mode,
        )
        if b.mode == "service" and (b.owner_sid != "S-1-5-19" or not b.service_sid):
            raise ProbeError("Actual LocalService and service SID required.")
        handle, parent = api.open(b.parent_pid, 0)
        try:
            validate_parent(parent, b)
            api.listener(b.port, b.parent_pid)
            result["parent"] = parent.public()
        finally:
            api.close(handle)
    return result


def record_memory(api: WindowsAPI, handle: int, record: dict[str, Any], phase: str) -> None:
    """At most three fixed observations per bound process, no polling thread."""
    observations = record.setdefault("memory_observations", [])
    if len(observations) >= 3:
        raise ProbeError("Fixed memory-observation count exceeded")
    try:
        value = api.memory(handle)
    except OSError as exc:
        value = {"status": "unavailable", "error_class": type(exc).__name__}
    observations.append({"phase": phase, **value})


def record_access(api: WindowsAPI, handle: int, record: dict[str, Any], phase: str) -> None:
    """At most the bound observation and one failed-source observation."""
    observations = record.setdefault("access_observations", [])
    if len(observations) >= 2:
        record["access_observations_truncated"] = True
        return
    try:
        value = api.process_access(handle)
    except BaseException as exc:
        # Additive evidence cannot replace the original observation failure.
        value = {"status": "unavailable", "requested_access": PROCESS_ACCESS, "failure": failure_details(exc)}
    observations.append({"phase": phase, **value})


def failure_snapshot(
    api: WindowsAPI,
    *,
    phase: str,
    parent: tuple[int, ProcessIdentity] | None,
    held: dict[int, tuple[int, ProcessIdentity, str]],
    requests: list[Request],
    seen: set[int],
    started: float,
    discovered_count: int,
) -> dict[str, Any]:
    """One bounded zero-wait snapshot before cleanup; no new handles or reads."""
    processes: list[dict[str, Any]] = []

    def observe(handle: int, identity: ProcessIdentity, role: str) -> None:
        item: dict[str, Any] = {"pid": identity.pid, "created": identity.created, "role": role}
        if role == "worker":
            item["input_marker_observed"] = identity.pid in seen
        try:
            item["state"] = "alive" if api.alive(handle) else "ended"
        except BaseException as exc:
            item.update(state="unavailable", failure=failure_details(exc))
        processes.append(item)

    if parent is not None:
        observe(*parent, "backend")
    for index, (handle, identity, role) in enumerate(held.values()):
        if index == 4:
            break
        observe(handle, identity, role)
    return {
        "phase": phase,
        "elapsed_seconds": time.monotonic() - started,
        "marker_seen_count": len(seen),
        "bound_role_counts": dict(Counter(role for _, _, role in held.values())),
        "last_discovered_count": discovered_count,
        "bound_processes": processes,
        "processes_truncated": len(held) > 4,
        "requests_done": [r.done.is_set() for r in requests[:3]],
        "request_failures": [failure_details(r.error) if r.error else None for r in requests[:3]],
        "requests_truncated": len(requests) > 3,
    }


def observe_held_responses(
    api: WindowsAPI,
    parent_handle: int,
    parent_metrics: dict[str, Any],
    binding: PackageBinding,
    requests: list[HeldResponseRequest],
    roles: list[int],
    token: str,
    started: float,
    *,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = evidence if evidence is not None else {}
    report["scope"] = "two held 25MiB XML responses; not the 128MiB JSON/HTML maxima"
    while not all(request.header_ready.is_set() for request in requests):
        known = [request.header_received_at for request in requests if request.header_ready.is_set()]
        deadline = min(started + 20, min(known) + 10 if known else started + 20)
        if any(request.done.is_set() for request in requests) or time.monotonic() >= deadline:
            raise Inconclusive("Two timely held response headers could not be observed.")
        time.sleep(0.002)
    drain_deadline = held_response_deadline(requests)
    # The product sends headers only after worker output and native cleanup.
    # Independently confirm the held kernel identities ended before measuring.
    if any(api.alive(handle) for handle in roles) or api.children(binding.parent_pid):
        raise Inconclusive("Response headers overlap live processing roles.")
    if not api.alive(parent_handle):
        raise ProbeError("Bound backend ended before response measurement.")
    api.listener(binding.port, binding.parent_pid)
    report.update(
        {
            "response_bytes_each": HELD_XML_BYTES,
            "total_response_payload_bytes": 2 * HELD_XML_BYTES,
            "common_drain_deadline": drain_deadline,
            "headers": [request.header_record for request in requests],
            "headers_monotonic": [request.header_received_at for request in requests],
            "receive_buffer_bytes": [request.connection.receive_buffer_bytes for request in requests],
            "bound_roles_ended_before_measurement": True,
        }
    )
    samples: list[float] = []
    capacities: list[dict[str, Any]] = []
    report["loaded_health_seconds"] = samples
    report["capacity_before_and_after_measurement"] = capacities
    for index in range(2):
        held_response_deadline(requests)
        capacity = Request(binding.port, token, b"<synthetic-capacity/>", export=True)
        capacity_started = time.monotonic()
        capacity.start()
        try:
            capacity.thread.join(1)
            elapsed = time.monotonic() - capacity_started
            held_response_deadline(requests)
            if not capacity.done.is_set() or capacity.error is not None:
                raise ProbeError("The third request exceeded its fixed capacity-probe deadline.")
            validate_held_capacity(capacity.record, elapsed)
            capacities.append({**capacity.record, "elapsed_seconds": elapsed})
        finally:
            capacity.abort()
            capacity.thread.join(1)
            if capacity.thread.is_alive():
                raise ProbeError("The owned capacity client did not stop.")
        if index == 0:
            report["memory_query_started_monotonic"] = time.monotonic()
            record_memory(api, parent_handle, parent_metrics, "both-held")
            report["memory_query_finished_monotonic"] = time.monotonic()
            for _ in range(3):
                held_response_deadline(requests)
                samples.append(health(binding.port))
            validate_health(samples)
    held_response_deadline(requests)
    report["body_read_calls_before_release"] = [request.body_reads for request in requests]
    report["released_monotonic"] = time.monotonic()
    for request in requests:
        request.allow_reading(drain_deadline)
    for request in requests:
        request.thread.join(max(0, drain_deadline - time.monotonic()))
    if time.monotonic() >= drain_deadline or any(not request.done.is_set() or request.error for request in requests):
        raise ProbeError("Both responses did not drain within the common 25-second deadline.")
    report["drained_monotonic"] = time.monotonic()
    report["drain_elapsed_since_first_header_seconds"] = report["drained_monotonic"] - min(report["headers_monotonic"])
    return report


def run(args: argparse.Namespace, api: WindowsAPI) -> dict[str, Any]:
    probe_started = time.monotonic()
    if not args.confirm_isolated_environment:
        raise ProbeError("An explicitly isolated package test is required.")
    action = "desktop" if args.mode == "desktop" else "service-recovery"
    controller = context_binding(action)
    exe = acceptance._file(Path(args.executable))
    if exe["sha256"] != args.executable_sha256:
        raise ProbeError("Installed executable bytes differ.")
    b = PackageBinding(
        args.parent_pid,
        args.parent_created,
        str(Path(args.executable).absolute()),
        args.executable_sha256,
        args.owner_sid,
        args.service_sid,
        args.port,
        args.mode,
    )
    if b.mode == "service" and (b.owner_sid != "S-1-5-19" or not b.service_sid):
        raise ProbeError("Actual LocalService and service SID required.")
    output = acceptance._safe_path(args.output_directory)
    output.mkdir(mode=0o700)
    nonce = uuid4().hex
    report: dict[str, Any] = {
        "schema_version": 1,
        "case": args.case,
        "status": "INCONCLUSIVE",
        "nonce": nonce,
        "controller": controller,
        "package": asdict(b),
        "role_job_runtime_commit_limit": "not externally observable without retaining private product job handles; no limit claim",
        "frozen_scope": "installed desktop/service EXE; unsigned CI technical probe only",
        "memory_scope": "kernel process-lifetime peaks through query, not job limits; reused backend peaks include earlier cases",
        "ready_measurement_scope": "upper bound from HTTP request start to first observed invoice marker; includes upload/IPC, not exact internal READY time",
        "cold_start_scope": "fresh role processes; OS/filesystem/antivirus caches are not flushed",
        "process_metrics": {},
    }
    requests: list[Request] = []
    held: dict[int, tuple[int, ProcessIdentity, str]] = {}
    parent_handle = 0
    bound_parent: tuple[int, ProcessIdentity] | None = None
    seen: set[int] = set()
    primary_error: BaseException | None = None
    cleanup_details: list[dict[str, Any]] = []
    phase = "open-parent"
    discovered_count = 0
    try:
        parent_handle, parent = api.open(b.parent_pid, 0)
        phase = "validate-parent"
        validate_parent(parent, b)
        bound_parent = parent_handle, parent
        parent_metrics: dict[str, Any] = {"role": "backend", "identity": parent.public()}
        report["process_metrics"][str(parent.pid)] = parent_metrics
        record_access(api, parent_handle, parent_metrics, "bound")
        record_memory(api, parent_handle, parent_metrics, "bound")
        phase = "preflight-listener"
        api.listener(b.port, b.parent_pid)
        phase = "preflight-children"
        if api.children(b.parent_pid):
            raise ProbeError("Pre-existing direct children prevent isolated role attribution.")
        phase = "read-token"
        token = acceptance._read(args.token_file).decode("ascii").strip()
        if not 32 <= len(token) <= 512 or any(c.isspace() for c in token):
            raise ProbeError("Token file invalid.")
        phase = "baseline-health"
        report["baseline_health_seconds"] = [health(b.port) for _ in range(3)]
        validate_health(report["baseline_health_seconds"])
        phase = "prepare-requests"
        xml_export = args.case in {"xml25", "held-responses"}
        payload, marker = (maximum_xml(), b"") if xml_export else processing_fixture(nonce)
        report["synthetic_input"] = {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        count = 2 if args.case in {"health", "held-responses"} else 1
        held_requests = (
            [HeldResponseRequest(b.port, token, payload) for _ in range(count)] if args.case == "held-responses" else []
        )
        requests = (
            list(held_requests)
            if held_requests
            else [Request(b.port, token, payload, export=xml_export) for _ in range(count)]
        )
        requests_started = time.monotonic()
        phase = "start-requests"
        for request in requests:
            request.start()
        del token
        deadline = time.monotonic() + 20
        while True:
            phase = "discover-children"
            discovered = api.children(b.parent_pid)
            discovered_count = len(discovered)
            for pid in discovered:
                if pid not in held:
                    phase = "open-role"
                    handle, p = api.open(pid, b.parent_pid)
                    try:
                        phase = "validate-role"
                        role = role_of(p, b)
                    except BaseException:
                        try:
                            api.close(handle)
                        except BaseException as close_error:
                            cleanup_details.append(failure_details(close_error))
                        raise
                    held[pid] = (handle, p, role)
                    metrics: dict[str, Any] = {"role": role, "identity": p.public()}
                    report["process_metrics"][str(pid)] = metrics
                    record_access(api, handle, metrics, "bound")
                    if role == "worker":
                        metrics["input_observation"] = new_input_observation(p)
                    record_memory(api, handle, metrics, "bound")
                handle, p, role = held[pid]
                phase = "observe-input"
                if role == "worker" and pid not in seen and marker:
                    metrics = report["process_metrics"][str(pid)]
                    try:
                        observed = api.peek_marker(handle, int(p.argv[4]), marker, metrics["input_observation"])
                    except BaseException:
                        record_access(api, handle, metrics, "input-observation-failed")
                        raise
                    if observed:
                        seen.add(pid)
                        metrics["input_after_ready_upper_bound_seconds"] = time.monotonic() - requests_started
            workers = {pid for pid, (_, _, role) in held.items() if role == "worker"}
            if len(held) == count * 2 and (xml_export or (len(workers) == count and seen == workers)):
                break
            if any(r.done.is_set() for r in requests) or time.monotonic() >= deadline:
                raise Inconclusive("Live role/active-input observation raced with completion or startup.")
            time.sleep(0.002)
        phase = "validate-inventory"
        validate_role_counts(dict(Counter(role for _, _, role in held.values())), count)
        report["processes"] = [
            {**p.public(), "role": role, "input_after_ready_observed": p.pid in seen} for _, p, role in held.values()
        ]
        if not xml_export:
            require_active(marker_seen=len(seen) == count, requests_done=any(r.done.is_set() for r in requests))
            if args.case == "controlled-stop":
                report["ready_qpc_ticks"], report["qpc_frequency"] = api.qpc()
        phase = "case-" + args.case
        if args.case == "held-responses":
            held_evidence: dict[str, Any] = {}
            report["held_responses"] = held_evidence
            observe_held_responses(
                api,
                parent_handle,
                parent_metrics,
                b,
                held_requests,
                [h for h, _, _ in held.values()],
                acceptance._read(args.token_file).decode("ascii").strip(),
                requests_started,
                evidence=held_evidence,
            )
        elif args.case == "health":
            samples = []
            for _ in range(3):
                if any(r.done.is_set() for r in requests):
                    raise Inconclusive("Two active jobs ended before health sampling.")
                samples.append(health(b.port))
            validate_health(samples)
            report["loaded_health_seconds"] = samples
            require_active(marker_seen=True, requests_done=any(r.done.is_set() for r in requests))
            capacity_started = time.monotonic()
            capacity = Request(
                b.port,
                acceptance._read(args.token_file).decode("ascii").strip(),
                b"<synthetic-capacity/>",
                export=True,
            )
            capacity.start()
            try:
                capacity.thread.join(1)
                elapsed = time.monotonic() - capacity_started
                require_active(marker_seen=True, requests_done=any(r.done.is_set() for r in requests))
                if not capacity.done.is_set() or capacity.error or capacity.record.get("status") != 503 or elapsed >= 1:
                    raise ProbeError("Third tiny request did not receive capacity503 within one second.")
                report["third_request_capacity"] = {**capacity.record, "elapsed_seconds": elapsed}
            finally:
                capacity.close()
        elif args.case in {"worker-death", "supervisor-death", "parent-death", "controlled-stop"}:
            if context_binding(action) != controller or acceptance._file(Path(b.executable)) != exe:
                raise ProbeError("Context or executable changed before action.")
            if set(api.children(b.parent_pid)) != set(held) or not all(api.alive(h) for h, _, _ in held.values()):
                raise Inconclusive("Live role inventory changed before action; nothing terminated.")
            if any(api.children(p.pid) for _, p, _ in held.values()):
                raise ProbeError("Unexpected role descendants prevent a complete process binding.")
            for handle, p, _ in held.values():
                record_memory(api, handle, report["process_metrics"][str(p.pid)], "before-action")
            require_active(marker_seen=len(seen) == count, requests_done=any(r.done.is_set() for r in requests))
            ready = {
                **report,
                "status": "READY",
                "ready_monotonic": time.monotonic(),
                "expected_controller_action": "stop-bound-parent" if args.case == "controlled-stop" else args.case,
            }
            write_new_json(output / "ready.json", ready)
            report["ready_sha256"] = acceptance._file(output / "ready.json")["sha256"]
            if args.case == "controlled-stop":
                # Controller must issue its bound real service/desktop stop now.
                controlled_stop(api, output, report, parent_handle, [h for h, _, _ in held.values()])
            else:
                target = (
                    parent_handle
                    if args.case == "parent-death"
                    else next(h for h, _, role in held.values() if role == args.case.removesuffix("-death"))
                )
                api.terminate(target)
                wait_ended(api, [h for h, _, _ in held.values()])
                if args.case == "parent-death":
                    wait_ended(api, [parent_handle])
        phase = "complete-requests"
        for request in requests:
            request.thread.join(45)
            if not request.done.is_set():
                raise ProbeError("HTTP test request exceeded its bounded wait.")
        wait_ended(api, [h for h, _, _ in held.values()])
        if args.case in {"health", "xml25", "held-responses"}:
            if any(r.error is not None or r.record.get("status") != 200 for r in requests):
                raise ProbeError("Bound normal synthetic request failed.")
            if xml_export and not all(request.record["byte_identical"] for request in requests):
                raise ProbeError("Maximum XML bytes changed.")
            if args.case == "health" and not all(r.record["pdf_markers"] for r in requests):
                raise ProbeError("PDF response incomplete.")
        else:
            if any(r.record.get("status") == 200 for r in requests):
                raise Inconclusive("Request succeeded before injected termination.")
        report["responses"] = [
            {**r.record, "transport_error": type(r.error).__name__ if r.error else None} for r in requests
        ]
        report["bound_role_exit_confirmed"] = True
        phase = "recovery"
        if args.case not in {"parent-death", "controlled-stop"}:
            if not api.alive(parent_handle):
                raise ProbeError("Parent unexpectedly exited.")
            api.listener(b.port, b.parent_pid)
            recovery = Request(
                b.port, acceptance._read(args.token_file).decode("ascii").strip(), b"<synthetic-recovery/>", export=True
            )
            requests.append(recovery)
            recovery.start()
            recovery.thread.join(20)
            if not recovery.done.is_set() or recovery.error or not recovery.record.get("byte_identical"):
                raise ProbeError("Fresh request after cleanup failed.")
            report["fresh_request_after_cleanup"] = recovery.record
        else:
            report["fresh_request_after_restart"] = "required separately by the existing PS controller"
        report["status"] = "PASS"
        return report
    except Inconclusive as exc:
        primary_error = exc
        report["reason"] = str(exc)
        raise
    except BaseException as exc:
        primary_error = exc
        report["status"] = "FAIL"
        report["error_class"] = type(exc).__name__
        raise
    finally:
        if primary_error is not None:
            report["failure"] = failure_details(primary_error)
            report["failure_snapshot"] = failure_snapshot(
                api,
                phase=phase,
                parent=bound_parent,
                held=held,
                requests=requests,
                seen=seen,
                started=probe_started,
                discovered_count=discovered_count,
            )
        # Never repair failed product cleanup by killing unidentified processes.
        close_errors = []
        for request in requests:
            try:
                request.close()
            except Exception as exc:
                close_errors.append(type(exc).__name__)
                cleanup_details.append(failure_details(exc))
        if args.case == "held-responses":
            # Abort every owned socket before joining any client, including
            # clients whose header/response stage failed. Never free observers
            # while a helper thread could still use them.
            deadline = time.monotonic() + 2
            for request in requests:
                try:
                    request.abort()
                except Exception as exc:
                    close_errors.append(type(exc).__name__)
                    cleanup_details.append(failure_details(exc))
            for request in requests:
                if request.thread.ident is not None:
                    request.thread.join(max(0, deadline - time.monotonic()))
                    if request.thread.is_alive():
                        close_errors.append("UnconfirmedClientEnd")
        for handle, p, _ in held.values():
            try:
                record_memory(api, handle, report["process_metrics"][str(p.pid)], "before-close")
            except Exception as exc:
                close_errors.append(type(exc).__name__)
                cleanup_details.append(failure_details(exc))
            try:
                api.close(handle)
            except Exception as exc:
                close_errors.append(type(exc).__name__)
                cleanup_details.append(failure_details(exc))
        if parent_handle:
            try:
                record_memory(api, parent_handle, report["process_metrics"][str(b.parent_pid)], "before-close")
                if args.case == "held-responses" and report["status"] == "PASS":
                    validate_held_memory(report["process_metrics"][str(b.parent_pid)])
            except Exception as exc:
                close_errors.append(type(exc).__name__)
                cleanup_details.append(failure_details(exc))
            try:
                api.close(parent_handle)
            except Exception as exc:
                close_errors.append(type(exc).__name__)
                cleanup_details.append(failure_details(exc))
        cleanup_details.extend(getattr(api, "cleanup_diagnostics", []))
        if cleanup_details:
            report["observer_cleanup"] = cleanup_details[:16]
            report["observer_cleanup_truncated"] = len(cleanup_details) > 16
        if close_errors:
            report["status"] = "FAIL"
            report["observer_close_errors"] = close_errors
        report["total_elapsed_seconds"] = time.monotonic() - probe_started
        report["memory_observation_complete"] = bool(report["process_metrics"]) and all(
            item.get("memory_observations", [{}])[-1].get("status") == "observed"
            for item in report["process_metrics"].values()
        )
        write_new_json(output / "result.json", report)
        if close_errors and primary_error is None:
            raise ProbeError("Bound observer cleanup could not be confirmed.")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--confirm-isolated-environment", action="store_true")
    p.add_argument("--mode", choices=("desktop", "service"), required=True)
    p.add_argument("--case", choices=CASES, required=True)
    p.add_argument("--parent-pid", type=int, required=True)
    p.add_argument("--parent-created", type=int, required=True)
    p.add_argument("--executable", required=True)
    p.add_argument("--executable-sha256", required=True)
    p.add_argument("--owner-sid", required=True)
    p.add_argument("--service-sid")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--token-file", type=Path, required=True)
    p.add_argument("--output-directory", type=Path, required=True)
    p.add_argument("--verify-only", choices=("context", "parent"))
    return p


def main() -> int:
    args = parser().parse_args()
    if not 0 < args.parent_pid < 2**32 or args.parent_created <= 0 or not 1 <= args.port <= 65535:
        raise ProbeError("Invalid fixed process/listener binding.")
    # Independent last-resort harness deadline; never a successful result.
    watchdog = threading.Timer(90, lambda: os._exit(124))
    watchdog.daemon = True
    watchdog.start()
    try:
        if args.verify_only:
            print(json.dumps(verify_only(args)))
            return 0
        report = run(args, WindowsAPI())
        print(json.dumps({"status": report["status"], "case": args.case}))
        return 0
    except (ProbeError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "INCONCLUSIVE" if isinstance(exc, Inconclusive) else "FAIL",
                    "error_class": type(exc).__name__,
                    "failure": failure_details(exc),
                }
            )
        )
        return 2
    finally:
        watchdog.cancel()


if __name__ == "__main__":
    raise SystemExit(main())
