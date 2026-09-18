from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from scripts import windows_package_diagnostic as diagnostic

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "field,value",
    [
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("GITHUB_JOB", "windows-smoke"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_REPOSITORY", "synthetic/other"),
        ("GITHUB_REF", "refs/heads/main"),
        ("EINVOICE_WINDOWS_DIAGNOSTIC_ONLY", "false"),
        ("RUNNER_OS", "Linux"),
    ],
)
def test_diagnostic_requires_explicit_first_attempt_in_bound_job(monkeypatch, field, value):
    environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_JOB": "windows-diagnostic",
        "GITHUB_REPOSITORY": diagnostic.REPOSITORY,
        "GITHUB_REF": "refs/heads/codex/dependency-maintenance-2026-09",
        "GITHUB_RUN_ATTEMPT": "1",
        "RUNNER_OS": "Windows",
        "RUNNER_ARCH": "X64",
        "EINVOICE_WINDOWS_DIAGNOSTIC_ONLY": "true",
    }
    for name, expected in environment.items():
        monkeypatch.setenv(name, expected)
    diagnostic.diagnostic_environment()
    monkeypatch.setenv(field, value)
    with pytest.raises(diagnostic.DiagnosticError):
        diagnostic.diagnostic_environment()


@pytest.mark.parametrize(
    "failure", ["expired", "future", "controller", "blocked", "binding", "action", "status", "multiple"]
)
def test_diagnostic_requires_one_current_consumed_context(monkeypatch, failure):
    now = datetime.now(UTC)
    context = {
        "status": "RUNNING",
        "action": "desktop",
        "consumed_at_utc": diagnostic.acceptance.timestamp(now - timedelta(minutes=1)),
        "expires_at_utc": diagnostic.acceptance.timestamp(now + timedelta(minutes=1)),
    }
    state = {
        "blocked": False,
        "controller": "synthetic-controller",
        "binding": {"run": "1"},
        "contexts": {"one": context},
    }
    monkeypatch.setenv("EINVOICE_ACCEPTANCE_CONTROLLER", "synthetic-controller")
    monkeypatch.setattr(diagnostic, "diagnostic_environment", lambda: None)
    monkeypatch.setattr(diagnostic.acceptance, "verify", lambda _: state)
    monkeypatch.setattr(diagnostic.acceptance, "ci_binding", lambda _: {"run": "1"})
    assert diagnostic.active_context() == context
    if failure == "expired":
        context["expires_at_utc"] = diagnostic.acceptance.timestamp(now - timedelta(seconds=1))
    elif failure == "future":
        context["consumed_at_utc"] = diagnostic.acceptance.timestamp(now + timedelta(seconds=10))
    elif failure in {"controller", "binding"}:
        state[failure] = "different"
    elif failure == "blocked":
        state["blocked"] = True
    elif failure == "multiple":
        state["contexts"]["two"] = context.copy()
    elif failure == "action":
        context["action"] = "service-recovery"
    else:
        context["status"] = "PASS"
    with pytest.raises(diagnostic.DiagnosticError):
        diagnostic.active_context()


def metadata() -> dict:
    return {
        "id": diagnostic.ARTIFACT_ID,
        "name": diagnostic.ARTIFACT_NAME,
        "size_in_bytes": diagnostic.ARCHIVE_BYTES,
        "digest": "sha256:" + diagnostic.ARCHIVE_SHA256,
        "expired": False,
        "workflow_run": {"id": diagnostic.SOURCE_RUN, "head_sha": diagnostic.SOURCE_COMMIT},
    }


@pytest.mark.parametrize("field,value", [("expired", True), ("id", 123), ("digest", "sha256:" + "0" * 64)])
def test_diagnostic_rejects_wrong_or_expired_original_metadata(field, value):
    data = metadata()
    data[field] = value
    with pytest.raises(diagnostic.DiagnosticError):
        diagnostic.verify_metadata(data)


def test_diagnostic_rejects_different_source_run_or_candidate():
    data = metadata()
    diagnostic.verify_metadata(data)
    data["workflow_run"]["head_sha"] = "0" * 40
    with pytest.raises(diagnostic.DiagnosticError):
        diagnostic.verify_metadata(data)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/drive", "bundle/file:stream", "a\\escape"])
def test_diagnostic_archive_rejects_unsafe_members(name):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as z:
        z.writestr(name, b"synthetic")
    with zipfile.ZipFile(content) as z, pytest.raises(diagnostic.DiagnosticError):
        diagnostic.validate_members(z)


def test_diagnostic_archive_rejects_case_collision_and_symlinks():
    for names in (("a", "A"), ("link",)):
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as z:
            for name in names:
                info = zipfile.ZipInfo(name)
                if name == "link":
                    info.create_system = 3
                    info.external_attr = 0o120777 << 16
                z.writestr(info, b"synthetic")
        with zipfile.ZipFile(content) as z, pytest.raises(diagnostic.DiagnosticError):
            diagnostic.validate_members(z)


def test_diagnostic_extracts_only_verified_bytes_and_never_overwrites(tmp_path):
    content = io.BytesIO()
    payload = b"synthetic executable bytes, never run"
    with zipfile.ZipFile(content, "w") as z:
        z.writestr("owned.exe", payload)
    target = tmp_path / "owned.exe"
    with zipfile.ZipFile(content) as z:
        diagnostic.copy_member(z, "owned.exe", target, hashlib.sha256(payload).hexdigest())
        assert target.read_bytes() == payload
        with pytest.raises(FileExistsError):
            diagnostic.copy_member(z, "owned.exe", target, hashlib.sha256(payload).hexdigest())
        with pytest.raises(diagnostic.DiagnosticError):
            diagnostic.copy_member(z, "owned.exe", tmp_path / "wrong.exe", "0" * 64)


def test_diagnostic_context_requires_exact_command_and_every_bound_file(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostic, "ROOT", tmp_path)
    setup = tmp_path / diagnostic.SETUP_RELATIVE
    paths = diagnostic.required_paths(setup)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
    monkeypatch.setattr(diagnostic, "DESKTOP_SHA256", hashlib.sha256(b"synthetic").hexdigest())
    monkeypatch.setattr(diagnostic, "DESKTOP_EXE_SHA256", hashlib.sha256(b"synthetic").hexdigest())
    context = {
        "action": "desktop",
        "command": diagnostic.expected_command(setup),
        "artifacts": [diagnostic.acceptance._file(path) for path in paths],
    }
    monkeypatch.setattr(diagnostic, "active_context", lambda: context)
    assert diagnostic.verify_scope(setup)["cases"] == ["held-responses", "health"]
    context["command"] = context["command"][:-1]
    with pytest.raises(diagnostic.DiagnosticError):
        diagnostic.verify_scope(setup)
    context["command"] = diagnostic.expected_command(setup)
    context["artifacts"].pop()
    with pytest.raises(diagnostic.DiagnosticError):
        diagnostic.verify_scope(setup)


def test_diagnostic_workflow_is_manual_exclusive_and_never_builds_or_publishes():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]
    job = jobs["windows-diagnostic"]
    assert job["needs"] == "dispatch-inputs"
    assert "workflow_dispatch" in job["if"] and "windows_diagnostic_only" in job["if"]
    assert "prepare_dependencies != true" in job["if"] and "container_only != true" in job["if"]
    assert job["timeout-minutes"] <= 30
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ inputs.windows_diagnostic_only != true }}"
    for name in ("quality", "tests", "macos-processing", "windows-smoke", "docker", "prepare-dependencies"):
        assert "windows_diagnostic_only != true" in jobs[name]["if"]
    scripts = "\n".join(step.get("run", "") for step in job["steps"])
    assert "build_windows" not in scripts and "gh release" not in scripts
    assert "windows_package_diagnostic.py prepare" in scripts
    assert "-ProcessingDiagnosticOnly" in scripts and "acceptance_context.py run-ci" in scripts
    assert "windows_package_diagnostic.py" in scripts and "input-binding.json" in scripts
    assert any(step.get("if") == "always()" and "upload-artifact@" in step.get("uses", "") for step in job["steps"])
