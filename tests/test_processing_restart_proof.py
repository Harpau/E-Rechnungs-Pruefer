"""Restart success must precede the next fault; synthetic clients and handles only."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import threading
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from scripts import test_processing_package as probe
from tests.test_processing_observation_probe import envelope


@pytest.fixture
def restart(tmp_path, monkeypatch):
    executable = tmp_path / "synthetic-app.exe"
    executable.write_bytes(b"synthetic executable bytes; never run")
    token = tmp_path / "token"
    token.write_text("a" * 40)
    output = tmp_path / "case"
    output.mkdir()
    binding = probe.PackageBinding(
        10,
        100,
        str(executable),
        hashlib.sha256(executable.read_bytes()).hexdigest(),
        "S-1-5-19",
        "S-1-5-80-1",
        18080,
        "service",
    )
    controller = {"context_id": "synthetic-context", "action": "service-recovery", "harness": {"sha256": "d" * 64}}
    previous = {
        "schema_version": 2,
        "status": "PASS",
        "case": "parent-death",
        "package": asdict(replace(binding, parent_pid=9, parent_created=50)),
        "controller": controller,
        "bound_role_exit_confirmed": True,
    }
    source = output / "result.json"
    source.write_text(json.dumps(previous))
    args = argparse.Namespace(
        confirm_isolated_environment=True,
        case="parent-death",
        mode=binding.mode,
        parent_pid=binding.parent_pid,
        parent_created=binding.parent_created,
        executable=binding.executable,
        executable_sha256=binding.executable_sha256,
        owner_sid=binding.owner_sid,
        service_sid=binding.service_sid,
        port=binding.port,
        token_file=token,
        output_directory=output,
        restart_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    state = SimpleNamespace(
        args=args,
        binding=binding,
        controller=controller,
        previous=previous,
        source=source,
        requests=[],
        closed=[],
        events=[],
        response_change={},
        request_error=None,
        missing_response=False,
        observation_change=lambda value: None,
        after_send=lambda: None,
        context_calls=0,
        reject_context_at=None,
        parent_change=lambda value: value,
        children=[],
        alive=True,
        listener_error=False,
        close_error=False,
        abort_error=False,
        live_client=False,
    )

    def context(action):
        state.context_calls += 1
        state.events.append("context")
        if state.context_calls == state.reject_context_at:
            raise probe.ProbeError("Synthetic context expired")
        return copy.deepcopy(controller)

    class API:
        def open(self, pid, parent_pid):
            state.events.append("open")
            return 10, state.parent_change(
                probe.ProcessIdentity(
                    10,
                    100,
                    str(executable),
                    "S-1-5-19",
                    ("S-1-5-80-1",),
                    1,
                    (str(executable),),
                )
            )

        def close(self, handle):
            state.closed.append(handle)
            if state.close_error:
                raise OSError("synthetic observer close failure")

        def listener(self, port, parent):
            state.events.append("listener")
            if state.listener_error:
                raise probe.ProbeError("synthetic listener mismatch")

        def children(self, parent):
            return state.children

        def alive(self, handle):
            return state.alive

    class Request:
        def __init__(self, port, token, payload, *, export, observation_id):
            assert payload == probe.large_example("cii") and export is True
            self.done = threading.Event()
            self.error = state.request_error
            self.record = {
                "status": 200,
                "byte_identical": True,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "media_type": "application/xml",
            }
            self.record.update(state.response_change)
            self.thread = SimpleNamespace(ident=1, join=lambda *_: None, is_alive=lambda: state.live_client)
            self.closed = False
            state.requests.append(self)

        def start(self):
            state.events.append("send")
            if not state.missing_response:
                self.done.set()
            state.after_send()

        def abort(self):
            if state.abort_error:
                raise OSError("synthetic abort failure")
            self.closed = True

        def close(self):
            self.closed = True

    def observe(api, tracker, token):
        state.events.append("observation")
        value = envelope(complete=True, observation_id=tracker.observation_id)
        value["record"]["operation"] = "export_xml"
        state.observation_change(value)
        tracker.poll_count += 1
        tracker.update(value, before=(1199, 1000), after=(1201, 1000))
        return True

    monkeypatch.setattr(probe, "context_binding", context)
    monkeypatch.setattr(probe, "Request", Request)
    monkeypatch.setattr(probe, "fetch_observation", observe)
    state.api = API()
    return state


@pytest.mark.parametrize("case", ["parent-death", "controlled-stop"])
def test_restart_records_success_on_new_exact_instance_before_return(restart, case):
    restart.args.case = case
    restart.previous["case"] = case
    restart.source.write_text(json.dumps(restart.previous))
    restart.args.restart_source_sha256 = hashlib.sha256(restart.source.read_bytes()).hexdigest()
    original = restart.source.read_bytes()
    result = probe.verify_restart(restart.args, restart.api)
    assert result["status"] == "PASS" and result["case"] == case
    assert result["previous_result_sha256"] == restart.args.restart_source_sha256
    assert result["package"] == asdict(restart.binding)
    assert result["previous_package"] == restart.previous["package"]
    assert result["response"]["status"] == 200 and result["response"]["byte_identical"]
    phases = [e["phase"] for e in result["observations"][0]["latest"]["record"]["events"]]
    assert phases[-3:] == ["response_sending", "response_send_complete", "lease_released"]
    assert len(restart.requests) == 1 and restart.requests[0].closed and restart.closed == [10]
    assert restart.source.read_bytes() == original
    assert json.loads((restart.args.output_directory / "restart-result.json").read_bytes()) == json.loads(
        json.dumps(result)
    )
    assert (restart.args.output_directory / "restart-once.claim.json").is_file()
    assert restart.events.count("send") == 1 and restart.context_calls >= 3


@pytest.mark.parametrize("field,value", [("parent_pid", 9), ("parent_created", 50), ("parent_created", 49)])
def test_restart_cannot_reuse_old_instance(restart, field, value):
    setattr(restart.args, field, value)
    with pytest.raises(probe.ProbeError):
        probe.verify_restart(restart.args, restart.api)
    assert not restart.requests


@pytest.mark.parametrize(
    "field,value",
    [("status", 500), ("byte_identical", False), ("bytes", 0), ("sha256", "0" * 64), ("media_type", "text/plain")],
)
def test_restart_rejects_missing_or_wrong_response_proof(restart, field, value):
    restart.response_change[field] = value
    with pytest.raises(probe.ProbeError):
        probe.verify_restart(restart.args, restart.api)
    assert len(restart.requests) == 1 and restart.requests[0].closed
    assert json.loads((restart.args.output_directory / "restart-result.json").read_bytes())["status"] != "PASS"


@pytest.mark.parametrize("failure", ["missing", "transport"])
def test_restart_never_retries_missing_response_or_transport_failure(restart, failure):
    restart.missing_response = failure == "missing"
    restart.request_error = OSError("synthetic transport failure") if failure == "transport" else None
    with pytest.raises(probe.ProbeError):
        probe.verify_restart(restart.args, restart.api)
    with pytest.raises((probe.ProbeError, FileExistsError)):
        probe.verify_restart(restart.args, restart.api)
    assert len(restart.requests) == 1 and restart.requests[0].closed


@pytest.mark.parametrize("mismatch", ["parent", "observation", "operation", "transport", "cleanup"])
def test_restart_requires_exact_owner_and_successful_lifecycle(restart, mismatch):
    def change(value):
        record = value["record"]
        if mismatch == "parent":
            record["parent"]["creation_time"] = 99
        elif mismatch == "observation":
            record["observation_id"] = "0" * 32
        elif mismatch == "operation":
            record["operation"] = "report_pdf"
        elif mismatch == "transport":
            record["events"][-2]["phase"] = "transport_failed"
        else:
            record["roles"][0]["exit_code"] = None

    restart.observation_change = change
    with pytest.raises(probe.ProbeError):
        probe.verify_restart(restart.args, restart.api)
    assert json.loads((restart.args.output_directory / "restart-result.json").read_bytes())["status"] != "PASS"


@pytest.mark.parametrize("mismatch", ["missing-operation", "missing-result", "processing_error", "upload_failed"])
def test_restart_rejects_owner_without_successful_invoice_processing(restart, mismatch):
    def change(value):
        events = value["record"]["events"]
        if mismatch.startswith("missing"):
            removed = {"result_received"}
            if mismatch == "missing-operation":
                removed.add("operation_finished")
            events[:] = [event for event in events if event["phase"] not in removed]
        else:
            events.insert(6, {"sequence": 7, "phase": mismatch, "at": 600})
        for sequence, event in enumerate(events, 1):
            event["sequence"] = sequence
        value["record"]["revision"] = len(events) + 3

    restart.observation_change = change
    with pytest.raises(probe.ProbeError):
        probe.verify_restart(restart.args, restart.api)
    assert json.loads((restart.args.output_directory / "restart-result.json").read_bytes())["status"] != "PASS"
    assert len(restart.requests) == 1 and restart.requests[0].closed


@pytest.mark.parametrize("when", [2, 3])
def test_restart_rechecks_context_before_request_and_after_success(restart, when):
    restart.reject_context_at = when
    with pytest.raises(probe.ProbeError, match="context expired"):
        probe.verify_restart(restart.args, restart.api)
    if when == 2:
        assert not restart.requests
    else:
        assert len(restart.requests) == 1
        assert json.loads((restart.args.output_directory / "restart-result.json").read_bytes())["status"] != "PASS"


def test_restart_rejects_executable_change_after_response(restart):
    restart.after_send = lambda: restart.source.parent.parent.joinpath("synthetic-app.exe").write_bytes(b"changed")
    with pytest.raises(probe.ProbeError):
        probe.verify_restart(restart.args, restart.api)
    assert json.loads((restart.args.output_directory / "restart-result.json").read_bytes())["status"] != "PASS"


@pytest.mark.parametrize("name", ["restart-once.claim.json", "restart-result.json"])
def test_restart_existing_evidence_blocks_request_without_overwrite(restart, name):
    path = restart.args.output_directory / name
    path.write_text("preserved")
    with pytest.raises((probe.ProbeError, FileExistsError)):
        probe.verify_restart(restart.args, restart.api)
    assert not restart.requests and path.read_text() == "preserved"


def test_restart_rejects_wrong_source_receipt_before_request(restart):
    restart.args.restart_source_sha256 = "0" * 64
    with pytest.raises(probe.ProbeError):
        probe.verify_restart(restart.args, restart.api)
    assert not restart.requests


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", 11),
        ("created", 101),
        ("image", "other.exe"),
        ("owner_sid", "S-1-5-18"),
        ("groups", ()),
    ],
)
def test_restart_checks_live_identity_before_any_invoice(restart, field, value):
    restart.parent_change = lambda identity: replace(identity, **{field: value})
    with pytest.raises(probe.ProbeError):
        probe.verify_restart(restart.args, restart.api)
    assert not restart.requests and restart.closed == [10]


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 1),
        ("status", "INCONCLUSIVE"),
        ("case", "controlled-stop"),
        ("controller", {}),
        ("bound_role_exit_confirmed", False),
        ("package", {}),
    ],
)
def test_restart_does_not_upgrade_unbound_or_failed_previous_case(restart, field, value):
    restart.previous[field] = value
    restart.source.write_text(json.dumps(restart.previous))
    restart.args.restart_source_sha256 = hashlib.sha256(restart.source.read_bytes()).hexdigest()
    with pytest.raises(probe.ProbeError):
        probe.verify_restart(restart.args, restart.api)
    assert not restart.requests


@pytest.mark.parametrize("field,value", [("alive", False), ("children", [20]), ("listener_error", True)])
@pytest.mark.parametrize("after", [False, True])
def test_restart_checks_listener_parent_and_cleanup_before_and_after_invoice(restart, field, value, after):
    if after:
        restart.after_send = lambda: setattr(restart, field, value)
    else:
        setattr(restart, field, value)
    with pytest.raises(probe.ProbeError):
        probe.verify_restart(restart.args, restart.api)
    assert len(restart.requests) == int(after)
    if after:
        assert json.loads((restart.args.output_directory / "restart-result.json").read_bytes())["status"] != "PASS"


@pytest.mark.parametrize("failure", ["close_error", "abort_error", "live_client"])
def test_restart_never_passes_unconfirmed_observer_cleanup(restart, failure):
    setattr(restart, failure, True)
    with pytest.raises(probe.ProbeError):
        probe.verify_restart(restart.args, restart.api)
    assert restart.requests[0].closed and restart.closed == [10]
    assert json.loads((restart.args.output_directory / "restart-result.json").read_bytes())["status"] == "FAIL"


def test_restart_preserves_claim_when_result_write_fails(restart, monkeypatch):
    write = probe.write_new_json

    def write_except_result(path, value):
        if path.name == "restart-result.json":
            raise OSError("synthetic evidence failure")
        write(path, value)

    monkeypatch.setattr(probe, "write_new_json", write_except_result)
    with pytest.raises(OSError):
        probe.verify_restart(restart.args, restart.api)
    with pytest.raises(probe.ProbeError):
        probe.verify_restart(restart.args, restart.api)
    assert len(restart.requests) == 1 and restart.requests[0].closed
    assert (restart.args.output_directory / "restart-once.claim.json").is_file()


def test_powershell_requires_restart_receipt_before_assigning_next_parent():
    source = (probe.ROOT / "scripts/test_processing_package.ps1").read_text()
    section = source[source.index("$RestartGuard =") : source.index("$Current = $Next")]
    assert "--verify-restart" in section and "--restart-source-sha256" in section
    assert '"restart-result.json"' in section and "RestartReceipt.status" in section
    assert "RestartReceipt.package.parent_created" in section and "RestartReceipt.previous_result_sha256" in section
    assert "restart-once.claim.json" in source[source.index("finally {", source.index("$RestartGuard =")) :]
