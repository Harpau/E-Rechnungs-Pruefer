from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "dependency_audit", Path(__file__).resolve().parents[1] / "scripts/dependency_audit.py"
)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class Distribution:
    def __init__(self, name: str, version: str, direct: dict | None = None):
        self.metadata = {"Name": name}
        self.version = version
        self.direct = direct

    def read_text(self, filename: str) -> str | None:
        return json.dumps(self.direct) if self.direct else None


def test_inventory_includes_dev_and_bootstrap_and_excludes_only_bound_own_editable(tmp_path: Path) -> None:
    own = {"url": tmp_path.as_uri(), "dir_info": {"editable": True}}
    result = audit.collect_inventory(
        [Distribution("pip", "26.2.1"), Distribution("dev-only", "1.2"), Distribution("example", "1", own)],
        "example",
        tmp_path,
    )
    assert {p["name"] for p in result["packages"]} == {"pip", "dev-only"}
    assert result["excluded_editable"]["name"] == "example"


@pytest.mark.parametrize("name,path", [("foreign", "same"), ("example", "other")])
def test_foreign_or_unbound_editable_is_error(tmp_path: Path, name: str, path: str) -> None:
    origin = tmp_path if path == "same" else tmp_path / "elsewhere"
    direct = {"url": origin.as_uri(), "dir_info": {"editable": True}}
    with pytest.raises(audit.AuditError, match="editable"):
        audit.collect_inventory([Distribution(name, "1.0", direct)], "example", tmp_path)


def test_duplicate_distribution_is_error(tmp_path: Path) -> None:
    with pytest.raises(audit.AuditError, match="doppelt"):
        audit.collect_inventory([Distribution("Demo_Pkg", "1"), Distribution("demo-pkg", "1")], "own", tmp_path)


def test_audit_result_cannot_silently_skip_inventory_packages() -> None:
    inventory = {"packages": [{"name": "demo", "version": "1"}, {"name": "dev-only", "version": "2"}]}
    with pytest.raises(audit.AuditError, match="vollständig"):
        audit.validate_audit_result(inventory, {"dependencies": [{"name": "demo", "version": "1", "vulns": []}]})
    with pytest.raises(audit.AuditError, match="übersprungen"):
        audit.validate_audit_result(
            inventory,
            {"dependencies": [{"name": "demo", "version": "1", "skip_reason": "not found"}]},
        )


def test_audit_command_uses_frozen_inventory_without_marker_resolution(tmp_path: Path) -> None:
    command = audit.audit_command(tmp_path / "inventory.txt", tmp_path / "audit.json")
    assert "--strict" in command
    assert "--disable-pip" in command
    assert "--no-deps" in command
    assert "." not in command
    assert "--ignore-vuln" not in command


def test_stale_audit_report_cannot_turn_missing_new_output_into_success(tmp_path: Path, monkeypatch) -> None:
    packages = [{"name": "demo", "version": "1"}]
    inventory = {"schema_version": 1, "packages": packages, "inventory_sha256": audit.inventory_digest(packages)}
    output = tmp_path / "audit.json"
    output.write_text(json.dumps({"dependencies": [{"name": "demo", "version": "1", "vulns": []}]}))
    monkeypatch.setattr(audit.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", ""))
    with pytest.raises(audit.AuditError, match="keinen Bericht"):
        audit.audit_inventory(inventory, output)


def test_capture_other_interpreter_uses_stdlib_capture_child(tmp_path: Path, monkeypatch) -> None:
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(audit.subprocess, "run", run)
    result = audit.main(["capture", "--python", "target-python", "--output", str(tmp_path / "inventory.json")])
    assert result == 0
    assert commands[0][0] == "target-python"
    assert "--python" not in commands[0][1:]
