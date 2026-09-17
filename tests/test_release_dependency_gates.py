"""Release jobs must use the reviewed dependency set before package mutations."""

import json
import os
import subprocess
import sys
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


def test_ci_has_bounded_native_macos_source_job_and_preserved_evidence() -> None:
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["macos-processing"]
    assert job["runs-on"] == "macos-14"
    assert job["timeout-minutes"] <= 20
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    setup = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/setup-python@"))
    assert setup["with"]["python-version"] == "3.14.7"
    assert "cpython_security.py" not in commands and "-m venv" in commands
    assert "tests/test_processing_lifecycle.py" in commands and "tests/test_processing_watchdog.py" in commands
    assert "--junitxml=" in commands
    uploads = [step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")]
    assert len(uploads) == 1 and uploads[0]["if"] == "always()"
    assert uploads[0]["with"]["include-hidden-files"] is True
    assert uploads[0]["with"]["retention-days"] == 14


@pytest.mark.skipif(sys.platform == "win32", reason="macOS workflow uses the POSIX bash runner")
def test_macos_venv_failure_preserves_bound_startup_inventory_and_diagnostics(tmp_path: Path) -> None:
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["macos-processing"]
    command = next(step["run"] for step in job["steps"] if "startup-inventory.json" in step.get("run", ""))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python = fake_bin / "python"
    python.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "if sys.argv[1:3] == ['-m', 'venv']:\n"
        "    print('synthetic venv preparation failure', file=sys.stderr)\n"
        "    raise SystemExit(42)\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
    )
    python.chmod(0o755)
    context = {
        "GITHUB_REPOSITORY": "synthetic/invoice-checker",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_JOB": "macos-processing",
    }
    path_file = tmp_path / "github-path"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", command],
        cwd=tmp_path,
        env={
            **os.environ,
            **context,
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "GITHUB_PATH": str(path_file),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 42
    evidence = tmp_path / ".cache/macos-processing"
    inventory = json.loads((evidence / "startup-inventory.json").read_text())
    assert inventory["github_context"] == context
    assert inventory["scope"] == "native macOS source processing probe; no release runtime backport receipt"
    assert inventory["cpython_security_backport_applied"] is False
    assert inventory["python_version"] == sys.version.split()[0]
    assert inventory["python_executable"] == sys.executable
    assert inventory["python_base_prefix"] == sys.base_prefix
    assert "synthetic venv preparation failure" in (evidence / "preparation.stderr").read_text()
    assert not path_file.exists()


def test_macos_records_native_address_space_baseline_before_bounded_roles_start() -> None:
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["macos-processing"]
    commands = [step.get("run", "") for step in job["steps"]]
    inventory = next(i for i, command in enumerate(commands) if "virtual_memory_bytes()" in command)
    assert inventory < next(i for i, command in enumerate(commands) if "python -m pytest" in command)
    command = commands[inventory]
    for field in ("platform.machine()", "platform.system()", "platform.python_version()", "platform.python_build()"):
        assert field in command
    assert ".cache/macos-processing/native-baseline.json" in command


def test_macos_requires_bounded_heap_and_mmap_enforcement_before_native_catalog() -> None:
    steps = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["macos-processing"]["steps"]
    index = next(i for i, step in enumerate(steps) if "scripts/processing_probe.py" in step.get("run", ""))
    step = steps[index]
    assert not step.get("continue-on-error", False)
    assert step.get("if") is None
    command = step["run"]
    assert "--case address_space" in command and "--case heap" in command
    assert ".cache/macos-processing/address-space.json" in command
    assert "address-space.stdout" in command and "address-space.stderr" in command
    assert index < next(i for i, step in enumerate(steps) if "python -m pytest" in step.get("run", ""))
    assert index < next(i for i, step in enumerate(steps) if "scripts/processing_smoke.py" in step.get("run", ""))


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
