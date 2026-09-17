"""Bounded regression tests for controller startup and cancellation races."""

from __future__ import annotations

import asyncio
import io
import json
import select
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.configuration import Settings
from app.processing import manager as controller
from app.processing import native
from app.processing.budgets import ProcessingError
from app.upload_ingress import ReceivedUpload, UploadOptions


@pytest.mark.parametrize("failed_end", ["input_read", "output_write"])
def test_prepared_role_child_close_attempts_both_once_and_preserves_failure(monkeypatch, failed_end):
    prepared = native.PreparedRole()
    original_close = native.os.close
    calls = []

    def close_then_fail(fd):
        calls.append(fd)
        original_close(fd)
        if fd == getattr(prepared, failed_end):
            raise OSError("synthetic failure after FD was already released")

    monkeypatch.setattr(native.os, "close", close_then_fail)
    try:
        with pytest.raises(OSError):
            prepared.close_child()
        assert sorted(calls) == sorted([prepared.input_read, prepared.output_write])
        with pytest.raises(OSError):
            prepared.close()
        assert prepared.incoming.closed and prepared.outgoing.closed
        with pytest.raises(OSError):
            prepared.close()
        assert len(calls) == 2, "released numeric descriptors may now belong to another thread"
    finally:
        prepared.incoming.close()
        prepared.outgoing.close()
        # Old broken implementations never attempted the second child end.
        for fd in (prepared.input_read, prepared.output_write):
            if fd not in calls:
                original_close(fd)


def test_prepared_role_stream_close_attempts_every_owner_once_and_retains_error(monkeypatch):
    prepared = native.PreparedRole()
    prepared.close_child()
    incoming, outgoing = prepared.incoming, prepared.outgoing
    calls = []

    def broken_close():
        calls.append("incoming")
        incoming.close()
        raise OSError("synthetic stream close failure")

    def other_close():
        calls.append("outgoing")
        outgoing.close()

    monkeypatch.setattr(prepared, "incoming", SimpleNamespace(close=broken_close))
    monkeypatch.setattr(prepared, "outgoing", SimpleNamespace(close=other_close))
    try:
        with pytest.raises(OSError):
            prepared.close()
        assert calls == ["incoming", "outgoing"]
        with pytest.raises(OSError):
            prepared.close()
        assert calls == ["incoming", "outgoing"]
    finally:
        incoming.close()
        outgoing.close()


def test_console_close_failure_cannot_reclose_consumed_fd_and_still_reaps_tree(monkeypatch):
    from app.validators.kosit import KositValidator

    owner = controller.ProcessingManager()
    lease = owner.try_acquire()
    assert lease is not None
    tree = Mock()
    tree.cancelled.is_set.return_value = False
    lease.tree = tree
    context = Mock(__enter__=Mock(return_value="/synthetic/kosit"), __exit__=Mock())
    monkeypatch.setattr(controller.sys, "platform", "linux")
    monkeypatch.setattr(KositValidator, "configuration_state", lambda self: {"configured": True})
    monkeypatch.setattr("app.validators.kosit._kosit_temporary_directory", lambda: context)
    monkeypatch.setattr(controller.shutil, "which", lambda value: "/synthetic/java")
    prepared = Mock()
    prepared.broker_descriptors.return_value = (201, 202)
    monkeypatch.setattr(controller, "PreparedRole", Mock(return_value=prepared))
    monkeypatch.setattr(controller, "spawn_role", Mock(return_value=Mock(pid=99)))
    monkeypatch.setattr(controller.os, "pipe", Mock(side_effect=[(101, 102), (103, 104)]))
    calls = []

    def close(fd):
        calls.append(fd)
        if fd == 102:
            raise OSError("synthetic close failure")

    monkeypatch.setattr(controller.os, "close", close)
    cleanup = Mock()
    monkeypatch.setattr(lease, "_cleanup", cleanup)
    upload = ReceivedUpload("analyze", "example.xml", "application/xml", UploadOptions(True), memoryview(b"<x/>"))
    try:
        with pytest.raises(ProcessingError) as result:
            lease._execute(upload, Settings(kosit_enabled=True), deadline=time.monotonic() + 10)
        assert result.value.status == 503
        assert sorted(calls) == [101, 102, 103, 104]
        assert prepared.close.call_count == 2
        cleanup.assert_called_once_with(context)
        assert lease.poisoned
        lease.release()
        assert owner.active_count == 1
    finally:
        upload.close()


@pytest.mark.parametrize("spawn_failed", [False, True])
def test_unprepared_spawn_consumes_failed_close_and_attempts_every_remaining_fd(monkeypatch, spawn_failed):
    monkeypatch.setattr(native.sys, "platform", "linux")
    monkeypatch.setattr(native.os, "pipe", Mock(side_effect=[(101, 102), (103, 104)]))
    process = Mock()
    spawn = Mock(side_effect=OSError("synthetic spawn failure")) if spawn_failed else Mock(return_value=process)
    monkeypatch.setattr(native.subprocess, "Popen", spawn)
    calls = []

    def close(fd):
        calls.append(fd)
        if fd == 101:
            raise OSError("synthetic failure after the descriptor was released")

    monkeypatch.setattr(native.os, "close", close)
    with pytest.raises(OSError):
        native.spawn_role("supervisor", group=0)
    assert sorted(calls) == [101, 102, 103, 104], "each owned numeric FD must be consumed exactly once"
    if not spawn_failed:
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)


@pytest.mark.parametrize("wrapped_first", [False, True])
def test_prepared_constructor_cleanup_attempts_all_fds_and_wrapped_stream_after_error(monkeypatch, wrapped_first):
    monkeypatch.setattr(native.os, "pipe", Mock(side_effect=[(101, 102), (103, 104)]))
    incoming = Mock()
    wrapping = [incoming, OSError("synthetic fdopen failure")] if wrapped_first else OSError("synthetic fdopen failure")
    monkeypatch.setattr(native.os, "fdopen", Mock(side_effect=wrapping))
    calls = []

    def close(fd):
        calls.append(fd)
        if fd == 101:
            raise OSError("synthetic failed close")

    monkeypatch.setattr(native.os, "close", close)
    with pytest.raises(OSError, match="synthetic fdopen failure"):
        native.PreparedRole()
    assert sorted(calls) == ([101, 102, 104] if wrapped_first else [101, 102, 103, 104])
    if wrapped_first:
        incoming.close.assert_called_once_with()


def test_child_stream_close_failure_persists_and_still_attempts_native_handle_cleanup():
    incoming, outgoing = Mock(), Mock()
    incoming.close.side_effect = OSError("synthetic stream close failure")
    process, job = Mock(pid=501), Mock()
    job.active_process_count.return_value = 0
    child = native.Child(process, incoming, outgoing, job=job)
    assert child.finish(time.monotonic() + 1) is False
    assert child.close_channels() is False
    assert child.finish(time.monotonic() + 1) is False
    incoming.close.assert_called_once_with()
    outgoing.close.assert_called_once_with()
    process.wait.assert_called_once()
    job.close.assert_called_once_with()
    process.close.assert_called_once_with()


def test_tree_cleanup_preserves_channel_failure_and_attempts_all_remaining_handles(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "win32")
    children = []
    for pid in (501, 502):
        job = Mock()
        job.active_process_count.return_value = 0
        children.append(native.Child(Mock(pid=pid), Mock(), Mock(), job=job))
    children[0].incoming.close.side_effect = OSError("synthetic stream close failure")
    tree = native.ProcessTree()
    tree.roles = {"worker": children[0]}
    tree.supervisor = children[1]
    tree.outer_job = Mock()
    tree.outer_job.active_process_count.return_value = 0
    assert tree.cleanup(1) is False
    assert tree.cleaned is False
    for child in children:
        child.incoming.close.assert_called_once_with()
        child.outgoing.close.assert_called_once_with()
        child.process.wait.assert_called_once()
        child.process.close.assert_called_once_with()
        child.job.close.assert_called_once_with()
    tree.outer_job.close.assert_called_once_with()


def java_wait_fixture():
    owner = controller.ProcessingManager()
    lease = owner.try_acquire()
    assert lease is not None
    java, worker = Mock(), Mock()
    worker.process.poll.return_value = None
    lease.tree.supervisor = Mock()
    return lease, java, worker


def test_java_wait_stops_on_worker_death_without_reaping_group_leader():
    lease, java, worker = java_wait_fixture()

    def wait(*, timeout):
        assert 0 < timeout <= 0.05
        worker.process.poll.return_value = -9
        raise subprocess.TimeoutExpired("fixed-java", timeout)

    java.process.wait.side_effect = wait
    with pytest.raises(ProcessingError) as caught:
        lease._wait_for_java(java, worker, 120)
    assert (caught.value.status, caught.value.error_type) == (500, "processing_worker_error")
    java.process.wait.assert_called_once()
    lease.tree.supervisor.process.poll.assert_not_called()
    lease.tree.supervisor.process.wait.assert_not_called()


def test_java_wait_uses_one_fixed_monotonic_deadline_across_short_waits(monkeypatch):
    lease, java, worker = java_wait_fixture()
    clock = [100.0]
    waits = []
    monkeypatch.setattr(controller.time, "monotonic", lambda: clock[0])

    def wait(*, timeout):
        waits.append(timeout)
        clock[0] += timeout
        raise TimeoutError

    java.process.wait.side_effect = wait
    with pytest.raises(TimeoutError):
        lease._wait_for_java(java, worker, 0.12)
    assert len(waits) == 3 and max(waits) <= 0.05
    assert sum(waits) == pytest.approx(0.12)
    assert clock[0] == pytest.approx(100.12)


def test_java_success_cannot_hide_a_worker_that_died_during_the_final_wait():
    lease, java, worker = java_wait_fixture()

    def wait(*, timeout):
        worker.process.poll.return_value = -9
        return 0

    java.process.wait.side_effect = wait
    with pytest.raises(ProcessingError) as caught:
        lease._wait_for_java(java, worker, 120)
    assert caught.value.error_type == "processing_worker_error"


def test_java_wait_preserves_completed_java_exitcode_with_live_worker():
    lease, java, worker = java_wait_fixture()
    java.process.wait.return_value = 7
    assert lease._wait_for_java(java, worker, 120) == 7
    assert worker.process.poll.call_count == 2


def test_java_wait_observes_cancellation_before_another_native_wait():
    lease, java, worker = java_wait_fixture()

    def wait(*, timeout):
        lease.tree.cancelled.set()
        raise TimeoutError

    java.process.wait.side_effect = wait
    with pytest.raises(ProcessingError) as caught:
        lease._wait_for_java(java, worker, 120)
    assert caught.value.status == 503
    java.process.wait.assert_called_once()


def test_watchdog_installed_after_cancel_is_terminated_instead_of_surviving_last_group_kill(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "darwin")
    group_kill = Mock()
    monkeypatch.setattr(native.os, "killpg", group_kill, raising=False)
    monkeypatch.setattr(native, "ExitBindings", Mock())
    supervisor = SimpleNamespace(pid=12345, kill=Mock())
    watcher = SimpleNamespace(pid=12346, kill=Mock())
    tree = native.ProcessTree()
    tree.install(supervisor)
    tree.cancel()
    tree.install_watchdog(watcher)
    assert tree.watchdog is watcher
    watcher.kill.assert_called_once_with()
    assert group_kill.call_count == 1


def test_concurrent_shutdown_never_joins_an_unstarted_owner_thread(monkeypatch):
    real_thread = threading.Thread
    starting, start_allowed, stop_started, stop_done = (threading.Event() for _ in range(4))
    errors = []

    class DelayedStartThread:
        def __init__(self, **kwargs):
            self.delegate = real_thread(**kwargs)

        def start(self):
            starting.set()
            assert start_allowed.wait(2), "bounded startup synchronizer stalled"
            self.delegate.start()

        def join(self, timeout=None):
            self.delegate.join(timeout)

        def is_alive(self):
            return self.delegate.is_alive()

    owner = controller.ProcessingManager()
    lease = owner.try_acquire()
    assert lease is not None
    monkeypatch.setattr(
        lease, "_execute", lambda *args, **kwargs: controller.BufferedResult([], 0, "application/xml", {})
    )

    def shutdown():
        try:
            assert starting.wait(2)
            stop_started.set()
            owner.shutdown()
        except BaseException as error:
            errors.append(error)
        finally:
            stop_done.set()

    def unblock_start():
        try:
            assert stop_started.wait(2)
            # An uncoordinated shutdown fails here before the thread starts.
            # With proper coordination it waits for the ownership lock.
            stop_done.wait(0.05)
        finally:
            start_allowed.set()

    stopper = real_thread(target=shutdown)
    unblocker = real_thread(target=unblock_start)
    monkeypatch.setattr(controller.threading, "Thread", DelayedStartThread)
    stopper.start()
    unblocker.start()
    upload = ReceivedUpload("export_xml", "example.xml", "application/xml", UploadOptions(False), memoryview(b"<x/>"))

    async def run():
        try:
            await lease.run(upload, Settings())
        except asyncio.CancelledError:
            pass
        finally:
            upload.close()
            lease.release()

    try:
        asyncio.run(run())
    finally:
        start_allowed.set()
        stopper.join(2)
        unblocker.join(2)
    assert not stopper.is_alive() and not unblocker.is_alive()
    assert not errors
    assert not owner.accepting and owner.active_count == 0


def test_windows_outer_job_is_closed_if_supervisor_spawn_fails(monkeypatch):
    from app.processing import windows
    from app.validators.kosit import KositValidator

    monkeypatch.setattr(controller.sys, "platform", "win32")
    outer_job, supervisor_job = Mock(), Mock()
    for job in (outer_job, supervisor_job):
        job.active_process_count.return_value = 0
    monkeypatch.setattr(windows, "WindowsJob", Mock(side_effect=(outer_job, supervisor_job)))
    monkeypatch.setattr(KositValidator, "configuration_state", lambda self: {"configured": False})
    monkeypatch.setattr(controller, "spawn_role", Mock(side_effect=OSError("synthetic startup failure")))
    owner = controller.ProcessingManager()
    lease = owner.try_acquire()
    assert lease is not None
    upload = ReceivedUpload("export_xml", "example.xml", "application/xml", UploadOptions(False), memoryview(b"<x/>"))
    try:
        with pytest.raises(ProcessingError) as caught:
            lease._execute(upload, Settings(), deadline=time.monotonic() + 1)
        assert caught.value.status == 503
        outer_job.close.assert_called_once_with()
        supervisor_job.close.assert_called_once_with()
    finally:
        upload.close()
        lease.release()


def test_shutdown_attempts_every_lease_even_when_each_native_kill_fails(monkeypatch):
    owner = controller.ProcessingManager()
    leases = [owner.try_acquire(), owner.try_acquire()]
    calls = []

    def broken_cancel():
        calls.append("attempt")
        raise OSError("synthetic native cleanup failure")

    for lease in leases:
        assert lease is not None
        monkeypatch.setattr(lease, "cancel", broken_cancel)
    with pytest.raises(RuntimeError, match="Prozessabbruch"):
        owner.shutdown()
    assert calls == ["attempt", "attempt"]
    assert not owner.accepting and owner.active_count == 2
    assert all(lease is not None and lease.poisoned for lease in leases)


def test_cleanup_attempts_later_children_and_outer_job_after_one_native_query_fails(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "win32")
    tree = native.ProcessTree()
    tree.outer_job = Mock()
    tree.outer_job.active_process_count.return_value = 0
    children = []
    for pid in (501, 502, 503):
        job = Mock()
        job.active_process_count.return_value = 0
        child = native.Child(Mock(pid=pid), io.BytesIO(), io.BytesIO(), job=job)
        children.append(child)
    tree.roles = {"worker": children[0], "java": children[1]}
    tree.supervisor = children[2]
    children[0].job.active_process_count.side_effect = OSError("synthetic native query failure")
    try:
        assert tree.cleanup(0.1) is False
    except OSError:
        pass
    for child in children:
        child.process.wait.assert_called_once()
        child.job.close.assert_called_once()
        child.process.close.assert_called_once()
        assert child.incoming.closed and child.outgoing.closed
    tree.outer_job.close.assert_called_once()
    assert not tree.cleaned


def test_child_finish_keeps_wait_and_close_attempts_after_failed_termination():
    process, job = Mock(pid=501), Mock()
    job.terminate.side_effect = OSError("synthetic termination failure")
    job.active_process_count.return_value = 0
    child = native.Child(process, io.BytesIO(), io.BytesIO(), job=job)
    assert child.finish(time.monotonic() + 0.1) is False
    process.wait.assert_called_once()
    job.close.assert_called_once()
    process.close.assert_called_once()
    assert child.incoming.closed and child.outgoing.closed


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="native POSIX child-reaping proof")
@pytest.mark.parametrize("failure", ["cancel", "supervisor", "worker", "watchdog"])
def test_native_failure_after_ready_reaps_every_owned_child_and_releases_slot(monkeypatch, failure):
    if failure == "watchdog" and sys.platform != "darwin":
        pytest.skip("independent macOS watchdog role")
    owner = controller.ProcessingManager()
    lease = owner.try_acquire()
    assert lease is not None
    upload = ReceivedUpload("export_xml", "example.xml", "application/xml", UploadOptions(False), memoryview(b"<x/>"))
    real_write_payload = controller.write_payload
    injected = False
    children = []

    def interrupt_before_first_payload(stream, payload):
        nonlocal injected
        assert lease.ready.is_set() and not injected
        assert lease.tree.supervisor is not None
        children.extend([*lease.tree.roles.values(), lease.tree.supervisor])
        if lease.tree.watchdog is not None:
            children.append(lease.tree.watchdog)
        assert all(child.process.poll() is None for child in children)
        injected = True
        if failure == "cancel":
            lease.tree.cancel()
        else:
            child = {
                "supervisor": lease.tree.supervisor,
                "worker": lease.tree.roles["worker"],
                "watchdog": lease.tree.watchdog,
            }[failure]
            assert child is not None
            # Kill only a process handle created by this exact lease. Do not
            # reap the supervisor before the controller's last group signal.
            child.process.kill()
            if failure == "watchdog":
                # SIGKILL delivery is asynchronous. Confirm the guard really
                # exited before input; this does not reap the group leader.
                child.process.wait(timeout=1)
        real_write_payload(stream, payload)

    monkeypatch.setattr(controller, "write_payload", interrupt_before_first_payload)

    async def run():
        with pytest.raises(ProcessingError):
            await asyncio.wait_for(lease.run(upload, Settings()), timeout=20)

    try:
        asyncio.run(run())
        assert injected
        assert lease.tree.cleaned and not lease.poisoned
        assert children and all(child.process.returncode is not None for child in children)
        assert all(child.incoming.closed and child.outgoing.closed for child in children)
    finally:
        lease.tree.cancel()
        if lease.thread is not None:
            lease.thread.join(timeout=5)
        upload.close()
        lease.release()
    assert lease.thread is not None and not lease.thread.is_alive()
    assert owner.active_count == 0


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="native POSIX parent-death binding")
def test_native_controller_death_ends_all_previously_bound_roles_before_any_invoice_input():
    harness = r"""
import asyncio, json, threading
from app.configuration import Settings
from app.processing import manager as controller
from app.processing.budgets import ProcessingBudgets
from app.upload_ingress import ReceivedUpload, UploadOptions
owner = controller.ProcessingManager(ProcessingBudgets(
    start_seconds=4, python_seconds=2, base_job_seconds=8, cleanup_seconds=2))
lease = owner.try_acquire()
upload = ReceivedUpload('export_xml', 'example.xml', 'application/xml',
                        UploadOptions(False), memoryview(b'<x/>'))
def pause_before_invoice(_stream, _payload):
    assert lease.ready.is_set()
    children = [*lease.tree.roles.values(), lease.tree.supervisor]
    if lease.tree.watchdog is not None:
        children.append(lease.tree.watchdog)
    print(json.dumps({'pids': [child.pid for child in children]}), flush=True)
    threading.Event().wait(10)
    raise RuntimeError('bounded synthetic parent-death helper expired')
controller.write_payload = pause_before_invoice
try:
    asyncio.run(lease.run(upload, Settings()))
finally:
    upload.close()
    lease.release()
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", harness],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=native.child_environment(),
    )
    binding = None
    try:
        assert parent.stdout is not None and select.select([parent.stdout], [], [], 8)[0], "missing native READY"
        raw = parent.stdout.readline(4096)
        assert raw, "controller exited before all role bindings were ready"
        record = json.loads(raw)
        pids = record["pids"]
        assert len(pids) == (3 if sys.platform == "darwin" else 2)
        assert len(set(pids)) == len(pids) and all(type(pid) is int and pid > 1 for pid in pids)
        # Bind kernel identities before ending our exact controller handle.
        # No later signal is sent to an unbound numeric descendant PID.
        binding = native.ExitBindings(pids)
        parent.kill()
        parent.wait(timeout=3)
        assert binding.wait(time.monotonic() + 8), "one or more bound roles survived controller death"
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=3)
        if binding is not None:
            binding.close()
        if parent.stdout is not None:
            parent.stdout.close()
        if parent.stderr is not None:
            parent.stderr.close()
