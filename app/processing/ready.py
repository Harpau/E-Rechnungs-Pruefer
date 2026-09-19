"""Validate native readiness before transmitting any invoice bytes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .budgets import ProcessingBudgets
from .posix import baseline_ceiling_bytes
from .protocol import VERSION, ProtocolError


def _profile(value: object, *, memory: int, windows: bool, cpu: int | None = None) -> None:
    if not isinstance(value, dict):
        raise ProtocolError("Der aktive Prozessschutz wurde nicht bestätigt.")
    if windows:
        if value != {"job_memory_bytes": memory} or type(value.get("job_memory_bytes")) is not int:
            raise ProtocolError("Unzulässiges Job-Speicherprofil.")
        return
    expected = {"baseline_as_bytes", "address_space_bytes"}
    if cpu is not None:
        expected.add("cpu_seconds")
    if set(value) != expected or any(type(entry) is not int or entry <= 0 for entry in value.values()):
        raise ProtocolError("Unzulässiges POSIX-Speicherprofil.")
    try:
        ceiling = baseline_ceiling_bytes()
    except OSError as exc:
        raise ProtocolError("Für die Adressraumbasis fehlt ein geprüftes Plattformprofil.") from exc
    if value["baseline_as_bytes"] > ceiling or value["address_space_bytes"] - value["baseline_as_bytes"] != memory:
        raise ProtocolError("Der aktive Adressraum entspricht nicht dem geprüften Profil.")
    if cpu is not None and value["cpu_seconds"] != cpu:
        raise ProtocolError("Das CPU-Budget wurde nicht bestätigt.")


def validate_ready(
    message: Mapping[str, Any],
    *,
    budgets: ProcessingBudgets,
    java_enabled: bool,
    kosit_seconds: int,
    windows: bool,
    reserved_pids: set[int],
) -> tuple[int, ...]:
    if (
        set(message) != {"type", "role", "protocol", "limits", "worker", "java", "children"}
        or message["type"] != "ready"
        or message["role"] != "supervisor"
        or type(message["protocol"]) is not int
        or message["protocol"] != VERSION
    ):
        raise ProtocolError("Unvollständiger Startnachweis.")
    _profile(message["limits"], memory=budgets.supervisor_memory_bytes, windows=windows)
    worker_memory = budgets.python_start_memory_bytes if windows else budgets.python_memory_bytes
    for role, memory in (("worker", worker_memory), ("java", budgets.java_memory_bytes)):
        proof = message[role]
        if role == "java" and not java_enabled:
            if proof is not None:
                raise ProtocolError("Nicht angeforderter Java-Prozess.")
            continue
        if (
            not isinstance(proof, dict)
            or set(proof) != {"type", "role", "protocol", "limits"}
            or proof["type"] != "ready"
            or proof["role"] != role
            or type(proof["protocol"]) is not int
            or proof["protocol"] != VERSION
        ):
            raise ProtocolError("Unvollständiger Rollenstartnachweis.")
        _profile(
            proof["limits"],
            memory=memory,
            windows=windows,
            cpu=min(360, max(20, kosit_seconds * 2)) if role == "java" and not windows else None,
        )
    children = message["children"]
    if (
        not isinstance(children, list)
        or len(children) != (2 if java_enabled else 1)
        or any(type(pid) is not int or not 1 < pid < 2**31 or pid in reserved_pids for pid in children)
        or len(set(children)) != len(children)
    ):
        raise ProtocolError("Unzulässiges Kindprozessinventar.")
    return tuple(children)
