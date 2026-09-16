from __future__ import annotations

import itertools
import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/dependency-audit.yml").read_text(encoding="utf-8"))


def scripts(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job.get("steps", []))


def test_audit_matrix_covers_all_os_python_and_dependency_profiles() -> None:
    job = workflow()["jobs"]["environments"]
    matrix = job["strategy"]["matrix"]
    actual = set(itertools.product(matrix["os"], matrix["python-version"], matrix["profile"]))
    expected = set(
        itertools.product(
            ["ubuntu-24.04", "windows-2022", "macos-14"],
            ["3.11", "3.12", "3.13", "3.14"],
            ["runtime", "dev"],
        )
    )
    assert actual == expected
    assert job["strategy"]["fail-fast"] is False
    assert "exclude" not in matrix
    script = scripts(job)
    assert "venv" in script
    assert "pip==26.2.1" in script
    assert "setuptools==84.0.0" in script
    assert "wheel==0.48.0" in script
    assert "--no-build-isolation" in script
    assert '"capture"' in script
    assert '"audit"' in script


def test_matrix_capture_inventories_uses_each_target_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    job = workflow()["jobs"]["environments"]
    step = next(step for step in job["steps"] if step.get("name") == "Capture complete installed inventories")
    monkeypatch.setenv("TARGET_PY", "/target/python")
    monkeypatch.setenv("AUDITOR_PY", "/tools/python")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    exec(compile(step["run"], "capture-inventories", "exec"), {})
    assert calls == [
        (
            [
                "/target/python",
                "scripts/dependency_audit.py",
                "capture",
                "--output",
                "audit-evidence/target-inventory.json",
            ],
            {"check": True},
        ),
        (
            [
                "/tools/python",
                "scripts/dependency_audit.py",
                "capture",
                "--output",
                "audit-evidence/tools-inventory.json",
            ],
            {"check": True},
        ),
    ]


def test_matrix_audits_tools_even_when_target_audit_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job = workflow()["jobs"]["environments"]
    step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Audit target and auditor without suppressing either result"
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "audit-evidence").mkdir()
    monkeypatch.setenv("AUDITOR_PY", "/tools/python")
    calls = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1 if len(calls) == 1 else 0)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit) as error:
        exec(compile(step["run"], "audit-inventories", "exec"), {})
    assert error.value.code == 1
    assert [command[command.index("--inventory") + 1] for command, _ in calls] == [
        "audit-evidence/target-inventory.json",
        "audit-evidence/tools-inventory.json",
    ]
    assert all(command[0] == "/tools/python" and kwargs == {"check": False} for command, kwargs in calls)
    assert json.loads((tmp_path / "audit-evidence/outcomes.json").read_text()) == {"target": 1, "tools": 0}


def test_locked_audits_are_independent_and_include_native_inventory_check() -> None:
    jobs = workflow()["jobs"]
    windows = jobs["windows-lock"]
    source = jobs["source-lock"]
    assert "needs" not in windows
    assert "needs" not in source
    assert "dependency_lock.py check" in scripts(windows)
    assert "pip_audit --strict --disable-pip --require-hashes" in scripts(windows)
    assert "-r packaging/windows/requirements-release.txt" in scripts(windows)
    assert "--require-hashes --only-binary=:all:" in scripts(source)
    assert "--no-deps --no-build-isolation" in scripts(source)
    assert "dependency_lock.py verify" in scripts(source)
    assert "--installed" in scripts(source)
    assert "dependency_audit.py capture" in scripts(source)
    assert "dependency_audit.py audit" in scripts(source)
    assert any(step.get("with", {}).get("python-version") == "3.14.7" for step in source["steps"])


def test_every_profile_is_required_even_when_a_sibling_fails_or_is_skipped() -> None:
    jobs = workflow()["jobs"]
    gate = jobs["audit-gate"]
    assert set(gate["needs"]) == set(jobs) - {"audit-gate"}
    assert gate["if"] == "always()"
    assert jobs["docker"]["uses"] == "./.github/workflows/docker.yml"
    assert "secrets" not in jobs["docker"]


@pytest.mark.parametrize("status", ["failure", "cancelled", "skipped", "missing"])
def test_gate_script_rejects_non_success_and_missing_jobs(status: str, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = workflow()["jobs"]["audit-gate"]
    needs = {name: {"result": "success"} for name in gate["needs"]}
    if status == "missing":
        needs.pop("windows-lock")
    else:
        needs["windows-lock"]["result"] = status
    monkeypatch.setenv("NEEDS_JSON", json.dumps(needs))
    with pytest.raises(SystemExit) as error:
        exec(compile(scripts(gate), "audit-gate", "exec"), {})
    assert error.value.code == 1


def test_gate_script_accepts_complete_success(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = workflow()["jobs"]["audit-gate"]
    monkeypatch.setenv("NEEDS_JSON", json.dumps({name: {"result": "success"} for name in gate["needs"]}))
    exec(compile(scripts(gate), "audit-gate", "exec"), {})


def test_audit_events_permissions_and_failure_evidence() -> None:
    document = workflow()
    events = document.get("on", document.get(True))
    assert {"pull_request", "push", "workflow_dispatch", "schedule"} <= events.keys()
    assert events["push"]["branches"] == ["main"]
    assert events["schedule"] == [{"cron": "31 5 * * 2"}]
    assert "pull_request_target" not in events
    assert document["permissions"] == {"contents": "read"}
    for name, job in document["jobs"].items():
        if name in {"docker", "audit-gate"}:
            continue
        uploads = [step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")]
        assert uploads, name
        assert all(step.get("if") == "always()" for step in uploads)
        assert all(step["with"]["retention-days"] == 14 for step in uploads)
        assert "--ignore-vuln" not in scripts(job)
        assert not job.get("continue-on-error")
        assert not any(step.get("continue-on-error") for step in job["steps"])


def test_embedded_python_steps_compile() -> None:
    for name, job in workflow()["jobs"].items():
        for step in job.get("steps", []):
            if step.get("shell") == "python":
                compile(step["run"], f"{name}/{step.get('name', '')}", "exec")
