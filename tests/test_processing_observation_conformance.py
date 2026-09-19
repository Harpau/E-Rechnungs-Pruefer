"""Receipt guard regressions; these tests never claim native Windows evidence."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import processing_observation_conformance as conformance


@pytest.fixture
def passing_receipt():
    binding = {
        "commit": "a" * 40,
        "workflow_run": "123",
        "workflow_attempt": "1",
        "job": "windows-smoke",
        "runner_os": "Windows",
        "runner_name": "synthetic-runner",
    }
    sources = {"synthetic.py": {"size": 1, "sha256": "b" * 64}}
    catalog = conformance.case_catalog()
    tests = []
    for case in catalog:
        proof = None
        if case["native"]:
            proof = {
                "method": "real-processing-owner",
                "parent": {"pid": 100, "creation_time": 1000},
                "cleanup_confirmed": True,
                "lease_released": True,
                "roles": [
                    {"role": "supervisor", "pid": 101, "parent_pid": 100, "creation_time": 1001, "exit_code": 0},
                    {"role": "worker", "pid": 102, "parent_pid": 100, "creation_time": 1002, "exit_code": 0},
                ],
            }
        tests.append(
            {
                "nodeid": case["nodeid"],
                "phases": {"setup": "passed", "call": "passed", "teardown": "passed"},
                "xfail": False,
                "native_proof": proof,
                "native_failure": None,
            }
        )
    receipt = {
        "schema_version": 1,
        "status": "PASS",
        "platform": "win32",
        "binding": binding,
        "sources": sources,
        "catalog": catalog,
        "pytest": {"exit_code": 0, "collected": [case["nodeid"] for case in catalog], "tests": tests},
    }
    return receipt, binding, sources


def test_only_exact_complete_native_catalog_opens_guard(passing_receipt):
    receipt, binding, sources = passing_receipt
    conformance.validate_receipt(receipt, binding=binding, sources=sources)
    assert sum(case["native"] for case in receipt["catalog"]) >= 4


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
@pytest.mark.parametrize("outcome", ["skipped", "failed", None])
def test_no_skip_failure_or_missing_phase_can_open_guard(passing_receipt, phase, outcome):
    receipt, binding, sources = passing_receipt
    phases = receipt["pytest"]["tests"][0]["phases"]
    if outcome is None:
        del phases[phase]
    else:
        phases[phase] = outcome
    with pytest.raises(conformance.ConformanceError):
        conformance.validate_receipt(receipt, binding=binding, sources=sources)


@pytest.mark.parametrize("change", ["xfail", "missing", "extra", "duplicate", "collected", "exit", "platform"])
def test_zero_pytest_exit_is_not_complete_native_evidence(passing_receipt, change):
    receipt, binding, sources = passing_receipt
    if change == "xfail":
        receipt["pytest"]["tests"][0]["xfail"] = True
    elif change == "missing":
        receipt["pytest"]["tests"].pop()
    elif change == "extra":
        receipt["pytest"]["tests"].append({"nodeid": "foreign"})
    elif change == "duplicate":
        receipt["pytest"]["tests"][-1] = copy.deepcopy(receipt["pytest"]["tests"][0])
    elif change == "collected":
        receipt["pytest"]["collected"].pop()
    elif change == "exit":
        receipt["pytest"]["exit_code"] = 1
    elif change == "platform":
        receipt["platform"] = "darwin"
    with pytest.raises(conformance.ConformanceError):
        conformance.validate_receipt(receipt, binding=binding, sources=sources)


@pytest.mark.parametrize("change", ["cleanup", "released", "creation", "pid", "exit_code", "role", "parent", "missing"])
def test_native_fixture_cleanup_and_creation_binding_are_required(passing_receipt, change):
    receipt, binding, sources = passing_receipt
    case = next(item for item in receipt["pytest"]["tests"] if item["native_proof"])
    proof = case["native_proof"]
    if change == "cleanup":
        proof["cleanup_confirmed"] = False
    elif change == "released":
        proof["lease_released"] = False
    elif change == "creation":
        proof["roles"][0]["creation_time"] = None
    elif change == "pid":
        proof["roles"][1]["pid"] = proof["roles"][0]["pid"]
    elif change == "exit_code":
        proof["roles"][0]["exit_code"] = None
    elif change == "role":
        proof["roles"][0]["role"] = "unbound"
    elif change == "parent":
        proof["parent"]["creation_time"] = 1003
    else:
        case["native_proof"] = None
    with pytest.raises(conformance.ConformanceError):
        conformance.validate_receipt(receipt, binding=binding, sources=sources)


@pytest.mark.parametrize("field", ["commit", "workflow_run", "workflow_attempt", "job", "runner_os", "runner_name"])
def test_receipt_cannot_cross_candidate_run_or_runner(passing_receipt, field):
    receipt, binding, sources = passing_receipt
    changed = {**binding, field: "different"}
    with pytest.raises(conformance.ConformanceError):
        conformance.validate_receipt(receipt, binding=changed, sources=sources)


def test_source_or_catalog_change_invalidates_passing_receipt(passing_receipt):
    receipt, binding, sources = passing_receipt
    with pytest.raises(conformance.ConformanceError):
        conformance.validate_receipt(receipt, binding=binding, sources={})
    receipt["catalog"][0]["id"] = "different"
    with pytest.raises(conformance.ConformanceError):
        conformance.validate_receipt(receipt, binding=binding, sources=sources)


def test_local_platform_cannot_start_native_conformance(monkeypatch, tmp_path):
    monkeypatch.setattr(conformance.sys, "platform", "darwin")
    with pytest.raises(conformance.ConformanceError, match="Windows"):
        conformance.run(tmp_path / "native")
    assert not (tmp_path / "native").exists()


@pytest.mark.parametrize("expected", [None, "", "a" * 40])
def test_current_windows_binding_accepts_exact_commit_or_regular_ci_without_opt_in(monkeypatch, expected):
    monkeypatch.setattr(conformance.sys, "platform", "win32")
    for key, value in {
        "RUNNER_OS": "Windows",
        "RUNNER_NAME": "synthetic-runner",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_JOB": "windows-smoke",
    }.items():
        monkeypatch.setenv(key, value)
    if expected is None:
        monkeypatch.delenv("EINVOICE_CI_EXPECTED_COMMIT", raising=False)
    else:
        monkeypatch.setenv("EINVOICE_CI_EXPECTED_COMMIT", expected)
    monkeypatch.setattr(conformance.subprocess, "check_output", lambda *args, **kwargs: "a" * 40 + "\n")
    binding = conformance.current_binding()
    assert binding["commit"] == "a" * 40
    assert binding["workflow_attempt"] == "2", "ordinary CI retains its own positive attempt binding"


@pytest.mark.parametrize("expected", ["b" * 40, "A" * 40, "a" * 39, "a" * 41, " " + "a" * 40, "a" * 40 + "\n"])
def test_expected_commit_mismatch_or_noncanonical_value_fails_before_checkout_query(monkeypatch, expected):
    monkeypatch.setattr(conformance.sys, "platform", "win32")
    for key, value in {
        "RUNNER_OS": "Windows",
        "RUNNER_NAME": "synthetic-runner",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "windows-smoke",
        "EINVOICE_CI_EXPECTED_COMMIT": expected,
    }.items():
        monkeypatch.setenv(key, value)
    queries = []
    monkeypatch.setattr(
        conformance.subprocess, "check_output", lambda *args, **kwargs: queries.append(args) or "a" * 40 + "\n"
    )
    with pytest.raises(conformance.ConformanceError, match="Erwarteter"):
        conformance.current_binding()
    assert queries == []


@pytest.mark.parametrize("expected,success", [("", True), ("a" * 40, True), ("b" * 40, False), ("A" * 40, False)])
def test_dispatch_commit_guard_runs_the_bound_workflow_code_without_github_mutations(expected, success):
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    trigger = workflow.get("on", workflow.get(True))
    declared = trigger["workflow_dispatch"]["inputs"]["expected_commit"]
    assert declared["type"] == "string" and declared["default"] == ""
    assert declared.get("required", False) is False
    assert workflow["jobs"]["windows-smoke"]["env"]["EINVOICE_CI_EXPECTED_COMMIT"] == "${{ inputs.expected_commit }}"
    guard = next(step for step in workflow["jobs"]["dispatch-inputs"]["steps"] if step.get("id") == "expected_commit")
    assert guard["shell"] == "python"
    assert guard["env"] == {"EXPECTED_COMMIT": "${{ inputs.expected_commit }}", "ACTUAL_COMMIT": "${{ github.sha }}"}
    result = subprocess.run(
        [sys.executable, "-I", "-c", guard["run"]],
        env={**os.environ, "EXPECTED_COMMIT": expected, "ACTUAL_COMMIT": "a" * 40},
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert (result.returncode == 0) is success


@pytest.mark.parametrize("changed", [None, "result.json", "pytest-report.json", "pytest.stdout.log", "files.json"])
def test_install_guard_verifies_raw_evidence_before_accepting_receipt(passing_receipt, monkeypatch, tmp_path, changed):
    receipt, binding, sources = passing_receipt
    monkeypatch.setattr(conformance, "current_binding", lambda: binding)
    monkeypatch.setattr(conformance, "source_inventory", lambda: sources)
    conformance._write(tmp_path / "result.json", receipt)
    conformance._write(tmp_path / "pytest-report.json", receipt["pytest"])
    (tmp_path / "pytest.stdout.log").write_bytes(b"synthetic pytest output")
    (tmp_path / "pytest.stderr.log").write_bytes(b"")
    inventory = {path.name: conformance._file(path) for path in tmp_path.iterdir()}
    conformance._write(tmp_path / "files.json", inventory)
    if changed:
        (tmp_path / changed).write_text("{}", encoding="utf-8")
        with pytest.raises(conformance.ConformanceError):
            conformance.verify(tmp_path)
    else:
        conformance.verify(tmp_path)


def test_plugin_preserves_setup_fixture_failure_and_does_not_invent_call_pass():
    plugin = conformance.CatalogPlugin()
    report = SimpleNamespace(
        nodeid="synthetic-native-case",
        when="setup",
        outcome="failed",
        user_properties=[
            (
                "native_observation_failure",
                json.dumps({"primary_error": "SyntheticFailure", "cleanup_errors": ["CloseFailure"]}),
            )
        ],
    )
    plugin.pytest_runtest_logreport(report)
    result = plugin.result(1)["tests"][0]
    assert result["phases"] == {"setup": "failed"}
    assert result["native_failure"] == {"primary_error": "SyntheticFailure", "cleanup_errors": ["CloseFailure"]}
    assert result["native_proof"] is None


def test_plugin_records_xpass_and_duplicate_phase_as_nonpassing():
    plugin = conformance.CatalogPlugin()
    report = SimpleNamespace(nodeid="synthetic", when="call", outcome="passed", user_properties=[], wasxfail="expected")
    plugin.pytest_runtest_logreport(report)
    plugin.pytest_runtest_logreport(report)
    result = plugin.result(0)["tests"][0]
    assert result["xfail"] is True and result["phases"] == {"call": "duplicate"}


def test_native_fixture_accepts_authoritative_tree_cleanup_without_child_finish_cache():
    from tests.test_processing_observation_native import require_child_cleanup

    # ProcessTree.cleanup reaps/closes directly; Child.finish's optional cache
    # remains None on this normal path and is not a cleanup authority.
    child = SimpleNamespace(
        _finish_result=None,
        _channels_closed=True,
        _channel_close_failed=False,
        process=SimpleNamespace(returncode=0, _handle=0),
        job=SimpleNamespace(_handle=0),
    )
    require_child_cleanup(child)
    child.process._handle = 123
    with pytest.raises(AssertionError):
        require_child_cleanup(child)


def test_creation_query_failure_does_not_steal_child_from_manager_cleanup():
    from tests.test_processing_observation_native import remember_spawned_child

    def creation_failure():
        raise OSError("synthetic creation query failure")

    child = SimpleNamespace(pid=42, process=SimpleNamespace(creation_time=creation_failure))
    created, errors = {}, []
    assert remember_spawned_child(created, "worker", child, errors) is child
    assert created["worker"] == {"pid": 42, "creation_time": None, "child": child}
    assert errors == [{"role": "worker", "error_class": "OSError"}]


def test_workflow_requires_bound_conformance_before_each_product_context():
    root = Path(__file__).resolve().parents[1]
    steps = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]["windows-smoke"]["steps"]
    stage = next(step for step in steps if step.get("id") == "observation_conformance")
    assert "processing_observation_conformance.py run" in stage["run"]
    guarded = [step for step in steps if "acceptance_context.py run-ci" in step.get("run", "")]
    assert len(guarded) == 3
    for step in guarded:
        command = step["run"]
        assert steps.index(stage) < steps.index(step)
        assert command.index("processing_observation_conformance.py verify") < command.index(
            "acceptance_context.py run-ci"
        )
        assert "--artifact .cache/dependency-evidence/observation-conformance/result.json" in command
        assert "--artifact scripts/processing_observation_conformance.py" in command


def test_frozen_payload_verifies_observation_and_auth_source():
    from tests.test_windows_frozen_runtime import frozen

    assert frozen.APPLICATION_MODULES["app.processing.observation"] == "app/processing/observation.py"
    assert frozen.APPLICATION_MODULES["app.desktop_security"] == "app/desktop_security.py"
