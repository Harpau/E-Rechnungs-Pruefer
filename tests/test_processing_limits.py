from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.processing import posix
from app.processing.budgets import ProcessingBudgets


@pytest.mark.parametrize(
    "system,machine,gib",
    [("darwin", "arm64", 512), ("darwin", "x86_64", 64), ("linux", "aarch64", 1), ("linux", "x86_64", 1)],
)
def test_baseline_ceiling_depends_on_trusted_platform_and_architecture(monkeypatch, system, machine, gib):
    monkeypatch.setattr(posix, "sys", SimpleNamespace(platform=system))
    monkeypatch.setattr(posix, "platform", SimpleNamespace(machine=lambda: machine), raising=False)
    assert posix.baseline_ceiling_bytes() == gib * 1024**3


@pytest.mark.parametrize("system,machine", [("darwin", "unknown"), ("darwin", "i386"), ("win32", "AMD64")])
def test_unknown_posix_baseline_profile_fails_closed(monkeypatch, system, machine):
    monkeypatch.setattr(posix, "sys", SimpleNamespace(platform=system))
    monkeypatch.setattr(posix, "platform", SimpleNamespace(machine=lambda: machine), raising=False)
    with pytest.raises(OSError):
        posix.baseline_ceiling_bytes()


@pytest.mark.parametrize(
    "system,machine,gib", [("darwin", "arm64", 512), ("darwin", "x86_64", 64), ("linux", "aarch64", 1)]
)
@pytest.mark.parametrize("seal", [False, True])
@pytest.mark.parametrize("excess", [0, 1])
def test_start_and_seal_use_identical_baseline_ceiling_without_raising_headroom(
    monkeypatch, system, machine, gib, seal, excess
):
    import sys

    baseline = gib * 1024**3 + excess
    headroom = 8 * 1024**2
    limits = {1: (baseline + 4 * headroom,) * 2}
    resource = SimpleNamespace(
        RLIMIT_CORE=0,
        RLIMIT_AS=1,
        RLIMIT_NOFILE=2,
        RLIMIT_FSIZE=3,
        RLIMIT_CPU=4,
        RLIM_INFINITY=-1,
        setrlimit=lambda key, value: limits.__setitem__(key, value),
        getrlimit=lambda key: limits[key],
    )
    monkeypatch.setitem(sys.modules, "resource", resource)
    monkeypatch.setattr(posix, "sys", SimpleNamespace(platform=system))
    monkeypatch.setattr(posix, "platform", SimpleNamespace(machine=lambda: machine), raising=False)
    monkeypatch.setattr(posix, "virtual_memory_bytes", lambda: baseline)
    monkeypatch.setattr(
        posix,
        "signal",
        SimpleNamespace(SIGXCPU=24, SIG_DFL=0, SIG_UNBLOCK=0, signal=lambda *_: None, pthread_sigmask=lambda *_: None),
    )
    kwargs = {"memory_headroom": headroom} if seal else {"memory_headroom": headroom, "cpu_seconds": 30}
    function = posix.seal_runtime_memory if seal else posix.apply_limits
    if excess:
        before = dict(limits)
        with pytest.raises(OSError):
            function(**kwargs)
        assert limits == before
    else:
        proof = function(**kwargs)
        assert proof["baseline_as_bytes"] == baseline
        assert proof["address_space_bytes"] == baseline + headroom
        assert limits[1] == (baseline + headroom,) * 2


def test_linux_namespace_init_can_own_worker(monkeypatch):
    monkeypatch.setattr(posix.sys, "platform", "linux")
    getppid = Mock(side_effect=[1, 1])
    prctl = Mock(return_value=0)
    monkeypatch.setattr(posix.os, "getppid", getppid)
    monkeypatch.setattr(posix.ctypes, "CDLL", Mock(return_value=Mock(prctl=prctl)))
    # Windows can run this platform-independent binding regression too.
    monkeypatch.setattr(posix.signal, "SIGKILL", 9, raising=False)
    posix.bind_parent(1)
    prctl.assert_called_once_with(1, 9, 0, 0, 0)
    assert getppid.call_count == 2


@pytest.mark.parametrize("platform,parent", [("linux", 0), ("linux", -1), ("linux", True), ("darwin", 1)])
def test_invalid_parent_identity_is_rejected_before_native_binding(monkeypatch, platform, parent):
    monkeypatch.setattr(posix.sys, "platform", platform)
    monkeypatch.setattr(posix.os, "getppid", lambda: parent)
    monkeypatch.setattr(posix.ctypes, "CDLL", lambda *_a, **_k: pytest.fail("Invalid parent reached native binding"))
    with pytest.raises(OSError):
        posix.bind_parent(parent)


@pytest.mark.parametrize("parents", [[1], [123, 1]])
def test_linux_parent_loss_and_adoption_by_init_are_rejected(monkeypatch, parents):
    monkeypatch.setattr(posix.sys, "platform", "linux")
    monkeypatch.setattr(posix.os, "getppid", Mock(side_effect=parents))
    monkeypatch.setattr(posix.ctypes, "CDLL", Mock(return_value=Mock(prctl=Mock(return_value=0))))
    monkeypatch.setattr(posix.signal, "SIGKILL", 9, raising=False)
    with pytest.raises(OSError):
        posix.bind_parent(123)


@pytest.mark.parametrize(
    "platform,expected_mib",
    [("darwin", (4096, 1536, 1024)), ("linux", (2048, 768, 512)), ("win32", (2048, 768, 512))],
)
def test_native_profiles_keep_macos_address_space_separate_from_windows_commit(platform, expected_mib):
    budgets = ProcessingBudgets.for_platform(platform)
    assert (
        budgets.python_start_memory_bytes,
        budgets.python_memory_bytes,
        budgets.supervisor_memory_bytes,
    ) == tuple(value * 1024**2 for value in expected_mib)
    # Reconstructing the IPC snapshot cannot select a second platform profile.
    from dataclasses import asdict

    assert ProcessingBudgets(**asdict(budgets)) == budgets


@pytest.mark.parametrize("timeout,expected", [(1, 61), (60, 120), (300, 360)])
def test_deadline_preserves_supported_kosit_configuration(timeout: int, expected: int) -> None:
    budgets = ProcessingBudgets()
    assert budgets.job_seconds(official=True, kosit_seconds=timeout) == expected
    assert budgets.job_seconds(official=False, kosit_seconds=timeout) == 60


@pytest.mark.parametrize("timeout", [True, 0, -1, 301, float("nan")])
def test_invalid_kosit_deadlines_cannot_disable_limits(timeout) -> None:
    with pytest.raises(ValueError):
        ProcessingBudgets().job_seconds(official=True, kosit_seconds=timeout)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_jobs", 3),
        ("max_jobs", True),
        ("python_seconds", float("nan")),
        ("cleanup_seconds", -1),
        ("python_memory_bytes", 0),
        ("start_seconds", float("inf")),
    ],
)
def test_invalid_resource_configuration_fails_closed(field, value):
    with pytest.raises(ValueError):
        replace(ProcessingBudgets(), **{field: value})
