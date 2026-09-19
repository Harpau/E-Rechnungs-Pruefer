from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scripts import test_kosit_components as smoke


def _bound_components(tmp_path: Path) -> tuple[Path, Path, Path]:
    vendor = tmp_path / "vendor"
    (vendor / "validator").mkdir(parents=True)
    (vendor / "xrechnung").mkdir()
    jar = vendor / "validator" / "synthetic-standalone.jar"
    jar.write_bytes(b"synthetic jar for binding tests only")
    archive = tmp_path / "synthetic-configuration.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("scenarios.xml", "<scenarios/>")
        zipped.writestr("resources/synthetic.xsl", "<stylesheet/>")
        zipped.extractall(vendor / "xrechnung")
    lock = tmp_path / "components.lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "components": {
                    name: {
                        "version": "synthetic",
                        "filename": path.name,
                        "url": f"https://example.test/{path.name}",
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    for name, path in (("validator", jar), ("xrechnung", archive))
                },
                "standards": {
                    key: "synthetic"
                    for key in ("xrechnung", "xrechnung_configuration", "cen_en16931", "xrechnung_schematron")
                },
            }
        ),
        encoding="utf-8",
    )
    return vendor, archive, lock


@pytest.mark.parametrize("target", ["jar", "archive", "extracted", "extra", "symlink"])
def test_component_binding_rejects_changed_or_unbound_inputs(tmp_path: Path, target: str) -> None:
    vendor, archive, lock = _bound_components(tmp_path)
    if target == "jar":
        (vendor / "validator" / "synthetic-standalone.jar").write_bytes(b"changed")
    elif target == "archive":
        archive.write_bytes(b"changed")
    elif target == "extracted":
        (vendor / "xrechnung" / "scenarios.xml").write_text("changed", encoding="utf-8")
    elif target == "extra":
        (vendor / "xrechnung" / "unbound.xml").write_text("extra", encoding="utf-8")
    else:
        path = vendor / "xrechnung" / "scenarios.xml"
        path.unlink()
        try:
            path.symlink_to(archive)
        except OSError:
            pytest.skip("Symlinks are unavailable for this Windows test account")
    with pytest.raises(smoke.SmokeError):
        smoke.verify_components(vendor, archive, lock)


def test_component_binding_verifies_extracted_bytes_without_mutation(tmp_path: Path) -> None:
    vendor, archive, lock = _bound_components(tmp_path)
    binding = smoke.verify_components(vendor, archive, lock)
    assert binding["configuration_files_verified"] == 2
    assert archive.exists()
    assert (
        binding["validator_sha256"]
        == hashlib.sha256((vendor / "validator" / "synthetic-standalone.jar").read_bytes()).hexdigest()
    )


def _result(accepted: bool, exit_code: int) -> dict:
    decision = "accept" if accepted else "reject"
    return {
        "configured": True,
        "executed": True,
        "accepted": accepted,
        "exit_code": exit_code,
        "report_source": "file",
        "raw_report": (
            '<rep:report xmlns:rep="http://www.xoev.de/de/validator/varl/1">'
            f"<rep:assessment><rep:{decision}/></rep:assessment></rep:report>"
        ),
        "findings": [],
    }


@pytest.mark.parametrize("accepted,exit_code", [(True, 9), (False, 0)])
def test_report_check_uses_varl_decision_instead_of_exit_code(accepted: bool, exit_code: int) -> None:
    case = smoke.check_report("synthetic", _result(accepted, exit_code), accepted)
    assert case["accepted"] is accepted
    assert case["varl_assessment"] == ("accept" if accepted else "reject")


@pytest.mark.parametrize("defect", ["decision", "executed", "report", "source"])
def test_report_check_never_counts_missing_or_inconsistent_validation_as_success(defect: str) -> None:
    result = _result(True, 0)
    if defect == "decision":
        result["accepted"] = False
    elif defect == "executed":
        result["executed"] = False
    elif defect == "report":
        result["raw_report"] = "<unrelated/>"
    else:
        result["report_source"] = None
    with pytest.raises(smoke.SmokeError):
        smoke.check_report("synthetic", result, True)


def test_fixture_rejection_changes_only_the_document_type_code() -> None:
    cases = smoke.synthetic_cases()
    for syntax in ("cii", "ubl"):
        original = cases[f"{syntax}_accept"]
        invalid = cases[f"{syntax}_reject"]
        assert original != invalid
        assert invalid.count(b">999<") == 1
        assert invalid.replace(b">999<", b">380<") == original


def test_technical_failure_must_not_be_counted_as_invoice_rejection() -> None:
    result = {
        "configured": True,
        "executed": False,
        "accepted": None,
        "exit_code": 1,
        "raw_report": None,
        "findings": [{"id": "KOSIT-EXEC", "severity": "warning"}],
    }
    assert smoke.check_java_failure(result)["accepted"] is None
    result["accepted"] = False
    with pytest.raises(smoke.SmokeError):
        smoke.check_java_failure(result)


def test_cli_writes_failure_evidence_for_unbound_components(tmp_path: Path) -> None:
    vendor, archive, lock = _bound_components(tmp_path)
    (vendor / "xrechnung" / "scenarios.xml").write_text("changed", encoding="utf-8")
    output = tmp_path / "evidence" / "smoke.json"
    status = smoke.main(
        [
            "--vendor-root",
            str(vendor),
            "--config-archive",
            str(archive),
            "--lock-file",
            str(lock),
            "--java",
            "intentionally-unavailable-java",
            "--output",
            str(output),
        ]
    )
    assert status == 1
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["passed"] is False
    assert "Konfiguration verändert" in evidence["error"]


def test_cli_never_overwrites_existing_evidence_or_component(tmp_path: Path) -> None:
    output = tmp_path / "existing.json"
    original = b"preserved evidence"
    output.write_bytes(original)
    status = smoke.main(["--vendor-root", str(tmp_path), "--config-archive", str(output), "--output", str(output)])
    assert status == 1
    assert output.read_bytes() == original
