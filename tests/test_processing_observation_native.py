"""Native pre-install fixtures: real owner/roles, no installer or service mutation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from app.configuration import Settings
from app.processing import manager as processing
from app.processing.observation import clock_stamp
from app.upload_ingress import ReceivedUpload, UploadOptions

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="real Windows owner/process conformance")


def require_child_cleanup(child):
    """Check resources that ProcessTree.cleanup actually reaps and closes."""
    assert type(child.process.returncode) is int
    assert child._channels_closed is True and child._channel_close_failed is False
    assert child.process._handle == 0
    assert child.job is not None and child.job._handle == 0


def remember_spawned_child(created, role, child, errors):
    """An optional fixture query cannot steal ownership from the real manager."""
    item = {"pid": child.pid, "creation_time": None, "child": child}
    created[role] = item
    try:
        item["creation_time"] = child.process.creation_time()
    except Exception as exc:
        errors.append({"role": role, "error_class": type(exc).__name__})
    return child


@pytest.fixture
def completed_owner(monkeypatch, record_property):
    """Observe owner-created handles at spawn and after real bounded cleanup."""
    owner = processing.ProcessingManager()
    identifier = uuid4().hex
    lease = owner.try_acquire(observation_id=identifier, operation="export_xml")
    assert lease is not None
    payload = b'<?xml version="1.0"?>\n<synthetic-observation>native-example</synthetic-observation>\n'
    upload = ReceivedUpload(
        "export_xml", "synthetic-observation.xml", "application/xml", UploadOptions(False), memoryview(payload)
    )
    created, identity_errors = {}, []
    spawn = processing.spawn_role

    def observe_spawn(role, **kwargs):
        child = spawn(role, **kwargs)
        # Query the exact handle just returned by the real creation API; never
        # reopen a PID, retain an invoice channel, or pause a product worker.
        return remember_spawned_child(created, role, child, identity_errors)

    monkeypatch.setattr(processing, "spawn_role", observe_spawn)
    primary_error = None
    try:
        result = asyncio.run(lease.run(upload, Settings()))
        assert b"".join(result.chunks) == payload
        assert result.media_type == "application/xml"
        lease.observe("response_sending")
        lease.observe("response_send_complete")
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        # Fixture cleanup is limited to the manager we created. Never touch an
        # installed backend. A failed cleanup cannot produce the proof below.
        cleanup_errors = []
        for cleanup in (upload.close, lease.release, owner.shutdown):
            try:
                cleanup()
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        if lease.thread is not None:
            lease.thread.join(timeout=owner.budgets.cleanup_seconds)
            if lease.thread.is_alive():
                cleanup_errors.append("UnconfirmedOwnerThreadEnd")
        if primary_error is not None or cleanup_errors or identity_errors:
            # Preserve diagnostics even when the fixture never reaches its test
            # body. A cleanup exception cannot replace the original failure.
            evidence = {
                "status": "FAIL",
                "primary_error": type(primary_error).__name__ if primary_error else None,
                "cleanup_errors": cleanup_errors,
                "identity_errors": identity_errors,
                "owner_pid": os.getpid(),
                "tree_cleaned": lease.tree.cleaned,
                "lease_released": lease.released,
                "roles": [
                    {
                        "role": name,
                        "pid": item["pid"],
                        "creation_time": item["creation_time"],
                        "exit_code": item["child"].process.returncode,
                    }
                    for name, item in created.items()
                ],
            }
            record_property("native_observation_failure", json.dumps(evidence, sort_keys=True))
        if cleanup_errors and primary_error is None:
            raise RuntimeError("Native fixture cleanup failed: " + ", ".join(cleanup_errors))
    assert not identity_errors, "Native fixture identity query failed after ownership transfer"
    assert lease.tree.cleaned and lease.released and not lease.poisoned
    assert lease.tree.outer_job is not None and lease.tree.outer_job._handle == 0
    assert owner.active_count == 0
    assert lease.thread is not None and not lease.thread.is_alive()
    snapshot = owner.observations.snapshot(identifier)
    assert snapshot is not None and snapshot["record"]["available"] is True
    roles = snapshot["record"]["roles"]
    assert {role["role"] for role in roles} == {"supervisor", "worker"}
    assert set(created) == {"supervisor", "worker"}
    for role in roles:
        original = created[role["role"]]
        assert role["pid"] == original["pid"]
        assert role["creation_time"] == original["creation_time"] > 0
        assert role["parent_pid"] == os.getpid()
        assert type(role["exit_code"]) is int
        assert role["exit_code"] == original["child"].process.returncode
        require_child_cleanup(original["child"])
    proof = {
        "method": "real-processing-owner",
        "parent": snapshot["record"]["parent"],
        "cleanup_confirmed": True,
        "lease_released": True,
        "roles": roles,
    }
    record_property("native_observation_proof", json.dumps(proof, sort_keys=True))
    return owner, identifier, snapshot, created


def test_fast_owner_completion(completed_owner):
    owner, identifier, snapshot, _ = completed_owner
    # The first inspection happens after both real children have already ended.
    second = owner.observations.snapshot(identifier)
    assert second is not None
    assert second["record"] == snapshot["record"]
    assert second["instance_id"] == snapshot["instance_id"]
    assert second["record"]["observation_id"] == identifier
    phases = [event["phase"] for event in second["record"]["events"]]
    assert "operation_finished" in phases and "cleanup_confirmed" in phases
    assert phases[-1] == "lease_released"


def test_native_lifecycle_order(completed_owner):
    _, _, snapshot, _ = completed_owner
    events = snapshot["record"]["events"]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    phases = [event["phase"] for event in events]
    required = [
        "admitted",
        "ready",
        "input_received",
        "operation_entering",
        "operation_finished",
        "result_received",
        "cleanup_started",
        "cleanup_confirmed",
        "response_sending",
        "response_send_complete",
        "lease_released",
    ]
    assert phases == required
    interval = events[phases.index("operation_finished")]
    assert type(interval["started"]) is int and type(interval["finished"]) is int
    assert 0 < interval["started"] <= interval["finished"]
    now = clock_stamp()
    assert now is not None and now["kind"] == "qpc"
    assert snapshot["record"]["clock"] == {"kind": "qpc", "frequency": now["frequency"]}
    assert interval["finished"] <= snapshot["snapshot"]["ticks"] <= now["ticks"]
    # Parent receipt time need not precede the worker's actual start/end.
    assert all(type(event["at"]) is int for event in events)


def test_ended_role_before_binding(completed_owner):
    import win32api
    import win32con
    import win32security

    from scripts import test_processing_package as probe

    owner, identifier, snapshot, created = completed_owner
    # Identities were inventoried using creation handles. Final metadata is bound
    # only now, after cleanup, without consulting a potentially reused PID.
    assert all(item["child"].process.returncode is not None for item in created.values())
    assert snapshot["record"]["parent"]["pid"] == os.getpid()
    assert snapshot["record"]["parent"]["creation_time"] > 0
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
    finally:
        token.Close()
    executable = Path(sys.executable).absolute()
    with executable.open("rb") as stream:
        executable_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    binding = probe.PackageBinding(
        os.getpid(),
        snapshot["record"]["parent"]["creation_time"],
        str(executable),
        executable_sha256,
        sid,
        None,
        0,
        "desktop",
    )
    tracker = probe.ObservationTracker(binding, identifier, "export_xml")
    before = clock_stamp()
    retained = owner.observations.snapshot(identifier)
    after = clock_stamp()
    assert before is not None and after is not None
    assert retained is not None and retained["record"]["roles"] == snapshot["record"]["roles"]
    tracker.update(retained, before=(before["ticks"], before["frequency"]), after=(after["ticks"], after["frequency"]))
    probe.require_owner_cleanup(tracker)
    assert tracker.record["roles"] == snapshot["record"]["roles"]
    assert owner.observations.snapshot(uuid4().hex) is None


def test_native_fixture_cleanup(completed_owner):
    owner, identifier, snapshot, created = completed_owner
    assert owner.active_count == 0
    for item in created.values():
        require_child_cleanup(item["child"])
    assert all(type(role["exit_code"]) is int for role in snapshot["record"]["roles"])
    owner.startup()
    replacement = owner.try_acquire(observation_id=uuid4().hex, operation="export_xml")
    assert replacement is not None
    replacement.release()
    owner.shutdown()
    assert owner.active_count == 0
    assert owner.observations.snapshot(identifier) is not None
