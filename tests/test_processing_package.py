"""Safety contracts of the native unsigned package observer; no Windows mutations."""

from __future__ import annotations

from dataclasses import replace

import pytest

from scripts import test_processing_package as probe


def process(pid=20, parent=10, role="worker"):
    return probe.ProcessIdentity(
        pid,
        200,
        r"C:\App\app.exe",
        "S-1-5-19",
        ("S-1-5-80-1",),
        parent,
        (r"C:\App\app.exe", "--einvoice-processing", role, str(parent), "40", "41"),
    )


def binding():
    return probe.PackageBinding(10, 100, r"C:\App\app.exe", "a" * 64, "S-1-5-19", "S-1-5-80-1", 18080, "service")


@pytest.mark.parametrize(
    "change",
    [
        {"image": r"C:\Other\app.exe"},
        {"owner_sid": "S-1-5-18"},
        {"groups": ()},
        {"parent_pid": 11},
        {"created": 99},
        {"argv": ("wrong",)},
        {"argv": (r"C:\App\app.exe", "--einvoice-processing", "worker", "11", "40", "41")},
        {"argv": (r"C:\App\app.exe", "--einvoice-processing", "worker", "10", "-1", "41")},
        {"argv": (r"C:\App\app.exe", "--einvoice-processing", "worker", "10", "40", "40")},
    ],
)
def test_role_mismatch_fails_before_any_action(change):
    with pytest.raises(probe.ProbeError):
        probe.role_of(replace(process(), **change), binding())


def test_role_bound_to_exact_creation_parent_image_and_enabled_service_sid():
    assert probe.role_of(process(), binding()) == "worker"


def test_parent_identity_must_match_original_kernel_creation_time():
    p = replace(process(pid=10), argv=(r"C:\App\app.exe",), created=100)
    probe.validate_parent(p, binding())
    with pytest.raises(probe.ProbeError):
        probe.validate_parent(replace(p, created=101), binding())


def test_synthetic_payload_marker_is_only_in_invoice_and_under_cap():
    payload, marker = probe.processing_fixture("b" * 32)
    assert len(payload) == 8 * 1024**2
    assert marker in payload and b"<!--" in payload
    assert b"-->" in payload
    assert marker not in b"synthetic-processing.xml"


@pytest.mark.parametrize(
    "role_counts", [{"worker": 1}, {"worker": 1, "supervisor": 2}, {"worker": 1, "supervisor": 1, "java": 1}]
)
def test_parent_kill_requires_complete_exact_live_inventory(role_counts):
    with pytest.raises(probe.ProbeError):
        probe.validate_role_counts(role_counts, 1)


def test_full_role_set_and_two_slot_set_are_distinct():
    probe.validate_role_counts({"worker": 1, "supervisor": 1}, 1)
    probe.validate_role_counts({"worker": 2, "supervisor": 2}, 2)


def test_active_observation_never_passes_after_request_finished():
    with pytest.raises(probe.Inconclusive):
        probe.require_active(marker_seen=True, requests_done=True)
    with pytest.raises(probe.Inconclusive):
        probe.require_active(marker_seen=False, requests_done=False)
    probe.require_active(marker_seen=True, requests_done=False)


def test_ready_and_result_binding_cannot_reuse_existing_output(tmp_path):
    target = tmp_path / "ready.json"
    probe.write_new_json(target, {"nonce": "a" * 32})
    with pytest.raises(FileExistsError):
        probe.write_new_json(target, {"nonce": "b" * 32})


def test_xml_multipart_has_no_official_field():
    media, body = probe.multipart(b"<synthetic/>", export=True)
    assert media.startswith("multipart/form-data; boundary=")
    assert b'name="official"' not in body
    assert body.count(b"Content-Disposition:") == 1


def test_processing_multipart_has_fixed_options_no_user_supplied_command():
    _, body = probe.multipart(b"<synthetic/>", export=False)
    assert b'name="official"\r\n\r\nfalse' in body
    assert b'name="scope"\r\n\r\nreadable' in body


def test_health_sample_above_one_second_is_failed_not_rounded_down():
    probe.validate_health([0.1, 0.4, 0.999])
    with pytest.raises(probe.ProbeError):
        probe.validate_health([0.1, 1.00001, 0.2])
    with pytest.raises(probe.ProbeError):
        probe.validate_health([])


@pytest.fixture()
def synthetic_run(tmp_path, monkeypatch):
    import argparse
    import threading
    from types import SimpleNamespace

    exe = tmp_path / "app.exe"
    exe.write_bytes(b"synthetic fixture bytes, never executed")
    token = tmp_path / "token"
    token.write_text("a" * 40)
    args = argparse.Namespace(
        confirm_isolated_environment=True,
        mode="service",
        case="parent-death",
        parent_pid=10,
        parent_created=100,
        executable=str(exe),
        executable_sha256=probe.acceptance._file(exe)["sha256"],
        owner_sid="S-1-5-19",
        service_sid="S-1-5-80-1",
        port=18080,
        token_file=token,
        output_directory=tmp_path / "evidence",
    )
    monkeypatch.setattr(probe, "context_binding", lambda action: {"context_id": "synthetic", "action": action})
    monkeypatch.setattr(probe, "health", lambda port: 0.05)
    monkeypatch.setattr(probe, "processing_fixture", lambda nonce: (b"<synthetic/>", b"marker"))

    class API:
        def __init__(self):
            self.scans = 0
            self.kills = []
            self.closed = []
            self.ended = set()
            self.extra = False
            self.peek = True

        def open(self, pid, parent_pid):
            role = "worker" if pid == 20 else "supervisor"
            argv = (
                (str(exe),)
                if pid == 10
                else (
                    str(exe),
                    "--einvoice-processing",
                    role,
                    "10",
                    "40",
                    "41",
                    *(["42", "43"] if role == "supervisor" else []),
                )
            )
            return pid, probe.ProcessIdentity(
                pid, 100 if pid == 10 else 200, str(exe), "S-1-5-19", ("S-1-5-80-1",), parent_pid, argv
            )

        def children(self, pid):
            if pid != 10:
                return []
            self.scans += 1
            if self.scans == 1:
                return []
            return [20, 21, *([22] if self.extra and self.scans > 2 else [])]

        def alive(self, h):
            return h not in self.ended

        def terminate(self, h):
            self.kills.append(h)
            self.ended.update((10, 20, 21))

        def close(self, h):
            self.closed.append(h)

        def peek_marker(self, *args):
            return self.peek

        def listener(self, *args):
            pass

        def memory(self, handle):
            return {"status": "observed", "method": "synthetic-mock-only"}

    api = API()
    requests = []

    class Request:
        def __init__(self, *a, **kw):
            self.done = threading.Event()
            self.error = None
            self.record = {}
            self.closed = False
            self.thread = SimpleNamespace(join=self.join)
            requests.append(self)

        def start(self):
            pass

        def join(self, *a):
            self.record = {"status": 500}
            self.done.set()

        def close(self):
            self.closed = True

    monkeypatch.setattr(probe, "Request", Request)
    return args, api, requests


def test_parent_termination_uses_only_bound_handle_then_confirms_every_role(synthetic_run):
    args, api, requests = synthetic_run
    result = probe.run(args, api)
    assert result["status"] == "PASS" and result["bound_role_exit_confirmed"]
    assert api.kills == [10] and set(api.closed) == {10, 20, 21}
    assert all(r.closed for r in requests)
    assert (args.output_directory / "ready.json").exists()
    assert "token" not in (args.output_directory / "result.json").read_text().replace("token paths", "")


def test_parent_is_never_killed_with_an_unbound_live_child(synthetic_run):
    args, api, requests = synthetic_run
    api.extra = True
    with pytest.raises(probe.Inconclusive):
        probe.run(args, api)
    assert api.kills == [] and all(r.closed for r in requests)
    assert not (args.output_directory / "ready.json").exists()


def test_request_completion_race_never_terminates_parent(synthetic_run):
    args, api, requests = synthetic_run

    def observed(*a):
        requests[0].done.set()
        return True

    api.peek_marker = observed
    with pytest.raises(probe.Inconclusive):
        probe.run(args, api)
    assert api.kills == []


def test_context_change_before_action_prevents_kernel_mutation(synthetic_run, monkeypatch):
    args, api, _ = synthetic_run
    contexts = iter([{"context_id": "one"}, {"context_id": "two"}])
    monkeypatch.setattr(probe, "context_binding", lambda action: next(contexts))
    with pytest.raises(probe.ProbeError):
        probe.run(args, api)
    assert api.kills == []


def test_listener_mismatch_prevents_reading_token_or_starting_request(synthetic_run):
    args, api, requests = synthetic_run
    args.token_file.unlink()

    def reject(*a):
        raise probe.ProbeError("Wrong listener")

    api.listener = reject
    with pytest.raises(probe.ProbeError, match="Wrong listener"):
        probe.run(args, api)
    assert not requests and not api.kills


def test_keyboard_interrupt_preserves_failure_and_closes_only_owned_observers(synthetic_run):
    args, api, requests = synthetic_run

    def interrupted(*a):
        raise KeyboardInterrupt

    api.peek_marker = interrupted
    with pytest.raises(KeyboardInterrupt):
        probe.run(args, api)
    assert not api.kills and set(api.closed) == {10, 20}
    assert all(r.closed for r in requests)
    assert '"status":"FAIL"' in (args.output_directory / "result.json").read_text()


def test_unexpected_role_descendant_blocks_parentkill(synthetic_run):
    args, api, _ = synthetic_run
    original = api.children
    api.children = lambda pid: [99] if pid == 20 else original(pid)
    with pytest.raises(probe.ProbeError, match="descendants"):
        probe.run(args, api)
    assert not api.kills


def test_observer_close_failure_cannot_leave_pass_receipt(synthetic_run):
    args, api, _ = synthetic_run

    def close(handle):
        api.closed.append(handle)
        if handle == 20:
            raise OSError("synthetic close failure")

    api.close = close
    with pytest.raises(probe.ProbeError, match="cleanup"):
        probe.run(args, api)
    assert set(api.closed) == {10, 20, 21}
    assert '"status":"FAIL"' in (args.output_directory / "result.json").read_text()


@pytest.mark.parametrize(
    "third_status,race,expected", [(503, False, "PASS"), (200, False, "FAIL"), (503, True, "INCONCLUSIVE")]
)
def test_third_request_capacity_proof_requires_two_still_active_jobs(
    synthetic_run, monkeypatch, third_status, race, expected
):
    import threading
    from types import SimpleNamespace

    args, api, _ = synthetic_run
    args.case = "health"
    original_open = api.open

    def opened(pid, parent):
        handle, identity = original_open(pid, parent)
        if pid == 22:
            identity = replace(
                identity, argv=(str(args.executable), "--einvoice-processing", "worker", "10", "40", "41")
            )
        return handle, identity

    api.open = opened

    def children(pid):
        if pid != 10:
            return []
        api.scans += 1
        return [] if api.scans == 1 else [20, 21, 22, 23]

    api.children = children
    requests = []

    class Request:
        def __init__(self, *a, **kw):
            self.index = len(requests)
            requests.append(self)
            self.done = threading.Event()
            self.error = None
            self.record = {}
            self.thread = SimpleNamespace(join=self.join)

        def start(self):
            pass

        def close(self):
            pass

        def join(self, *a):
            if self.index == 2:
                self.record = {"status": third_status}
                if race:
                    requests[0].done.set()
            else:
                self.record = {"status": 200, "pdf_markers": True, "byte_identical": True}
                api.ended.update((20, 21, 22, 23))
            self.done.set()

    monkeypatch.setattr(probe, "Request", Request)
    if expected == "PASS":
        assert probe.run(args, api)["third_request_capacity"]["status"] == 503
    else:
        with pytest.raises(probe.Inconclusive if race else probe.ProbeError):
            probe.run(args, api)
        assert not api.kills


def test_kernel_parent_binding_rejects_stale_snapshot():
    from types import SimpleNamespace

    api = object.__new__(probe.WindowsAPI)

    def query(handle, kind, raw, size, returned):
        info = raw._obj
        info.pid = 20
        info.parent = 99
        returned._obj.value = size
        return 0

    api.n = SimpleNamespace(NtQueryInformationProcess=query)
    with pytest.raises(probe.ProbeError, match="parent"):
        api.kernel_parent(123, 20, 10)
    assert api.kernel_parent(123, 20, 0) == 99
    with pytest.raises(probe.ProbeError, match="PID"):
        api.kernel_parent(123, 21, 0)


def stop_receipt():
    report = {
        "nonce": "a" * 32,
        "package": {"parent_pid": 10, "parent_created": 100},
        "controller": {"context_id": "synthetic"},
        "ready_sha256": "b" * 64,
        "ready_qpc_ticks": 100,
        "qpc_frequency": 1000,
    }
    receipt = {
        "schema_version": 1,
        "action": "stop-bound-parent",
        "nonce": report["nonce"],
        "package": report["package"],
        "controller": report["controller"],
        "ready_sha256": report["ready_sha256"],
        "qpc_ticks": 200,
        "qpc_frequency": 1000,
    }
    return report, receipt


@pytest.mark.parametrize(
    "field,value",
    [
        ("nonce", "c" * 32),
        ("ready_sha256", "c" * 64),
        ("package", {"parent_pid": 11, "parent_created": 100}),
        ("controller", {}),
        ("qpc_ticks", 99),
        ("qpc_ticks", 301),
        ("qpc_ticks", True),
        ("qpc_frequency", 1001),
    ],
)
def test_stop_receipt_must_bind_ready_parent_context_and_native_clock(field, value):
    report, receipt = stop_receipt()
    receipt[field] = value
    with pytest.raises(probe.ProbeError):
        probe.validate_stop_receipt(receipt, report, (300, 1000))


def test_stop_receipt_deadline_is_action_time_not_ready_time():
    report, receipt = stop_receipt()
    assert probe.validate_stop_receipt(receipt, report, (300, 1000)) == (200, 1000)


def test_late_observation_cannot_claim_five_second_role_cleanup():
    from types import SimpleNamespace

    api = SimpleNamespace(qpc=lambda: (5201, 1000), alive=lambda handle: False)
    with pytest.raises(probe.ProbeError, match="deadline"):
        probe.wait_ended_qpc(api, [20, 21], start=200, frequency=1000, seconds=5)
    assert probe.wait_ended_qpc(api, [10], start=200, frequency=1000, seconds=10) == 5.001


def test_readonly_prestart_guard_checks_context_before_native_work(synthetic_run, monkeypatch):
    args, api, _ = synthetic_run
    args.verify_only = "context"
    calls = []

    def stale(action):
        calls.append(action)
        raise probe.ProbeError("stale")

    monkeypatch.setattr(probe, "context_binding", stale)
    with pytest.raises(probe.ProbeError, match="stale"):
        probe.verify_only(args, api)
    assert calls == ["service-recovery"] and not api.closed


def test_controlled_stop_requires_both_native_deadlines_and_receipt_hash(tmp_path):
    import hashlib
    from types import SimpleNamespace

    report, receipt = stop_receipt()
    path = tmp_path / "stop-action.json"
    probe.write_new_json(path, receipt)
    api = SimpleNamespace(qpc=lambda: (300, 1000), alive=lambda h: False)
    probe.controlled_stop(api, tmp_path, report, 10, [20, 21])
    assert report["role_exit_after_stop_seconds"] == 0.1
    assert report["parent_exit_after_stop_seconds"] == 0.1
    assert report["stop_action_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_changed_stop_receipt_is_not_retried(tmp_path, monkeypatch):
    report, _ = stop_receipt()

    def changed(path):
        raise probe.acceptance.ContextError("changed file")

    monkeypatch.setattr(probe.acceptance, "_read", changed)
    with pytest.raises(probe.acceptance.ContextError, match="changed"):
        probe.controlled_stop(None, tmp_path, report, 10, [20, 21])


def test_powershell_stop_receipt_is_atomically_published_and_scm_stop_nonblocking():
    source = (probe.ROOT / "scripts/test_processing_package.ps1").read_text()
    flush = source.index("$StopStream.Flush($true)")
    publish = source.index("[IO.File]::Move($StopTemporaryPath, $StopPath)")
    stop = source.index("& $StopBackend $Current")
    assert flush < publish < stop
    assert "$StopStream = [IO.File]::Open($StopTemporaryPath, [IO.FileMode]::CreateNew" in source
    service = (probe.ROOT / "scripts/test_windows_service_package.ps1").read_text()
    callback = service.split("$StopProcessingService = {", 1)[1].split("$ProcessingParent = Invoke-", 1)[0]
    assert "Stop-Service $ServiceName -NoWait" in callback


@pytest.mark.parametrize(
    "script,action",
    [
        ("test_windows_package.ps1", "[void]$StopEvent.Set()"),
        ("test_windows_service_package.ps1", "Stop-Service $ServiceName -NoWait"),
    ],
)
def test_stop_controller_checks_deadline_immediately_at_native_action(script, action):
    source = (probe.ROOT / "scripts" / script).read_text()
    before = source[: source.index(action)].rstrip()
    assert before.endswith("& $AssertStopAllowed")


def test_package_memory_preserves_lifetime_peaks_and_marks_failed_query():
    from types import SimpleNamespace

    calls = []

    def memory(handle):
        calls.append(handle)
        if len(calls) == 2:
            raise OSError("short process ended")
        return {"status": "observed", "peak_working_set_bytes": 123, "peak_private_commit_bytes": 456}

    api = SimpleNamespace(memory=memory)
    record = {}
    probe.record_memory(api, 42, record, "bound")
    probe.record_memory(api, 42, record, "before-close")
    assert calls == [42, 42]
    assert record["memory_observations"][0]["peak_private_commit_bytes"] == 456
    assert record["memory_observations"][1]["status"] == "unavailable"
    assert "peak_private_commit_bytes" not in record["memory_observations"][1]


def test_held_receive_buffer_is_set_before_loopback_connect(monkeypatch):
    calls = []

    class Socket:
        def settimeout(self, value):
            calls.append(("timeout", value))

        def setsockopt(self, *values):
            calls.append(("buffer", values))

        def getsockopt(self, *values):
            return 65536

        def connect(self, target):
            calls.append(("connect", target))

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(probe.socket, "socket", lambda *_a: Socket())
    connection = probe.HeldHTTPConnection(18080)
    connection.connect()
    assert next(i for i, call in enumerate(calls) if call[0] == "buffer") < next(
        i for i, call in enumerate(calls) if call[0] == "connect"
    )
    assert calls[-1] == ("connect", ("127.0.0.1", 18080))
    assert connection.receive_buffer_bytes == 65536


@pytest.fixture()
def held_requests(monkeypatch):
    import threading
    from types import SimpleNamespace

    requests = []

    class Held:
        def __init__(self, at=10.0):
            self.header_ready = threading.Event()
            self.header_ready.set()
            self.release_reading = threading.Event()
            self.done = threading.Event()
            self.error = None
            self.body_reads = 0
            self.header_received_at = at
            self.header_record = {"status": 200, "content_length": 25 * 1024**2}
            self.drain_deadline = None
            self.connection = SimpleNamespace(receive_buffer_bytes=65536)
            requests.append(self)

        def allow_reading(self, deadline):
            self.drain_deadline = deadline
            self.release_reading.set()

    Held()
    Held(11.0)
    monkeypatch.setattr(probe.time, "monotonic", lambda: 12.0)
    return requests


def test_both_response_headers_and_unread_bodies_define_one_held_window(held_requests):
    assert probe.held_response_deadline(held_requests) == 35.0
    assert all(not request.release_reading.is_set() for request in held_requests)


@pytest.mark.parametrize("change", ["finished", "reading", "wrong_length", "wrong_status", "missing_header", "late"])
def test_incomplete_or_late_response_hold_cannot_be_evidence(held_requests, monkeypatch, change):
    request = held_requests[1]
    if change == "finished":
        request.done.set()
    elif change == "reading":
        request.body_reads = 1
    elif change == "wrong_length":
        request.header_record["content_length"] -= 1
    elif change == "wrong_status":
        request.header_record["status"] = 503
    elif change == "missing_header":
        request.header_ready.clear()
    else:
        monkeypatch.setattr(probe.time, "monotonic", lambda: 25.0)
    with pytest.raises(probe.ProbeError):
        probe.held_response_deadline(held_requests)


def test_later_header_never_restarts_the_common_send_deadline(held_requests, monkeypatch):
    held_requests[1].header_received_at = 19.0
    monkeypatch.setattr(probe.time, "monotonic", lambda: 20.0)
    assert probe.held_response_deadline(held_requests) == 35.0


def test_capacity_response_requires_exact_error_code_not_just_503():
    probe.validate_held_capacity({"status": 503, "error_type": "analysis_capacity_error"}, 0.25)
    for record, elapsed in (
        ({"status": 503, "error_type": "analysis_unavailable_error"}, 0.25),
        ({"status": 503}, 0.25),
        ({"status": 200, "error_type": "analysis_capacity_error"}, 0.25),
        ({"status": 503, "error_type": "analysis_capacity_error"}, 1.0),
    ):
        with pytest.raises(probe.ProbeError):
            probe.validate_held_capacity(record, elapsed)


def test_held_backend_memory_requires_all_three_real_byte_counters():
    observation = {
        "status": "observed",
        "unit": "bytes",
        "method": "K32GetProcessMemoryInfo",
        "sample_working_set_bytes": 100,
        "peak_working_set_bytes": 200,
        "sample_private_commit_bytes": 150,
        "peak_private_commit_bytes": 250,
    }
    metrics = {
        "memory_observations": [{"phase": phase, **observation} for phase in ("bound", "both-held", "before-close")]
    }
    probe.validate_held_memory(metrics)
    metrics["memory_observations"][1] = {"phase": "both-held", "status": "unavailable"}
    with pytest.raises(probe.ProbeError):
        probe.validate_held_memory(metrics)


def test_held_case_is_in_shared_package_catalog():
    assert "held-responses" in probe.CASES
    source = (probe.ROOT / "scripts/test_processing_package.ps1").read_text()
    assert 'foreach ($Case in @("held-responses",' in source


@pytest.fixture()
def held_transport(monkeypatch):
    payload = b"<tiny/>"
    monkeypatch.setattr(probe, "HELD_XML_BYTES", len(payload))
    calls = []

    class Socket:
        def settimeout(self, value):
            calls.append("timeout")

        def shutdown(self, _how):
            calls.append("shutdown")

    class Response:
        status = 200
        chunks = [payload, b""]

        def getheaders(self):
            return [("Content-Length", str(len(payload)))]

        def getheader(self, _key):
            return "application/xml"

        def read1(self, _size):
            calls.append("body-read")
            return self.chunks.pop(0)

    response = Response()

    class Connection:
        sock = Socket()
        receive_buffer_bytes = 65536

        def __init__(self, _port):
            pass

        def request(self, *args):
            calls.append("request")

        def getresponse(self):
            return response

        def close(self):
            calls.append("close")

    monkeypatch.setattr(probe, "HeldHTTPConnection", Connection)
    return payload, response, calls


def test_actual_client_thread_does_not_read_body_before_explicit_release(held_transport):
    payload, _response, calls = held_transport
    request = probe.HeldResponseRequest(18080, "a" * 40, payload)
    request.start()
    try:
        assert request.header_ready.wait(1)
        assert not request.done.is_set() and request.body_reads == 0 and "body-read" not in calls
        request.allow_reading(probe.time.monotonic() + 1)
        request.thread.join(1)
        assert request.done.is_set() and request.error is None
        assert request.record["byte_identical"] and request.record["bytes"] == len(payload)
    finally:
        request.close()
        request.thread.join(1)
    assert not request.thread.is_alive()


def test_aborting_held_client_wakes_waiter_without_reading_body(held_transport):
    payload, _response, calls = held_transport
    request = probe.HeldResponseRequest(18080, "a" * 40, payload)
    request.start()
    assert request.header_ready.wait(1)
    request.close()
    request.thread.join(1)
    assert not request.thread.is_alive() and request.done.is_set() and request.error is not None
    assert "shutdown" in calls and "body-read" not in calls


def test_abort_during_request_send_shuts_down_the_still_attached_owned_socket():
    from types import SimpleNamespace

    calls = []
    request = probe.Request(18080, "a" * 40, b"<x/>", export=True)
    request.connection = SimpleNamespace(
        sock=SimpleNamespace(shutdown=lambda _how: calls.append("shutdown")),
        close=lambda: calls.append("close"),
    )
    request.abort()
    assert calls == ["shutdown", "close"]


def test_bad_headers_never_enter_a_held_window(held_transport):
    payload, response, calls = held_transport
    response.status = 503
    request = probe.HeldResponseRequest(18080, "a" * 40, payload)
    request.start()
    request.thread.join(1)
    assert request.done.is_set() and request.error is not None and not request.header_ready.is_set()
    assert "body-read" not in calls
    request.close()


def test_held_client_cannot_extend_its_original_deadline(held_transport):
    payload, _response, _calls = held_transport
    request = probe.HeldResponseRequest(18080, "a" * 40, payload)
    request.start()
    try:
        assert request.header_ready.wait(1)
        with pytest.raises(probe.ProbeError, match="extended"):
            request.allow_reading(request.drain_deadline + 1)
    finally:
        request.close()
        request.thread.join(1)
    assert not request.thread.is_alive()


@pytest.mark.parametrize("failure", [None, "live-role", "wrong-capacity-code", "slow-health"])
def test_held_window_observes_parent_between_capacity_proofs_before_releasing_readers(
    held_requests, monkeypatch, failure
):
    import threading
    from types import SimpleNamespace

    calls = []
    capacities = []
    metrics = {}

    def memory(handle):
        assert handle == 10 and all(not r.release_reading.is_set() for r in held_requests)
        calls.append("memory")
        return {"status": "observed"}

    api = SimpleNamespace(
        alive=lambda handle: handle == 10 or failure == "live-role",
        children=lambda _pid: [],
        listener=lambda *_args: None,
        memory=memory,
    )

    class Capacity:
        def __init__(self, *args, **kwargs):
            self.done = threading.Event()
            self.error = None
            self.record = {
                "status": 503,
                "error_type": "other" if failure == "wrong-capacity-code" else "analysis_capacity_error",
            }
            self.thread = SimpleNamespace(join=lambda _timeout: self.done.set(), is_alive=lambda: False)
            self.aborted = False
            capacities.append(self)

        def start(self):
            calls.append("capacity")

        def abort(self):
            self.aborted = True

    monkeypatch.setattr(probe, "Request", Capacity)

    def health(_port):
        assert all(not r.release_reading.is_set() for r in held_requests)
        calls.append("health")
        return 1.0 if failure == "slow-health" else 0.05

    monkeypatch.setattr(probe, "health", health)
    for request in held_requests:
        request.thread = SimpleNamespace(join=lambda _timeout, request=request: request.done.set())
    if failure:
        evidence = {}
        with pytest.raises(probe.ProbeError):
            probe.observe_held_responses(
                api, 10, metrics, binding(), held_requests, [20, 21, 22, 23], "a" * 40, 10, evidence=evidence
            )
        assert all(not r.release_reading.is_set() for r in held_requests)
        assert evidence["scope"].startswith("two held 25MiB")
        if failure != "live-role":
            assert len(evidence["headers"]) == 2
    else:
        result = probe.observe_held_responses(
            api, 10, metrics, binding(), held_requests, [20, 21, 22, 23], "a" * 40, 10
        )
        assert calls == ["capacity", "memory", "health", "health", "health", "capacity"]
        assert all(r.release_reading.is_set() and r.drain_deadline == 35 for r in held_requests)
        assert result["body_read_calls_before_release"] == [0, 0]
        assert metrics["memory_observations"][0]["phase"] == "both-held"
    assert all(capacity.aborted for capacity in capacities)


@pytest.mark.parametrize("missing_counter", [False, True])
def test_complete_held_case_keeps_parent_binding_and_never_passes_missing_memory(
    synthetic_run, monkeypatch, missing_counter
):
    import threading
    from types import SimpleNamespace

    args, api, _unused = synthetic_run
    args.case = "held-responses"
    payload = b"<tiny/>"
    monkeypatch.setattr(probe, "HELD_XML_BYTES", len(payload))
    monkeypatch.setattr(probe, "maximum_xml", lambda: payload)
    original_open = api.open

    def opened(pid, parent):
        handle, identity = original_open(pid, parent)
        if pid == 22:
            identity = replace(
                identity, argv=(str(args.executable), "--einvoice-processing", "worker", "10", "40", "41")
            )
        return handle, identity

    api.open = opened

    def children(pid):
        api.scans += 1
        return [20, 21, 22, 23] if api.scans == 2 else []

    api.children = children
    api.alive = lambda handle: handle == 10
    memory_handles = []

    def memory(handle):
        memory_handles.append(handle)
        if missing_counter and handle == 10 and memory_handles.count(10) == 2:
            raise OSError("synthetic counter unavailable")
        return {
            "status": "observed",
            "method": "K32GetProcessMemoryInfo",
            "unit": "bytes",
            "sample_working_set_bytes": 100,
            "peak_working_set_bytes": 200,
            "sample_private_commit_bytes": 150,
            "peak_private_commit_bytes": 250,
        }

    api.memory = memory
    clients = []

    class Client:
        def __init__(self, _port, _token, payload, **_kw):
            self.done = threading.Event()
            self.error = None
            self.closed = False
            self.record = (
                {"status": 503, "error_type": "analysis_capacity_error"}
                if b"capacity" in payload
                else {
                    "status": 200,
                    "byte_identical": True,
                }
            )
            self.thread = SimpleNamespace(join=self.join, is_alive=lambda: False, ident=123)
            clients.append(self)

        def start(self):
            self.done.set()

        def join(self, _timeout):
            self.done.set()

        def close(self):
            self.closed = True

        def abort(self):
            self.close()

    class Held(Client):
        def __init__(self, *args):
            super().__init__(*args)
            self.header_ready = threading.Event()
            self.release_reading = threading.Event()
            self.header_received_at = 0.0
            self.header_record = {"status": 200, "content_length": len(payload)}
            self.body_reads = 0
            self.connection = SimpleNamespace(receive_buffer_bytes=65536)

        def start(self):
            self.header_received_at = probe.time.monotonic()
            self.header_ready.set()

        def allow_reading(self, deadline):
            self.release_reading.set()

        def join(self, _timeout):
            assert self.release_reading.is_set()
            self.done.set()

    monkeypatch.setattr(probe, "Request", Client)
    monkeypatch.setattr(probe, "HeldResponseRequest", Held)
    if missing_counter:
        with pytest.raises(probe.ProbeError):
            probe.run(args, api)
        assert '"status":"FAIL"' in (args.output_directory / "result.json").read_text()
    else:
        result = probe.run(args, api)
        assert result["status"] == "PASS" and result["fresh_request_after_cleanup"]["byte_identical"]
        assert result["held_responses"]["bound_roles_ended_before_measurement"]
        assert [v["phase"] for v in result["process_metrics"]["10"]["memory_observations"]] == [
            "bound",
            "both-held",
            "before-close",
        ]
    assert memory_handles.count(10) == 3 and api.kills == []
    assert set(api.closed) == {10, 20, 21, 22, 23} and all(client.closed for client in clients)


@pytest.mark.parametrize("first_exists", [True, False])
def test_powershell_resolves_exactly_first_python_application_not_concatenated_paths(tmp_path, first_exists):
    import json
    import shutil
    import subprocess

    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is required for the native resolver regression")
    candidates = [tmp_path / f"candidate {index}" / "python.exe" for index in range(4)]
    for index, path in enumerate(candidates):
        path.parent.mkdir()
        if index != 0 or first_exists:
            path.write_text("synthetic path fixture; never executed")

    def quoted(path):
        return "'" + str(path).replace("'", "''") + "'"

    harness = tmp_path / "resolver.ps1"
    harness.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        + ". "
        + quoted(probe.ROOT / "scripts/test_processing_package.ps1")
        + "\n"
        + "function Get-Command { @("
        + ",".join("[pscustomobject]@{Source=" + quoted(path) + "}" for path in candidates)
        + ") }\n"
        + "try { $Selected = Resolve-BoundProcessingPython; @{status='ok'; path=$Selected; scalar=($Selected -is [string])} | ConvertTo-Json -Compress }\n"
        + "catch { @{status='rejected'} | ConvertTo-Json -Compress }\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-File", str(harness)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == (
        {"status": "ok", "path": str(candidates[0]), "scalar": True} if first_exists else {"status": "rejected"}
    )


def test_powershell_uses_same_resolved_interpreter_for_guard_and_helper():
    source = (probe.ROOT / "scripts/test_processing_package.ps1").read_text()
    assert "$PythonExecutable = Resolve-BoundProcessingPython" in source
    assert "$CheckRaw = & $PythonExecutable @CheckArguments" in source
    assert "$Info.FileName = $PythonExecutable" in source
    assert "(Get-Command python -CommandType Application).Source" not in source
