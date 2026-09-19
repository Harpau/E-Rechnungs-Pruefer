"""Opt-in, bounded owner metadata. Never owns invoice buffers or controls work."""

from __future__ import annotations

import copy
import ctypes
import json
import os
import secrets
import sys
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from typing import Any

from .protocol import ProtocolError

OBSERVATION_HEADER = b"x-einvoice-observation-id"
OBSERVATION_PATH = "/api/processing-observation"
BEARER_SCOPE_KEY = "einvoice.authenticated_api_bearer"
MAX_EVENTS = 16
MAX_RECORD_BYTES = 8 * 1024
MAX_SNAPSHOT_BYTES = 16 * 1024
MAX_TERMINAL = 16
TERMINAL_SECONDS = 120
_OPERATIONS = {"analyze", "export_xml", "report_html", "report_pdf"}
_PHASES = {
    "admitted",
    "ready",
    "input_received",
    "operation_entering",
    "operation_finished",
    "result_received",
    "processing_error",
    "cleanup_started",
    "cleanup_confirmed",
    "response_sending",
    "response_send_complete",
    "lease_released",
    "poisoned",
    "upload_failed",
    "transport_failed",
}
_PREVIOUS = {
    "ready": "admitted",
    "input_received": "ready",
    "operation_entering": "input_received",
    "operation_finished": "operation_entering",
    "result_received": "operation_finished",
    "cleanup_confirmed": "cleanup_started",
    "response_send_complete": "response_sending",
}


class ObservationConflict(ValueError):
    """A retained correlation label cannot be reused for a different job."""


def valid_observation_id(value: object) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def _integer(value: object, *, zero: bool = False) -> bool:
    return type(value) is int and (0 if zero else 1) <= value < 2**63


@lru_cache(maxsize=1)
def _qpc_api() -> Any:
    api: Any = vars(ctypes)["WinDLL"]("kernel32", use_last_error=True)
    for name in ("QueryPerformanceCounter", "QueryPerformanceFrequency"):
        function = getattr(api, name)
        function.argtypes = [ctypes.POINTER(ctypes.c_int64)]
        function.restype = ctypes.c_int
    return api


def clock_stamp() -> dict[str, Any] | None:
    """A clock failure invalidates evidence, never the processing result."""
    try:
        if sys.platform == "win32":
            api = _qpc_api()
            ticks, frequency = ctypes.c_int64(), ctypes.c_int64()
            if not api.QueryPerformanceCounter(ctypes.byref(ticks)) or not api.QueryPerformanceFrequency(
                ctypes.byref(frequency)
            ):
                return None
            stamp = {"kind": "qpc", "frequency": frequency.value, "ticks": ticks.value}
        else:
            stamp = {"kind": "monotonic_ns", "frequency": 1_000_000_000, "ticks": time.monotonic_ns()}
        return stamp if _integer(stamp["frequency"]) and _integer(stamp["ticks"]) else None
    except Exception:
        return None


def retention_time() -> float:
    return time.monotonic()


def clock_descriptor(stamp: dict[str, Any] | None) -> dict[str, Any] | None:
    return {"kind": stamp["kind"], "frequency": stamp["frequency"]} if stamp else None


def _valid_clock(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"kind", "frequency"}
        and value["kind"] in {"qpc", "monotonic_ns"}
        and _integer(value["frequency"])
    )


def validate_binding(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"job_id", "clock"}
        or not valid_observation_id(value["job_id"])
        or (value["clock"] is not None and not _valid_clock(value["clock"]))
    ):
        raise ProtocolError("Ungültige interne Beobachtungsbindung.")
    return value


def validate_entering(message: dict[str, Any], binding: dict[str, Any]) -> None:
    if message != {"type": "operation_entering", "job_id": binding["job_id"]}:
        raise ProtocolError("Unvollständiger Beobachtungsbeginn.")


def finished_message(
    binding: dict[str, Any], started: dict[str, Any] | None, finished: dict[str, Any] | None
) -> dict[str, Any]:
    if (
        not started
        or not finished
        or clock_descriptor(started) != binding["clock"]
        or clock_descriptor(finished) != binding["clock"]
        or finished["ticks"] < started["ticks"]
    ):
        return {"type": "observation_unavailable", "job_id": binding["job_id"]}
    return {
        "type": "operation_finished",
        "job_id": binding["job_id"],
        "clock": binding["clock"],
        "started": started["ticks"],
        "finished": finished["ticks"],
    }


def validate_finished(message: dict[str, Any], binding: dict[str, Any]) -> None:
    if message == {"type": "observation_unavailable", "job_id": binding["job_id"]}:
        return
    if (
        set(message) != {"type", "job_id", "clock", "started", "finished"}
        or message["type"] != "operation_finished"
        or message["job_id"] != binding["job_id"]
        or not _valid_clock(message["clock"])
        or message["clock"] != binding["clock"]
        or not _integer(message["started"])
        or not _integer(message["finished"])
        or message["started"] > message["finished"]
    ):
        raise ProtocolError("Ungültiges Beobachtungsende.")


def parent_identity() -> dict[str, Any]:
    creation = None
    if sys.platform == "win32":
        from .windows import _load_api

        creation = _load_api().creation_time(-1)
    return {"pid": os.getpid(), "creation_time": creation}


class ObservationLedger:
    """At most two occupied records plus sixteen bounded terminal records."""

    def __init__(self) -> None:
        self.instance_id = secrets.token_hex(16)
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}
        self._terminal: OrderedDict[str, float] = OrderedDict()
        self._evicted = 0

    def _expire(self, now: float) -> None:
        for identifier, completed in tuple(self._terminal.items()):
            if now - completed >= TERMINAL_SECONDS:
                del self._terminal[identifier]
                del self._records[identifier]

    def register(self, identifier: str, operation: str) -> dict[str, Any]:
        if not valid_observation_id(identifier) or operation not in _OPERATIONS:
            raise ValueError("Ungültige Beobachtungskennung oder Operation.")
        stamp = clock_stamp()
        try:
            parent = parent_identity()
        except Exception:
            parent = {"pid": os.getpid(), "creation_time": None}
            stamp = None
        record = {
            "observation_id": identifier,
            "job_id": secrets.token_hex(16),
            "operation": operation,
            "parent": parent,
            "roles": [],
            "clock": clock_descriptor(stamp),
            "available": stamp is not None,
            "revision": 1,
            "events": [{"sequence": 1, "phase": "admitted", "at": stamp["ticks"] if stamp else None}],
        }
        with self._lock:
            self._expire(retention_time())
            if identifier in self._records:
                raise ObservationConflict("Die Beobachtungskennung wird bereits verwendet.")
            if len(self._records) - len(self._terminal) >= 2:
                raise ValueError("Die begrenzte Beobachtungskapazität ist nicht verfügbar.")
            self._records[identifier] = record
        return {"job_id": record["job_id"], "clock": record["clock"]}

    def invalidate(self, identifier: str) -> None:
        with self._lock:
            record = self._records.get(identifier)
            if record is not None and record["available"]:
                record["available"] = False
                record["revision"] += 1

    def note(self, identifier: str, phase: str, *, interval: dict[str, Any] | None = None) -> None:
        with self._lock:
            record = self._records.get(identifier)
            if record is None:
                return
            # This is the owner publication time, not the worker interval. Read
            # after acquiring our metadata lock so concurrent publishers cannot
            # invert otherwise valid event timestamps. QPC was initialized at
            # admission; no serialization, file or pipe access happens here.
            stamp = clock_stamp()
            phases = {event["phase"] for event in record["events"]}
            if phase in phases:
                return
            if phase not in _PHASES or len(phases) >= MAX_EVENTS or "lease_released" in phases:
                record["available"] = False
                record["revision"] += 1
                return
            previous = _PREVIOUS.get(phase)
            if previous is not None and previous not in phases:
                record["available"] = False
            at = stamp["ticks"] if stamp else None
            if not stamp or clock_descriptor(stamp) != record["clock"]:
                record["available"] = False
            last = record["events"][-1]["at"]
            if at is not None and last is not None and at < last:
                record["available"] = False
            event: dict[str, Any] = {"sequence": len(record["events"]) + 1, "phase": phase, "at": at}
            if phase == "operation_finished":
                if interval is None:
                    record["available"] = False
                else:
                    validate_finished(interval, {"job_id": record["job_id"], "clock": record["clock"]})
                    if interval["type"] == "observation_unavailable":
                        record["available"] = False
                    else:
                        event.update(started=interval["started"], finished=interval["finished"])
                        ready = next((e["at"] for e in record["events"] if e["phase"] == "ready"), None)
                        if (
                            at is None
                            or ready is None
                            or interval["started"] < ready - 1
                            or interval["finished"] > at + 1
                        ):
                            record["available"] = False
            record["events"].append(event)
            record["revision"] += 1
            if phase == "lease_released":
                self._terminal[identifier] = retention_time()
                while len(self._terminal) > MAX_TERMINAL:
                    oldest, _ = self._terminal.popitem(last=False)
                    del self._records[oldest]
                    self._evicted = min(self._evicted + 1, 2**63 - 1)

    def bind_roles(self, identifier: str, roles: list[dict[str, Any]]) -> None:
        if not 2 <= len(roles) <= 4:
            raise ValueError("Ungültiges Beobachtungsinventar.")
        for role in roles:
            if (
                set(role) != {"role", "pid", "parent_pid", "creation_time", "exit_code"}
                or role["role"] not in {"worker", "supervisor", "java", "watchdog"}
                or not _integer(role["pid"])
                or not _integer(role["parent_pid"])
                or (role["creation_time"] is not None and not _integer(role["creation_time"]))
                or role["exit_code"] is not None
                or (sys.platform == "win32" and role["creation_time"] is None)
            ):
                raise ValueError("Ungültige native Beobachtungsidentität.")
        if len({r["pid"] for r in roles}) != len(roles) or len({r["role"] for r in roles}) != len(roles):
            raise ValueError("Mehrdeutiges Beobachtungsinventar.")
        with self._lock:
            record = self._records[identifier]
            if record["roles"]:
                raise ValueError("Beobachtungsrollen sind bereits gebunden.")
            record["roles"] = [dict(role) for role in roles]
            record["revision"] += 1

    def ended_roles(self, identifier: str, statuses: dict[int, int | None]) -> None:
        with self._lock:
            record = self._records[identifier]
            if set(statuses) != {role["pid"] for role in record["roles"]}:
                record["available"] = False
            for role in record["roles"]:
                code = statuses.get(role["pid"])
                if type(code) is not int or not -(2**32) <= code < 2**32:
                    record["available"] = False
                else:
                    role["exit_code"] = code
            record["revision"] += 1

    def snapshot(self, identifier: str) -> dict[str, Any] | None:
        if not valid_observation_id(identifier):
            return None
        with self._lock:
            self._expire(retention_time())
            record = self._records.get(identifier)
            if record is None:
                return None
            detached = copy.deepcopy(record)
            evicted = self._evicted
        # Timestamp the completed copy. A publication racing with a premature
        # timestamp must never make the detached record appear to be future data.
        stamp = clock_stamp()
        # Serialization never holds an owner/capacity/ledger lock.
        if len(json.dumps(detached, separators=(",", ":")).encode("ascii")) > MAX_RECORD_BYTES:
            self.invalidate(identifier)
            raise ValueError("Die Beobachtungsgrenze wurde überschritten.")
        return {
            "schema_version": 1,
            "instance_id": self.instance_id,
            "snapshot": stamp,
            "evicted_records": evicted,
            "record": detached,
        }
