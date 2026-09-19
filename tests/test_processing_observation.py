from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from dataclasses import replace

import pytest

from app.configuration import Settings
from app.processing import observation
from app.processing.manager import ProcessingManager
from app.upload_ingress import ReceivedUpload, UploadOptions


def test_owner_history_survives_fast_completed_request_and_contains_no_input():
    manager = ProcessingManager()
    nonce = "a" * 32
    lease = manager.try_acquire(observation_id=nonce, operation="export_xml")
    assert lease is not None
    payload = b"<synthetic-observation>DO_NOT_RETAIN_THIS_TEXT</synthetic-observation>"
    upload = ReceivedUpload(
        "export_xml", "not-a-ledger-filename.xml", "application/xml", UploadOptions(False), memoryview(payload)
    )
    try:
        result = asyncio.run(lease.run(upload, Settings()))
        assert b"".join(result.chunks) == payload
        lease.observe("response_sending")
        lease.observe("response_send_complete")
    finally:
        upload.close()
        lease.release()
        manager.shutdown()
    snapshot = manager.observations.snapshot(nonce)
    assert snapshot is not None
    record = snapshot["record"]
    assert record["available"] is True
    phases = [event["phase"] for event in record["events"]]
    assert phases == [
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
    interval = next(event for event in record["events"] if event["phase"] == "operation_finished")
    assert 0 < interval["started"] <= interval["finished"] <= interval["at"] + 1
    assert all(role["exit_code"] is not None for role in record["roles"])
    assert record["parent"]["pid"] > 1
    assert manager.active_count == 0
    serialized = json.dumps(snapshot)
    assert "DO_NOT_RETAIN" not in serialized and "not-a-ledger-filename" not in serialized
    assert len(serialized.encode()) <= 16 * 1024


def test_duplicate_retained_nonce_cannot_replace_job_or_leak_capacity():
    manager = ProcessingManager()
    first = manager.try_acquire(observation_id="a" * 32, operation="export_xml")
    assert first is not None
    snapshot = manager.observations.snapshot("a" * 32)
    with pytest.raises(observation.ObservationConflict):
        manager.try_acquire(observation_id="a" * 32, operation="export_xml")
    assert manager.active_count == 1
    assert manager.observations.snapshot("a" * 32)["record"]["job_id"] == snapshot["record"]["job_id"]
    first.release()
    with pytest.raises(observation.ObservationConflict):
        manager.try_acquire(observation_id="a" * 32, operation="export_xml")
    assert manager.active_count == 0


def test_terminal_eviction_is_bounded_visible_and_does_not_change_admission():
    manager = ProcessingManager()
    for number in range(25):
        lease = manager.try_acquire(observation_id=f"{number:032x}", operation="export_xml")
        assert lease is not None
        lease.release()
    assert manager.observations.snapshot(f"{0:032x}") is None
    snapshot = manager.observations.snapshot(f"{24:032x}")
    assert snapshot["evicted_records"] == 9
    assert manager.active_count == 0


def test_ttl_never_releases_or_evicts_active_poisoned_lease(monkeypatch):
    now = 10.0
    monkeypatch.setattr(observation, "retention_time", lambda: now)
    manager = ProcessingManager()
    lease = manager.try_acquire(observation_id="b" * 32, operation="export_xml")
    assert lease is not None
    lease.poisoned = True
    lease.release()
    now = 1000.0
    snapshot = manager.observations.snapshot("b" * 32)
    assert snapshot is not None
    assert snapshot["record"]["events"][-1]["phase"] == "poisoned"
    assert manager.active_count == 1


def test_terminal_ttl_removes_metadata_without_changing_slot_state(monkeypatch):
    now = 10.0
    monkeypatch.setattr(observation, "retention_time", lambda: now)
    manager = ProcessingManager()
    lease = manager.try_acquire(observation_id="c" * 32, operation="export_xml")
    assert lease is not None
    lease.release()
    now = 129.99
    assert manager.observations.snapshot("c" * 32) is not None
    now = 130.0
    assert manager.observations.snapshot("c" * 32) is None
    assert manager.active_count == 0


def test_clock_failure_makes_observation_unavailable_without_losing_capacity(monkeypatch):
    monkeypatch.setattr(observation, "clock_stamp", lambda: None)
    manager = ProcessingManager()
    lease = manager.try_acquire(observation_id="d" * 32, operation="export_xml")
    assert lease is not None
    lease.release()
    snapshot = manager.observations.snapshot("d" * 32)
    assert snapshot is not None and not snapshot["record"]["available"]
    assert manager.active_count == 0


def test_snapshot_is_detached_from_mutable_owner_state():
    manager = ProcessingManager()
    lease = manager.try_acquire(observation_id="e" * 32, operation="export_xml")
    assert lease is not None
    first = manager.observations.snapshot("e" * 32)
    first["record"]["events"][0]["phase"] = "forged"
    first["record"]["parent"]["pid"] = 9
    second = manager.observations.snapshot("e" * 32)
    assert second["record"]["events"][0]["phase"] == "admitted"
    assert second["record"]["parent"]["pid"] != 9
    lease.release()


def test_snapshot_clock_follows_its_detached_copy_when_owner_publishes_concurrently(monkeypatch):
    ledger = observation.ObservationLedger()
    ledger.register("8" * 32, "export_xml")
    read_clock = observation.clock_stamp
    inject = True

    def publish_after_clock_read():
        nonlocal inject
        stamp = read_clock()
        if inject:
            inject = False
            ledger.note("8" * 32, "upload_failed")
        return stamp

    monkeypatch.setattr(observation, "clock_stamp", publish_after_clock_read)
    snapshot = ledger.snapshot("8" * 32)
    assert all(event["at"] <= snapshot["snapshot"]["ticks"] for event in snapshot["record"]["events"])
    assert ledger.snapshot("8" * 32)["record"]["events"][-1]["phase"] == "upload_failed"


def test_concurrent_publication_uses_the_time_when_its_update_becomes_visible(monkeypatch):
    ledger = observation.ObservationLedger()
    ledger.register("7" * 32, "export_xml")
    stamp = observation.clock_stamp()
    attempted = threading.Event()
    original = ledger._lock

    class ContendedLock:
        def __enter__(self):
            attempted.set()
            original.acquire()

        def __exit__(self, *args):
            original.release()

    monkeypatch.setattr(ledger, "_lock", ContendedLock())
    monkeypatch.setattr(observation, "clock_stamp", lambda: dict(stamp))
    original.acquire()
    publisher = threading.Thread(target=ledger.note, args=("7" * 32, "upload_failed"))
    try:
        publisher.start()
        assert attempted.wait(1)
        # A publisher waited while another metadata operation owned the lock.
        stamp["ticks"] += 1000
    finally:
        original.release()
        publisher.join(2)
    assert not publisher.is_alive()
    event = ledger.snapshot("7" * 32)["record"]["events"][-1]
    assert event["at"] == stamp["ticks"]


@pytest.mark.parametrize("value", ["", "A" * 32, "a" * 31, "a" * 33, "x" * 32, True, None])
def test_observation_identifiers_are_canonical_and_bounded(value):
    assert not observation.valid_observation_id(value)


def test_conformance_observation_loss_and_limits(monkeypatch):
    # Fixed catalog case: pure metadata/validation, no owned native fixtures.
    now = 10.0
    monkeypatch.setattr(observation, "retention_time", lambda: now)
    ledger = observation.ObservationLedger()
    binding = ledger.register("1" * 32, "export_xml")
    ledger.note("1" * 32, "poisoned")
    for number in range(2, 30):
        identifier = f"{number:032x}"
        ledger.register(identifier, "export_xml")
        ledger.note(identifier, "lease_released")
    assert ledger.snapshot("1" * 32)["record"]["available"]
    assert ledger.snapshot(f"{2:032x}") is None
    assert ledger.snapshot(f"{29:032x}")["evicted_records"] == 12
    now = 1000.0
    assert ledger.snapshot("1" * 32) is not None
    assert ledger.snapshot(f"{29:032x}") is None
    for bad in [
        {"type": "operation_entering", "job_id": "2" * 32},
        {"type": "operation_finished", "job_id": binding["job_id"]},
    ]:
        with pytest.raises(observation.ProtocolError):
            observation.validate_entering(bad, binding)
    for start, end in [(True, 10), (10, False), (20, 10), (-1, 10)]:
        with pytest.raises(observation.ProtocolError):
            observation.validate_finished(
                {
                    "type": "operation_finished",
                    "job_id": binding["job_id"],
                    "clock": binding["clock"],
                    "started": start,
                    "finished": end,
                },
                binding,
            )
    ledger.note("1" * 32, "operation_finished")  # no preceding input/entry
    assert not ledger.snapshot("1" * 32)["record"]["available"]
    monkeypatch.setattr(observation, "clock_stamp", lambda: None)
    ledger.register("3" * 32, "export_xml")
    assert not ledger.snapshot("3" * 32)["record"]["available"]
    assert observation.finished_message(binding, None, None) == {
        "type": "observation_unavailable",
        "job_id": binding["job_id"],
    }


def test_observed_operation_error_keeps_terminal_interval_and_cleanup():
    from app.processing.budgets import ProcessingError

    manager = ProcessingManager()
    lease = manager.try_acquire(observation_id="f" * 32, operation="analyze")
    assert lease is not None
    upload = ReceivedUpload("analyze", "synthetic.xml", "application/xml", UploadOptions(False), memoryview(b"not xml"))
    try:
        with pytest.raises(ProcessingError):
            asyncio.run(lease.run(upload, Settings()))
    finally:
        upload.close()
        lease.release()
        manager.shutdown()
    record = manager.observations.snapshot("f" * 32)["record"]
    phases = [event["phase"] for event in record["events"]]
    assert record["available"]
    assert {"operation_finished", "processing_error", "cleanup_confirmed", "lease_released"} <= set(phases)
    assert "result_received" not in phases


def test_failing_owner_ledger_does_not_change_original_xml_or_cleanup(monkeypatch):
    manager = ProcessingManager()
    lease = manager.try_acquire(observation_id="9" * 32, operation="export_xml")
    assert lease is not None

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic observer failure")

    monkeypatch.setattr(manager.observations, "note", broken)
    payload = b"<synthetic/>"
    upload = ReceivedUpload("export_xml", "synthetic.xml", "application/xml", UploadOptions(False), memoryview(payload))
    try:
        result = asyncio.run(lease.run(upload, Settings()))
        assert b"".join(result.chunks) == payload
    finally:
        upload.close()
        lease.release()
        manager.shutdown()
    assert lease.tree.cleaned and manager.active_count == 0
    assert not manager.observations.snapshot("9" * 32)["record"]["available"]


def _require_observed_cleanup(manager, lease, identifier):
    """Retained evidence must agree with the actual original child owners."""
    assert lease.thread is not None
    lease.thread.join(timeout=0.2)
    assert not lease.thread.is_alive()
    assert lease.tree.cleaned and lease.released and not lease.poisoned
    assert manager.active_count == 0
    children = dict(lease.tree.roles)
    children["supervisor"] = lease.tree.supervisor
    if lease.tree.watchdog is not None:
        children["watchdog"] = lease.tree.watchdog
    record = manager.observations.snapshot(identifier)["record"]
    assert record["available"] is True
    assert {role["role"] for role in record["roles"]} == set(children)
    for role in record["roles"]:
        child = children[role["role"]]
        assert child is not None and child.pid == role["pid"]
        assert type(child.process.returncode) is int
        assert role["exit_code"] == child.process.returncode
        assert child.incoming.closed and child.outgoing.closed
        assert child._channels_closed and not child._channel_close_failed
        if sys.platform == "win32":
            assert child.process._handle == 0 and child.job._handle == 0
    if sys.platform == "win32":
        assert lease.tree.outer_job._handle == 0
    phases = [event["phase"] for event in record["events"]]
    assert {"ready", "input_received", "operation_entering", "processing_error", "cleanup_confirmed"} <= set(phases)
    assert phases.index("cleanup_started") < phases.index("cleanup_confirmed") < phases.index("lease_released")
    assert phases[-1] == "lease_released"
    assert "result_received" not in phases and "response_send_complete" not in phases
    return phases


@pytest.mark.parametrize(
    "mode,status,error_type,finished",
    [("memory", 422, "processing_limit_error", True), ("cpu", 504, "processing_timeout_error", False)],
)
def test_observed_native_budget_and_timeout_keep_error_cleanup_and_followup(
    monkeypatch, mode, status, error_type, finished
):
    from app.processing import native
    from app.processing.budgets import ProcessingError
    from tests.test_processing_faults import _budgets, _fault_command, _real_export, _upload

    ordinary = _fault_command(monkeypatch, mode)
    overrides = {"python_memory_bytes": (128 if sys.platform == "win32" else 64) * 1024**2} if mode == "memory" else {}
    manager = ProcessingManager(_budgets(**overrides))
    identifier = "4" * 32
    lease = manager.try_acquire(observation_id=identifier, operation="export_xml")
    assert lease is not None
    upload = _upload()

    async def run():
        started = time.monotonic()
        try:
            with pytest.raises(ProcessingError) as caught:
                await asyncio.wait_for(lease.run(upload, Settings(kosit_enabled=False)), timeout=12)
            assert (caught.value.status, caught.value.error_type) == (status, error_type)
            assert time.monotonic() - started < 8, "the helper's emergency exit cannot count as a budget pass"
        finally:
            upload.close()
            lease.release()
        phases = _require_observed_cleanup(manager, lease, identifier)
        assert ("operation_finished" in phases) is finished
        monkeypatch.setattr(native, "role_command", ordinary)
        manager.budgets = _budgets()
        await _real_export(manager)
        assert manager.active_count == 0

    try:
        asyncio.run(run())
    finally:
        manager.shutdown()


@pytest.mark.parametrize("action", ["cancel", "shutdown"])
def test_observed_native_interruption_after_entry_cleans_and_releases(monkeypatch, action):
    from app.processing import native
    from tests.test_processing_faults import _budgets, _fault_command, _real_export, _upload

    ordinary = _fault_command(monkeypatch, "idle")
    manager = ProcessingManager(replace(_budgets(), python_seconds=2))
    identifier = "5" * 32
    lease = manager.try_acquire(observation_id=identifier, operation="export_xml")
    assert lease is not None
    upload = _upload()

    async def run():
        pending = asyncio.create_task(lease.run(upload, Settings(kosit_enabled=False)))
        try:
            deadline = time.monotonic() + 8
            while True:
                record = manager.observations.snapshot(identifier)["record"]
                if any(event["phase"] == "operation_entering" for event in record["events"]):
                    break
                assert not pending.done(), "interruption must follow actual native input acceptance and entry"
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            assert not pending.done() and lease.python_deadline is not None
            started = time.monotonic()
            if action == "shutdown":
                await asyncio.to_thread(manager.shutdown)
            else:
                pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, timeout=4)
            assert time.monotonic() - started < 4
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            upload.close()
            lease.release()
        phases = _require_observed_cleanup(manager, lease, identifier)
        assert "operation_finished" not in phases, "hard interruption cannot manufacture a terminal operation interval"
        monkeypatch.setattr(native, "role_command", ordinary)
        manager.budgets = _budgets()
        if action == "shutdown":
            assert manager.accepting is False
            manager.startup()
        await _real_export(manager)
        assert manager.active_count == 0

    try:
        asyncio.run(asyncio.wait_for(run(), timeout=16))
    finally:
        manager.shutdown()
