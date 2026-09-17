from __future__ import annotations

from dataclasses import replace

import pytest

from app.processing.budgets import ProcessingBudgets


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
