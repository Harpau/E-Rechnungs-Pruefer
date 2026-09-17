"""Native tests are Windows-only; orchestration regressions run everywhere."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from app.processing import native, windows


class FakeAPI:
    def __init__(self):
        self.events = []
        self.closed = []
        self.inheritable = {40: True, 41: True, 42: True, 90: False, 100: False, 200: True}
        self.limits = None
        self.failure = None
        self.running = True
        self.exit_code_value = 7

    def create_job(self):
        self.events.append("job")
        return 100

    def set_limits(self, handle, limits):
        self.events.append("limits")
        self.limits = limits
        if self.failure == "limits":
            raise OSError("set limit failed")

    def get_limits(self, handle):
        self.events.append("verify-limits")
        return replace(self.limits, memory_bytes=1) if self.failure == "verify-limits" else self.limits

    def is_inheritable(self, handle):
        return self.inheritable[handle]

    def null_handle(self):
        self.events.append("null")
        return 200

    def create_process(self, **kwargs):
        self.events.append(("create", kwargs))
        if self.failure == "create":
            raise OSError("CreateProcess failed")
        return 300, 301, 1234

    def is_in_job(self, process, job):
        self.events.append(("membership", process, job))
        return self.failure != "membership"

    def resume(self, thread):
        self.events.append(("resume", thread))
        if self.failure == "resume":
            raise OSError("ResumeThread failed")

    def close_handle(self, handle):
        self.closed.append(handle)

    def terminate_process(self, handle, exit_code):
        self.events.append(("terminate-process", handle, exit_code))
        self.running = False

    def terminate_job(self, handle, exit_code):
        self.events.append(("terminate-job", handle, exit_code))
        self.running = False

    def wait(self, handle, milliseconds):
        self.events.append(("wait", handle, milliseconds))
        return not self.running

    def exit_code(self, handle):
        return self.exit_code_value

    def duplicate_process_handle(self, handle):
        self.events.append(("duplicate", handle))
        return 400

    def active_process_count(self, handle):
        return int(self.running)

    def assign_current_process(self, handle):
        self.events.append(("assign-current", handle))

    def process_ids(self, handle):
        return (1234,) if self.running else ()


@pytest.fixture
def api(monkeypatch):
    instance = FakeAPI()
    monkeypatch.setattr(windows, "_load_api", lambda: instance)
    return instance


def job():
    return windows.WindowsJob(2 * 1024**3, process_memory_bytes=768 * 1024**2, active_processes=4)


@pytest.mark.parametrize("observe", ["poll", "wait"])
def test_confirmed_process_exit_remains_available_after_handle_cleanup(api, observe):
    process = windows.NativeProcess(api, 300, 1234, [r"C:\Python\python.exe"])
    assert process.returncode is None
    assert process.poll() is None
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(timeout=0)
    assert process.returncode is None
    api.running = False
    assert getattr(process, observe)() == 7
    process.close()
    events = list(api.events)
    assert process.returncode == process.poll() == process.wait(timeout=0) == 7
    assert api.events == events, "closed handles must never be queried again"


def test_closing_unobserved_process_handle_does_not_invent_an_exit_receipt(api):
    process = windows.NativeProcess(api, 300, 1234, [r"C:\Python\python.exe"])
    process.close()
    assert process.returncode is None
    with pytest.raises(ValueError, match="geschlossen"):
        process.poll()


def test_limits_creation_attributes_and_membership_precede_resume(api):
    with job() as owner:
        process = owner.spawn([r"C:\Python\python.exe", "arg with space"], {"EXAMPLE": "ä"}, job_handles=(90,))
        assert process.pid == 1234
        create = next(event[1] for event in api.events if isinstance(event, tuple) and event[0] == "create")
        assert create["jobs"] == (90, 100)
        assert create["handles"] == (200,)
        assert create["stdio"] == (200, 200, 200)
        assert api.events[:3] == ["job", "limits", "verify-limits"]
        assert api.events[-1] == ("resume", 301)
        assert ("membership", 300, 90) in api.events and ("membership", 300, 100) in api.events
        assert api.closed == [301, 200]
        process.close()
    assert api.closed == [301, 200, 300, 100]


def test_explicit_handle_allowlist_does_not_include_jobs_or_change_flags(api):
    with job() as owner:
        process = owner.spawn([r"C:\Python\python.exe"], {}, inherited_handles=(40,), stdio_handles=(40, 41, 42))
        create = next(event[1] for event in api.events if isinstance(event, tuple) and event[0] == "create")
        assert create["handles"] == (40, 41, 42)
        assert create["stdio"] == (40, 41, 42)
        process.close()
    assert not {40, 41, 42}.intersection(api.closed)


@pytest.mark.parametrize("failure", ["limits", "verify-limits"])
def test_unconfigured_job_is_closed_and_cannot_start_a_process(api, failure):
    api.failure = failure
    with pytest.raises(OSError):
        job()
    assert api.closed == [100]
    assert not any(isinstance(event, tuple) and event[0] == "create" for event in api.events)


@pytest.mark.parametrize("failure", ["create", "membership", "resume"])
def test_failed_start_closes_handles_and_never_returns_a_process(api, failure):
    owner = job()
    api.failure = failure
    with pytest.raises(OSError):
        owner.spawn([r"C:\Python\python.exe"], {})
    if failure == "create":
        assert api.closed == [200]
    else:
        assert ("terminate-process", 300, 1) in api.events
        assert 301 in api.closed and 300 in api.closed and 200 in api.closed
    if failure == "membership":
        assert ("resume", 301) not in api.events
    owner.close()


@pytest.mark.parametrize("handles", [(100,), (-1,), (0,), (True,), (40, 40)])
def test_invalid_or_job_handle_cannot_be_inherited(api, handles):
    with job() as owner, pytest.raises((ValueError, OSError)):
        owner.spawn([r"C:\Python\python.exe"], {}, inherited_handles=handles)
    assert "null" not in api.events


def test_noninheritable_ipc_handle_rejected_without_flag_mutation(api):
    api.inheritable[40] = False
    with job() as owner, pytest.raises(OSError):
        owner.spawn([r"C:\Python\python.exe"], {}, inherited_handles=(40,))
    assert api.inheritable[40] is False


@pytest.mark.parametrize(
    "command,environment",
    [
        (["python.exe"], {}),
        ([r"\Python\python.exe"], {}),
        ([], {}),
        ([r"C:\Python\python.exe", "x\0y"], {}),
        ([r"C:\Python\python.exe"], {"X": "value", "x": "other"}),
        ([r"C:\Python\python.exe"], {"X=Y": "value"}),
        ([r"C:\Python\python.exe"], {"X": "bad\0value"}),
    ],
)
def test_ambiguous_process_parameters_rejected_before_native_start(api, command, environment):
    with job() as owner, pytest.raises(ValueError):
        owner.spawn(command, environment)
    assert "null" not in api.events


def test_native_process_timeout_exit_and_close_are_explicit(api):
    with job() as owner:
        process = owner.spawn([r"C:\Python\python.exe"], {})
        assert process.poll() is None
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(0.001)
        process.kill()
        assert process.wait(1) == 7 and process.poll() == 7
        process.close()
        process.close()
        assert process.poll() == 7
        owner.terminate()
        assert owner.active_process_count() == 0
    with pytest.raises(ValueError):
        owner.spawn([r"C:\Python\python.exe"], {})


def test_wait_does_not_hold_the_lock_needed_to_kill(api):
    import threading

    waiting = threading.Event()
    killed = threading.Event()
    original_kill = api.terminate_process

    def wait(handle, milliseconds):
        waiting.set()
        return killed.wait(timeout=1)

    def kill(handle, exit_code):
        original_kill(handle, exit_code)
        killed.set()

    api.wait = wait
    api.terminate_process = kill
    result = []
    with job() as owner:
        process = owner.spawn([r"C:\Python\python.exe"], {})
        thread = threading.Thread(target=lambda: result.append(process.wait(1)))
        thread.start()
        assert waiting.wait(1)
        process.kill()
        thread.join(2)
        process.close()
    assert result == [7]


def test_only_explicit_supervisor_self_binding_uses_late_assignment(api):
    with job() as owner:
        process = owner.spawn([r"C:\Python\python.exe"], {})
        assert not any(isinstance(event, tuple) and event[0] == "assign-current" for event in api.events)
        owner.assign_current_process()
        assert api.events[-2:] == [("assign-current", 100), ("membership", -1, 100)]
        assert owner.process_ids() == (1234,)
        process.close()


def test_memory_limits_only_lower_and_preserve_existing_process_cap(api):
    with job() as owner:
        owner.lower_memory_limit(1024**3)
        assert api.limits.memory_bytes == 1024**3
        assert api.limits.process_memory_bytes == 768 * 1024**2
        owner.lower_memory_limit(512 * 1024**2)
        assert api.limits.process_memory_bytes == 512 * 1024**2
        with pytest.raises(ValueError):
            owner.lower_memory_limit(1024**3)
        with pytest.raises(ValueError):
            owner.lower_memory_limit(512 * 1024**2, process_memory_bytes=513 * 1024**2)


def test_failed_memory_lowering_verification_closes_the_job(api):
    owner = job()
    api.failure = "verify-limits"
    with pytest.raises(OSError):
        owner.lower_memory_limit(1024**3)
    assert api.closed == [100]
    with pytest.raises(ValueError):
        _ = owner.handle


def test_auxiliary_handle_cleanup_failure_does_not_return_or_leak_running_child(api):
    original = api.close_handle

    def fail_thread_close(handle):
        original(handle)
        if handle == 301:
            raise OSError("Synthetic thread close failure")

    api.close_handle = fail_thread_close
    with job() as owner, pytest.raises(OSError):
        owner.spawn([r"C:\Python\python.exe"], {})
    assert ("terminate-process", 300, 1) in api.events
    assert 300 in api.closed and 200 in api.closed


@pytest.mark.parametrize(
    "kwargs",
    [
        {"memory_bytes": 0, "active_processes": 1},
        {"memory_bytes": True, "active_processes": 1},
        {"memory_bytes": 1024, "active_processes": 0},
        {"memory_bytes": 1024, "active_processes": 1, "process_memory_bytes": 2048},
    ],
)
def test_invalid_limits_fail_before_creating_job(api, kwargs):
    with pytest.raises(ValueError):
        windows.WindowsJob(**kwargs)
    assert not api.events


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows creation-time job containment")
def test_native_creation_time_membership_stdio_and_kill_on_close(tmp_path):
    code = "import ctypes; k=ctypes.WinDLL('kernel32',use_last_error=True); ok=ctypes.c_int(); k.IsProcessInJob(ctypes.c_void_p(-1),None,ctypes.byref(ok)); raise SystemExit(23 if ok.value else 24)"
    with windows.WindowsJob(512 * 1024**2, active_processes=2) as owner:
        process = owner.spawn([native.python_executable(), "-I", "-c", code], native.child_environment())
        try:
            assert process.wait(10) == 23
        finally:
            process.close()
        ready_file = tmp_path / "synthetic-running-pid.txt"
        sleeping_code = (
            "import os,time; from pathlib import Path; "
            f"Path({str(ready_file)!r}).write_text(str(os.getpid()),encoding='ascii'); time.sleep(10)"
        )
        launched = time.monotonic()
        sleeping = owner.spawn([native.python_executable(), "-I", "-c", sleeping_code], native.child_environment())
        try:
            assert _wait_control_file(ready_file, sleeping) == sleeping.pid
            assert sleeping.poll() is None
            owner.close()
            # Kill-on-close promises termination, not a particular exit code.
            # A zero exit value is valid when the held handle confirms exit.
            sleeping.wait(5)
            assert sleeping.returncode is not None
            assert time.monotonic() - launched < 10, "natural sleeper exit is not kill-on-close evidence"
        finally:
            owner.close()
            sleeping.close()


def _native_open_process(pid):
    import ctypes

    api = windows._load_api()
    function = api.dll.OpenProcess
    function.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
    function.restype = ctypes.c_void_p
    return api, function(0x100000, False, pid)


def _wait_control_file(path, process):
    import time

    deadline = time.monotonic() + 10
    while not path.is_file():
        assert process.poll() is None, "Trusted native helper exited before READY"
        assert time.monotonic() < deadline, "Trusted native helper missed READY deadline"
        time.sleep(0.02)
    return int(path.read_text(encoding="ascii"))


@pytest.mark.skipif(sys.platform != "win32", reason="real inherited nested job chain")
def test_native_nested_job_keeps_descendants_in_outer_kill_on_close(tmp_path):
    from pathlib import Path

    pid_file = tmp_path / "synthetic-grandchild-pid.txt"
    source_root = str(Path(__file__).resolve().parents[1])
    code = (
        "import sys,time; from pathlib import Path; "
        f"sys.path.insert(0,{source_root!r}); "
        "from app.processing.windows import WindowsJob; from app.processing import native; "
        "import os; inner=WindowsJob(256*1024**2,active_processes=1); "
        "child=inner.spawn([native.python_executable(),'-I','-c','import time; time.sleep(10)'],native.child_environment()); "
        f"Path({str(pid_file)!r}).write_text(str(child.pid),encoding='ascii'); "
        "time.sleep(10)"
    )
    with windows.WindowsJob(768 * 1024**2, active_processes=3) as owner:
        launched = time.monotonic()
        process = owner.spawn([native.python_executable(), "-I", "-c", code], native.child_environment())
        descendant_handle = None
        try:
            descendant = _wait_control_file(pid_file, process)
            api, descendant_handle = _native_open_process(descendant)
            assert descendant_handle and not api.wait(descendant_handle, 0)
            assert descendant in owner.process_ids() and owner.active_process_count() == 2
            deadline = time.monotonic() + 5
            owner.close()
            process.wait(max(0, deadline - time.monotonic()))
            assert process.returncode is not None
            assert api.wait(descendant_handle, max(0, int((deadline - time.monotonic()) * 1000)))
            observed = time.monotonic()
            assert observed <= deadline
            assert observed - launched < 10, "natural helper exit is not nested-job termination evidence"
        finally:
            if descendant_handle:
                api.close_handle(descendant_handle)
            process.close()


@pytest.mark.skipif(sys.platform != "win32", reason="real parent death before ResumeThread")
def test_native_parent_death_after_create_before_resume_leaves_no_unbound_child(tmp_path):
    from pathlib import Path

    pid_file = tmp_path / "synthetic-suspended-child-pid.txt"
    marker = tmp_path / "child-must-not-execute.txt"
    source_root = str(Path(__file__).resolve().parents[1])
    code = f"""
import os, sys
from pathlib import Path
sys.path.insert(0, {source_root!r})
from app.processing import native, windows
original = windows._Win32.create_process
def die_before_resume(self, **kwargs):
    result = original(self, **kwargs)
    Path({str(pid_file)!r}).write_text(str(result[2]), encoding="ascii")
    os._exit(0)
windows._Win32.create_process = die_before_resume
owner = windows.WindowsJob(256 * 1024**2, active_processes=1)
owner.spawn([native.python_executable(), "-I", "-c", {f'from pathlib import Path; Path({str(marker)!r}).write_text("unexpected")'!r}], native.child_environment())
"""
    completed = subprocess.run(
        [native.python_executable(), "-I", "-c", code],
        env=native.child_environment(),
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert not marker.exists()
    api, handle = _native_open_process(int(pid_file.read_text(encoding="ascii")))
    if handle:
        try:
            assert api.wait(handle, 10000)
        finally:
            api.close_handle(handle)
    else:
        import ctypes

        assert vars(ctypes)["get_last_error"]() in {87, 1168}


@pytest.mark.skipif(sys.platform != "win32", reason="real explicit HANDLE_LIST allowlist")
def test_native_only_selected_pipe_is_inherited():
    import msvcrt

    selected_read, selected_write = os.pipe()
    other_read, other_write = os.pipe()
    process = None
    try:
        selected = msvcrt.get_osfhandle(selected_write)
        other = msvcrt.get_osfhandle(other_write)
        os.set_handle_inheritable(selected, True)
        os.set_handle_inheritable(other, True)
        code = f"""
import ctypes
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
k.WriteFile.restype = ctypes.c_int32
written = ctypes.c_uint32()
assert k.WriteFile({selected}, b"ok", 2, ctypes.byref(written), None) and written.value == 2
assert not k.WriteFile({other}, b"unexpected", 10, ctypes.byref(written), None)
"""
        with windows.WindowsJob(256 * 1024**2, active_processes=1) as owner:
            process = owner.spawn(
                [native.python_executable(), "-I", "-c", code],
                native.child_environment(),
                inherited_handles=(selected,),
            )
            assert process.wait(10) == 0
            os.close(selected_write)
            selected_write = -1
            os.close(other_write)
            other_write = -1
            assert os.read(selected_read, 10) == b"ok"
            assert os.read(other_read, 10) == b""
    finally:
        if process is not None:
            process.close()
        for descriptor in (selected_read, selected_write, other_read, other_write):
            if descriptor != -1:
                os.close(descriptor)


@pytest.mark.parametrize("fail_update", [False, True])
def test_raw_ctypes_startup_attributes_include_atomic_jobs_and_allowlist(monkeypatch, fail_update):
    import ctypes
    from types import SimpleNamespace

    calls = []

    def initialize(storage, count, flags, size):
        assert count == 2 and flags == 0
        ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t)).contents.value = 128
        return storage is not None

    def update(storage, flags, key, array, length, previous, returned):
        calls.append((key, tuple(array), length))
        return not fail_update

    def create(
        application, command, process_security, thread_security, inherit, flags, environment, cwd, startup, info
    ):
        assert application == r"C:\Python\python.exe" and command.value == application
        assert process_security is thread_security is None
        assert inherit and flags == 0x08080404  # NO_WINDOW | EX | UNICODE | SUSPENDED
        value = ctypes.cast(startup, ctypes.POINTER(windows._StartupInfoEx)).contents
        assert value.StartupInfo.cb == ctypes.sizeof(windows._StartupInfoEx)
        assert value.StartupInfo.dwFlags == 0x100 and value.lpAttributeList
        assert (value.StartupInfo.hStdInput, value.StartupInfo.hStdOutput, value.StartupInfo.hStdError) == (40, 41, 41)
        result = ctypes.cast(info, ctypes.POINTER(windows._ProcessInformation)).contents
        result.hProcess, result.hThread, result.dwProcessId = 300, 301, 1234
        calls.append("create")
        return True

    api = windows._Win32.__new__(windows._Win32)
    api.dll = SimpleNamespace(
        InitializeProcThreadAttributeList=initialize,
        UpdateProcThreadAttribute=update,
        DeleteProcThreadAttributeList=lambda storage: calls.append("delete"),
        CreateProcessW=create,
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 122, raising=False)
    arguments = dict(
        application=r"C:\Python\python.exe",
        command_line=r"C:\Python\python.exe",
        environment="X=example\0\0",
        cwd=None,
        jobs=(90, 100),
        handles=(40, 41),
        stdio=(40, 41, 41),
    )
    if fail_update:
        with pytest.raises(OSError):
            api.create_process(**arguments)
        assert "create" not in calls
    else:
        assert api.create_process(**arguments) == (300, 301, 1234)
        assert calls[:2] == [
            (0x2000D, (90, 100), ctypes.sizeof(ctypes.c_void_p) * 2),
            (0x20002, (40, 41), ctypes.sizeof(ctypes.c_void_p) * 2),
        ]
    assert calls[-1] == "delete"


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows source interpreter and venv identity")
def test_native_direct_interpreter_pid_and_venv_packages_are_exact(tmp_path):
    import json

    import fastapi

    path = tmp_path / "synthetic-interpreter-identity.json"
    code = (
        "import os,sys,json,time,fastapi; from pathlib import Path; "
        "record={'pid':os.getpid(),'prefix':sys.prefix,'base_prefix':sys.base_prefix,"
        "'executable':sys.executable,'fastapi':fastapi.__file__}; "
        f"Path({str(path)!r}).write_text(json.dumps(record),encoding='utf-8'); time.sleep(10)"
    )
    with windows.WindowsJob(512 * 1024**2, active_processes=1) as owner:
        process = owner.spawn([native.python_executable(), "-I", "-c", code], native.child_environment())
        try:
            import time

            deadline = time.monotonic() + 8
            while not path.exists():
                assert process.poll() is None, "direct interpreter exited before identity proof"
                assert time.monotonic() < deadline
                time.sleep(0.01)
            value = json.loads(path.read_text(encoding="utf-8"))
            assert value["pid"] == process.pid
            assert owner.process_ids() == (process.pid,)
            assert owner.active_process_count() == 1
            for name, expected in (
                ("prefix", sys.prefix),
                ("base_prefix", sys.base_prefix),
                ("executable", sys.executable),
                ("fastapi", fastapi.__file__),
            ):
                assert os.path.normcase(value[name]) == os.path.normcase(expected)
        finally:
            owner.terminate()
            process.wait(5)
            process.close()
