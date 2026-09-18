"""Prepare and bind the fixed C6 Windows bytes for the authorized diagnostic.

No installation, process control, fallback artifact, or implicit workflow retry.
The current checkout identifies the harness; SOURCE_COMMIT identifies the product.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import acceptance_context as acceptance  # noqa: E402

REPOSITORY = "Harpau/E-Rechnungs-Pruefer"
SOURCE_COMMIT = "630444b57e81d524a61b128abe78ce68278719b5"
SOURCE_RUN = 35263705992
ARTIFACT_ID = 10515799142
ARTIFACT_NAME = "windows-x64-package-48b2be57eba3b40cf75c732f35cc80d6e96f507a"
ARCHIVE_BYTES = 396717424
ARCHIVE_SHA256 = "80199f1c5e466824fa418824b7303b24f842363a12e176f2fdedcdf8e1d477bf"
PREFIX = "E-Rechnungs-Pruefer-2.0.3-Windows-x64"
DESKTOP_SHA256 = "c269896505e95be1d46c5ae4c0c4934205f1c485b520e5c379947afaf1990b60"
DESKTOP_EXE_SHA256 = "ea87b2ef3a77feec9ee4bfb39ed4dacf00f1fd240c280624a2d67c17db0ba214"
OUTER = {
    f"{PREFIX}-Setup.exe": DESKTOP_SHA256,
    f"{PREFIX}-Dienst-Setup.exe": "702c2f23a94b322e8213137ce994ee35503b377f2a9489fe9615268e65cdb33b",
    f"{PREFIX}-Binaries.zip": "6d9f31306d22150c81d0faa5af0e1829cddd116821dd132ecf70cc7887719b30",
    f"{PREFIX}-SHA256SUMS.txt": "373b82faa5bb2aad09fe7304a45529a7203901504b0c9632c60b1c13a552bf8b",
}
INNER = {
    "bundle/desktop/E-Rechnungs-Pruefer.exe": DESKTOP_EXE_SHA256,
    "bundle/service/E-Rechnungs-Pruefer-Dienst.exe": "86cdbcf3511ed94a547b95b30d5887dd11ab44cdd925c82dd37c636babfb23a7",
    "bundle/E-Rechnungs-Pruefer-Oeffnen.exe": "290d3c655cfc0fd0db2b25098ee12dd921fbd39e9fa82ecd9f0bc7bb461241c4",
}
SETUP_RELATIVE = Path("dist") / f"{PREFIX}-Setup.exe"
EXE_RELATIVE = Path("build/windows/bundle/E-Rechnungs-Pruefer/E-Rechnungs-Pruefer.exe")
EVIDENCE_RELATIVE = Path(".cache/windows-diagnostic")


class DiagnosticError(ValueError):
    pass


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def verify_metadata(value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("workflow_run"), dict):
        raise DiagnosticError("Original artifact metadata is incomplete.")
    if (
        value.get("expired") is not False
        or any(
            value.get(key) != expected
            for key, expected in {
                "id": ARTIFACT_ID,
                "name": ARTIFACT_NAME,
                "size_in_bytes": ARCHIVE_BYTES,
                "digest": "sha256:" + ARCHIVE_SHA256,
                "expired": False,
            }.items()
        )
        or any(
            value.get("workflow_run", {}).get(key) != expected
            for key, expected in {"id": SOURCE_RUN, "head_sha": SOURCE_COMMIT}.items()
        )
    ):
        raise DiagnosticError("Originalartifact metadata differs; no fallback permitted.")


def validate_members(archive: zipfile.ZipFile) -> None:
    seen: set[str] = set()
    members = archive.infolist()
    if len(members) > 5000 or sum(member.file_size for member in members) > 1024**3:
        raise DiagnosticError("Archive inventory exceeds the fixed input budget.")
    for member in members:
        name = member.filename
        path = PurePosixPath(name)
        parts = name.removesuffix("/").split("/")
        if (
            not name
            or path.is_absolute()
            or "\\" in name
            or ":" in name
            or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts)
            or name.casefold() in seen
            or stat.S_ISLNK(member.external_attr >> 16)
        ):
            raise DiagnosticError("Unsafe, duplicate or linked archive member.")
        seen.add(name.casefold())


def copy_member(archive: zipfile.ZipFile, name: str, target: Path, expected: str) -> None:
    target = acceptance._safe_path(target)
    value = hashlib.sha256()
    with archive.open(name) as source, target.open("xb") as destination:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
            destination.write(chunk)
    if value.hexdigest() != expected:
        raise DiagnosticError("Extracted original bytes differ; retained for diagnosis, never executed.")


def diagnostic_environment() -> None:
    required = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_JOB": "windows-diagnostic",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": "refs/heads/codex/dependency-maintenance-2026-09",
        "GITHUB_RUN_ATTEMPT": "1",
        "RUNNER_OS": "Windows",
        "RUNNER_ARCH": "X64",
        "EINVOICE_WINDOWS_DIAGNOSTIC_ONLY": "true",
    }
    if any(os.environ.get(name) != expected for name, expected in required.items()):
        raise DiagnosticError("Only the explicitly dispatched first Windows diagnostic attempt is allowed.")


def prepare() -> dict[str, Any]:
    diagnostic_environment()
    binding = acceptance.ci_binding("2.0.3")
    destination = acceptance._safe_path(ROOT / EVIDENCE_RELATIVE)
    destination.mkdir(parents=True, exist_ok=False)
    route = f"repos/{REPOSITORY}/actions/artifacts/{ARTIFACT_ID}"
    raw = subprocess.check_output(["gh", "api", route], timeout=60)
    if len(raw) > 64 * 1024:
        raise DiagnosticError("Artifact metadata too large.")
    metadata = json.loads(raw)
    (destination / "artifact-metadata.json").write_bytes(raw)
    verify_metadata(metadata)
    original = destination / "original.zip"
    with original.open("xb") as output:
        subprocess.run(["gh", "api", route + "/zip"], stdout=output, timeout=300, check=True)
    if original.stat().st_size != ARCHIVE_BYTES or digest(original) != ARCHIVE_SHA256:
        raise DiagnosticError("Original archive bytes differ; nothing extracted.")
    dist = acceptance._safe_path(ROOT / "dist")
    dist.mkdir(exist_ok=False)
    with zipfile.ZipFile(original) as archive:
        validate_members(archive)
        if set(archive.namelist()) != set(OUTER):
            raise DiagnosticError("Original artifact does not contain exactly the four published files.")
        for name, expected in OUTER.items():
            copy_member(archive, name, dist / name, expected)
    expected_manifest = {**INNER, **{name: h for name, h in OUTER.items() if not name.endswith("SHA256SUMS.txt")}}
    lines = (dist / f"{PREFIX}-SHA256SUMS.txt").read_text().splitlines()
    if (
        len(lines) != len(expected_manifest)
        or {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in lines} != expected_manifest
    ):
        raise DiagnosticError("Published manifest differs from the pinned owned files.")
    copies: list[dict[str, Any]] = []
    with zipfile.ZipFile(dist / f"{PREFIX}-Binaries.zip") as archive:
        validate_members(archive)
        for name, expected in INNER.items():
            relative = EXE_RELATIVE if name.startswith("bundle/desktop/") else EVIDENCE_RELATIVE / Path(name).name
            target = acceptance._safe_path(ROOT / relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_member(archive, name, target, expected)
            copies.append({"archive_member": name, "target": relative.as_posix(), "sha256": expected})
    receipt = {
        "status": "PASS",
        "scope": "fixed original product artifact preparation; no product execution",
        "harness_binding": binding,
        "product_source_commit": SOURCE_COMMIT,
        "product_source_run": SOURCE_RUN,
        "artifact_id": ARTIFACT_ID,
        "archive_sha256": ARCHIVE_SHA256,
        "outer_files": OUTER,
        "derived_copies": copies,
        "frozen_receipts": "Historical C6 receipts remain provenance only; this is not a new build.",
    }
    acceptance._exclusive(destination / "input-binding.json", acceptance.canonical(receipt) + b"\n")
    return receipt


def expected_command(setup: Path) -> list[str]:
    return [
        "pwsh",
        "-NoProfile",
        "-File",
        "scripts/test_windows_package.ps1",
        "-ConfirmIsolatedEnvironment",
        "-Setup",
        SETUP_RELATIVE.as_posix(),
        "-ProcessingDiagnosticOnly",
    ]


def required_paths(setup: Path) -> list[Path]:
    return [
        setup,
        ROOT / EXE_RELATIVE,
        ROOT / EVIDENCE_RELATIVE / "input-binding.json",
        *(
            ROOT / "scripts" / name
            for name in (
                "windows_package_diagnostic.py",
                "test_windows_package.ps1",
                "test_processing_package.ps1",
                "test_processing_package.py",
                "processing_smoke.py",
            )
        ),
    ]


def active_context() -> dict[str, Any]:
    diagnostic_environment()
    state = acceptance.verify(Path(os.environ.get("EINVOICE_ACCEPTANCE_ROOT", "")))
    if state["blocked"] or state["controller"] != os.environ.get("EINVOICE_ACCEPTANCE_CONTROLLER"):
        raise DiagnosticError("Diagnostic controller is blocked or differs.")
    if state["binding"] != acceptance.ci_binding("2.0.3"):
        raise DiagnosticError("Diagnostic checkout/run/attempt binding differs.")
    active = [context for context in state["contexts"].values() if context["status"] == "RUNNING"]
    if len(active) != 1 or active[0]["action"] != "desktop":
        raise DiagnosticError("One consumed Desktop diagnostic context required.")
    context = active[0]
    if (
        not acceptance.parse_timestamp(context["consumed_at_utc"])
        <= datetime.now(UTC)
        < acceptance.parse_timestamp(context["expires_at_utc"])
    ):
        raise DiagnosticError("Diagnostic context expired.")
    return context


def verify_scope(setup: Path) -> dict[str, Any]:
    context = active_context()
    if context["command"] != expected_command(setup) or acceptance._safe_path(setup) != ROOT / SETUP_RELATIVE:
        raise DiagnosticError("Command or original installer path differs from diagnostic scope.")
    for path in required_paths(setup):
        if acceptance._file(path) not in context["artifacts"]:
            raise DiagnosticError("Diagnostic input or harness is not bound to the consumed context.")
    if digest(setup) != DESKTOP_SHA256 or digest(ROOT / EXE_RELATIVE) != DESKTOP_EXE_SHA256:
        raise DiagnosticError("Diagnostic requires the exact original C6 Desktop product bytes.")
    return {"status": "PASS", "scope": "processing-diagnostic-only", "cases": ["held-responses", "health"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    scope = commands.add_parser("verify-scope")
    scope.add_argument("--setup", type=Path, required=True)
    args = parser.parse_args()
    try:
        value = prepare() if args.command == "prepare" else verify_scope(args.setup)
        print(json.dumps(value))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "FAIL", "error_class": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
