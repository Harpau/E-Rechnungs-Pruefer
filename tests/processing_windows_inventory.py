"""Read-only, bounded diagnostic identities from an already owned Windows job.

No argv/environment query and no process mutation by PID. A PID is only a query
candidate: the held handle must independently prove membership and identity.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import asdict
from pathlib import PureWindowsPath
from typing import Any, Protocol

from app.processing import windows

QUERY_ACCESS = 0x1000 | 0x100000  # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
MAX_PROCESSES = 64


class JobOwner(Protocol):
    @property
    def handle(self) -> int: ...

    def process_ids(self) -> tuple[int, ...]: ...

    def active_process_count(self) -> int: ...


class InventoryAPI(Protocol):
    def open(self, pid: int) -> int: ...

    def member(self, handle: int, job: int) -> bool: ...

    def identity(self, handle: int) -> tuple[int, int, str]: ...

    def running(self, handle: int) -> bool: ...

    def limits(self, job: int) -> dict[str, int | None]: ...

    def close(self, handle: int) -> None: ...


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class NativeInventoryAPI:
    api: windows._Win32

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows inventory requires a Windows kernel")
        self.api = windows._load_api()
        dll = self.api.dll
        dll.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
        dll.OpenProcess.restype = ctypes.c_void_p
        dll.GetProcessId.argtypes = [ctypes.c_void_p]
        dll.GetProcessId.restype = ctypes.c_uint32
        dll.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(_FileTime)] * 4
        dll.GetProcessTimes.restype = ctypes.c_int32
        dll.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        dll.QueryFullProcessImageNameW.restype = ctypes.c_int32

    def open(self, pid: int) -> int:
        # These documented query APIs need no PROCESS_ALL_ACCESS or VM_READ:
        # https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocess
        # https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-queryfullprocessimagenamew
        value = self.api.dll.OpenProcess(QUERY_ACCESS, False, pid)
        if not value:
            raise OSError("OpenProcess query failed")
        return int(value)

    def member(self, handle: int, job: int) -> bool:
        return self.api.is_in_job(handle, job)

    def identity(self, handle: int) -> tuple[int, int, str]:
        pid = int(self.api.dll.GetProcessId(handle))
        created, exited, kernel, user = (_FileTime() for _ in range(4))
        if not pid or not self.api.dll.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        ):
            raise OSError("Process creation identity query failed")
        capacity = ctypes.c_uint32(32768)
        image = ctypes.create_unicode_buffer(capacity.value)
        if not self.api.dll.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(capacity)):
            raise OSError("Process image identity query failed")
        return pid, (int(created.high) << 32) | int(created.low), image.value

    def running(self, handle: int) -> bool:
        return not self.api.wait(handle, 0)

    def limits(self, job: int) -> dict[str, int | None]:
        return asdict(self.api.get_limits(job))

    def close(self, handle: int) -> None:
        self.api.close_handle(handle)


def capture_inventory(owner: JobOwner, *, api: InventoryAPI | None = None) -> dict[str, Any]:
    """Return explicit incomplete evidence on any race/query/close failure.

    The caller retains the job handle for the entire call. Query handles remain
    held until the final snapshot, and every successful open has one close
    attempt even after another identity/close fails. Never infer success from an
    empty list or silently omit a PID which cannot be queried.
    """
    result: dict[str, Any] = {
        "schema_version": 1,
        "complete": False,
        "query_access": QUERY_ACCESS,
        "maximum_processes": MAX_PROCESSES,
        "processes": [],
        "errors": [],
    }
    held: list[tuple[int, int]] = []
    try:
        pids = owner.process_ids()
        if not 1 <= len(pids) <= MAX_PROCESSES:
            raise ValueError("Empty or oversized job inventory")
        if any(type(pid) is not int or not 0 < pid <= 0xFFFFFFFF for pid in pids) or len(set(pids)) != len(pids):
            raise ValueError("Ambiguous job PID inventory")
        result["snapshot_pids"] = list(pids)
        job = owner.handle
        api = api or NativeInventoryAPI()
        result["job_limits"] = api.limits(job)
        result["active_process_count"] = owner.active_process_count()
        for pid in pids:
            try:
                held.append((pid, api.open(pid)))
            except (OSError, ValueError) as error:
                result["errors"].append({"pid": pid, "stage": "open", "error_type": type(error).__name__})
        for pid, handle in held:
            record: dict[str, Any] = {"requested_pid": pid}
            result["processes"].append(record)
            try:
                record["job_member_before"] = api.member(handle, job)
                if not record["job_member_before"]:
                    raise ValueError("Query handle is not a member of the held job")
                record["running_before"] = api.running(handle)
                identity = api.identity(handle)
                record.update(pid=identity[0], creation_filetime=identity[1], image_path=identity[2])
                second_identity = api.identity(handle)
                record["job_member_after"] = api.member(handle, job)
                record["running_after"] = api.running(handle)
                if (
                    identity != second_identity
                    or identity[0] != pid
                    or identity[1] <= 0
                    or not PureWindowsPath(identity[2]).is_absolute()
                    or "\0" in identity[2]
                    or not all(
                        record[key]
                        for key in ("job_member_before", "job_member_after", "running_before", "running_after")
                    )
                ):
                    raise ValueError("Unconfirmed held process identity")
            except (OSError, ValueError) as error:
                result["errors"].append({"pid": pid, "stage": "identity", "error_type": type(error).__name__})
        result["final_snapshot_pids"] = list(owner.process_ids())
        result["final_active_process_count"] = owner.active_process_count()
        if (
            set(result["final_snapshot_pids"]) != set(pids)
            or len(result["final_snapshot_pids"]) != len(pids)
            or result["active_process_count"] != len(pids)
            or result["final_active_process_count"] != len(pids)
        ):
            raise ValueError("Job inventory changed during query")
    except (OSError, ValueError) as error:
        result["errors"].append({"stage": "inventory", "error_type": type(error).__name__})
    finally:
        while held:
            pid, handle = held.pop()
            try:
                assert api is not None
                api.close(handle)
            except (OSError, ValueError) as error:
                result["errors"].append({"pid": pid, "stage": "close", "error_type": type(error).__name__})
    result["complete"] = bool(result["processes"]) and not result["errors"]
    return result


def comparison_can_continue(record: dict[str, Any]) -> bool:
    """An unexpected *bound* inventory is diagnostic; uncertain cleanup stops."""
    inventory = record.get("inventory")
    errors = record.get("errors")
    active = record.get("owned_job_active_after_cleanup")
    return (
        record.get("owned_process_exit_confirmed") is True
        and type(active) is int
        and active == 0
        and record.get("cleanup_within_deadline") is True
        and isinstance(inventory, dict)
        and inventory.get("complete") is True
        and isinstance(errors, list)
        and all(
            isinstance(error, str) and not error.startswith(("cleanup:", "close:", "descriptor_close:"))
            for error in errors
        )
    )
