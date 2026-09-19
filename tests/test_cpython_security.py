from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from scripts import cpython_security as security

ORIGINAL = Path(__file__).parent / "fixtures/cpython3147/urllib-request.source"


def load_module(source: bytes) -> ModuleType:
    module = ModuleType("_synthetic_urllib_request")
    exec(compile(source, "synthetic-cpython-request.py", "exec"), module.__dict__)
    return module


def test_original_interpreter_reproduces_credential_leak() -> None:
    module = load_module(ORIGINAL.read_bytes())
    manager = module.HTTPPasswordMgrWithPriorAuth()
    manager.add_password(None, "https://example.invalid/", "synthetic", "secret", is_authenticated=True)
    request = module.Request("http://example.invalid/")
    module.HTTPBasicAuthHandler(manager).http_request(request)
    assert request.has_header("Authorization")
    with pytest.raises(security.SecurityPatchError, match="scheme"):
        security.run_security_regressions(module)


@pytest.mark.parametrize("crlf", [False, True])
def test_exact_backport_blocks_leak_and_preserves_supported_matching(crlf: bool) -> None:
    original = ORIGINAL.read_bytes()
    if crlf:
        original = original.replace(b"\n", b"\r\n")
    patched = security.patch_bytes(original)
    metadata = security.security_metadata()
    assert hashlib.sha256(patched).hexdigest() == metadata["after_crlf_sha256" if crlf else "after_sha256"]
    report = security.run_security_regressions(load_module(patched))
    assert report["passed"] is True
    assert "prior-auth-no-https-to-http-header" in report["cases"]


@pytest.mark.parametrize("change", [b"# changed\n", b"\n", b" "])
def test_unknown_source_is_rejected(change: bytes) -> None:
    with pytest.raises(security.SecurityPatchError, match="source hash"):
        security.patch_bytes(ORIGINAL.read_bytes() + change)


def test_second_application_is_rejected() -> None:
    with pytest.raises(security.SecurityPatchError, match="source hash"):
        security.patch_bytes(security.patch_bytes(ORIGINAL.read_bytes()))


def test_mixed_line_endings_are_rejected() -> None:
    mixed = ORIGINAL.read_bytes().replace(b"\n", b"\r\n", 1)
    with pytest.raises(security.SecurityPatchError, match="source hash"):
        security.patch_bytes(mixed)


def test_receipt_binds_source_patch_helper_and_result(tmp_path: Path) -> None:
    target = tmp_path / "request.py"
    original = ORIGINAL.read_bytes()
    target.write_bytes(security.patch_bytes(original))
    receipt = security.make_receipt(original, target.read_bytes())
    assert security.validate_receipt(receipt, target=target) == receipt
    target.write_bytes(original)
    with pytest.raises(security.SecurityPatchError, match="target"):
        security.validate_receipt(receipt, target=target)


@pytest.mark.parametrize(
    "field",
    [
        "advisory",
        "upstream_commit",
        "input_sha256",
        "output_sha256",
        "patch_sha256",
        "metadata_sha256",
        "helper_sha256",
        "line_endings",
    ],
)
def test_forged_receipt_is_rejected(field: str) -> None:
    original = ORIGINAL.read_bytes()
    receipt = copy.deepcopy(security.make_receipt(original, security.patch_bytes(original)))
    receipt[field] = "forged"
    with pytest.raises(security.SecurityPatchError):
        security.validate_receipt(receipt)


def test_receipt_unknown_fields_fail_closed() -> None:
    original = ORIGINAL.read_bytes()
    receipt = security.make_receipt(original, security.patch_bytes(original))
    receipt["ignored"] = "not allowed"
    with pytest.raises(security.SecurityPatchError):
        security.validate_receipt(receipt)


def test_symlink_patch_target_is_rejected_without_mutation(tmp_path: Path) -> None:
    actual = tmp_path / "actual.py"
    actual.write_bytes(ORIGINAL.read_bytes())
    link = tmp_path / "request.py"
    try:
        link.symlink_to(actual)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this host")
    with pytest.raises(security.SecurityPatchError, match="regular"):
        security.apply_file(link, tmp_path / "receipt.json")
    assert actual.read_bytes() == ORIGINAL.read_bytes()


def test_apply_removes_only_own_bytecode_and_preserves_original(tmp_path: Path) -> None:
    target = tmp_path / "request.py"
    target.write_bytes(ORIGINAL.read_bytes())
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    stale = cache / "request.cpython-314.pyc"
    stale.write_bytes(b"synthetic stale bytecode")
    other = cache / "parse.cpython-314.pyc"
    other.write_bytes(b"unrelated bytecode")
    receipt = tmp_path / "receipt.json"
    security.apply_file(target, receipt)
    assert not stale.exists()
    assert other.read_bytes() == b"unrelated bytecode"
    assert receipt.with_suffix(".before.source").read_bytes() == ORIGINAL.read_bytes()
    assert receipt.is_file()


def test_existing_receipt_prevents_mutation(tmp_path: Path) -> None:
    target = tmp_path / "request.py"
    target.write_bytes(ORIGINAL.read_bytes())
    receipt = tmp_path / "receipt.json"
    receipt.write_text("previous evidence")
    with pytest.raises(security.SecurityPatchError):
        security.apply_file(target, receipt)
    assert target.read_bytes() == ORIGINAL.read_bytes()


def test_receipt_parent_symlink_cannot_write_outside_runtime(tmp_path: Path) -> None:
    target = tmp_path / "request.py"
    target.write_bytes(ORIGINAL.read_bytes())
    elsewhere = tmp_path / "outside"
    elsewhere.mkdir()
    link = tmp_path / "share"
    try:
        link.symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(security.SecurityPatchError, match="parent"):
        security.apply_file(target, link / "cpython-security.json")
    assert target.read_bytes() == ORIGINAL.read_bytes()
    assert list(elsewhere.iterdir()) == []


def test_receipt_parent_windows_junction_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "request.py"
    target.write_bytes(ORIGINAL.read_bytes())
    parent = tmp_path / "share"
    parent.mkdir()
    original_lstat = Path.lstat

    def lstat(path: Path, **kwargs: Any) -> Any:
        info = original_lstat(path, **kwargs)
        if path == parent:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(security.SecurityPatchError, match="reparse"):
        security.apply_file(target, parent / "cpython-security.json")
    assert target.read_bytes() == ORIGINAL.read_bytes()
    assert list(parent.iterdir()) == []


@pytest.fixture
def private_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    prefix = tmp_path / "private-python"
    target = prefix / "lib/urllib/request.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(ORIGINAL.read_bytes())
    monkeypatch.setattr(security, "require_version", lambda: None)
    monkeypatch.setattr(sys, "base_prefix", str(prefix))
    monkeypatch.setattr(security, "runtime_target", lambda: target)
    return prefix, target


def test_apply_current_rejects_foreign_prefix_before_mutation(
    private_runtime: tuple[Path, Path], tmp_path: Path
) -> None:
    prefix, target = private_runtime
    with pytest.raises(security.SecurityPatchError, match="prefix"):
        security.apply_current(tmp_path, prefix / security.RECEIPT_RELATIVE)
    assert target.read_bytes() == ORIGINAL.read_bytes()


def test_apply_current_rejects_foreign_stdlib(
    private_runtime: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix, target = private_runtime
    outside = tmp_path / "foreign-request.py"
    outside.write_bytes(ORIGINAL.read_bytes())
    monkeypatch.setattr(security, "runtime_target", lambda: outside)
    with pytest.raises(security.SecurityPatchError, match="prefix"):
        security.apply_current(prefix, prefix / security.RECEIPT_RELATIVE)
    assert target.read_bytes() == outside.read_bytes() == ORIGINAL.read_bytes()


def test_apply_current_rejects_foreign_receipt(private_runtime: tuple[Path, Path], tmp_path: Path) -> None:
    prefix, target = private_runtime
    with pytest.raises(security.SecurityPatchError, match="Receipt"):
        security.apply_current(prefix, tmp_path / "foreign.json")
    assert target.read_bytes() == ORIGINAL.read_bytes()


@pytest.mark.parametrize("inside_source", [False, True])
def test_clone_rejects_existing_or_nested_destination(
    private_runtime: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inside_source: bool
) -> None:
    prefix, target = private_runtime
    monkeypatch.setattr(sys, "platform", "linux")
    destination = prefix / "nested" if inside_source else tmp_path
    with pytest.raises(security.SecurityPatchError, match="destination"):
        security.clone_runtime(destination)
    assert target.read_bytes() == ORIGINAL.read_bytes()


def test_clone_uses_private_executable_and_preserves_original(
    private_runtime: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix, target = private_runtime
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("PYTHONHOME", "untrusted")
    destination = tmp_path / "copied-python"
    calls = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        assert command[0] == str(destination / "bin/python3")
        assert command[1] == "-I"
        assert command[command.index("--prefix") + 1] == str(destination)
        assert "PYTHONHOME" not in kwargs["env"]
        assert "VIRTUAL_ENV" not in kwargs["env"]
        assert (destination / target.relative_to(prefix)).read_bytes() == ORIGINAL.read_bytes()
        return subprocess.CompletedProcess(command, 0, json.dumps({"behavior_passed": True}), "")

    monkeypatch.setattr(subprocess, "run", run)
    security.clone_runtime(destination)
    assert len(calls) == 1
    assert target.read_bytes() == ORIGINAL.read_bytes()
    assert not (prefix / security.RECEIPT_RELATIVE).exists()


def test_verify_runtime_checks_loaded_module_not_only_source(
    private_runtime: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib

    prefix, target = private_runtime
    security.apply_file(target, prefix / security.RECEIPT_RELATIVE)
    patched = load_module(target.read_bytes())
    patched.__file__ = str(target)
    monkeypatch.setattr(urllib, "request", patched, raising=False)
    monkeypatch.setitem(sys.modules, "urllib.request", patched)
    assert security.verify_runtime()["behavior_passed"] is True
    stale = load_module(ORIGINAL.read_bytes())
    stale.__file__ = str(target)
    monkeypatch.setattr(urllib, "request", stale)
    monkeypatch.setitem(sys.modules, "urllib.request", stale)
    with pytest.raises(security.SecurityPatchError, match="scheme"):
        security.verify_runtime()


def test_verify_runtime_rejects_import_shadowing(
    private_runtime: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib

    prefix, target = private_runtime
    security.apply_file(target, prefix / security.RECEIPT_RELATIVE)
    shadow = tmp_path / "shadow.py"
    shadow.write_bytes(target.read_bytes())
    patched = load_module(shadow.read_bytes())
    patched.__file__ = str(shadow)
    monkeypatch.setattr(urllib, "request", patched, raising=False)
    monkeypatch.setitem(sys.modules, "urllib.request", patched)
    with pytest.raises(security.SecurityPatchError, match="outside"):
        security.verify_runtime()
