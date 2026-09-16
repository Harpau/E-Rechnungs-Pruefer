from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "acceptance_context.py"
NOW = datetime(2026, 9, 16, 12, tzinfo=UTC)


@pytest.fixture
def module():
    spec = importlib.util.spec_from_file_location("acceptance_context_under_test", SCRIPT)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def binding():
    return {
        "kind": "vm",
        "commit": "a" * 40,
        "version": "0.0.0",
        "vm_uuid": "11111111-1111-4111-8111-111111111111",
        "snapshot_uuid": "22222222-2222-4222-8222-222222222222",
        "identity": "synthetic-operator",
        "os_version": "synthetic-windows",
    }


def prepared(module, tmp_path):
    root = tmp_path / "evidence"
    artifact = tmp_path / "synthetic-installer.exe"
    artifact.write_bytes(b"synthetic bytes; never executable")
    module.initialize(root, "controller-one", binding(), ["desktop-test"], now=NOW)
    context = module.issue(root, "controller-one", "desktop-test", [artifact], autonomous_minutes=30, now=NOW)
    return root, artifact, context


def attestation(module, tmp_path, context, target=None, now=NOW):
    path = tmp_path / "preflight.json"
    path.write_text(
        json.dumps(
            {
                "schema": module.PREFLIGHT_SCHEMA,
                "binding_sha256": module.digest(target or binding()),
                "context_id": context["id"],
                "observed_at_utc": module.timestamp(now),
                "collision_free": True,
                "state_verified": True,
                "signatures_verified": False,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_lifecycle_binds_artifacts_claims_once_and_preserves_events(module, tmp_path):
    root, artifact, context = prepared(module, tmp_path)
    events_before = (root / "events.ndjson").read_bytes()
    preflight = attestation(module, tmp_path, context)
    receipt = module.guard(root, "controller-one", context["id"], binding(), preflight=preflight, now=NOW)
    assert receipt["status"] == "RUNNING"
    assert receipt["artifacts"][0]["name"] == artifact.name
    assert (root / "events.ndjson").read_bytes().startswith(events_before)
    with pytest.raises(module.ContextError, match="einmalig"):
        module.guard(root, "controller-one", context["id"], binding(), preflight=preflight, now=NOW)
    evidence = tmp_path / "result-log.txt"
    evidence.write_text("synthetic PASS", encoding="utf-8")
    result = module.complete(root, "controller-one", context["id"], "PASS", [evidence], now=NOW)
    assert result["status"] == "PASS"
    assert module.verify(root)["contexts"][context["id"]]["status"] == "PASS"
    assert (root / "receipts" / f"{context['id']}.json").is_file()
    with pytest.raises(module.ContextError, match="abgeschlossen"):
        module.complete(root, "controller-one", context["id"], "PASS", [evidence], now=NOW)


@pytest.mark.parametrize("reason", ["uac", "pin", "password", "visual"])
def test_user_wait_has_exactly_120_minutes_and_expires_closed(module, tmp_path, reason):
    root, artifact, _ = prepared(module, tmp_path)
    context = module.issue(root, "controller-one", "desktop-test", [artifact], user_wait=reason, now=NOW)
    assert module.parse_timestamp(context["expires_at_utc"]) - NOW == timedelta(minutes=120)
    preflight = attestation(module, tmp_path, context, now=NOW + timedelta(minutes=120))
    with pytest.raises(module.ContextError, match="abgelaufen"):
        module.guard(
            root, "controller-one", context["id"], binding(), preflight=preflight, now=NOW + timedelta(minutes=120)
        )


@pytest.mark.parametrize("minutes", [None, 0, -1, 121, True])
def test_autonomous_ttl_is_explicit_and_bounded(module, tmp_path, minutes):
    root, artifact, _ = prepared(module, tmp_path)
    with pytest.raises(module.ContextError, match="Gültigkeit"):
        module.issue(root, "controller-one", "desktop-test", [artifact], autonomous_minutes=minutes, now=NOW)


def test_wait_cannot_override_the_human_context_lifetime(module, tmp_path):
    root, artifact, _ = prepared(module, tmp_path)
    with pytest.raises(module.ContextError, match="Gültigkeit"):
        module.issue(
            root, "controller-one", "desktop-test", [artifact], user_wait="uac", autonomous_minutes=30, now=NOW
        )


@pytest.mark.parametrize("field", ["vm_uuid", "snapshot_uuid", "identity", "commit", "version"])
def test_guard_rejects_target_mismatch(module, tmp_path, field):
    root, _, context = prepared(module, tmp_path)
    other = binding()
    other[field] = "33333333-3333-4333-8333-333333333333" if field.endswith("uuid") else "b" * 40
    with pytest.raises(module.ContextError, match="Bindung"):
        module.guard(root, "controller-one", context["id"], other, now=NOW)


def test_guard_requires_fresh_bound_vm_preflight(module, tmp_path):
    root, _, context = prepared(module, tmp_path)
    with pytest.raises(module.ContextError, match="Vorprüfung"):
        module.guard(root, "controller-one", context["id"], binding(), now=NOW)
    preflight = attestation(module, tmp_path, context, now=NOW - timedelta(minutes=6))
    with pytest.raises(module.ContextError, match="Vorprüfung"):
        module.guard(root, "controller-one", context["id"], binding(), preflight=preflight, now=NOW)


def test_guard_rejects_modified_artifact_without_consuming_claim(module, tmp_path):
    root, artifact, context = prepared(module, tmp_path)
    artifact.write_bytes(b"changed")
    preflight = attestation(module, tmp_path, context)
    with pytest.raises(module.ContextError, match="Artefakt"):
        module.guard(root, "controller-one", context["id"], binding(), preflight=preflight, now=NOW)
    assert module.verify(root)["contexts"][context["id"]]["status"] == "READY"


def test_controller_scope_concurrent_writer_and_reinitialize_fail_closed(module, tmp_path):
    root, artifact, _ = prepared(module, tmp_path)
    with pytest.raises(module.ContextError, match="Controller"):
        module.issue(root, "someone-else", "desktop-test", [artifact], autonomous_minutes=30, now=NOW)
    with pytest.raises(module.ContextError, match="Scope"):
        module.issue(root, "controller-one", "restore", [artifact], autonomous_minutes=30, now=NOW)
    (root / ".writer-lock").mkdir()
    with pytest.raises(module.ContextError, match="Controller"):
        module.issue(root, "controller-one", "desktop-test", [artifact], autonomous_minutes=30, now=NOW)
    with pytest.raises(module.ContextError, match="existiert"):
        module.initialize(root, "controller-one", binding(), ["desktop-test"], now=NOW)


@pytest.mark.parametrize("filename", ["events.ndjson", "acceptance-plan.json"])
def test_corrupt_or_inconsistent_persistent_state_cannot_be_used(module, tmp_path, filename):
    root, artifact, _ = prepared(module, tmp_path)
    (root / filename).write_text("{}\n", encoding="utf-8")
    with pytest.raises(module.ContextError):
        module.issue(root, "controller-one", "desktop-test", [artifact], autonomous_minutes=30, now=NOW)


def test_failure_blocks_new_mutation_and_cannot_be_changed_to_pass(module, tmp_path):
    root, artifact, context = prepared(module, tmp_path)
    preflight = attestation(module, tmp_path, context)
    module.guard(root, "controller-one", context["id"], binding(), preflight=preflight, now=NOW)
    module.complete(root, "controller-one", context["id"], "FAIL_PRODUCT", [preflight], now=NOW)
    with pytest.raises(module.ContextError, match="gesperrt"):
        module.issue(root, "controller-one", "desktop-test", [artifact], autonomous_minutes=30, now=NOW)
    with pytest.raises(module.ContextError):
        module.complete(root, "controller-one", context["id"], "PASS", [preflight], now=NOW)


def test_finishing_an_already_running_context_cannot_clear_another_context_failure(module, tmp_path):
    root, artifact, running = prepared(module, tmp_path)
    abandoned = module.issue(root, "controller-one", "desktop-test", [artifact], autonomous_minutes=30, now=NOW)
    preflight = attestation(module, tmp_path, running)
    module.guard(root, "controller-one", running["id"], binding(), preflight=preflight, now=NOW)
    module.complete(root, "controller-one", abandoned["id"], "ABORTED", [preflight], now=NOW)
    assert module.verify(root)["blocked"] is True
    module.complete(root, "controller-one", running["id"], "PASS", [preflight], now=NOW)
    assert module.verify(root)["blocked"] is True
    with pytest.raises(module.ContextError, match="gesperrt"):
        module.issue(root, "controller-one", "desktop-test", [artifact], autonomous_minutes=30, now=NOW)


def test_verify_rejects_unblocked_state_even_with_consistent_event_chain_after_failure(module, tmp_path):
    root, _, context = prepared(module, tmp_path)
    log = tmp_path / "abort.txt"
    log.write_text("Synthetic aborted attempt.", encoding="utf-8")
    module.complete(root, "controller-one", context["id"], "ABORTED", [log], now=NOW)
    state = module.verify(root)
    state["blocked"] = False
    module._persist(root, state, "synthetic-invalid-reset")
    with pytest.raises(module.ContextError, match="Sperre"):
        module.verify(root)


@pytest.mark.parametrize("operation", ["issue", "guard"])
def test_clock_rollback_before_latest_state_cannot_issue_or_consume_context(module, tmp_path, operation):
    root, artifact, context = prepared(module, tmp_path)
    module.issue(
        root, "controller-one", "desktop-test", [artifact], autonomous_minutes=30, now=NOW + timedelta(minutes=10)
    )
    original = module.verify(root)
    rollback = NOW + timedelta(minutes=5)
    with pytest.raises(module.ContextError, match="Zustandsänderung"):
        if operation == "issue":
            module.issue(root, "controller-one", "desktop-test", [artifact], autonomous_minutes=30, now=rollback)
        else:
            preflight = attestation(module, tmp_path, context, now=rollback)
            module.guard(root, "controller-one", context["id"], binding(), preflight=preflight, now=rollback)
    assert module.verify(root) == original


def test_symlink_artifact_and_evidence_root_are_rejected(module, tmp_path):
    root, artifact, _ = prepared(module, tmp_path)
    link = tmp_path / "linked.exe"
    try:
        link.symlink_to(artifact)
    except OSError:
        pytest.skip("Symlink privilege unavailable")
    with pytest.raises(module.ContextError, match="Link"):
        module.issue(root, "controller-one", "desktop-test", [link], autonomous_minutes=30, now=NOW)
    directory_link = tmp_path / "linked-root"
    directory_link.symlink_to(root, target_is_directory=True)
    with pytest.raises(module.ContextError, match="Link"):
        module.verify(directory_link)


def test_ci_binding_uses_actual_checkout_and_complete_runner_identity(module, monkeypatch):
    environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_JOB": "windows-smoke",
        "GITHUB_REPOSITORY": "example/project",
        "RUNNER_NAME": "synthetic-runner",
        "RUNNER_OS": "Windows",
        "RUNNER_ARCH": "X64",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(module, "checkout_commit", lambda: "a" * 40)
    actual = module.ci_binding("0.0.0")
    assert actual["workflow_attempt"] == "2"
    monkeypatch.setattr(module, "checkout_commit", lambda: "b" * 40)
    with pytest.raises(module.ContextError, match="Checkout"):
        module.ci_binding("0.0.0")


def test_only_one_context_may_be_running(module, tmp_path):
    root, artifact, first = prepared(module, tmp_path)
    second = module.issue(root, "controller-one", "desktop-test", [artifact], autonomous_minutes=30, now=NOW)
    preflight = attestation(module, tmp_path, first)
    module.guard(root, "controller-one", first["id"], binding(), preflight=preflight, now=NOW)
    with pytest.raises(module.ContextError, match="parallele"):
        module.guard(root, "controller-one", second["id"], binding(), preflight=preflight, now=NOW)


def test_completion_requires_consumed_context_and_nonempty_raw_evidence(module, tmp_path):
    root, _, context = prepared(module, tmp_path)
    log = tmp_path / "log.txt"
    log.write_text("observed", encoding="utf-8")
    with pytest.raises(module.ContextError):
        module.complete(root, "controller-one", context["id"], "PASS", [log], now=NOW)
    preflight = attestation(module, tmp_path, context)
    module.guard(root, "controller-one", context["id"], binding(), preflight=preflight, now=NOW)
    log.write_bytes(b"")
    with pytest.raises(module.ContextError, match="Leerer"):
        module.complete(root, "controller-one", context["id"], "PASS", [log], now=NOW)


@pytest.mark.parametrize("target", ["receipt", "evidence"])
def test_verify_detects_corrupted_terminal_evidence(module, tmp_path, target):
    root, _, context = prepared(module, tmp_path)
    preflight = attestation(module, tmp_path, context)
    module.guard(root, "controller-one", context["id"], binding(), preflight=preflight, now=NOW)
    module.complete(root, "controller-one", context["id"], "PASS", [preflight], now=NOW)
    filename = f"{context['id']}.json" if target == "receipt" else f"{context['id']}-evidence-0.bin"
    (root / "receipts" / filename).write_bytes(b"corrupted")
    with pytest.raises(module.ContextError, match="Abschluss"):
        module.verify(root)


def test_expired_unconsumed_context_can_only_be_closed_without_pass(module, tmp_path):
    root, _, context = prepared(module, tmp_path)
    log = tmp_path / "abort.txt"
    log.write_text("No product mutation: waiting context expired.", encoding="utf-8")
    result = module.complete(root, "controller-one", context["id"], "ABORTED", [log], now=NOW + timedelta(hours=3))
    assert result["status"] == "ABORTED"
    assert module.verify(root)["blocked"] is True


def test_signed_action_requires_fresh_signature_attestation(module, tmp_path):
    root, artifact, _ = prepared(module, tmp_path)
    context = module.issue(
        root, "controller-one", "desktop-test", [artifact], autonomous_minutes=30, require_signatures=True, now=NOW
    )
    preflight = attestation(module, tmp_path, context)
    with pytest.raises(module.ContextError, match="Vorprüfung"):
        module.guard(root, "controller-one", context["id"], binding(), preflight=preflight, now=NOW)


@pytest.mark.parametrize("change", ["schema", "collision", "context", "future"])
def test_wrong_preflight_is_not_authorization(module, tmp_path, change):
    root, _, context = prepared(module, tmp_path)
    preflight = attestation(module, tmp_path, context)
    report = json.loads(preflight.read_text())
    field, value = {
        "schema": ("schema", "unknown"),
        "collision": ("collision_free", False),
        "context": ("context_id", "unbound-context"),
        "future": ("observed_at_utc", module.timestamp(NOW + timedelta(seconds=1))),
    }[change]
    report[field] = value
    preflight.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(module.ContextError, match="Vorprüfung"):
        module.guard(root, "controller-one", context["id"], binding(), preflight=preflight, now=NOW)


def test_unknown_binding_schema_and_duplicate_json_keys_fail_closed(module, tmp_path):
    target = binding()
    target["secret"] = "not-accepted"
    with pytest.raises(module.ContextError, match="Schema"):
        module.initialize(tmp_path / "evidence", "controller", target, ["test"], now=NOW)
    report = tmp_path / "duplicate.json"
    report.write_text('{"kind":"vm","kind":"ci"}', encoding="utf-8")
    with pytest.raises(module.ContextError, match="Doppelter"):
        module.read_json(report)


def test_parent_traversal_is_not_normalized_into_an_allowed_path(module, tmp_path):
    root, artifact, _ = prepared(module, tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    with pytest.raises(module.ContextError, match="Pfadsegmente"):
        module.issue(
            root, "controller-one", "desktop-test", [nested / ".." / artifact.name], autonomous_minutes=30, now=NOW
        )


def test_cli_json_contract_and_failure_exit_code(module, tmp_path, capsys):
    root = tmp_path / "cli-evidence"
    target = tmp_path / "binding.json"
    target.write_text(json.dumps(binding()), encoding="utf-8")
    assert (
        module.main(
            ["init", "--root", str(root), "--controller", "operator", "--binding", str(target), "--scope", "test"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["binding"] == binding()
    artifact = tmp_path / "synthetic.exe"
    artifact.write_bytes(b"synthetic")
    args = [
        "issue",
        "--root",
        str(root),
        "--controller",
        "operator",
        "--action",
        "test",
        "--artifact",
        str(artifact),
        "--autonomous-minutes",
        "30",
    ]
    assert module.main(args) == 0
    context = json.loads(capsys.readouterr().out)
    assert context["status"] == "READY"
    assert (
        module.main(
            [
                "guard",
                "--root",
                str(root),
                "--controller",
                "operator",
                "--context",
                context["id"],
                "--binding",
                str(target),
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert not output.out
    assert "Vorprüfung" in output.err


def prepare_ci(module, tmp_path, monkeypatch):
    target = {
        "kind": "ci",
        "commit": "a" * 40,
        "version": "0.0.0",
        "workflow_run": "123",
        "workflow_attempt": "1",
        "job": "test",
        "runner": "runner",
        "runner_os": "Windows",
        "runner_arch": "X64",
        "repository": "example/project",
    }
    root = tmp_path / "ci-evidence"
    module.initialize(root, "operator", target, ["desktop"])
    monkeypatch.setattr(module, "ci_binding", lambda version: target)
    artifact = tmp_path / "synthetic.exe"
    artifact.write_bytes(b"not executable")
    script = tmp_path / "synthetic.ps1"
    script.write_text("synthetic script; never executed", encoding="utf-8")
    return root, artifact, script


@pytest.mark.parametrize("exit_code", [0, 7])
def test_run_ci_records_bound_command_and_never_calls_failure_pass(module, tmp_path, monkeypatch, exit_code):
    root, artifact, script = prepare_ci(module, tmp_path, monkeypatch)
    invoked = []

    def execute(command, *, stdout, stderr, check):
        invoked.append(command)
        stdout.write(b"unchanged raw subprocess output\r\n")
        return type("Result", (), {"returncode": exit_code})()

    monkeypatch.setattr(module.subprocess, "run", execute)
    command = ["pwsh", "-NoProfile", "-File", str(script)]
    result, actual_exit = module.run_ci(
        root, "operator", "desktop", [artifact], [script], tmp_path / "raw.log", command
    )
    assert actual_exit == exit_code
    assert result["status"] == ("PASS" if exit_code == 0 else "INCONCLUSIVE")
    assert result["context"]["command"] == command
    assert invoked == [command]
    assert len(result["context"]["artifacts"]) == 2
    assert b"unchanged raw subprocess output\r\n" in (tmp_path / "raw.log").read_bytes()
    module.verify(root)


def test_run_ci_guard_failure_does_not_launch_command(module, tmp_path, monkeypatch):
    root, artifact, script = prepare_ci(module, tmp_path, monkeypatch)

    def reject(*args, **kwargs):
        raise module.ContextError("Guard rejected")

    def forbidden(*args, **kwargs):
        pytest.fail("Subprocess must not start after guard rejection")

    monkeypatch.setattr(module, "guard", reject)
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    with pytest.raises(module.ContextError, match="Guard rejected"):
        module.run_ci(
            root, "operator", "desktop", [artifact], [script], tmp_path / "raw.log", ["pwsh", "-File", str(script)]
        )


def test_run_ci_rejects_unbound_command_script(module, tmp_path, monkeypatch):
    root, artifact, script = prepare_ci(module, tmp_path, monkeypatch)
    with pytest.raises(module.ContextError, match="Skript"):
        module.run_ci(
            root, "operator", "desktop", [artifact], [script], tmp_path / "raw.log", ["pwsh", "-Command", "unbound"]
        )
