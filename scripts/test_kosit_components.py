#!/usr/bin/env python3
"""Exercise the prepared, locked KoSIT bundle with local synthetic invoices.

No downloads, installation or invoice network access occur here. Retain the
locked configuration ZIP from preparation and pass it with --config-archive:
its members are compared byte-for-byte with the actual prepared vendor tree.
The Java-start failure uses a deliberately incomplete temporary JAR. The two
exit-code fault injections retain real Java execution and its real VARL report;
only the exit code handed to the production adapter is changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.configuration import Settings  # noqa: E402
from app.validators import kosit  # noqa: E402
from app.validators.kosit import KositValidator  # noqa: E402
from scripts.install_kosit import InstallError, load_lock, sha256_file  # noqa: E402

DEFAULT_LOCK_FILE = PROJECT_ROOT / "packaging" / "kosit" / "components.lock.json"


class SmokeError(RuntimeError):
    """A missing binding or unexpected validation outcome fails the smoke."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def verify_components(vendor_root: Path, config_archive: Path, lock_file: Path) -> dict[str, Any]:
    """Verify actual installed bytes; a label or installer receipt is insufficient."""
    locked = load_lock(lock_file)
    components = locked["components"]
    jar = vendor_root / "validator" / components["validator"]["filename"]
    configuration = vendor_root / "xrechnung"
    for label, path in (("validator", jar), ("xrechnung", config_archive)):
        _require(path.is_file() and not path.is_symlink(), f"Komponente fehlt oder ist verlinkt: {path}")
        _require(sha256_file(path) == components[label]["sha256"], f"Komponenten-SHA-256 stimmt nicht: {label}")
    _require(configuration.is_dir() and not configuration.is_symlink(), "Konfigurationsverzeichnis fehlt.")
    expected: set[str] = set()
    with zipfile.ZipFile(config_archive) as archive:
        for member in archive.infolist():
            name = PurePosixPath(member.filename)
            _require(
                not name.is_absolute() and ".." not in name.parts and "\\" not in member.filename,
                f"Unsicherer Konfigurationspfad: {member.filename}",
            )
            if member.is_dir():
                continue
            _require(member.filename not in expected, "Doppelter Pfad im Konfigurationsarchiv.")
            expected.add(member.filename)
            path = configuration.joinpath(*name.parts)
            _require(path.is_file() and not path.is_symlink(), f"Konfigurationsdatei fehlt: {member.filename}")
            _require(
                all(not parent.is_symlink() for parent in path.parents if parent != configuration.parent),
                f"Verlinkter Konfigurationspfad: {member.filename}",
            )
            _require(path.read_bytes() == archive.read(member), f"Konfiguration verändert: {member.filename}")
    actual = {path.relative_to(configuration).as_posix() for path in configuration.rglob("*") if path.is_file()}
    _require(bool(expected) and actual == expected, "Konfigurationsdateien fehlen oder sind nicht im Lock gebunden.")
    _require((configuration / "scenarios.xml").is_file(), "Gebundene scenarios.xml fehlt.")
    return {
        "lock_sha256": sha256_file(lock_file),
        "validator_sha256": sha256_file(jar),
        "configuration_archive_sha256": sha256_file(config_archive),
        "configuration_files_verified": len(expected),
        "standards": locked["standards"],
        "validator_version": components["validator"]["version"],
        "validator_path": str(jar.resolve()),
        "configuration_path": str(configuration.resolve()),
    }


def synthetic_cases() -> dict[str, bytes]:
    cases: dict[str, bytes] = {}
    for syntax, tag in (("cii", "ram:TypeCode"), ("ubl", "cbc:InvoiceTypeCode")):
        payload = (PROJECT_ROOT / "app" / "examples" / f"{syntax}-rechnung-demo.xml").read_bytes()
        old = f"<{tag}>380</{tag}>".encode()
        _require(payload.count(old) == 1, f"Synthetisches {syntax}-Beispiel hat keinen eindeutigen Belegartcode.")
        cases[f"{syntax}_accept"] = payload
        cases[f"{syntax}_reject"] = payload.replace(old, f"<{tag}>999</{tag}>".encode())
    cases["malformed_xml"] = b"<Invoice"
    return cases


def _case_summary(name: str, result: dict[str, Any]) -> dict[str, Any]:
    raw_report = result.get("raw_report")
    return {
        "name": name,
        "passed": True,
        "executed": result.get("executed"),
        "accepted": result.get("accepted"),
        "exit_code": result.get("exit_code"),
        "report_source": result.get("report_source"),
        "report_sha256": hashlib.sha256(raw_report.encode()).hexdigest() if raw_report else None,
        "finding_ids": [finding["id"] for finding in result.get("findings", [])],
    }


def check_report(name: str, result: dict[str, Any], accepted: bool) -> dict[str, Any]:
    raw_report = result.get("raw_report")
    if not isinstance(raw_report, str):
        raise SmokeError(f"{name}: gültiger VARL-Bericht fehlt.")
    _, decision, assessment, valid = KositValidator._parse_report(raw_report.encode())
    _require(valid and assessment in {"accept", "reject"}, f"{name}: ausdrückliche VARL-Entscheidung fehlt.")
    _require(
        result.get("executed") is True
        and result.get("accepted") is accepted
        and decision is accepted
        and result.get("report_source") == "file",
        f"{name}: KoSIT-Ergebnis entspricht nicht der erwarteten VARL-Entscheidung {accepted}; "
        f"executed={result.get('executed')}, accepted={result.get('accepted')}, "
        f"findings={[finding['id'] for finding in result.get('findings', [])]}",
    )
    return {**_case_summary(name, result), "varl_assessment": assessment}


def check_java_failure(result: dict[str, Any]) -> dict[str, Any]:
    _require(
        result.get("configured") is True
        and result.get("executed") is False
        and result.get("accepted") is None
        and isinstance(result.get("exit_code"), int)
        and result["exit_code"] != 0
        and result.get("raw_report") is None
        and any(item["id"] == "KOSIT-EXEC" and item["severity"] == "warning" for item in result["findings"]),
        "Technischer Java-Fehler muss ohne Rechnungsentscheidung als KOSIT-EXEC gewertet werden.",
    )
    return {**_case_summary("java_start_failure", result), "fault_injection": "jar_with_missing_main_class"}


def _check_exit_code_fault(validator: KositValidator, payload: bytes, accepted: bool) -> dict[str, Any]:
    run_process = kosit._run_kosit_process
    original_codes: list[int] = []
    injected_code = 9 if accepted else 0

    def execute_with_changed_code(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        completed = run_process(*args, **kwargs)
        original_codes.append(completed.returncode)
        completed.returncode = injected_code
        return completed

    name = "varl_accept_overrides_exit_code" if accepted else "varl_reject_overrides_exit_code"
    with patch.object(kosit, "_run_kosit_process", execute_with_changed_code):
        result = validator.validate(payload, f"synthetic-{name}.xml")
    case = check_report(name, result, accepted)
    _require(len(original_codes) == 1, f"{name}: echter Java-Prozess wurde nicht genau einmal ausgeführt.")
    _require("KOSIT-RESULT-MISMATCH" in case["finding_ids"], f"{name}: Rückgabecode-Abweichung wurde nicht gemeldet.")
    return {**case, "fault_injection": "return_code_only", "actual_java_exit_code": original_codes[0]}


def run_smoke(vendor_root: Path, java: str, config_archive: Path, lock_file: Path) -> dict[str, Any]:
    binding = verify_components(vendor_root, config_archive, lock_file)
    java_path = shutil.which(java)
    if java_path is None:
        raise SmokeError(f"Java wurde nicht gefunden: {java}")
    java = str(Path(java_path).resolve())
    version = subprocess.run([java, "-version"], capture_output=True, timeout=30, check=False)
    _require(version.returncode == 0, "Java-Versionsabfrage ist fehlgeschlagen.")
    configuration = Path(binding["configuration_path"])
    settings = Settings(
        kosit_enabled=True,
        kosit_java_bin=java,
        kosit_validator_jar=Path(binding["validator_path"]),
        kosit_scenarios=(configuration / "scenarios.xml",),
        kosit_repositories=(configuration,),
        kosit_timeout_seconds=120,
    )
    validator = KositValidator(settings)
    fixtures = synthetic_cases()
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "passed": False,
        "bindings": binding,
        "system": platform.platform(),
        "python_version": platform.python_version(),
        "java_path": str(Path(java_path).resolve()),
        "java_executable_sha256": sha256_file(Path(java_path)),
        "java_version": (version.stdout + version.stderr).decode("utf-8", errors="replace").strip(),
        "fixtures_sha256": {name: hashlib.sha256(payload).hexdigest() for name, payload in fixtures.items()},
        "cases": [],
    }
    for name, payload in fixtures.items():
        result = validator.validate(payload, f"synthetic-{name}.xml")
        case = check_report(name, result, name.endswith("_accept"))
        if name.endswith("_reject"):
            _require(
                any("BR-CL-01" in finding.get("message", "") for finding in result["findings"]),
                f"{name}: Ablehnung muss die absichtlich ungültige Belegart benennen (BR-CL-01).",
            )
        if name == "malformed_xml":
            _require("val-xml.1" in case["finding_ids"], "Unverarbeitbares XML muss als XML-Fehler erkannt werden.")
        evidence["cases"].append(case)
    with tempfile.TemporaryDirectory(prefix="kosit-smoke-fault-") as temporary:
        broken_jar = Path(temporary) / "synthetic-missing-main-class.jar"
        with zipfile.ZipFile(broken_jar, "w") as archive:
            archive.writestr(
                "META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\nMain-Class: synthetic.MissingMainClass\r\n\r\n"
            )
        failed = KositValidator(replace(settings, kosit_validator_jar=broken_jar))
        evidence["cases"].append(check_java_failure(failed.validate(fixtures["ubl_accept"], "synthetic.xml")))
    for accepted in (True, False):
        payload = fixtures["ubl_accept" if accepted else "ubl_reject"]
        evidence["cases"].append(_check_exit_code_fault(validator, payload, accepted))
    # Fail if preparation changed while the real validators were running.
    _require(binding == verify_components(vendor_root, config_archive, lock_file), "Komponenten änderten sich im Lauf.")
    evidence["passed"] = True
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor-root", type=Path, required=True, help="Vorbereitetes vendor/kosit-Verzeichnis")
    parser.add_argument("--java", default="java", help="Java-Programm oder vollständiger Pfad")
    parser.add_argument("--config-archive", type=Path, required=True, help="Unverändertes ZIP aus dem Komponenten-Lock")
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)
    parser.add_argument("--output", type=Path, required=True, help="JSON-Nachweis dieses Smoke-Tests")
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        print(f"Nachweisziel existiert bereits und wird nicht überschrieben: {args.output}", file=sys.stderr)
        return 1
    try:
        evidence = run_smoke(args.vendor_root.resolve(), args.java, args.config_archive.resolve(), args.lock_file)
    except (SmokeError, InstallError, OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        evidence = {"schema_version": 1, "passed": False, "error": str(exc)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    print(f"KoSIT-Komponenten-Smoke: {'bestanden' if evidence['passed'] else 'fehlgeschlagen'} ({args.output})")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
