"""Owner evidence, without native process actions or network access."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

import pytest

from scripts import test_processing_package as probe


def binding() -> probe.PackageBinding:
    return probe.PackageBinding(10, 100, r"C:\App\app.exe", "a" * 64, "S-1-5-19", "S-1-5-80-1", 18080, "service")


def envelope(*, complete: bool = False, observation_id: str = "b" * 32) -> dict[str, Any]:
    phases = ["admitted", "ready", "input_received", "operation_entering"]
    if complete:
        phases += [
            "operation_finished",
            "result_received",
            "cleanup_started",
            "cleanup_confirmed",
            "response_sending",
            "response_send_complete",
            "lease_released",
        ]
    events: list[dict[str, Any]] = []
    for index, phase in enumerate(phases):
        event: dict[str, Any] = {"sequence": index + 1, "phase": phase, "at": (index + 1) * 100}
        if phase == "operation_finished":
            event.update(started=401, finished=499)
        events.append(event)
    return {
        "schema_version": 1,
        "instance_id": "a" * 32,
        "snapshot": {"kind": "qpc", "frequency": 1000, "ticks": 1200},
        "evicted_records": 0,
        "record": {
            "observation_id": observation_id,
            "job_id": "c" * 32,
            "operation": "report_pdf",
            "parent": {"pid": 10, "creation_time": 100},
            "roles": [
                {"role": role, "pid": pid, "parent_pid": 10, "creation_time": 200, "exit_code": 0 if complete else None}
                for role, pid in [("worker", 20), ("supervisor", 21)]
            ],
            "clock": {"kind": "qpc", "frequency": 1000},
            "available": True,
            "revision": len(phases) + 3,
            "events": events,
        },
    }


def tracker(*, complete: bool = False) -> Any:
    result = probe.ObservationTracker(binding(), "b" * 32, "report_pdf")
    result.update(envelope(complete=complete), before=(1199, 1000), after=(1201, 1000))
    return result


def test_fast_completed_owner_record_needs_no_foreign_process_open() -> None:
    result = tracker(complete=True)
    probe.require_owner_cleanup(result)
    assert result.record["roles"][0]["exit_code"] == 0
    assert not probe.PROCESS_ACCESS & 0x40
    assert not hasattr(probe.WindowsAPI, "peek_marker")


def normal_owner_value(observation_id: str, operation: str, *, index: int = 0, change: str = "none") -> dict[str, Any]:
    """Synthetic normal response evidence; never historical release receipts."""
    value = envelope(complete=True, observation_id=observation_id)
    record = value["record"]
    record["operation"] = operation
    record["job_id"] = observation_id
    for role in record["roles"]:
        role["pid"] += index * 2
    phases = [event["phase"] for event in record["events"]]
    if change == "r1-failed":
        phases[phases.index("response_send_complete")] = "transport_failed"
    elif change == "missing-complete":
        phases.remove("response_send_complete")
    elif change == "missing-sending":
        phases.remove("response_sending")
    elif change == "missing-release":
        phases.remove("lease_released")
    elif change == "wrong-order":
        phases[-3:-1] = reversed(phases[-3:-1])
    elif change == "duplicate-complete":
        phases.insert(-1, "response_send_complete")
    elif change == "contradictory":
        phases.insert(-1, "transport_failed")
    elif change == "wrong-parent":
        record["parent"]["creation_time"] += 1
    elif change == "wrong-observation":
        record["observation_id"] = "f" * 32
    elif change == "unconfirmed-cleanup":
        record["roles"][0]["exit_code"] = None
    elif change == "missing-cleanup":
        phases.remove("cleanup_confirmed")
    record["events"] = [
        {
            "sequence": index + 1,
            "phase": phase,
            "at": (index + 1) * 100,
            **({"started": 401, "finished": 499} if phase == "operation_finished" else {}),
        }
        for index, phase in enumerate(phases)
    ]
    record["revision"] = len(phases) + 3
    return value


@pytest.mark.parametrize("mode", ["desktop", "service"])
@pytest.mark.parametrize("case", ["health", "xml25", "held-responses"])
@pytest.mark.parametrize(
    "change",
    [
        "none",
        "r1-failed",
        "missing-complete",
        "missing-sending",
        "missing-release",
        "wrong-order",
        "duplicate-complete",
        "contradictory",
        "wrong-parent",
        "wrong-observation",
        "unconfirmed-cleanup",
        "missing-cleanup",
    ],
)
def test_normal_response_requires_positive_bound_send_completion(mode: str, case: str, change: str) -> None:
    package = replace(binding(), mode=mode, service_sid=None if mode == "desktop" else binding().service_sid)
    operation = "report_pdf" if case == "health" else "export_xml"
    count = 1 if case == "xml25" else 2

    def validate() -> None:
        trackers = []
        for index in range(count):
            identifier = str(index + 1) * 32
            result = probe.ObservationTracker(package, identifier, operation)
            value = normal_owner_value(identifier, operation, index=index, change=change if index == 0 else "none")
            result.update(value, before=(1199, 1000), after=(1201, 1000))
            trackers.append(result)
        probe.require_successful_transport(trackers)

    if change == "none":
        validate()
    elif change in {"r1-failed", "contradictory"}:
        with pytest.raises(probe.ProbeError) as captured:
            validate()
        assert not isinstance(captured.value, probe.Inconclusive)
    else:
        with pytest.raises(probe.Inconclusive):
            validate()


def test_successful_send_records_must_have_distinct_job_and_role_bindings() -> None:
    result = tracker(complete=True)
    with pytest.raises(probe.Inconclusive):
        probe.require_successful_transport([result, result])
    with pytest.raises(probe.Inconclusive):
        probe.require_successful_transport([])


@pytest.mark.parametrize(
    "change",
    [
        "instance",
        "job",
        "parent",
        "nonce",
        "event",
        "revision",
        "creation",
        "extra",
        "unavailable",
        "bool",
        "frequency",
        "future",
        "missing-role",
    ],
)
def test_invalid_or_changed_owner_binding_never_becomes_evidence(change: str) -> None:
    result = tracker()
    value = envelope()
    record = value["record"]
    if change == "instance":
        value["instance_id"] = "d" * 32
    elif change == "job":
        record["job_id"] = "d" * 32
    elif change == "parent":
        record["parent"]["creation_time"] += 1
    elif change == "nonce":
        record["observation_id"] = "d" * 32
    elif change == "event":
        record["events"][2]["at"] += 1
    elif change == "revision":
        record["revision"] -= 1
    elif change == "creation":
        record["roles"][0]["creation_time"] += 1
    elif change == "extra":
        record["filename"] = "must never enter evidence"
    elif change == "unavailable":
        record["available"] = False
    elif change == "bool":
        record["revision"] = True
    elif change == "frequency":
        record["clock"]["frequency"] += 1
    elif change == "future":
        value["snapshot"]["ticks"] = 5000
    elif change == "missing-role":
        record["roles"].pop()
    with pytest.raises(probe.ProbeError):
        result.update(value, before=(1199, 1000), after=(1201, 1000))


def test_active_receipt_requires_fresh_clock_ack_entering_and_open_request() -> None:
    result = tracker()
    probe.require_active_observation([result], now=(1202, 1000), requests_done=False)
    for now, done in [((2200, 1000), False), ((1202, 1001), False), ((1202, 1000), True)]:
        with pytest.raises(probe.Inconclusive):
            probe.require_active_observation([result], now=now, requests_done=done)
    with pytest.raises(probe.Inconclusive):
        probe.require_active_observation([tracker(complete=True)], now=(1202, 1000), requests_done=False)


def test_exit_code_without_cleanup_event_is_not_cleanup() -> None:
    value = envelope()
    for role in value["record"]["roles"]:
        role["exit_code"] = 0
    result = probe.ObservationTracker(binding(), "b" * 32, "report_pdf")
    result.update(value, before=(1199, 1000), after=(1201, 1000))
    with pytest.raises(probe.Inconclusive):
        probe.require_owner_cleanup(result)


def intervals() -> tuple[list[Any], list[dict[str, Any]], dict[str, Any]]:
    second = probe.ObservationTracker(binding(), "d" * 32, "report_pdf")
    value = envelope(complete=True, observation_id="d" * 32)
    value["record"]["job_id"] = "e" * 32
    for role in value["record"]["roles"]:
        role["pid"] += 2
    second.update(value, before=(1199, 1000), after=(1201, 1000))
    records = [tracker(complete=True), second]
    samples = [{"started": start, "finished": start + 3, "frequency": 1000, "ok": True} for start in [410, 420, 430]]
    capacity = {
        "started": 440,
        "finished": 450,
        "frequency": 1000,
        "ok": True,
        "status": 503,
        "error_type": "analysis_capacity_error",
    }
    return records, samples, capacity


def test_health_requires_three_complete_authoritative_intervals() -> None:
    records, samples, capacity = intervals()
    assert probe.validate_health_overlap(records, samples, capacity)["witness_indices"] == [0, 1, 2]
    with pytest.raises(probe.Inconclusive):
        probe.validate_health_overlap(records, samples[:2], capacity)


@pytest.mark.parametrize(
    "change", ["slow-boundary", "failed-boundary", "ambiguous", "capacity-outside", "capacity-wrong", "only-entering"]
)
def test_health_boundary_or_capacity_violation_cannot_be_cherry_picked(change: str) -> None:
    records, samples, capacity = intervals()
    if change == "slow-boundary":
        samples.append({"started": 450, "finished": 1600, "frequency": 1000, "ok": True})
    elif change == "failed-boundary":
        samples.append({"started": 450, "finished": 550, "frequency": 1000, "ok": False})
    elif change == "ambiguous":
        samples[0]["started"] = 401
    elif change == "capacity-outside":
        capacity["finished"] = 510
    elif change == "capacity-wrong":
        capacity["error_type"] = "other"
    elif change == "only-entering":
        records[1] = tracker()
    with pytest.raises(probe.ProbeError):
        probe.validate_health_overlap(records, samples, capacity)


def test_snapshot_copy_does_not_retain_caller_mutable_evidence() -> None:
    value = envelope()
    result = probe.ObservationTracker(binding(), "b" * 32, "report_pdf")
    result.update(value, before=(1199, 1000), after=(1201, 1000))
    saved = copy.deepcopy(result.record)
    value["record"]["roles"].clear()
    assert result.record == saved


def test_two_records_cannot_claim_the_same_job_or_kernel_roles() -> None:
    records, samples, capacity = intervals()
    with pytest.raises(probe.Inconclusive):
        probe.validate_health_overlap([records[0], records[0]], samples, capacity)


def test_worker_started_before_owner_received_entering_remains_valid() -> None:
    value = envelope(complete=True)
    value["record"]["events"][4]["started"] = 310
    result = probe.ObservationTracker(binding(), "b" * 32, "report_pdf")
    result.update(value, before=(1199, 1000), after=(1201, 1000))
    assert result.record["events"][4]["started"] < result.record["events"][3]["at"]


def test_observer_get_is_bearer_bound_and_never_replaces_a_missing_record(monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    from types import SimpleNamespace

    result = probe.ObservationTracker(binding(), "b" * 32, "report_pdf")
    calls: list[Any] = []
    statuses = iter([200, 404])
    clocks = iter([(1199, 1000), (1201, 1000), (1202, 1000), (1203, 1000)])

    def bounded_get(port, path, *, headers, limit):
        assert port == 18080 and limit == probe.OBSERVATION_LIMIT
        calls.append(("GET", path, headers))
        return next(statuses), "no-store", json.dumps(envelope()).encode()

    monkeypatch.setattr(probe, "bounded_get", bounded_get)
    api = SimpleNamespace(qpc=lambda: next(clocks))
    assert probe.fetch_observation(api, result, "synthetic-api-token")
    assert calls[0] == (
        "GET",
        "/api/processing-observation",
        {"Authorization": "Bearer synthetic-api-token", "X-Einvoice-Observation-Id": "b" * 32},
    )
    with pytest.raises(probe.Inconclusive, match="disappeared"):
        probe.fetch_observation(api, result, "synthetic-api-token")
    assert len(calls) == 2


def test_conformance_observation_identity_clock_safety() -> None:
    """Stable mandatory catalog node; every bound negative must be exercised."""
    test_fast_completed_owner_record_needs_no_foreign_process_open()
    test_active_receipt_requires_fresh_clock_ack_entering_and_open_request()
    test_exit_code_without_cleanup_event_is_not_cleanup()
    test_snapshot_copy_does_not_retain_caller_mutable_evidence()
    test_two_records_cannot_claim_the_same_job_or_kernel_roles()
    test_worker_started_before_owner_received_entering_remains_valid()
    for change in (
        "instance",
        "job",
        "parent",
        "nonce",
        "event",
        "revision",
        "creation",
        "extra",
        "unavailable",
        "bool",
        "frequency",
        "future",
        "missing-role",
    ):
        test_invalid_or_changed_owner_binding_never_becomes_evidence(change)


def test_conformance_observation_health_boundaries() -> None:
    """No native side effects: validates actual clock-correlated evidence rules."""
    test_health_requires_three_complete_authoritative_intervals()
    for change in (
        "slow-boundary",
        "failed-boundary",
        "ambiguous",
        "capacity-outside",
        "capacity-wrong",
        "only-entering",
    ):
        test_health_boundary_or_capacity_violation_cannot_be_cherry_picked(change)


def test_absolute_observer_deadline_closes_only_its_owned_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    calls: list[Any] = []
    channel = SimpleNamespace(
        settimeout=lambda value: calls.append(("timeout", value)),
        shutdown=lambda how: calls.append(("shutdown", how)),
        close=lambda: calls.append("close"),
        connect=lambda address: calls.append(("connect", address)),
    )
    monkeypatch.setattr(probe.socket, "socket", lambda *args: channel)
    connection = probe.ObserverHTTPConnection(18080)
    connection.expire()
    with pytest.raises(probe.ProbeError):
        connection.connect()
    assert not any(isinstance(item, tuple) and item[0] == "connect" for item in calls)
    assert calls.count("close") == 1


def test_powershell_stop_binds_owner_identity_and_snapshot_freshness() -> None:
    source = (probe.ROOT / "scripts/test_processing_package.ps1").read_text(encoding="utf-8")
    for member in ("instance_id", "observation_id", "job_id", "revision", "snapshot_ticks", "frequency"):
        assert "$Active[0]." + member in source
    guard = source[source.index("$AssertStopAllowed = {") : source.index("& $StopBackend $Current $AssertStopAllowed")]
    assert "$ObservationAge -lt 0 -or $ObservationAge -ge 1" in guard
    assert "$Active[0].snapshot_ticks" in guard
    assert "active_observations = @($Ready.active_observations)" in source


@pytest.mark.parametrize("close_fails", [False, True])
def test_bounded_get_cannot_pass_an_expired_deadline_even_after_response(
    monkeypatch: pytest.MonkeyPatch, close_fails: bool
) -> None:
    import threading
    from types import SimpleNamespace

    closed: list[str] = []

    class Connection:
        def __init__(self, _port: int) -> None:
            self.expired = threading.Event()
            self.channel = SimpleNamespace(close=lambda: closed.append("channel"))

        def request(self, *_args: Any, **_kwargs: Any) -> None:
            self.expire()

        def getresponse(self) -> Any:
            def close_response() -> None:
                closed.append("response")
                if close_fails:
                    raise OSError("synthetic close failure")

            return SimpleNamespace(
                read=lambda size: b"{}", status=200, getheader=lambda name: "no-store", close=close_response
            )

        def expire(self) -> None:
            self.expired.set()

        def close(self) -> None:
            closed.append("connection")

    monkeypatch.setattr(probe, "ObserverHTTPConnection", Connection)
    with pytest.raises(probe.ProbeError, match="time/size"):
        probe.bounded_get(18080, "/api/health", headers={}, limit=16384)
    assert closed == ["response", "connection", "channel"]
