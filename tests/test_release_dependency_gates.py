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


def test_ci_runs_new_windows_native_job_and_ingress_regressions_before_package_mutations() -> None:
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["windows-smoke"]
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    for test in (
        "upload_ingress",
        "http_upload",
        "processing_windows",
        "processing_lifecycle",
        "processing_ready",
        "processing_manager",
        "processing_entrypoints",
    ):
        assert "tests/test_" + test + ".py" in commands
    assert commands.index("tests/test_processing_windows.py") < commands.index("acceptance_context.py init-ci")


def test_ci_has_bounded_native_macos_job_with_private_interpreter_and_preserved_evidence() -> None:
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["macos-processing"]
    assert job["runs-on"] == "macos-14"
    assert job["timeout-minutes"] <= 20
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "cpython_security.py clone" in commands and "-m venv" in commands
    assert "tests/test_processing_lifecycle.py" in commands and "tests/test_processing_watchdog.py" in commands
    assert "--junitxml=" in commands
    uploads = [step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")]
    assert len(uploads) == 1 and uploads[0]["if"] == "always()"
    assert uploads[0]["with"]["include-hidden-files"] is True
    assert uploads[0]["with"]["retention-days"] == 14


def test_macos_records_native_address_space_baseline_before_bounded_roles_start() -> None:
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["macos-processing"]
    commands = [step.get("run", "") for step in job["steps"]]
    inventory = next(i for i, command in enumerate(commands) if "virtual_memory_bytes()" in command)
    assert inventory < next(i for i, command in enumerate(commands) if "python -m pytest" in command)
    command = commands[inventory]
    for field in ("platform.machine()", "platform.system()", "platform.python_version()", "platform.python_build()"):
        assert field in command
    assert ".cache/macos-processing/native-baseline.json" in command


@pytest.mark.parametrize("job_name", ["macos-processing", "windows-smoke"])
def test_ci_native_catalog_covers_all_five_cases_and_saves_raw_output(job_name) -> None:
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"][job_name]
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "tests/test_processing_faults.py" in commands
    command = next(step["run"] for step in job["steps"] if "scripts/processing_smoke.py" in step.get("run", ""))
    for case in ("small", "xml_25mib", "cii_large", "ubl_large", "parallel"):
        assert "--case " + case in command
    assert "--output" in command and ".stdout" in command and ".stderr" in command
    if job_name == "windows-smoke":
        assert "if ($LASTEXITCODE -ne 0)" in command


@pytest.mark.parametrize("workflow", ["ci.yml", "release.yml"])
def test_native_package_probes_bind_helpers_and_exact_build_executable(workflow):
    content = (ROOT / ".github/workflows" / workflow).read_text()
    for action, executable in (("desktop", "E-Rechnungs-Pruefer"), ("service-recovery", "E-Rechnungs-Pruefer-Dienst")):
        command = next(line for line in content.splitlines() if "run-ci " in line and "--action " + action in line)
        for helper in ("test_processing_package.py", "test_processing_package.ps1", "processing_smoke.py"):
            assert "--artifact scripts/" + helper in command
        assert f'--artifact "build/windows/bundle/{executable}/{executable}.exe"' in command
