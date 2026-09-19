"""One bounded native Windows observer catalog and its fail-closed install guard.

This helper owns synthetic test processes only. It cannot install or operate a
product. A passing pytest exit code alone never opens the installation guard.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NATIVE_MODULE = "tests/test_processing_observation_native.py"
CATALOG = (
    (
        "authentication-contract",
        "tests/test_processing_observation_api.py::test_conformance_authentication_contract",
        False,
    ),
    ("fast-owner-completion", f"{NATIVE_MODULE}::test_fast_owner_completion", True),
    ("native-lifecycle-order", f"{NATIVE_MODULE}::test_native_lifecycle_order", True),
    ("ended-role-before-binding", f"{NATIVE_MODULE}::test_ended_role_before_binding", True),
    ("native-fixture-cleanup", f"{NATIVE_MODULE}::test_native_fixture_cleanup", True),
    (
        "identity-clock-safety",
        "tests/test_processing_observation_probe.py::test_conformance_observation_identity_clock_safety",
        False,
    ),
    (
        "health-interval-boundaries",
        "tests/test_processing_observation_probe.py::test_conformance_observation_health_boundaries",
        False,
    ),
    (
        "observation-loss-and-limits",
        "tests/test_processing_observation.py::test_conformance_observation_loss_and_limits",
        False,
    ),
)
MAX_RECEIPT_BYTES = 1024 * 1024
OUTER_SECONDS = 360


class ConformanceError(RuntimeError):
    """The current native catalog has no complete, bound passing receipt."""


def case_catalog() -> list[dict[str, Any]]:
    return [{"id": case, "nodeid": node, "native": native} for case, node, native in CATALOG]


def _positive(value: Any) -> bool:
    return type(value) is int and 0 < value < 2**64


def _native_proof(proof: Any) -> None:
    if not isinstance(proof, dict) or set(proof) != {
        "method",
        "parent",
        "cleanup_confirmed",
        "lease_released",
        "roles",
    }:
        raise ConformanceError("Native Fixturebindung fehlt.")
    if (
        proof["method"] != "real-processing-owner"
        or proof["cleanup_confirmed"] is not True
        or proof["lease_released"] is not True
    ):
        raise ConformanceError("Native Fixturebereinigung wurde nicht bestätigt.")
    parent = proof["parent"]
    if (
        not isinstance(parent, dict)
        or set(parent) != {"pid", "creation_time"}
        or any(not _positive(parent[key]) for key in parent)
    ):
        raise ConformanceError("Native Fixture-Parentbindung fehlt.")
    roles = proof["roles"]
    if not isinstance(roles, list) or len(roles) != 2:
        raise ConformanceError("Genau zwei native Fixture-Rollen sind erforderlich.")
    for role in roles:
        if not isinstance(role, dict) or set(role) != {"role", "pid", "parent_pid", "creation_time", "exit_code"}:
            raise ConformanceError("Native Rollenidentität fehlt.")
        if any(not _positive(role[key]) for key in ("pid", "parent_pid", "creation_time")):
            raise ConformanceError("Native Rollenidentität ist unvollständig.")
        if type(role["exit_code"]) is not int or not -(2**31) <= role["exit_code"] < 2**32:
            raise ConformanceError("Natives Rollenende wurde nicht bestätigt.")
        if role["parent_pid"] != parent["pid"] or role["creation_time"] < parent["creation_time"]:
            raise ConformanceError("Native Rolle gehört nicht zum gebundenen Fixture-Parent.")
    if (
        {role["role"] for role in roles} != {"supervisor", "worker"}
        or len({role["pid"] for role in roles}) != 2
        or len({role["parent_pid"] for role in roles}) != 1
        or any(role["pid"] == role["parent_pid"] for role in roles)
    ):
        raise ConformanceError("Native Rollenbindung ist widersprüchlich.")


def validate_receipt(receipt: Any, *, binding: dict[str, str], sources: dict[str, Any]) -> None:
    """Validate content independently of the CLI's file/hash checks."""
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version",
        "status",
        "platform",
        "binding",
        "sources",
        "catalog",
        "pytest",
    }:
        raise ConformanceError("Ungültiges Konformitätsreceipt.")
    if (
        type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["status"] != "PASS"
        or receipt["platform"] != "win32"
        or receipt["binding"] != binding
        or receipt["sources"] != sources
        or not sources
        or receipt["catalog"] != case_catalog()
    ):
        raise ConformanceError("Konformitätsreceipt gehört nicht zum aktuellen Windowslauf.")
    report = receipt["pytest"]
    nodes = [case[1] for case in CATALOG]
    if (
        not isinstance(report, dict)
        or set(report) != {"exit_code", "collected", "tests"}
        or type(report["exit_code"]) is not int
        or report["exit_code"] != 0
        or report["collected"] != nodes
        or not isinstance(report["tests"], list)
        or len(report["tests"]) != len(nodes)
    ):
        raise ConformanceError("Der verpflichtende native Fallkatalog ist unvollständig.")
    for case, result in zip(case_catalog(), report["tests"], strict=True):
        if (
            not isinstance(result, dict)
            or set(result) != {"nodeid", "phases", "xfail", "native_proof", "native_failure"}
            or result["nodeid"] != case["nodeid"]
            or result["phases"] != {"setup": "passed", "call": "passed", "teardown": "passed"}
            or result["xfail"] is not False
            or result["native_failure"] is not None
        ):
            raise ConformanceError("Fehlender, übersprungener oder fehlgeschlagener Pflichtfall.")
        if case["native"]:
            _native_proof(result["native_proof"])
        elif result["native_proof"] is not None:
            raise ConformanceError("Unerwarteter nativer Fixturebeleg.")


def _json(path: Path) -> Any:
    raw = path.read_bytes()
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ConformanceError("Konformitätsreceipt ist zu groß.")
    return json.loads(raw)


def _write(path: Path, value: Any) -> None:
    raw = json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ConformanceError("Konformitätsreceipt ist zu groß.")
    with path.open("xb") as stream:
        stream.write(raw)


def _file(path: Path) -> dict[str, Any]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400:
        raise ConformanceError("Konformitätsevidence muss eine reguläre Datei sein.")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        finished = os.fstat(stream.fileno())
    after = path.lstat()
    identities = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns) for item in (before, opened, finished, after)
    }
    if len(identities) != 1 or before.st_mode != after.st_mode or opened.st_mode != finished.st_mode:
        raise ConformanceError("Konformitätsdatei wurde während der Prüfung verändert.")
    return {"size": before.st_size, "sha256": digest}


def current_binding() -> dict[str, str]:
    if sys.platform != "win32" or os.environ.get("RUNNER_OS") != "Windows":
        raise ConformanceError("Diese Konformität benötigt einen echten Windows-Runner.")
    keys = {
        "commit": "GITHUB_SHA",
        "workflow_run": "GITHUB_RUN_ID",
        "workflow_attempt": "GITHUB_RUN_ATTEMPT",
        "job": "GITHUB_JOB",
        "runner_os": "RUNNER_OS",
        "runner_name": "RUNNER_NAME",
    }
    binding = {key: os.environ.get(environment, "") for key, environment in keys.items()}
    if (
        not re.fullmatch(r"[0-9a-f]{40}", binding["commit"])
        or not binding["workflow_run"].isdigit()
        or not binding["workflow_attempt"].isdigit()
        or int(binding["workflow_attempt"]) < 1
        or binding["job"] != "windows-smoke"
        or not binding["runner_name"]
        or len(binding["runner_name"]) > 256
    ):
        raise ConformanceError("Eindeutiger aktueller GitHub-Kontext fehlt.")
    expected = os.environ.get("EINVOICE_CI_EXPECTED_COMMIT", "")
    if expected and (not re.fullmatch(r"[0-9a-f]{40}", expected) or binding["commit"] != expected):
        raise ConformanceError("Erwarteter Dispatch-Commit stimmt nicht mit dem aktuellen GitHub-Lauf überein.")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=10).strip()
    if head != binding["commit"]:
        raise ConformanceError("Konformitätscommit weicht vom Checkout ab.")
    return binding


def source_inventory() -> dict[str, Any]:
    files = (
        subprocess.check_output(
            [
                "git",
                "ls-files",
                "-z",
                "app",
                "scripts",
                "tests",
                "packaging/windows",
                ".github/workflows/ci.yml",
                "pyproject.toml",
                "VERSION",
            ],
            cwd=ROOT,
            timeout=10,
        )
        .decode("utf-8")
        .split("\0")
    )
    inventory = {relative: _file(ROOT / relative) for relative in sorted(files) if relative}
    required = {
        "scripts/processing_observation_conformance.py",
        "app/processing/observation.py",
        "app/desktop_security.py",
        *[case[1].split("::", 1)[0] for case in CATALOG],
    }
    if not required <= inventory.keys():
        raise ConformanceError("Konformitätscode und Pflichtfälle müssen im gebundenen Commit enthalten sein.")
    return inventory


class CatalogPlugin:
    """Record every phase, not JUnit's aggregate successful-test count."""

    def __init__(self) -> None:
        self.collected: list[str] = []
        self.tests: dict[str, Any] = {}

    def pytest_collection_finish(self, session: Any) -> None:
        self.collected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report: Any) -> None:
        result = self.tests.setdefault(
            report.nodeid,
            {"nodeid": report.nodeid, "phases": {}, "xfail": False, "native_proof": None, "native_failure": None},
        )
        if report.when in result["phases"]:
            result["phases"][report.when] = "duplicate"
        else:
            result["phases"][report.when] = report.outcome
        result["xfail"] |= hasattr(report, "wasxfail")
        failures = [value for key, value in report.user_properties if key == "native_observation_failure"]
        if failures:
            try:
                result["native_failure"] = json.loads(failures[-1])
            except (ValueError, TypeError):
                result["native_failure"] = "invalid"
        if report.when == "call":
            proofs = [value for key, value in report.user_properties if key == "native_observation_proof"]
            if len(proofs) == 1:
                try:
                    result["native_proof"] = json.loads(proofs[0])
                except (ValueError, TypeError):
                    result["native_proof"] = "invalid"
            elif proofs:
                result["native_proof"] = "duplicate"

    def result(self, exit_code: int) -> dict[str, Any]:
        return {"exit_code": exit_code, "collected": self.collected, "tests": list(self.tests.values())}


def _pytest(output: Path) -> int:
    if sys.platform != "win32":
        raise ConformanceError("Native Tests benötigen Windows.")
    import pytest

    plugin = CatalogPlugin()
    code = int(
        pytest.main(["-q", "--strict-config", "--strict-markers", *[case[1] for case in CATALOG]], plugins=[plugin])
    )
    _write(output / "pytest-report.json", plugin.result(code))
    return code


def run(output: Path) -> None:
    binding = current_binding()
    sources = source_inventory()
    output.mkdir(parents=True, exist_ok=False)
    exit_code = -1
    with (output / "pytest.stdout.log").open("xb") as stdout, (output / "pytest.stderr.log").open("xb") as stderr:
        try:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "_pytest", "--output", str(output.absolute())],
                cwd=ROOT,
                stdout=stdout,
                stderr=stderr,
                timeout=OUTER_SECONDS,
                check=False,
            )
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            pass  # subprocess.run terminates/reaps only its own helper; this is failed evidence.
    report = _json(output / "pytest-report.json") if (output / "pytest-report.json").is_file() else None
    receipt = {
        "schema_version": 1,
        "status": "PASS",
        "platform": sys.platform,
        "binding": binding,
        "sources": sources,
        "catalog": case_catalog(),
        "pytest": report,
    }
    failure = None
    try:
        if exit_code != 0 or current_binding() != binding or source_inventory() != sources:
            raise ConformanceError("Nativer Testprozess oder unveränderte Quellenbindung fehlgeschlagen.")
        validate_receipt(receipt, binding=binding, sources=sources)
    except (ConformanceError, OSError, ValueError) as exc:
        receipt["status"] = "FAIL"
        failure = exc
    _write(output / "result.json", receipt)
    names = ["result.json", "pytest.stdout.log", "pytest.stderr.log"]
    if (output / "pytest-report.json").is_file():
        names.append("pytest-report.json")
    _write(output / "files.json", {name: _file(output / name) for name in names})
    if failure is not None:
        raise ConformanceError(str(failure)) from failure


def verify(output: Path) -> None:
    binding = current_binding()
    names = {"result.json", "pytest.stdout.log", "pytest.stderr.log", "pytest-report.json"}
    inventory = _json(output / "files.json")
    if not isinstance(inventory, dict) or set(inventory) != names:
        raise ConformanceError("Vollständiges Roh-Evidenceinventar fehlt.")
    if inventory != {name: _file(output / name) for name in sorted(names)}:
        raise ConformanceError("Konformitätsrohdateien stimmen nicht mit ihrem Inventar überein.")
    receipt = _json(output / "result.json")
    if receipt.get("pytest") != _json(output / "pytest-report.json"):
        raise ConformanceError("Receipt und ursprünglicher Testbericht widersprechen sich.")
    validate_receipt(receipt, binding=binding, sources=source_inventory())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "verify", "_pytest"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.action == "_pytest":
            return _pytest(args.output)
        (run if args.action == "run" else verify)(args.output)
        print(json.dumps({"status": "PASS", "scope": "native-observation-conformance", "cases": len(CATALOG)}))
        return 0
    except (ConformanceError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Konformitätsprüfung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
