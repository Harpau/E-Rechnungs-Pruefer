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

CASES = ("health", "worker-death", "supervisor-death", "parent-death", "controlled-stop", "xml25")
MAX_RESULT = 64 * 1024**2
_native_ctypes: Any = ctypes


class ProbeError(RuntimeError):
    pass


class Inconclusive(ProbeError):
    pass


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
        self._define(self.s, "CommandLineToArgvW", [ctypes.c_wchar_p, P], P)
        self._define(self.ip, "GetExtendedTcpTable", [P, P, ctypes.c_int, D, D, D], D)

    @staticmethod
    def _define(lib: Any, name: str, args: list[Any], result: Any) -> None:
        f = getattr(lib, name)
        f.argtypes = args
        f.restype = result

    @staticmethod
    def _check(value: Any) -> None:
        if not value:
            raise _native_ctypes.WinError(_native_ctypes.get_last_error())

    def close(self, handle: int) -> None:
        self._check(self.k.CloseHandle(handle))

    def alive(self, handle: int) -> bool:
        status = self.k.WaitForSingleObject(handle, 0)
        if status not in (0, 258):
            raise ProbeError("Kernel process wait failed.")
        return status == 258

    def terminate(self, handle: int) -> None:
        self._check(self.k.TerminateProcess(handle, 71))

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
            raise ProbeError("Process snapshot failed.")
        result = []
        try:
            e = Entry()
            e.size = ctypes.sizeof(e)
            available = self.k.Process32FirstW(h, ctypes.byref(e))
            while available:
                if e.parent == parent:
                    result.append(int(e.pid))
                available = self.k.Process32NextW(h, ctypes.byref(e))
            if _native_ctypes.get_last_error() != 18:
                raise ProbeError("Incomplete process snapshot.")
        finally:
            self.close(h)
        return result

    def _argv(self, handle: int) -> tuple[str, ...]:
        class UString(ctypes.Structure):
            _fields_ = [("length", ctypes.c_uint16), ("maximum", ctypes.c_uint16), ("buffer", ctypes.c_void_p)]

        raw = ctypes.create_string_buffer(65536)
        needed = ctypes.c_uint32()
        if self.n.NtQueryInformationProcess(handle, 60, raw, len(raw), ctypes.byref(needed)) != 0:
            raise ProbeError("Kernel command line could not be bound.")
        text = UString.from_buffer(raw)
        address = int(text.buffer or 0)
        if text.length % 2 or not ctypes.addressof(raw) <= address <= ctypes.addressof(raw) + len(raw) - text.length:
            raise ProbeError("Invalid native command line envelope.")
        command = ctypes.wstring_at(address, text.length // 2)
        count = ctypes.c_int()
        argv = self.s.CommandLineToArgvW(command, ctypes.byref(count))
        self._check(argv)
        try:
            if not 1 <= count.value <= 16:
                raise ProbeError("Unexpected process arguments.")
            values = ctypes.cast(argv, ctypes.POINTER(ctypes.c_wchar_p))
            return tuple(values[i] for i in range(count.value))
        finally:
            self.k.LocalFree(argv)

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
        if self.n.NtQueryInformationProcess(
            handle, 0, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned)
        ) != 0 or returned.value != ctypes.sizeof(info):
            raise ProbeError("Native parent identity could not be queried.")
        if info.pid != pid:
            raise ProbeError("Held process PID differs from snapshot.")
        if expected_parent and info.parent != expected_parent:
            raise ProbeError("Native parent differs from snapshot.")
        return int(info.parent)

    def qpc(self) -> tuple[int, int]:
        ticks, frequency = ctypes.c_int64(), ctypes.c_int64()
        self._check(self.k.QueryPerformanceCounter(ctypes.byref(ticks)))
        self._check(self.k.QueryPerformanceFrequency(ctypes.byref(frequency)))
        if ticks.value <= 0 or frequency.value <= 0:
            raise ProbeError("Native monotonic clock invalid.")
        return ticks.value, frequency.value

    def memory(self, handle: int) -> dict[str, Any]:
        return windows_memory_counters(handle)

    def open(self, pid: int, parent_pid: int) -> tuple[int, ProcessIdentity]:
        # QUERY_LIMITED_INFORMATION | QUERY_INFORMATION | DUP_HANDLE | SYNCHRONIZE | TERMINATE
        h = self.k.OpenProcess(0x1000 | 0x400 | 0x40 | 0x100000 | 1, False, pid)
        self._check(h)
        try:
            times = [ctypes.c_uint64() for _ in range(4)]
            self._check(self.k.GetProcessTimes(h, *(ctypes.byref(t) for t in times)))
            path = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_uint32(len(path))
            self._check(self.k.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size)))
            token = self.security.OpenProcessToken(h, 8)
            try:
                owner = self.security.ConvertSidToStringSid(
                    self.security.GetTokenInformation(token, self.security.TokenUser)[0]
                )
                groups = tuple(
                    self.security.ConvertSidToStringSid(sid)
                    for sid, attributes in self.security.GetTokenInformation(token, self.security.TokenGroups)
                    if attributes & 4
                )
            finally:
                token.Close()
            actual_parent = self.kernel_parent(h, pid, parent_pid)
            identity = ProcessIdentity(pid, times[0].value, path.value, owner, groups, actual_parent, self._argv(h))
            if not self.alive(h):
                raise Inconclusive("Process exited before identity was bound.")
            return int(h), identity
        except BaseException:
            self.close(h)
            raise

    def peek_marker(self, handle: int, input_handle: int, marker: bytes) -> bool:
        duplicate = ctypes.c_void_p()
        self._check(
            self.k.DuplicateHandle(
                handle, input_handle, self.k.GetCurrentProcess(), ctypes.byref(duplicate), 0, False, 2
            )
        )
        try:
            raw = ctypes.create_string_buffer(65536)
            read = ctypes.c_uint32()
            available = ctypes.c_uint32()
            if not self.k.PeekNamedPipe(duplicate, raw, len(raw), ctypes.byref(read), ctypes.byref(available), None):
                if _native_ctypes.get_last_error() in (109, 232, 233):
                    return False
                raise ProbeError("Bound input pipe cannot be observed safely.")
            return marker in raw.raw[: read.value]
        finally:
            self.close(int(duplicate.value or 0))

    def listener(self, port: int, parent: int) -> None:
        size = ctypes.c_uint32(256 * 1024)
        raw = ctypes.create_string_buffer(size.value)
        if self.ip.GetExtendedTcpTable(raw, ctypes.byref(size), False, 2, 3, 0) != 0:
            raise ProbeError("Listening port identity could not be obtained.")
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
            response = self.connection.getresponse()
            digest = hashlib.sha256()
            size = 0
            prefix = b""
            tail = b""
            while chunk := response.read(65536):
                if size + len(chunk) > MAX_RESULT:
                    raise ProbeError("Response exceeded the fixed test output bound.")
                if export and response.status == 200 and payload[size : size + len(chunk)] != chunk:
                    raise ProbeError("XML export differs from original bytes.")
                size += len(chunk)
                digest.update(chunk)
                prefix = (prefix + chunk)[:8]
                tail = (tail + chunk)[-1024:]
            self.record = {
                "status": response.status,
                "bytes": size,
                "sha256": digest.hexdigest(),
                "media_type": response.getheader("Content-Type"),
                "pdf_markers": prefix.startswith(b"%PDF-") and b"%%EOF" in tail,
                "byte_identical": export and response.status == 200 and size == len(payload),
            }
        except BaseException as exc:
            self.error = exc
        finally:
            self.connection.close()
            self.done.set()

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.connection.close()


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
    try:
        parent_handle, parent = api.open(b.parent_pid, 0)
        validate_parent(parent, b)
        parent_metrics: dict[str, Any] = {"role": "backend", "identity": parent.public()}
        report["process_metrics"][str(parent.pid)] = parent_metrics
        record_memory(api, parent_handle, parent_metrics, "bound")
        api.listener(b.port, b.parent_pid)
        if api.children(b.parent_pid):
            raise ProbeError("Pre-existing direct children prevent isolated role attribution.")
        token = acceptance._read(args.token_file).decode("ascii").strip()
        if not 32 <= len(token) <= 512 or any(c.isspace() for c in token):
            raise ProbeError("Token file invalid.")
        report["baseline_health_seconds"] = [health(b.port) for _ in range(3)]
        validate_health(report["baseline_health_seconds"])
        payload, marker = (maximum_xml(), b"") if args.case == "xml25" else processing_fixture(nonce)
        report["synthetic_input"] = {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        count = 2 if args.case == "health" else 1
        requests = [Request(b.port, token, payload, export=args.case == "xml25") for _ in range(count)]
        requests_started = time.monotonic()
        for request in requests:
            request.start()
        del token
        deadline = time.monotonic() + 20
        seen: set[int] = set()
        while True:
            for pid in api.children(b.parent_pid):
                if pid not in held:
                    handle, p = api.open(pid, b.parent_pid)
                    try:
                        role = role_of(p, b)
                    except BaseException:
                        api.close(handle)
                        raise
                    held[pid] = (handle, p, role)
                    metrics: dict[str, Any] = {"role": role, "identity": p.public()}
                    report["process_metrics"][str(pid)] = metrics
                    record_memory(api, handle, metrics, "bound")
                handle, p, role = held[pid]
                if role == "worker" and pid not in seen and marker and api.peek_marker(handle, int(p.argv[4]), marker):
                    seen.add(pid)
                    report["process_metrics"][str(pid)]["input_after_ready_upper_bound_seconds"] = (
                        time.monotonic() - requests_started
                    )
            workers = {pid for pid, (_, _, role) in held.items() if role == "worker"}
            if len(held) == count * 2 and (args.case == "xml25" or (len(workers) == count and seen == workers)):
                break
            if any(r.done.is_set() for r in requests) or time.monotonic() >= deadline:
                raise Inconclusive("Live role/active-input observation raced with completion or startup.")
            time.sleep(0.002)
        validate_role_counts(dict(Counter(role for _, _, role in held.values())), count)
        report["processes"] = [
            {**p.public(), "role": role, "input_after_ready_observed": p.pid in seen} for _, p, role in held.values()
        ]
        if args.case != "xml25":
            require_active(marker_seen=len(seen) == count, requests_done=any(r.done.is_set() for r in requests))
            if args.case == "controlled-stop":
                report["ready_qpc_ticks"], report["qpc_frequency"] = api.qpc()
        if args.case == "health":
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
        for request in requests:
            request.thread.join(45)
            if not request.done.is_set():
                raise ProbeError("HTTP test request exceeded its bounded wait.")
        wait_ended(api, [h for h, _, _ in held.values()])
        if args.case in {"health", "xml25"}:
            if any(r.error is not None or r.record.get("status") != 200 for r in requests):
                raise ProbeError("Bound normal synthetic request failed.")
            if args.case == "xml25" and not requests[0].record["byte_identical"]:
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
        report["reason"] = str(exc)
        raise
    except BaseException as exc:
        report["status"] = "FAIL"
        report["error_class"] = type(exc).__name__
        raise
    finally:
        # Never repair failed product cleanup by killing unidentified processes.
        close_errors = []
        for request in requests:
            try:
                request.close()
            except Exception as exc:
                close_errors.append(type(exc).__name__)
        for handle, p, _ in held.values():
            try:
                record_memory(api, handle, report["process_metrics"][str(p.pid)], "before-close")
            except Exception as exc:
                close_errors.append(type(exc).__name__)
            try:
                api.close(handle)
            except Exception as exc:
                close_errors.append(type(exc).__name__)
        if parent_handle:
            try:
                record_memory(api, parent_handle, report["process_metrics"][str(b.parent_pid)], "before-close")
            except Exception as exc:
                close_errors.append(type(exc).__name__)
            try:
                api.close(parent_handle)
            except Exception as exc:
                close_errors.append(type(exc).__name__)
        if close_errors:
            report["status"] = "FAIL"
            report["observer_close_errors"] = close_errors
        report["total_elapsed_seconds"] = time.monotonic() - probe_started
        report["memory_observation_complete"] = bool(report["process_metrics"]) and all(
            item.get("memory_observations", [{}])[-1].get("status") == "observed"
            for item in report["process_metrics"].values()
        )
        write_new_json(output / "result.json", report)
        if close_errors:
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
                }
            )
        )
        return 2
    finally:
        watchdog.cancel()


if __name__ == "__main__":
    raise SystemExit(main())
