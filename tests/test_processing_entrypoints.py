from __future__ import annotations

import ast
import builtins
import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = ("entrypoint.py", "service_entrypoint.py")


def _fake_module(monkeypatch, name, **attributes):
    module = ModuleType(name)
    module.__dict__.update(attributes)
    monkeypatch.setitem(sys.modules, name, module)
    return module


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
@pytest.mark.parametrize("status", [37, 70])
def test_private_dispatch_precedes_every_desktop_or_service_import(monkeypatch, entrypoint, status):
    dispatch = Mock(return_value=status)
    _fake_module(monkeypatch, "app.processing.bootstrap", dispatch_if_requested=dispatch)
    original_import = builtins.__import__

    def restricted_import(name, *args, **kwargs):
        if name in {"app.windows_launcher", "app.windows_service", "app.main", "pystray", "servicemanager"}:
            pytest.fail(f"private role imported UI/server/SCM module: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", restricted_import)
    path = ROOT / "packaging/windows" / entrypoint
    arguments = ["--einvoice-processing", "synthetic-role"]
    monkeypatch.setattr(sys, "argv", [str(path), *arguments])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(path), run_name="__main__")
    assert caught.value.code == status
    dispatch.assert_called_once_with(arguments)


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_normal_entrypoint_retains_existing_main_arguments(monkeypatch, entrypoint):
    dispatch = Mock(return_value=None)
    main = Mock(return_value=0)
    _fake_module(monkeypatch, "app.processing.bootstrap", dispatch_if_requested=dispatch)
    target = "app.windows_launcher" if entrypoint == "entrypoint.py" else "app.windows_service"
    _fake_module(monkeypatch, target, main=main, DIRECT_START_EXIT_CODE=2)
    path = ROOT / "packaging/windows" / entrypoint
    arguments = ["--health-check"]
    monkeypatch.setattr(sys, "argv", [str(path), *arguments])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(path), run_name="__main__")
    assert caught.value.code == 0
    dispatch.assert_called_once_with(arguments)
    main.assert_called_once_with(arguments)


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
@pytest.mark.parametrize(
    "arguments",
    [
        ["--einvoice-processing"],
        ["--einvoice-processing", "unknown", "1", "2", "3"],
        ["--health-check", "--einvoice-processing", "worker"],
        ["--einvoice-processing=worker"],
    ],
)
def test_malformed_private_roles_never_fall_back_to_ui(monkeypatch, entrypoint, arguments):
    from app.processing import bootstrap

    _fake_module(monkeypatch, "app.processing.bootstrap", dispatch_if_requested=bootstrap.dispatch_if_requested)
    forbidden = Mock(side_effect=AssertionError("malformed role fell through to UI/SCM"))
    for name in ("app.windows_launcher", "app.windows_service"):
        _fake_module(monkeypatch, name, main=forbidden, DIRECT_START_EXIT_CODE=2)
    path = ROOT / "packaging/windows" / entrypoint
    monkeypatch.setattr(sys, "argv", [str(path), *arguments])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(path), run_name="__main__")
    assert caught.value.code == 70
    forbidden.assert_not_called()


@pytest.mark.parametrize("spec_name", ["e_rechnungs_pruefer.spec", "e_rechnungs_pruefer_service.spec"])
def test_frozen_specs_include_all_private_process_roles(spec_name):
    tree = ast.parse((ROOT / "packaging/windows" / spec_name).read_text(encoding="utf-8"))
    value = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "hidden_imports" for target in node.targets)
    )
    names = {node.value for node in ast.walk(value) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert {
        f"app.processing.{name}"
        for name in ("bootstrap", "worker", "supervisor", "operations", "native", "watchdog", "windows")
    } <= names


@pytest.mark.parametrize("failure", [False, True])
def test_loopback_stop_closes_admission_before_uvicorn_even_on_cleanup_failure(monkeypatch, failure):
    from app.server_runtime import LoopbackServer

    server = object.__new__(LoopbackServer)
    server.server = SimpleNamespace(should_exit=False)
    calls = []

    def shutdown():
        assert server.server.should_exit is False
        calls.append("shutdown")
        if failure:
            raise OSError("synthetic cleanup failure")

    _fake_module(monkeypatch, "app.processing.manager", manager=SimpleNamespace(shutdown=shutdown))
    if failure:
        with pytest.raises(OSError):
            server.request_stop()
    else:
        server.request_stop()
    assert calls == ["shutdown"] and server.server.should_exit is True
