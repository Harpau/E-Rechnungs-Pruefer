"""Windows source roles must be the interpreter, never a venv redirector PID."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.processing import native

BASE = r"C:\private-python\python.exe"
VENV = r"C:\project\.venv\Scripts\python.exe"


def windows_venv(monkeypatch: pytest.MonkeyPatch, **changes: object) -> None:
    values: dict[str, object] = {
        "platform": "win32",
        "executable": VENV,
        "_base_executable": BASE,
        "prefix": r"C:\project\.venv",
        "base_prefix": r"C:\private-python",
    }
    values.update(changes)
    monkeypatch.setattr(native, "sys", SimpleNamespace(**values))
    monkeypatch.setattr(native.os.path, "isfile", lambda value: value in {BASE, VENV})


def test_windows_venv_roles_use_owned_base_interpreter_and_derived_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_venv(monkeypatch)
    monkeypatch.setenv("__PYVENV_LAUNCHER__", r"C:\attacker\python.exe")
    monkeypatch.setenv("PYTHONPATH", r"C:\attacker")
    monkeypatch.setenv("EINVOICE_API_TOKEN", "synthetic-secret")
    command = native.role_command("worker", ["123", "40", "41"])
    assert command[0] == BASE
    assert command[1] == "-I"
    assert command[-5:] == [native.ROLE_FLAG, "worker", "123", "40", "41"]
    environment = native.child_environment()
    assert environment["__PYVENV_LAUNCHER__"] == VENV
    assert "PYTHONPATH" not in environment and "EINVOICE_API_TOKEN" not in environment
    assert native.python_executable() == BASE


@pytest.mark.parametrize("base", [None, "", "python.exe", r"\private-python\python.exe", "C:\\bad\0.exe"])
def test_invalid_venv_base_never_falls_back_to_redirector(monkeypatch: pytest.MonkeyPatch, base: object) -> None:
    windows_venv(monkeypatch, _base_executable=base)
    with pytest.raises(OSError):
        native.role_command("worker", ["123", "40", "41"])
    with pytest.raises(OSError):
        native.child_environment()


def test_missing_owned_interpreter_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_venv(monkeypatch)
    monkeypatch.setattr(native.os.path, "isfile", lambda _value: False)
    with pytest.raises(OSError):
        native.python_executable()


def test_frozen_start_never_selects_a_source_base_or_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_venv(monkeypatch, frozen=True, executable=r"C:\app\ERechnung.exe", _base_executable=None)
    monkeypatch.setenv("__PYVENV_LAUNCHER__", VENV)
    command = native.role_command("worker", ["123", "40", "41"])
    assert command[:3] == [r"C:\app\ERechnung.exe", native.ROLE_FLAG, "worker"]
    environment = native.child_environment()
    assert "__PYVENV_LAUNCHER__" not in environment
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_windows_base_installation_has_no_venv_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_venv(monkeypatch, executable=BASE, prefix=r"C:\private-python")
    assert native.role_command("worker", ["123", "40", "41"])[0] == BASE
    assert "__PYVENV_LAUNCHER__" not in native.child_environment()


def test_posix_interpreter_selection_remains_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_venv(monkeypatch, platform="linux", executable="/venv/bin/python", _base_executable=None)
    assert native.python_executable() == "/venv/bin/python"
    assert "__PYVENV_LAUNCHER__" not in native.child_environment()
