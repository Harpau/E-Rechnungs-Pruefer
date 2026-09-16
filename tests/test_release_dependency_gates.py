"""Release jobs must use the reviewed dependency set before package mutations."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("workflow", ["ci.yml", "release.yml"])
def test_windows_mutations_are_bound_before_the_first_installer_run(workflow: str) -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"]
    job = jobs["windows-smoke" if workflow == "ci.yml" else "windows-release"]
    commands = [step.get("run", "") for step in job["steps"]]
    initialization = next(i for i, command in enumerate(commands) if "acceptance_context.py init-ci" in command)
    tests = [(i, command) for i, command in enumerate(commands) if "acceptance_context.py run-ci" in command]
    assert len(tests) == 3
    assert all(initialization < i for i, _ in tests)
    for _, command in tests:
        assert "--artifact" in command
        assert "--script scripts/test_windows_" in command
        assert "--evidence" in command
        assert "-ConfirmIsolatedEnvironment" in command
        if workflow == "release.yml":
            assert "-RequireSignature" in command
    assert any("acceptance_context.py verify" in command for command in commands)


@pytest.mark.parametrize("workflow,job_name", [("ci.yml", "quality"), ("release.yml", "source-release")])
def test_source_build_uses_frozen_dependencies_and_checks_inventory_after_build(workflow: str, job_name: str) -> None:
    job = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"][job_name]
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "--require-hashes --only-binary=:all:" in commands
    assert "-r packaging/python/requirements-source-release.txt" in commands
    assert "--no-deps --no-build-isolation -e ." in commands
    assert "--upgrade pip" not in commands
    assert commands.index("scripts/build_release.py") < commands.rindex("dependency_audit.py capture")
    assert "dependency_lock.py verify" in commands
    assert "--installed" in commands
    assert "--force-reinstall" in commands
    assert "-m venv .venv" in commands


def test_source_distribution_includes_all_native_lock_profiles() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text()
    assert "recursive-include packaging *.txt *.json *.spec *.iss" in manifest
