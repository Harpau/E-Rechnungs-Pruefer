from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

from app.configuration import Settings
from app.processing.budgets import ProcessingBudgets, ProcessingError
from app.processing.manager import ProcessingManager
from app.upload_ingress import ReceivedUpload, UploadOptions


@pytest.mark.skipif(sys.platform not in {"darwin", "linux", "win32"}, reason="native processing platform")
def test_native_xml_export_preserves_original_bytes_and_releases_slot():
    payload = b'<?xml version="1.0"?>\n<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>\n'
    manager = ProcessingManager()
    lease = manager.try_acquire()
    assert lease is not None
    upload = ReceivedUpload("export_xml", "example.xml", "application/xml", UploadOptions(False), memoryview(payload))
    try:
        result = asyncio.run(lease.run(upload, Settings()))
        assert b"".join(result.chunks) == payload
        assert result.media_type == "application/xml"
    finally:
        upload.close()
        lease.release()
    assert manager.active_count == 0


def test_two_leases_cover_upload_and_response_until_explicit_release():
    manager = ProcessingManager()
    first, second = manager.try_acquire(), manager.try_acquire()
    assert first is not None and second is not None
    assert manager.try_acquire() is None
    first.release()
    first.release()
    replacement = manager.try_acquire()
    assert replacement is not None
    assert manager.try_acquire() is None
    second.release()
    replacement.release()
    assert manager.active_count == 0


def test_release_requires_native_cleanup_proof_even_if_owner_thread_already_ended():
    manager = ProcessingManager()
    lease = manager.try_acquire()
    assert lease is not None
    # A completed future alone is not evidence that its native resources ended.
    lease.tree.outer_job = object()
    lease.release()
    assert lease.poisoned and not lease.released and manager.active_count == 1


def test_shutdown_closes_admission():
    manager = ProcessingManager()
    manager.shutdown()
    assert manager.try_acquire() is None
    manager.startup()
    lease = manager.try_acquire()
    assert lease is not None
    lease.release()


def test_child_environment_drops_tokens_and_python_path(monkeypatch):
    from app.processing.native import child_environment

    monkeypatch.setenv("EINVOICE_API_TOKEN", "secret")
    monkeypatch.setenv("EINVOICE_DESKTOP_TOKEN", "secret")
    monkeypatch.setenv("PYTHONPATH", str(Path("/untrusted")))
    environment = child_environment()
    assert not any("TOKEN" in key or key == "PYTHONPATH" for key in environment)


def test_slow_temporary_cleanup_has_one_deadline_and_retains_capacity(monkeypatch):
    from app.processing.manager import BufferedResult

    manager = ProcessingManager(ProcessingBudgets(cleanup_seconds=0.05))
    lease = manager.try_acquire()
    assert lease is not None
    started, finish = threading.Event(), threading.Event()

    class SlowContext:
        def __exit__(self, *args):
            started.set()
            assert finish.wait(1), "bounded cleanup helper expired"

    context = SlowContext()
    monkeypatch.setattr(lease.tree, "cleanup", lambda timeout: True)

    def execute(*args, **kwargs):
        lease.ready.set()
        lease._cleanup(context)
        return BufferedResult([], 0, "application/xml", {})

    monkeypatch.setattr(lease, "_execute", execute)
    upload = ReceivedUpload("export_xml", "example.xml", "application/xml", UploadOptions(False), memoryview(b"<x/>"))
    before = time.monotonic()
    try:
        with pytest.raises(ProcessingError) as result:
            asyncio.run(lease.run(upload, Settings()))
        assert result.value.status == 503
        assert started.is_set() and time.monotonic() - before < 0.5
        assert lease.poisoned and lease._retained_context is context
        lease.release()
        assert manager.active_count == 1
    finally:
        finish.set()
        if lease.thread is not None:
            lease.thread.join(2)
        upload.close()
    assert lease.thread is not None and not lease.thread.is_alive()


@pytest.mark.skipif(sys.platform not in {"darwin", "linux", "win32"}, reason="native processing platform")
def test_pipe_finalization_failure_still_reaps_every_role_and_retains_the_slot(monkeypatch):
    from app.processing.native import PreparedRole

    close = PreparedRole.close

    def close_then_fail(prepared):
        close(prepared)
        raise OSError("synthetic descriptor finalization error")

    monkeypatch.setattr(PreparedRole, "close", close_then_fail)
    manager = ProcessingManager()
    lease = manager.try_acquire()
    assert lease is not None
    upload = ReceivedUpload("export_xml", "example.xml", "application/xml", UploadOptions(False), memoryview(b"<x/>"))
    try:
        with pytest.raises(ProcessingError) as error:
            asyncio.run(lease.run(upload, Settings()))
        assert error.value.status == 503
        assert lease.tree.cleaned and lease.poisoned
        assert lease.thread is not None
        lease.thread.join(1)
        assert not lease.thread.is_alive()
        assert lease.tree.supervisor is not None and lease.tree.supervisor.process.returncode is not None
        assert all(child.process.returncode is not None for child in lease.tree.roles.values())
    finally:
        upload.close()
        lease.release()
        manager.shutdown()
    assert manager.active_count == 1
