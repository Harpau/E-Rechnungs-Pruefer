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
