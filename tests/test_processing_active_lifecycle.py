from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import processing_lifecycle_probe as probe


def test_fixed_platform_catalog_has_no_arbitrary_target() -> None:
    assert probe.cases_for_platform("linux") == ("cancel", "disconnect", "worker", "supervisor", "parent")
    assert probe.cases_for_platform("win32") == probe.cases_for_platform("linux")
    assert probe.cases_for_platform("darwin")[-1] == "watchdog"
    with pytest.raises(ValueError):
        probe.cases_for_platform("unsupported")


def test_atomic_evidence_does_not_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    probe.record(path, {"first": True})
    with pytest.raises(FileExistsError):
        probe.record(path, {"first": False})
    assert json.loads(path.read_text()) == {"first": True}


def test_cleanup_requires_bound_identity_and_confirmed_end(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    job = runtime / "einvoice-kosit-synthetic"
    job.mkdir(mode=0o700)
    (job / "invoice.xml").write_bytes(b"<synthetic/>")
    (job / "invoice.xml").chmod(0o600)
    identity = probe.directory_identity(job)
    with pytest.raises(RuntimeError, match="native end"):
        probe.cleanup_owned_temp(runtime, identity, ended=False)
    wrong = {**identity, "ino": identity["ino"] + 1}
    with pytest.raises(RuntimeError, match="identity"):
        probe.cleanup_owned_temp(runtime, wrong, ended=True)
    assert job.exists()
    result = probe.cleanup_owned_temp(runtime, identity, ended=True)
    assert result["present_after_native_end"] is True
    assert result["harness_removed"] is True
    assert not job.exists()


def test_cleanup_rejects_foreign_directory(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    foreign = tmp_path / "einvoice-kosit-foreign"
    foreign.mkdir(mode=0o700)
    with pytest.raises(RuntimeError, match="scope"):
        probe.cleanup_owned_temp(runtime, probe.directory_identity(foreign), ended=True)
    assert foreign.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symbolic-link fixture")
def test_cleanup_rejects_replaced_symlink(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    job = runtime / "einvoice-kosit-synthetic"
    job.mkdir(mode=0o700)
    identity = probe.directory_identity(job)
    job.rmdir()
    job.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RuntimeError):
        probe.cleanup_owned_temp(runtime, identity, ended=True)
    assert job.is_symlink()


def test_bounded_json_reader_rejects_excessive_marker(tmp_path: Path) -> None:
    path = tmp_path / "too-large.json"
    path.write_bytes(b" " * (probe.RECORD_LIMIT + 1))
    with pytest.raises(RuntimeError, match="record"):
        probe.read_record(path)


def test_multipart_is_fixed_synthetic_and_bounded() -> None:
    payload = b"<synthetic/>"
    body = probe.multipart(payload, official=True)
    assert b'name="official"' in body and b"true" in body
    assert body.count(payload) == 1
    assert b'name="official"' not in probe.multipart(payload, official=False)


def test_public_cli_requires_new_output(tmp_path: Path) -> None:
    output = tmp_path / "existing.json"
    output.write_text("historical")
    with pytest.raises(FileExistsError):
        probe.run_catalog(output)
    assert output.read_text() == "historical"


def test_late_exit_observation_never_counts_as_five_second_pass() -> None:
    assert probe.ended_in_budget(True, 10.0, 15.00001) is False
    assert probe.ended_in_budget(True, 10.0, 15.0) is True
    assert probe.ended_in_budget(False, 10.0, 11.0) is False


def test_only_parent_case_permits_retained_private_temporary_files() -> None:
    retained = {"present_after_native_end": True}
    assert probe.temporary_result_allowed("parent", retained)
    for case in ("cancel", "disconnect", "worker", "supervisor", "watchdog"):
        assert not probe.temporary_result_allowed(case, retained)
        assert probe.temporary_result_allowed(case, {"present_after_native_end": False})


def test_active_fault_recheck_rejects_ended_request_or_role() -> None:
    with pytest.raises(RuntimeError, match="no longer active"):
        probe.require_active(request_done=True, all_running=True)
    with pytest.raises(RuntimeError, match="no longer active"):
        probe.require_active(request_done=False, all_running=False)
    probe.require_active(request_done=False, all_running=True)


@pytest.mark.parametrize("failure", ["exception", "cancel", "timeout", "overflow", "duplicate-start"])
def test_request_preserves_bounded_partial_response(tmp_path: Path, failure: str) -> None:
    receipt = tmp_path / "response.json"

    async def application(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 503})
        await send({"type": "http.response.body", "body": b"synthetic error", "more_body": True})
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure == "timeout":
            await asyncio.Event().wait()
        if failure == "overflow":
            await send({"type": "http.response.body", "body": b"x" * (2 * 1024**2), "more_body": False})
        if failure == "duplicate-start":
            await send({"type": "http.response.start", "status": 200})
        raise RuntimeError("synthetic send error")

    async def exercise() -> None:
        request = probe.asgi_request(application, "/api/xml", b"<synthetic/>", asyncio.Event(), evidence=receipt)
        if failure in {"cancel", "timeout"}:
            result = await asyncio.wait_for(request, 0.02)
            assert result["cancelled"] is True
        else:
            with pytest.raises(RuntimeError):
                await request

    asyncio.run(exercise())
    result = probe.read_record(receipt)
    assert result["status"] == 503
    assert result["complete"] is False
    assert result["failure_class"] in {"RuntimeError", "CancelledError"}
    assert result["body_size"] >= len(b"synthetic error")
    assert result["hashed_body_size"] <= 2 * 1024**2
    assert len(bytes.fromhex(result["body_prefix_hex"])) <= 512
    assert result["body_prefix_hex"].startswith(b"synthetic error".hex())
    assert result["sha256_complete"] is (failure != "overflow")
    if failure != "overflow":
        assert result["sha256"] == hashlib.sha256(b"synthetic error").hexdigest()


def test_request_distinguishes_missing_final_body(tmp_path: Path) -> None:
    async def application(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"<synthetic/>", "more_body": True})

    receipt = tmp_path / "response.json"
    result = asyncio.run(
        probe.asgi_request(application, "/api/xml", b"<synthetic/>", asyncio.Event(), evidence=receipt)
    )
    assert result["status"] == 200
    assert result["sha256"] == hashlib.sha256(b"<synthetic/>").hexdigest()
    assert result["complete"] is False
    assert probe.read_record(receipt) == result


def synthetic_controller(
    monkeypatch: pytest.MonkeyPatch, directory: Path, *, fresh_failure: str, shutdown_failure: bool = False
) -> tuple[Any, list[str]]:
    from app import main
    from app.processing import manager as manager_module
    from app.processing import native

    calls: list[str] = []
    tree = SimpleNamespace(
        cleaned=True,
        supervisor=SimpleNamespace(pid=100),
        roles={"worker": SimpleNamespace(pid=101), "java": SimpleNamespace(pid=102)},
        watchdog=None,
    )
    lease = SimpleNamespace(
        ready=SimpleNamespace(is_set=lambda: True), ready_manifest={}, python_deadline=None, tree=tree, poisoned=False
    )

    def shutdown() -> None:
        calls.append("shutdown")
        if shutdown_failure:
            raise OSError("synthetic shutdown failure")

    manager = SimpleNamespace(lock=threading.Lock(), leases=[lease], active_count=0, shutdown=shutdown)

    async def application(scope: Any, receive: Any, send: Any) -> None:
        calls.append(scope["path"])
        if scope["path"] == "/api/analyze":
            probe.record(directory / "java-active.json", {"pid": 102})
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await send({"type": "http.response.start", "status": 499})
                await send({"type": "http.response.body", "body": b"synthetic cancelled", "more_body": False})
                if fresh_failure == "cancel-receipt-error":
                    raise
            return
        await send({"type": "http.response.start", "status": 503 if fresh_failure == "status" else 200})
        body = b"wrong synthetic bytes" if fresh_failure == "hash" else b"<synthetic-byte-exact-export/>\n"
        await send({"type": "http.response.body", "body": body, "more_body": fresh_failure == "incomplete"})
        if fresh_failure == "exception":
            raise RuntimeError("synthetic fresh exception")

    monkeypatch.setattr(main, "app", application)
    monkeypatch.setattr(main, "manager", main.manager)
    monkeypatch.setattr(main, "settings", main.settings)
    monkeypatch.setattr(native, "role_command", native.role_command)
    monkeypatch.setattr(probe.tempfile, "tempdir", probe.tempfile.tempdir)
    monkeypatch.setattr(manager_module, "ProcessingManager", lambda: manager)
    monkeypatch.setattr(probe, "BoundExits", lambda pids: SimpleNamespace(all_running=lambda: True, close=lambda: None))
    probe.record(directory / "go.json", {"bound": True})
    return manager, calls


@pytest.mark.parametrize("fresh_failure", ["status", "hash", "incomplete", "exception"])
@pytest.mark.parametrize("shutdown_failure", [False, True])
def test_controller_preserves_primary_failure_and_both_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_failure: str, shutdown_failure: bool
) -> None:
    _, calls = synthetic_controller(
        monkeypatch, tmp_path, fresh_failure=fresh_failure, shutdown_failure=shutdown_failure
    )
    result = asyncio.run(probe.controller("cancel", tmp_path))
    assert result["passed"] is False
    assert result["failure_class"] == "RuntimeError"
    assert result["failure_phase"] == "fresh_xml"
    assert "fresh" in result["failure"]
    assert result["response"]["status"] == 499
    assert result["fresh_xml"]["status"] == (503 if fresh_failure == "status" else 200)
    assert result["fresh_xml"]["body_size"] > 0
    assert result["fresh_xml"] == probe.read_record(tmp_path / "fresh-response.json")
    assert result["response"] == probe.read_record(tmp_path / "interrupted-response.json")
    assert result["before_shutdown"] == {"active_count": 0, "tree_cleaned": True, "poisoned": False}
    assert bool(result.get("finalization_errors")) is shutdown_failure
    if shutdown_failure:
        assert result["finalization_errors"][0]["phase"] == "manager_shutdown"
        assert result["finalization_errors"][0]["failure_class"] == "OSError"
    assert calls == ["/api/analyze", "/api/xml", "shutdown"]


def test_controller_success_requires_complete_fresh_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, calls = synthetic_controller(monkeypatch, tmp_path, fresh_failure="none")
    result = asyncio.run(probe.controller("cancel", tmp_path))
    assert result["passed"] is True
    assert result["fresh_xml"]["complete"] is True
    assert result["after_shutdown"]["active_count"] == 0
    assert calls == ["/api/analyze", "/api/xml", "shutdown"]


def test_fault_binding_failure_survives_observer_close_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, calls = synthetic_controller(monkeypatch, tmp_path, fresh_failure="none")

    def close() -> None:
        raise OSError("synthetic observer close failure")

    monkeypatch.setattr(probe, "BoundExits", lambda pids: SimpleNamespace(all_running=lambda: False, close=close))
    result = asyncio.run(probe.controller("cancel", tmp_path))
    assert result["passed"] is False
    assert result["failure_class"] == "RuntimeError"
    assert result["failure"] == "fault target is no longer active"
    assert result["failure_phase"] == "fault_binding"
    assert result["finalization_errors"][0]["phase"] == "fault_observer_close"
    assert calls == ["/api/analyze", "shutdown"]


def test_interrupted_receipt_failure_blocks_fresh_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, calls = synthetic_controller(monkeypatch, tmp_path, fresh_failure="cancel-receipt-error")
    original = probe.record

    def write(path: Path, value: object) -> None:
        if path.name == "interrupted-response.json":
            raise OSError("synthetic receipt publication failure")
        original(path, value)

    monkeypatch.setattr(probe, "record", write)
    result = asyncio.run(probe.controller("cancel", tmp_path))
    assert result["passed"] is False
    assert result["failure_phase"] == "interrupted_response"
    assert result["response"]["evidence_error"]["failure_class"] == "OSError"
    assert calls == ["/api/analyze", "shutdown"]


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt, SystemExit])
def test_fresh_exception_and_receipt_failure_keep_response_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[BaseException]
) -> None:
    _, calls = synthetic_controller(monkeypatch, tmp_path, fresh_failure="exception")
    original = probe.record

    def write(path: Path, value: object) -> None:
        if path.name == "fresh-response.json":
            raise error("synthetic receipt publication failure")
        original(path, value)

    monkeypatch.setattr(probe, "record", write)
    result = asyncio.run(probe.controller("cancel", tmp_path))
    assert result["passed"] is False
    assert result["failure"] == "synthetic fresh exception"
    assert result["fresh_xml"]["status"] == 200
    assert result["fresh_xml"]["body_size"] == len(b"<synthetic-byte-exact-export/>\n")
    assert result["fresh_xml"]["sha256"] == hashlib.sha256(b"<synthetic-byte-exact-export/>\n").hexdigest()
    assert result["fresh_xml"]["evidence_error"]["failure_class"] == error.__name__
    assert result["fresh_xml"]["failure_class"] == "RuntimeError"
    assert calls == ["/api/analyze", "/api/xml", "shutdown"]


def test_catalog_preserves_partial_results_and_stops_on_first_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def run(case: str, directory: Path) -> dict[str, Any]:
        calls.append(case)
        return {"case": case, "passed": False, "failure": "synthetic unresolved failure"}

    monkeypatch.setattr(probe, "run_case", run)
    output = tmp_path / "catalog.json"
    result = probe.run_catalog(output)
    assert calls == ["cancel"]
    assert result["passed"] is False
    assert result["not_started"] == list(probe.cases_for_platform(probe.sys.platform)[1:])
    assert probe.read_record(output) == result


@pytest.mark.parametrize("publication_failure", [False, True])
def test_outer_finalization_preserves_primary_error_and_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publication_failure: bool
) -> None:
    calls: list[str] = []

    def kill() -> None:
        calls.append("kill")
        raise OSError("synthetic kill observation error")

    def wait(*, timeout: float) -> None:
        calls.append("wait")
        raise TimeoutError("synthetic own controller still running")

    process = SimpleNamespace(pid=99, poll=lambda: None, kill=kill, wait=wait)
    roles = {name: 100 + index for index, name in enumerate(("supervisor", "worker", "java"))}
    if probe.sys.platform == "darwin":
        roles["watchdog"] = 103
    java = {"pid": roles["java"], "parent_pid": 99, "go_observed": True, "temporary": {}}
    if probe.sys.platform == "win32":
        java.update(pid=104, parent_pid=roles["java"])
    active = {"controller_pid": 99, "input_acknowledged": True, "roles": roles, "java": java}
    now = probe.time.monotonic()
    result = {"passed": False, "failure": "synthetic primary failure", "fresh_xml": {"status": 503}}
    markers = {"active.json": active, "fault.json": {"monotonic": now}, "result.json": result}
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(probe, "wait_record", lambda path, process, deadline: markers[path.name])

    class Binding:
        identities = [{"pid": pid} for pid in roles.values()]
        observations = 0

        def all_running(self) -> bool:
            return True

        def wait(self, deadline: float) -> bool:
            self.observations += 1
            if self.observations > 1:
                raise OSError("synthetic exit observation error")
            return True

        def close(self) -> None:
            raise OSError("synthetic observer close error")

    def no_cleanup(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("unconfirmed controller end must not reach temporary cleanup")

    monkeypatch.setattr(probe, "BoundExits", lambda pids: Binding())
    monkeypatch.setattr(probe, "cleanup_owned_temp", no_cleanup)
    original = probe.record

    def write(path: Path, value: object) -> None:
        if publication_failure and path.name == "outer-result.json":
            raise OSError("synthetic outer receipt publication failure")
        original(path, value)

    monkeypatch.setattr(probe, "record", write)
    directory = tmp_path / "cancel"
    outer = probe.run_case("cancel", directory)
    assert outer["passed"] is False
    assert outer["failure"] == "controller fault result failed"
    assert outer["controller_result"] == result
    assert outer["controller_end_confirmed"] is False
    expected = {"controller_stop", "controller_wait", "bound_exit_observation", "bound_observer_close"}
    if publication_failure:
        expected.add("outer_result_publication")
    assert {item["phase"] for item in outer["finalization_errors"]} == expected
    assert calls == ["kill", "wait"]
    if publication_failure:
        assert not (directory / "outer-result.json").exists()
    else:
        assert probe.read_record(directory / "outer-result.json") == outer


def test_retained_temporary_inventory_can_be_read_without_removal(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    job = runtime / "einvoice-kosit-synthetic"
    job.mkdir(mode=0o700)
    child = job / "synthetic.txt"
    child.write_text("synthetic")
    child.chmod(0o600)
    result = probe.cleanup_owned_temp(runtime, probe.directory_identity(job), ended=True, remove=False)
    assert result["present_after_native_end"] is True
    assert result["harness_removed"] is False
    assert child.read_text() == "synthetic"


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt, SystemExit])
def test_catalog_retains_prior_cases_when_case_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[BaseException]
) -> None:
    calls: list[str] = []

    def run(case: str, directory: Path) -> dict[str, Any]:
        calls.append(case)
        if case == "disconnect":
            raise error("synthetic evidence publication failure")
        return {"case": case, "passed": True}

    monkeypatch.setattr(probe, "run_case", run)
    output = tmp_path / "catalog.json"
    result = probe.run_catalog(output)
    assert result["passed"] is False
    assert calls == ["cancel", "disconnect"]
    assert result["cases"][0]["passed"] is True
    assert result["cases"][1]["failure_class"] == error.__name__
    assert result["not_started"] == list(probe.cases_for_platform(probe.sys.platform)[2:])
    assert probe.read_record(output) == result
