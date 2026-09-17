"""The native catalog must exercise and preserve the exact offline image."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_docker_catalog_runs_every_case_in_the_existing_restricted_container() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/docker.yml").read_text())
    job = workflow["jobs"]["docker"]
    steps = job["steps"]
    catalogs = [step for step in steps if "scripts/processing_smoke.py" in step.get("run", "")]
    assert len(catalogs) == 1
    catalog = catalogs[0]
    assert catalog["if"] == "always() && steps.runtime.outcome == 'success'"
    assert catalog["timeout-minutes"] == 7
    assert catalog["shell"] == "bash"
    command = catalog["run"]
    assert "container_id='${{ steps.runtime.outputs.container_id }}'" in command
    assert 'docker exec "$container_id" python scripts/processing_smoke.py' in command
    assert "docker run" not in command and "--user" not in command and "--network" not in command
    for case in ("small", "xml_25mib", "cii_large", "ubl_large", "hybrid_pdf", "parallel"):
        assert command.count("--case " + case) == 1
    assert "--output /tmp/processing-smoke-evidence/catalog.json" in command
    assert ".stdout.txt" in command and ".stderr.txt" in command
    assert "|| status=$?" in command and 'exit "$status"' in command
    assert 'tarfile.open(fileobj=sys.stdout.buffer, mode="w|")' in command
    assert 'archive.add("/tmp/processing-smoke-evidence", arcname="processing-smoke-evidence")' in command
    assert "processing-smoke-raw.tar" in command
    assert "extract" not in command.lower()
    # Archive/read failures may not turn a failed catalog into success.
    assert command.count("|| status=1") == 2
    runtime = next(step["run"] for step in steps if step.get("id") == "runtime")
    assert "--network none --read-only --cap-drop ALL" in runtime
    assert "--security-opt no-new-privileges --tmpfs /tmp:rw,nosuid,nodev,size=256m" in runtime
    assert {entry["arch"] for entry in job["strategy"]["matrix"]["include"]} == {"amd64", "arm64"}


def test_active_lifecycle_catalog_is_independent_of_os_audit_and_preserves_failed_evidence() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/docker.yml").read_text())
    matches = [
        step
        for step in workflow["jobs"]["docker"]["steps"]
        if "scripts/processing_lifecycle_probe.py" in step.get("run", "")
    ]
    assert len(matches) == 1
    step = matches[0]
    assert step["if"] == "always() && steps.runtime.outcome == 'success'"
    assert step["timeout-minutes"] == 7
    command = step["run"]
    assert 'docker exec "$container_id" python scripts/processing_lifecycle_probe.py' in command
    assert "docker run" not in command and "--privileged" not in command
    assert "--output /tmp/processing-lifecycle-evidence/catalog.json" in command
    assert 'archive.add("/tmp/processing-lifecycle-evidence", arcname="processing-lifecycle-evidence")' in command
    assert "processing-lifecycle-raw.tar" in command and "extract" not in command.lower()
    assert command.count("|| status=1") == 2 and "|| status=$?" in command
    assert 'exit "$status"' in command


def test_source_platforms_preserve_active_lifecycle_proof_with_existing_job_artifacts() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    for job_name, folder in (("macos-processing", "macos-processing"), ("windows-smoke", "dependency-evidence")):
        steps = workflow["jobs"][job_name]["steps"]
        commands = [s["run"] for s in steps if "scripts/processing_lifecycle_probe.py" in s.get("run", "")]
        assert len(commands) == 1
        assert f"--output .cache/{folder}/active-lifecycle.json" in commands[0]
        assert "active-lifecycle.stdout" in commands[0] and "active-lifecycle.stderr" in commands[0]
        assert any(
            s.get("if") == "always()" and f".cache/{folder}/" in s.get("with", {}).get("path", "") for s in steps
        )


def test_macos_checks_inherited_watchdog_startup_before_invoice_catalogs() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["macos-processing"]["steps"]
    probe = next(step for step in steps if "scripts/processing_watchdog_probe.py" in step.get("run", ""))
    assert probe["timeout-minutes"] == 1
    assert "continue-on-error" not in probe and "if" not in probe
    assert "--output .cache/macos-processing/watchdog-startup.json" in probe["run"]
    assert "watchdog-startup.stdout" in probe["run"] and "watchdog-startup.stderr" in probe["run"]
    assert "setsid" not in probe["run"]
    for step in steps:
        if "python -m pytest" in step.get("run", "") or "scripts/processing_smoke.py" in step.get("run", ""):
            assert steps.index(probe) < steps.index(step)


def test_real_java_calibration_uses_the_bound_image_and_prepared_windows_components() -> None:
    docker = yaml.safe_load((ROOT / ".github/workflows/docker.yml").read_text())
    steps = docker["jobs"]["docker"]["steps"]
    step = next(s for s in steps if "scripts/processing_kosit_probe.py" in s.get("run", ""))
    assert step["if"] == "always() && steps.runtime.outcome == 'success'"
    assert step["timeout-minutes"] == 7
    command = step["run"]
    assert 'docker exec "$container_id" python scripts/processing_kosit_probe.py' in command
    assert "--vendor-root /app/vendor/kosit --java /usr/bin/java" in command
    assert "--config-archive '/locked-components/${{ steps.runtime.outputs.config_archive }}'" in command
    assert "processing-kosit-raw.tar" in command and "processing-kosit.json" in command
    assert command.count("|| status=1") == 2 and 'exit "$status"' in command
    assert "docker run" not in command and "extract" not in command.lower()
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = ci["jobs"]["windows-smoke"]["steps"]
    probe = next(s for s in steps if "scripts/processing_kosit_probe.py" in s.get("run", ""))
    preparation = next(s for s in steps if "python scripts/prepare_windows_components.py" in s.get("run", ""))
    build = next(s for s in steps if "scripts\\build_windows.ps1" in s.get("run", ""))
    assert steps.index(preparation) < steps.index(probe) < steps.index(build)
    assert probe["timeout-minutes"] == 7 and "continue-on-error" not in probe
    assert "--config-archive $archive" in probe["run"] and "--java runtime/java/bin/java.exe" in probe["run"]


def test_completed_windows_build_is_preserved_if_later_package_acceptance_fails() -> None:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = ci["jobs"]["windows-smoke"]["steps"]
    build = next(step for step in steps if step.get("id") == "windows_build")
    artifact = next(step for step in steps if step.get("with", {}).get("name", "").startswith("windows-x64-package-"))
    assert "scripts\\build_windows.ps1" in build["run"]
    assert artifact["if"] == "always() && steps.windows_build.outcome == 'success'"
    assert artifact["with"]["if-no-files-found"] == "error"
    paths = artifact["with"]["path"]
    for suffix in (
        "Windows-x64-Setup.exe",
        "Windows-x64-Dienst-Setup.exe",
        "Windows-x64-Binaries.zip",
        "Windows-x64-SHA256SUMS.txt",
    ):
        assert suffix in paths
