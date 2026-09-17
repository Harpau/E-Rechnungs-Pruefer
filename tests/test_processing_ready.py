"""Strict readiness and IPC metadata must precede every invoice transfer."""

from __future__ import annotations

import copy
import io
import json

import pytest

from app.processing.budgets import ProcessingBudgets
from app.processing.protocol import (
    CONTROL_LIMIT,
    FrameKind,
    ProtocolError,
    decode_control,
    encode_control,
    read_control,
    write_frame,
)
from app.processing.ready import _profile as validate_memory_profile
from app.processing.ready import validate_ready

BUDGETS = ProcessingBudgets()
RESERVED = {101, 102, 103}


def _profile(memory, windows, cpu=None):
    if windows:
        return {"job_memory_bytes": memory}
    result = {"baseline_as_bytes": 8 * 1024**2, "address_space_bytes": 8 * 1024**2 + memory}
    if cpu is not None:
        result["cpu_seconds"] = cpu
    return result


def _ready(*, windows=False, java=True):
    worker_memory = BUDGETS.python_start_memory_bytes if windows else BUDGETS.python_memory_bytes
    return {
        "type": "ready",
        "role": "supervisor",
        "protocol": 1,
        "limits": _profile(BUDGETS.supervisor_memory_bytes, windows),
        "worker": {"type": "ready", "role": "worker", "protocol": 1, "limits": _profile(worker_memory, windows)},
        "java": {
            "type": "ready",
            "role": "java",
            "protocol": 1,
            "limits": _profile(BUDGETS.java_memory_bytes, windows, 60),
        }
        if java
        else None,
        "children": [501, 502] if java else [501],
    }


def _validate(message, *, windows=False, java=True):
    return validate_ready(
        message, budgets=BUDGETS, java_enabled=java, kosit_seconds=30, windows=windows, reserved_pids=RESERVED
    )


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("java", [False, True])
def test_exact_ready_roundtrip_accepts_only_requested_profiles(windows, java):
    record = _ready(windows=windows, java=java)
    assert _validate(decode_control(encode_control(record)), windows=windows, java=java) == (
        (501, 502) if java else (501,)
    )


@pytest.mark.parametrize(
    "path,value",
    [
        (("protocol",), True),
        (("protocol",), 1.0),
        (("protocol",), 2),
        (("role",), "worker"),
        (("type",), "result"),
        (("worker", "protocol"), True),
        (("worker", "role"), "java"),
        (("worker", "type"), "complete"),
        (("worker",), None),
        (("java",), None),
        (("java", "role"), "worker"),
        (("java", "protocol"), 1.0),
        (("children",), [True, 502]),
        (("children",), [0, 502]),
        (("children",), [1, 502]),
        (("children",), [501, 501]),
        (("children",), [101, 502]),
        (("children",), [501, 2**31]),
        (("children",), [501.0, 502]),
        (("children",), ["501", 502]),
        (("children",), [501]),
        (("children",), [501, 502, 503]),
        (("children",), (501, 502)),
        (("limits", "baseline_as_bytes"), True),
        (("limits", "address_space_bytes"), float("inf")),
        (("worker", "limits", "address_space_bytes"), 0),
        (("java", "limits", "cpu_seconds"), 61),
        (("java", "limits", "cpu_seconds"), True),
    ],
)
def test_incomplete_ambiguous_or_wrong_ready_fails_closed(path, value):
    message = _ready()
    target = message
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ProtocolError):
        _validate(message)


@pytest.mark.parametrize("role", [None, "worker", "java"])
def test_unknown_profile_fields_are_not_silently_ignored(role):
    message = _ready()
    target = message if role is None else message[role]
    target["unapproved"] = "value"
    with pytest.raises(ProtocolError):
        _validate(message)


@pytest.mark.parametrize("role", [None, "worker", "java"])
def test_windows_commit_proof_requires_integer_bytes_not_equal_floats(role):
    message = _ready(windows=True)
    target = message if role is None else message[role]
    memory = target["limits"]["job_memory_bytes"]
    target["limits"]["job_memory_bytes"] = float(memory)
    with pytest.raises(ProtocolError):
        validate_memory_profile(target["limits"], memory=memory, windows=True)


def test_windows_worker_start_proof_cannot_claim_already_lowered_runtime_budget():
    message = _ready(windows=True)
    message["worker"]["limits"]["job_memory_bytes"] = BUDGETS.python_memory_bytes
    with pytest.raises(ProtocolError):
        _validate(message, windows=True)


def test_disabled_java_must_have_neither_proof_nor_extra_child():
    message = _ready(java=False)
    message["java"] = _ready()["java"]
    with pytest.raises(ProtocolError):
        _validate(message, java=False)
    message = _ready(java=False)
    message["children"].append(502)
    with pytest.raises(ProtocolError):
        _validate(message, java=False)


def test_address_space_baseline_and_delta_are_both_bounded():
    message = _ready()
    message["limits"]["address_space_bytes"] += 1
    with pytest.raises(ProtocolError):
        _validate(message)
    message = _ready()
    message["limits"] = {
        "baseline_as_bytes": 64 * 1024**3 + 1,
        "address_space_bytes": 64 * 1024**3 + 1 + BUDGETS.supervisor_memory_bytes,
    }
    with pytest.raises(ProtocolError):
        _validate(message)


def test_valid_json_cannot_exceed_control_budget_or_arrive_as_data_frame():
    message = _ready()
    oversized = copy.deepcopy(message)
    oversized["children"] = [123456789] * 2000
    raw = json.dumps(oversized, separators=(",", ":")).encode("ascii")
    assert len(raw) > CONTROL_LIMIT
    with pytest.raises(ProtocolError):
        encode_control(oversized)
    with pytest.raises(ProtocolError):
        decode_control(raw)
    stream = io.BytesIO()
    write_frame(stream, FrameKind.DATA, encode_control(message))
    stream.seek(0)
    with pytest.raises(ProtocolError):
        read_control(stream)
