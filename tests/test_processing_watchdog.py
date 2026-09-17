from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.processing import watchdog

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX watchdog binding tests")


@pytest.fixture
def native_fakes(monkeypatch):
    kills = []
    ready = []
    clock = [10.0]
    read_fd, write_fd = os.pipe()
    ready_read, ready_write = os.pipe()
    spec = dict(
        parent_pid=100,
        parent_pgid=90,
        supervisor_pid=200,
        session_id=90,
        liveness_fd=read_fd,
        ready_fd=ready_write,
        deadline=11.0,
    )
    native_os = SimpleNamespace(
        getpid=lambda: 201,
        getpgrp=lambda: 200,
        getppid=lambda: 100,
        getsid=lambda _pid: 90,
        getpgid=lambda pid: 90 if pid == 100 else 200,
        killpg=lambda group, sig: kills.append((group, sig)),
        read=os.read,
        set_blocking=os.set_blocking,
        fstat=os.fstat,
        O_ACCMODE=os.O_ACCMODE,
        O_RDONLY=os.O_RDONLY,
        O_WRONLY=os.O_WRONLY,
    )

    class Queue:
        events = []

        def control(self, _changes, _count, _timeout):
            clock[0] += 0.2
            events, self.events = self.events, []
            return events

        def close(self):
            pass

    queue = Queue()
    monkeypatch.setattr(watchdog, "os", native_os)
    monkeypatch.setattr(watchdog, "monotonic", lambda: clock[0])
    monkeypatch.setattr(watchdog, "_register", lambda *args: queue)
    monkeypatch.setattr(watchdog, "_send_ready", lambda fd, record: ready.append(record))
    monkeypatch.setattr(watchdog, "_is_process_exit", lambda event: event == "exit")
    monkeypatch.setattr(watchdog.sys, "platform", "darwin")
    try:
        yield spec, native_os, queue, kills, ready, clock, write_fd
    finally:
        for fd in (read_fd, write_fd, ready_read, ready_write):
            os.close(fd)


@pytest.mark.parametrize(
    "change",
    [
        {"parent_pid": True},
        {"supervisor_pid": 90},
        {"session_id": 91},
        {"deadline": float("nan")},
        {"deadline": 1000.0},
    ],
)
def test_invalid_binding_never_kills_a_process_group(native_fakes, change):
    spec, _native, _queue, kills, ready, _clock, _writer = native_fakes
    with pytest.raises(watchdog.WatchdogError):
        watchdog.run_watchdog(**(spec | change))
    assert kills == [] and ready == []


def test_wrong_actual_group_never_kills(native_fakes):
    spec, native, _queue, kills, ready, _clock, _writer = native_fakes
    native.getpgrp = lambda: 90
    with pytest.raises(watchdog.WatchdogError):
        watchdog.run_watchdog(**spec)
    assert kills == [] and ready == []


def test_parent_dies_during_registration_no_ready_and_bound_group_is_killed(native_fakes, monkeypatch):
    spec, native, queue, kills, ready, _clock, _writer = native_fakes

    def register(*args):
        native.getppid = lambda: 1
        return queue

    monkeypatch.setattr(watchdog, "_register", register)
    with pytest.raises(watchdog.WatchdogError):
        watchdog.run_watchdog(**spec)
    assert ready == [] and kills == [(200, signal.SIGKILL)]


def test_registration_failure_is_closed_after_verified_group(native_fakes, monkeypatch):
    spec, _native, _queue, kills, ready, _clock, _writer = native_fakes
    monkeypatch.setattr(watchdog, "_register", lambda *args: (_ for _ in ()).throw(OSError("synthetic")))
    with pytest.raises(watchdog.WatchdogError):
        watchdog.run_watchdog(**spec)
    assert ready == [] and kills == [(200, signal.SIGKILL)]


def test_absolute_deadline_kills_the_bound_group(native_fakes):
    spec, _native, _queue, kills, ready, _clock, _writer = native_fakes
    with pytest.raises(watchdog.WatchdogError):
        watchdog.run_watchdog(**spec)
    assert len(ready) == 1 and kills == [(200, signal.SIGKILL)]
    assert ready[0]["supervisor_pid"] == 200


def test_eof_before_ready_kills_group_without_claiming_ready(native_fakes):
    spec, native, _queue, kills, ready, _clock, _writer = native_fakes
    native.read = lambda *_args: b""
    with pytest.raises(watchdog.WatchdogError):
        watchdog.run_watchdog(**spec)
    assert ready == [] and kills == [(200, signal.SIGKILL)]


def test_unexpected_liveness_data_is_not_a_heartbeat(native_fakes):
    spec, native, _queue, kills, ready, _clock, _writer = native_fakes
    native.read = lambda *_args: b"x"
    with pytest.raises(watchdog.WatchdogError):
        watchdog.run_watchdog(**spec)
    assert ready == [] and kills == [(200, signal.SIGKILL)]


def test_pending_process_exit_prevents_ready(native_fakes):
    spec, _native, queue, kills, ready, _clock, _writer = native_fakes
    queue.events = ["exit"]
    with pytest.raises(watchdog.WatchdogError):
        watchdog.run_watchdog(**spec)
    assert ready == [] and kills == [(200, signal.SIGKILL)]


def test_group_change_at_kill_never_targets_the_old_or_parent_group(native_fakes):
    spec, native, _queue, kills, ready, clock, _writer = native_fakes
    native.getpgrp = lambda: 200 if clock[0] < 10.8 else 90
    with pytest.raises(watchdog.WatchdogError):
        watchdog.run_watchdog(**spec)
    assert len(ready) == 1 and kills == []


def test_expired_valid_deadline_kills_bound_group_without_ready(native_fakes):
    spec, _native, _queue, kills, ready, _clock, _writer = native_fakes
    with pytest.raises(watchdog.WatchdogError):
        watchdog.run_watchdog(**(spec | {"deadline": 9.0}))
    assert ready == [] and kills == [(200, signal.SIGKILL)]


@pytest.mark.skipif(sys.platform != "darwin", reason="Actual kqueue/group-kill behavior requires macOS")
@pytest.mark.parametrize("mode", ["eof", "deadline", "supervisor_exit"])
def test_native_watchdog_kills_only_its_isolated_synthetic_group(mode):
    harness = r"""
import json, os, select, signal, subprocess, sys, time
from app.processing.protocol import read_control
signal.alarm(8)
read_fd, writer = os.pipe()
ready_read, ready_write = os.pipe()
leader = subprocess.Popen(
    [sys.executable, '-c', 'import signal,time; signal.alarm(5); time.sleep(4)'],
    process_group=0, stdout=subprocess.PIPE,
)
watcher = None
closed_writer = False
confirmed_exit = False
started = time.monotonic()
mode = sys.argv[1]
arguments = dict(parent_pid=os.getpid(), parent_pgid=os.getpgrp(), supervisor_pid=leader.pid,
                 session_id=os.getsid(0), liveness_fd=read_fd, ready_fd=ready_write,
                 deadline=started + 1.5)
code = ('import signal; signal.alarm(5); from app.processing.watchdog import run_watchdog; '
        'run_watchdog(**' + repr(arguments) + ')')
try:
    watcher = subprocess.Popen([sys.executable, '-c', code], process_group=leader.pid,
                               pass_fds=(read_fd, ready_write), stdout=subprocess.DEVNULL)
    os.close(ready_write)
    os.close(read_fd)
    assert select.select([ready_read], [], [], 2.0)[0], 'missing READY'
    with os.fdopen(ready_read, 'rb', buffering=0) as stream:
        ready = read_control(stream)
    assert ready == dict(type='ready', role='watchdog', protocol=1, pid=watcher.pid,
                         supervisor_pid=leader.pid, parent_pid=os.getpid())
    if mode == 'eof':
        os.close(writer)
        closed_writer = True
    elif mode == 'supervisor_exit':
        leader.kill()  # Deliberately do not reap the group leader.
    assert watcher.wait(timeout=3.0) == -signal.SIGKILL
    # EOF proves the leader exited BEFORE our finally cleanup, without reaping
    # its PID and losing the group-identity reservation.
    assert select.select([leader.stdout], [], [], 0.5)[0], 'leader is still alive'
    assert os.read(leader.stdout.fileno(), 1) == b''
    confirmed_exit = True
    report = dict(mode=mode, ready=ready, watcher_returncode=watcher.returncode,
                  leader_exited_before_cleanup=True, elapsed_seconds=time.monotonic()-started)
finally:
    if not closed_writer:
        os.close(writer)
    # The group leader has never been reaped; this targets only our own group.
    if not confirmed_exit:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    leader.wait(timeout=1.0)
    leader.stdout.close()
    if watcher is not None:
        watcher.wait(timeout=1.0)
print(json.dumps(report))
"""
    result = subprocess.run(
        [sys.executable, "-c", harness, mode],
        start_new_session=True,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["mode"] == mode
    assert report["watcher_returncode"] == -signal.SIGKILL
    assert report["leader_exited_before_cleanup"] is True
    assert report["elapsed_seconds"] < 3.5
