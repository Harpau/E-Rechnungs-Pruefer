"""Small native fault jobs; no production debug entry point or unsafe load."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from app.configuration import Settings
from app.processing import manager as controller
from app.processing import native
from app.processing.budgets import ProcessingBudgets, ProcessingError
from app.upload_ingress import ReceivedUpload, UploadOptions

pytestmark = pytest.mark.skipif(sys.platform not in {"darwin", "linux", "win32"}, reason="native processing platform")
HELPER = Path(__file__).with_name("processing_fault_helper.py")


def _fault_command(monkeypatch, mode):
    ordinary = native.role_command

    def command(role, arguments):
        if role == "worker" or (mode == "java_lingering_child" and role == "java"):
            return [native.python_executable(), "-I", str(HELPER), mode, native.ROLE_FLAG, role, *arguments]
        return ordinary(role, arguments)

    monkeypatch.setattr(native, "role_command", command)
    return ordinary


def _budgets(**changes):
    return replace(
        ProcessingBudgets.for_platform(),
        start_seconds=8,
        python_seconds=0.35,
        base_job_seconds=10,
        cleanup_seconds=2,
        **changes,
    )


def _upload():
    return ReceivedUpload("export_xml", "example.xml", "application/xml", UploadOptions(False), memoryview(b"<x/>"))


async def _real_export(owner):
    lease = owner.try_acquire()
    assert lease is not None
    upload = _upload()
    try:
        result = await asyncio.wait_for(lease.run(upload, Settings(kosit_enabled=False)), timeout=12)
        assert b"".join(result.chunks) == b"<x/>"
        assert lease.tree.cleaned and not lease.poisoned
    finally:
        upload.close()
        lease.release()


@pytest.mark.parametrize(
    "mode,status,error_type",
    [
        ("cpu", 504, "processing_timeout_error"),
        ("memory", 422, "processing_limit_error"),
        ("oversized_output", 500, "processing_worker_error"),
        ("partial_ipc", 504, "processing_timeout_error"),
    ],
)
def test_native_fault_is_bounded_cleaned_and_followed_by_a_real_success(monkeypatch, mode, status, error_type):
    ordinary = _fault_command(monkeypatch, mode)
    overrides = {"python_memory_bytes": (128 if sys.platform == "win32" else 64) * 1024**2} if mode == "memory" else {}
    owner = controller.ProcessingManager(_budgets(**overrides))
    lease = owner.try_acquire()
    assert lease is not None
    upload = _upload()

    async def run():
        started = time.monotonic()
        try:
            with pytest.raises(ProcessingError) as caught:
                await asyncio.wait_for(lease.run(upload, Settings(kosit_enabled=False)), timeout=12)
            assert (caught.value.status, caught.value.error_type) == (status, error_type)
            assert lease.ready.is_set(), "failure must occur after actual native READY"
            assert lease.tree.cleaned and not lease.poisoned
            assert lease.thread is not None
            lease.thread.join(timeout=0.2)
            assert not lease.thread.is_alive()
            assert time.monotonic() - started < 8, "the helper's emergency stop is not the processing timeout"
        finally:
            upload.close()
            lease.release()
        assert owner.active_count == 0
        monkeypatch.setattr(native, "role_command", ordinary)
        # Use the normal platform profile for the follow-up, without resetting
        # the manager or clearing a potentially poisoned lease by hand.
        owner.budgets = _budgets()
        await _real_export(owner)
        assert owner.active_count == 0

    try:
        asyncio.run(run())
    finally:
        owner.shutdown()


def test_two_actual_native_workers_leave_health_responsive_and_third_upload_unqueued(monkeypatch):
    import httpx

    from app import main

    ordinary = _fault_command(monkeypatch, "idle")
    owner = controller.ProcessingManager(replace(_budgets(), python_seconds=2))
    monkeypatch.setattr(main, "manager", owner)
    monkeypatch.setattr(main, "settings", Settings(kosit_enabled=False))

    async def run():
        async with main.lifespan(main.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://testserver"
            ) as client:

                async def upload():
                    return await client.post("/api/xml", files={"file": ("example.xml", b"<x/>", "application/xml")})

                pending = [asyncio.create_task(upload()) for _ in range(2)]
                try:
                    deadline = time.monotonic() + 8
                    while True:
                        with owner.lock:
                            leases = tuple(owner.leases)
                        if len(leases) == 2 and all(lease.python_deadline is not None for lease in leases):
                            break
                        assert not any(task.done() for task in pending), (
                            "native job exited before simultaneous occupancy"
                        )
                        assert time.monotonic() < deadline, "two native jobs did not reach their operation phase"
                        await asyncio.sleep(0.01)
                    health = await asyncio.wait_for(client.get("/api/health"), timeout=1)
                    assert health.status_code == 200
                    overload = await asyncio.wait_for(upload(), timeout=1)
                    assert overload.status_code == 503 and overload.json()["type"] == "analysis_capacity_error"
                    assert overload.headers["retry-after"]
                    assert all(not task.done() for task in pending)
                    responses = await asyncio.wait_for(asyncio.gather(*pending), timeout=6)
                    assert all(response.status_code == 504 for response in responses)
                    assert all(lease.tree.cleaned and not lease.poisoned for lease in leases)
                    assert owner.active_count == 0
                    monkeypatch.setattr(native, "role_command", ordinary)
                    response = await asyncio.wait_for(upload(), timeout=12)
                    assert response.status_code == 200 and response.content == b"<x/>"
                finally:
                    for task in pending:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(run())
    assert owner.active_count == 0


@pytest.mark.skipif(sys.platform != "win32", reason="native inherited JVM-shaped Windows child")
def test_dead_java_launcher_cannot_leave_its_inherited_child_writing_during_report_collection(monkeypatch):
    from app.validators.kosit import KositValidator

    ordinary = _fault_command(monkeypatch, "java_lingering_child")
    monkeypatch.setattr(KositValidator, "configuration_state", lambda self: {"configured": True, "problems": []})
    owner = controller.ProcessingManager(replace(_budgets(), python_seconds=1))
    observed_children = []
    real_finish = native.Child.finish

    def recorded_finish(child, deadline):
        if child.job is not None:
            observed_children.extend(child.job.process_ids())
        return real_finish(child, deadline)

    monkeypatch.setattr(native.Child, "finish", recorded_finish)
    lease = owner.try_acquire()
    assert lease is not None
    upload = ReceivedUpload("analyze", "example.xml", "application/xml", UploadOptions(True), memoryview(b"<x/>"))

    async def run():
        try:
            result = await asyncio.wait_for(
                lease.run(upload, Settings(kosit_enabled=True, kosit_java_bin=sys.executable, kosit_timeout_seconds=2)),
                timeout=12,
            )
            assert observed_children, "the dead launcher must really leave an inherited process in its job"
            assert lease.tree.roles["java"]._finish_result is True
            proof = json.loads(b"".join(result.chunks))
            assert proof == {"executed": False, "accepted": None, "finding": "KOSIT-EXEC"}
            assert lease.tree.cleaned and not lease.poisoned
        finally:
            upload.close()
            lease.release()
        assert owner.active_count == 0
        monkeypatch.setattr(native, "role_command", ordinary)
        await _real_export(owner)

    try:
        asyncio.run(run())
    finally:
        owner.shutdown()
