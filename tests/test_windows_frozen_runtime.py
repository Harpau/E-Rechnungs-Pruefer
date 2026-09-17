from __future__ import annotations

import hashlib
import importlib.util
import stat
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_frozen_runtime", ROOT / "packaging/windows/verify_frozen_runtime.py"
)
assert SPEC and SPEC.loader
frozen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(frozen)


def test_code_comparison_ignores_only_nested_source_filenames() -> None:
    source = "def outer():\n    def inner():\n        return 42\n    return inner\n"
    left = compile(source, "private-build/urllib/request.py", "exec", optimize=1)
    right = compile(source, "urllib/request.py", "exec", optimize=1)
    assert frozen.code_digest(left) == frozen.code_digest(right)
    for changed in (
        right.replace(co_name="other"),
        right.replace(co_firstlineno=2),
        right.replace(co_flags=right.co_flags ^ 1),
        compile(source.replace("42", "41"), "urllib/request.py", "exec", optimize=1),
    ):
        assert frozen.code_digest(left) != frozen.code_digest(changed)


def test_code_comparison_preserves_constant_types() -> None:
    one = compile("value = 1", "a", "exec", optimize=1)
    boolean = compile("value = True", "a", "exec", optimize=1)
    assert frozen.code_digest(one) != frozen.code_digest(boolean)


@pytest.mark.parametrize("changed", [slice(None, 3, None), slice(None, 2, 1), slice(True, 2, None)])
def test_code_comparison_preserves_python314_slice_constants(changed) -> None:
    code = compile("value = 1", "a", "exec", dont_inherit=True)
    left = code.replace(co_consts=(slice(None, 2, None), None))
    right = code.replace(co_consts=(changed, None))
    assert frozen.code_digest(left) != frozen.code_digest(right)


@pytest.mark.parametrize(
    ("platform", "handle_mode", "allowed"),
    [("linux", 0o600, False), ("win32", 0o644, True), ("win32", 0o444, False)],
)
def test_file_identity_masks_only_windows_cross_api_execute_bits(tmp_path, monkeypatch, platform, handle_mode, allowed):
    path = tmp_path / "synthetic.exe"
    path.write_bytes(b"data")
    observed = path.stat()
    common = {field: getattr(observed, field) for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")}
    path_stat = SimpleNamespace(**common, st_mode=stat.S_IFREG | 0o755)
    handle_stat = SimpleNamespace(**common, st_mode=stat.S_IFREG | handle_mode)
    monkeypatch.setattr(Path, "lstat", lambda self: path_stat)
    monkeypatch.setattr(frozen, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(frozen, "os", SimpleNamespace(fstat=lambda _: handle_stat))
    if allowed:
        assert frozen.file_record(path)["size"] == 4
    else:
        with pytest.raises(frozen.FrozenRuntimeError, match="verändert"):
            frozen.file_record(path)


@pytest.fixture
def verified_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "request.py"
    source.write_bytes(b"sentinel = 7\n")
    calls = []

    def regressions(module: ModuleType):
        calls.append(module)
        assert module.sentinel == 7
        return {"passed": True, "cases": ["synthetic"]}

    helper = SimpleNamespace(
        verify_runtime=lambda: {
            "behavior_passed": True,
            "output_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "receipt_sha256": "a" * 64,
        },
        runtime_target=lambda: source,
        run_security_regressions=regressions,
    )
    monkeypatch.setattr(frozen, "security_helper", lambda: helper)
    paths = []
    for name in frozen.EXECUTABLE_NAMES:
        path = tmp_path / name
        path.write_bytes(b"synthetic frozen artifact " + name.encode())
        paths.append(path)
    monkeypatch.setattr(
        frozen,
        "read_frozen_code",
        lambda path: compile(source.read_bytes(), str(path), "exec", dont_inherit=True, optimize=1),
    )
    return helper, paths, source, calls


def test_exact_three_frozen_artifacts_are_verified_and_bound(verified_source) -> None:
    helper, paths, source, calls = verified_source
    result = frozen.verify_binaries(paths)
    assert result["passed"] is True
    assert len(calls) == len(result["artifacts"]) == 3
    assert result["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    for path, record in zip(paths, result["artifacts"], strict=True):
        assert record["name"] == path.name
        assert record["size"] == path.stat().st_size
        assert record["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_real_upstream_backport_code_runs_real_security_regressions(verified_source, monkeypatch) -> None:
    from scripts import cpython_security

    helper, paths, source, _ = verified_source
    original = (ROOT / "tests/fixtures/cpython3147/urllib-request.source").read_bytes()
    source.write_bytes(cpython_security.patch_bytes(original))
    monkeypatch.setattr(helper, "run_security_regressions", cpython_security.run_security_regressions)
    result = frozen.verify_binaries(paths)
    assert result["source_sha256"] == cpython_security.AFTER
    assert all(item["behavior"]["passed"] and item["behavior"]["cases"] for item in result["artifacts"])


def test_old_frozen_code_fails_before_it_is_executed(verified_source, monkeypatch: pytest.MonkeyPatch) -> None:
    _, paths, _, calls = verified_source
    monkeypatch.setattr(frozen, "read_frozen_code", lambda _: compile("raise RuntimeError('unsafe')", "x", "exec"))
    with pytest.raises(frozen.FrozenRuntimeError, match="Code"):
        frozen.verify_binaries(paths)
    assert not calls


@pytest.mark.parametrize("change", ["missing", "duplicate", "extra", "wrong-name"])
def test_artifact_scope_is_exact(verified_source, change: str) -> None:
    _, paths, _, calls = verified_source
    if change == "missing":
        paths.pop()
    elif change == "duplicate":
        paths[-1] = paths[0]
    elif change == "extra":
        paths.append(paths[0])
    else:
        wrong = paths[-1].with_name("other.exe")
        paths[-1].rename(wrong)
        paths[-1] = wrong
    with pytest.raises(frozen.FrozenRuntimeError, match="drei"):
        frozen.verify_binaries(paths)
    assert not calls


def test_failed_runtime_or_source_binding_stops_before_archive_read(verified_source, monkeypatch) -> None:
    helper, paths, _, calls = verified_source
    monkeypatch.setattr(helper, "verify_runtime", lambda: {"behavior_passed": False})
    with pytest.raises(frozen.FrozenRuntimeError, match="Laufzeit"):
        frozen.verify_binaries(paths)
    monkeypatch.setattr(
        helper,
        "verify_runtime",
        lambda: {"behavior_passed": True, "output_sha256": "0" * 64, "receipt_sha256": "a" * 64},
    )
    with pytest.raises(frozen.FrozenRuntimeError, match="Source"):
        frozen.verify_binaries(paths)
    assert not calls


def test_frozen_behavior_failure_is_not_pass(verified_source, monkeypatch) -> None:
    helper, paths, _, _ = verified_source
    monkeypatch.setattr(helper, "run_security_regressions", lambda _: {"passed": False})
    with pytest.raises(frozen.FrozenRuntimeError, match="regression"):
        frozen.verify_binaries(paths)


def test_artifact_change_during_code_verification_is_rejected(verified_source, monkeypatch) -> None:
    helper, paths, _, _ = verified_source

    def change_artifact(_):
        paths[0].write_bytes(b"changed after archive read")
        return {"passed": True}

    monkeypatch.setattr(helper, "run_security_regressions", change_artifact)
    with pytest.raises(frozen.FrozenRuntimeError, match="verändert"):
        frozen.verify_binaries(paths)


def test_cli_does_not_overwrite_existing_evidence(verified_source, tmp_path) -> None:
    _, paths, _, _ = verified_source
    output = tmp_path / "result.json"
    output.write_bytes(b"prior immutable evidence")
    argv = [value for path in paths for value in ("--executable", str(path))]
    assert frozen.main([*argv, "--output", str(output)]) == 1
    assert output.read_bytes() == b"prior immutable evidence"


@pytest.mark.skipif(sys.platform == "win32", reason="Symlink creation may require a Windows privilege")
def test_symlink_artifact_is_rejected(verified_source) -> None:
    _, paths, _, _ = verified_source
    target = paths[0].with_name("actual.exe")
    paths[0].rename(target)
    paths[0].symlink_to(target)
    with pytest.raises(frozen.FrozenRuntimeError, match="datei"):
        frozen.verify_binaries(paths)


@pytest.mark.parametrize("fault", ["missing-pyz", "two-pyz", "wrong-magic", "missing-module", "data", "not-code"])
def test_frozen_archive_missing_or_wrong_payload_fails_closed(tmp_path, monkeypatch, fault) -> None:
    code = compile("value = 1", "x", "exec")
    pyz = SimpleNamespace(toc={"urllib.request": (0, 0, 1)}, extract=lambda _: code)
    archive = SimpleNamespace(
        toc={"PYZ.pyz": (0, 1, 1, 0, "z")},
        extract=lambda _: b"PYZ\0" + importlib.util.MAGIC_NUMBER + b"synthetic",
        open_embedded_archive=lambda _: pyz,
    )
    if fault == "missing-pyz":
        archive.toc = {}
    elif fault == "two-pyz":
        archive.toc["extra.pyz"] = (0, 1, 1, 0, "z")
    elif fault == "wrong-magic":
        archive.extract = lambda _: b"PYZ\0BAD!"
    elif fault == "missing-module":
        pyz.toc = {}
    elif fault == "data":
        pyz.toc["urllib.request"] = (2, 0, 1)
    else:
        pyz.extract = lambda _: b"not code"
    monkeypatch.setattr(frozen, "archive_reader", lambda _: archive)
    with pytest.raises(frozen.FrozenRuntimeError):
        frozen.read_frozen_code(tmp_path / "example.exe")


def test_builder_checks_runtime_before_cleanup_and_frozen_code_before_signing() -> None:
    build = (ROOT / "scripts/build_windows.ps1").read_text(encoding="utf-8")
    runtime = build.index("verify-runtime")
    cleanup = build.index("Remove-Item $BuildRoot")
    pyinstaller = build.index("-m PyInstaller")
    verified = build.index("& $Python $FrozenRuntimeVerifier")
    sign = build.index('Sign-File (Join-Path $DesktopBundle "E-Rechnungs-Pruefer.exe")')
    assert runtime < cleanup < pyinstaller < verified < sign
    assert "if ($LASTEXITCODE -ne 0)" in build[runtime:cleanup]
    assert "if ($LASTEXITCODE -ne 0)" in build[verified:sign]
