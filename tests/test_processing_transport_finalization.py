"""Actual package finalization with only synthetic clients and Windows API adapters."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import test_processing_package as probe
from tests.test_processing_observation_probe import envelope, normal_owner_value
from tests.test_processing_package import synthetic_run as synthetic_run


@pytest.fixture
def normal_run(synthetic_run: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    args, api, _unused = synthetic_run
    clients: list[Any] = []
    trackers: list[Any] = []
    state = SimpleNamespace(change="none", fetches=0)
    api.children = lambda _parent: []
    api.alive = lambda handle: handle == 10
    api.exit_code = lambda _handle: None
    api.memory = lambda _handle: {
        "status": "observed",
        "method": "K32GetProcessMemoryInfo",
        "unit": "bytes",
        "sample_working_set_bytes": 100,
        "peak_working_set_bytes": 200,
        "sample_private_commit_bytes": 150,
        "peak_private_commit_bytes": 250,
    }
    monkeypatch.setattr(probe, "maximum_xml", lambda: b"<synthetic-tiny-xml/>")
    monkeypatch.setattr(probe.time, "sleep", lambda _delay: None)

    class Client:
        def __init__(self, _port: int, _token: str, payload: bytes, **kwargs: Any) -> None:
            self.observation_id = kwargs.get("observation_id")
            self.payload = payload
            self.done = threading.Event()
            self.error = None
            self.closed = False
            self.record = (
                {"status": 503, "error_type": "analysis_capacity_error"}
                if b"capacity" in payload
                else {"status": 200, "byte_identical": True, "pdf_markers": True}
            )
            self.thread = SimpleNamespace(join=self.join, ident=None, is_alive=lambda: False)
            clients.append(self)

        def start(self) -> None:
            pass

        def join(self, _timeout: float) -> None:
            self.done.set()

        def close(self) -> None:
            self.closed = True

        def abort(self) -> None:
            self.close()

    def fetch(api: Any, tracker: Any, _token: str) -> bool:
        state.fetches += 1
        if tracker not in trackers:
            trackers.append(tracker)
        index = trackers.index(tracker)
        complete = args.case != "health" or state.fetches >= 5
        value = (
            normal_owner_value(tracker.observation_id, tracker.operation, index=index, change=state.change)
            if complete
            else envelope(observation_id=tracker.observation_id)
        )
        record = value["record"]
        if not complete:
            record["job_id"] = tracker.observation_id
            for role in record["roles"]:
                role["pid"] += index * 2
        frequency = 1000000
        for event in record["events"][4:]:
            event["at"] += 1100000
            if event["phase"] == "operation_finished":
                event.update(started=1000000, finished=1100000)
        stamp = 1200000 if complete else api.qpc()[0]
        record["clock"]["frequency"] = frequency
        value["snapshot"] = {"kind": "qpc", "ticks": stamp, "frequency": frequency}
        tracker.poll_count += 1
        tracker.update(value, before=(stamp - 1, frequency), after=(stamp + 1, frequency))
        return True

    def health_sample(api: Any, _port: int) -> dict[str, Any]:
        start, frequency = api.qpc()
        end, _frequency = api.qpc()
        return {"started": start, "finished": end, "frequency": frequency, "ok": True}

    def held_phase(
        api: Any,
        parent_handle: int,
        metrics: dict[str, Any],
        _binding: Any,
        _requests: Any,
        _handles: Any,
        _token: str,
        _started: float,
        *,
        evidence: dict[str, Any],
    ) -> None:
        probe.record_memory(api, parent_handle, metrics, "both-held")
        evidence["synthetic_adapter"] = True

    monkeypatch.setattr(probe, "Request", Client)
    monkeypatch.setattr(probe, "HeldResponseRequest", Client)
    monkeypatch.setattr(probe, "fetch_observation", fetch)
    monkeypatch.setattr(probe, "timed_health", health_sample)
    monkeypatch.setattr(probe, "observe_held_responses", held_phase)
    return args, api, clients, trackers, state


@pytest.mark.parametrize("mode", ["desktop", "service"])
@pytest.mark.parametrize("case", ["health", "xml25", "held-responses"])
@pytest.mark.parametrize("change", ["none", "r1-failed", "missing-complete", "contradictory"])
def test_actual_finalization_gates_recovery_and_preserves_external_receipts(
    normal_run: Any, mode: str, case: str, change: str
) -> None:
    args, api, clients, trackers, state = normal_run
    args.mode, args.case, state.change = mode, case, change
    if mode == "desktop":
        args.service_sid = None
    if change == "none":
        probe.run(args, api)
    else:
        with pytest.raises(probe.ProbeError) as captured:
            probe.run(args, api)
        assert isinstance(captured.value, probe.Inconclusive) == (change == "missing-complete")
    report = json.loads((args.output_directory / "result.json").read_bytes())
    count = 1 if case == "xml25" else 2
    assert report["functional_outcome"] == "PASS"
    assert len(report["responses"]) == len(report["observations"]) == count
    assert all(receipt["status"] == 200 for receipt in report["responses"])
    marker = "pdf_markers" if case == "health" else "byte_identical"
    assert all(receipt[marker] for receipt in report["responses"])
    assert [item["latest"] for item in report["observations"]] == [tracker.envelope for tracker in trackers]
    recoveries = [client for client in clients if b"recovery" in client.payload]
    if change == "none":
        assert report["status"] == "PASS"
        assert report["fresh_request_after_cleanup"]["byte_identical"] and len(recoveries) == 1
    else:
        assert report["status"] == ("INCONCLUSIVE" if change == "missing-complete" else "FAIL")
        assert report["failure_snapshot"]["phase"] == "verify-response-send"
        assert "fresh_request_after_cleanup" not in report and not recoveries
        assert len(clients) == count + (1 if case == "health" else 0)
    assert not api.kills and all(client.closed for client in clients)


def test_intentionally_aborted_case_does_not_require_send_success(
    synthetic_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, api, _clients = synthetic_run

    def forbidden(_trackers: Any) -> None:
        pytest.fail("An intentionally aborted request must not need a send-success event.")

    monkeypatch.setattr(probe, "require_successful_transport", forbidden, raising=False)
    assert probe.run(args, api)["status"] == "PASS"
