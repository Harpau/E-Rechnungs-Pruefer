#!/usr/bin/env python3
"""Capture an installed environment and audit exactly that inventory, on any host.

Capture intentionally uses only the standard library, including in product images.
The separate audit process requires pip-audit, but never installs target packages.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1


class AuditError(ValueError):
    """An incomplete or ambiguous audit is a failure, never a clean result."""


def canonical_name(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise AuditError("Ungültiger Paketname im Inventar.")
    return re.sub(r"[-_.]+", "-", value).lower()


def inventory_packages(value: object) -> dict[str, str]:
    if not isinstance(value, list) or not value:
        raise AuditError("Paketinventar fehlt oder ist leer.")
    result: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict):
            raise AuditError("Ungültiger Inventareintrag.")
        name = canonical_name(item.get("name"))
        version = item.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.!+_-]+", version):
            raise AuditError(f"Ungültige Version: {name}")
        if name in result:
            raise AuditError(f"Distribution doppelt vorhanden: {name}")
        result[name] = version
    return result


def inventory_digest(packages: object) -> str:
    inventory = inventory_packages(packages)
    payload = json.dumps(sorted(inventory.items()), separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def collect_inventory(distributions: Any, own_name: str, project_root: Path) -> dict[str, Any]:
    packages: list[dict[str, str]] = []
    excluded: dict[str, str] | None = None
    seen: set[str] = set()
    for distribution in distributions:
        name = canonical_name(distribution.metadata.get("Name"))
        if name in seen:
            raise AuditError(f"Distribution doppelt vorhanden: {name}")
        seen.add(name)
        origin_text = distribution.read_text("direct_url.json")
        origin = json.loads(origin_text) if origin_text else {}
        if not isinstance(origin, dict):
            raise AuditError(f"Ungültige Herkunftsmetadaten: {name}")
        directory = origin.get("dir_info", {})
        if not isinstance(directory, dict):
            raise AuditError(f"Ungültige Verzeichnismetadaten: {name}")
        if directory.get("editable"):
            url = urlsplit(origin.get("url", ""))
            # Bind both distribution identity and the exact local checkout.
            if (
                name != canonical_name(own_name)
                or url.scheme != "file"
                or url.netloc not in ("", "localhost")
                or Path(url2pathname(url.path)).resolve() != project_root.resolve()
                or excluded is not None
            ):
                raise AuditError(f"Ungebundene editable-Distribution: {name}")
            excluded = {"name": name, "version": distribution.version, "project_root": str(project_root.resolve())}
        else:
            packages.append({"name": name, "version": distribution.version})
    packages.sort(key=lambda p: p["name"])
    return {
        "schema_version": SCHEMA_VERSION,
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "sys_platform": sys.platform,
            "machine": platform.machine(),
            "gil_disabled": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        },
        "packages": packages,
        "inventory_sha256": inventory_digest(packages),
        "excluded_editable": excluded,
    }


def installed_inventory(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    pyproject = project_root / "pyproject.toml"
    own_name = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["name"]
    return collect_inventory(importlib.metadata.distributions(), own_name, project_root)


def validate_inventory(inventory: dict[str, Any]) -> None:
    if inventory.get("schema_version") != SCHEMA_VERSION:
        raise AuditError("Nicht unterstütztes Inventar-Schema.")
    if inventory.get("inventory_sha256") != inventory_digest(inventory.get("packages")):
        raise AuditError("Inventarhash stimmt nicht mit den Paketen überein.")


def compare_lock(inventory: dict[str, Any], path: Path) -> None:
    # Keep image capture independent of packaging and the lock generator.
    expected: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+) --hash=sha256:([a-f0-9]{64})", line)
        if not match:
            raise AuditError("Lock ist nicht vollständig gepinnt, markerfrei und SHA-256-gehasht.")
        name = canonical_name(match[1])
        if name in expected:
            raise AuditError(f"Lock enthält einen doppelten Namen: {name}")
        expected[name] = match[2]
    actual = inventory_packages(inventory["packages"])
    if not expected or actual != expected:
        raise AuditError("Installiertes Inventar stimmt nicht vollständig mit dem Lock überein.")


def audit_command(requirements: Path, output: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pip_audit",
        "--strict",
        "--disable-pip",
        "--no-deps",
        "--progress-spinner",
        "off",
        "--format",
        "json",
        "--output",
        str(output),
        "-r",
        str(requirements),
    ]


def validate_audit_result(inventory: dict[str, Any], report: dict[str, Any]) -> None:
    dependencies = report.get("dependencies")
    if not isinstance(dependencies, list):
        raise AuditError("Audit enthält keine vollständige Abhängigkeitsliste.")
    for dependency in dependencies:
        if not isinstance(dependency, dict) or "skip_reason" in dependency:
            raise AuditError("Mindestens eine Distribution wurde beim Audit übersprungen.")
        if not isinstance(dependency.get("vulns"), list):
            raise AuditError("Audit enthält kein verifiziertes Ergebnis je Paket.")
    if inventory_packages(dependencies) != inventory_packages(inventory["packages"]):
        raise AuditError("Audit hat das Inventar nicht vollständig und versionsgenau erfasst.")


def audit_inventory(inventory: dict[str, Any], output: Path) -> int:
    validate_inventory(inventory)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="einvoice-audit-") as temporary:
        requirements = Path(temporary) / "inventory.txt"
        requirements.write_text(
            "".join(
                f"{name}=={version}\n" for name, version in sorted(inventory_packages(inventory["packages"]).items())
            ),
            encoding="utf-8",
        )
        fresh_output = Path(temporary) / "audit.json"
        # Retain the unmodified report and stderr, including on collection/network failure.
        result = subprocess.run(audit_command(requirements, fresh_output), capture_output=True, text=True, check=False)
        Path(str(output) + ".stderr.txt").write_text(result.stderr, encoding="utf-8")
        Path(str(output) + ".stdout.txt").write_text(result.stdout, encoding="utf-8")
        if not fresh_output.is_file():
            raise AuditError(f"Audit erzeugte keinen Bericht (Exitcode {result.returncode}).")
        raw_report = fresh_output.read_text(encoding="utf-8")
        output.write_text(raw_report, encoding="utf-8")
        report = json.loads(raw_report)
        validate_audit_result(inventory, report)
        if result.returncode != 0:
            return result.returncode
        if any(package["vulns"] for package in report["dependencies"]):
            raise AuditError("Schwachstellen trotz erfolgreichem Prozessstatus gemeldet.")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    capture.add_argument("--lock", type=Path)
    capture.add_argument("--python", help="Inventar in diesem Zielinterpreter ohne zusätzliche Pakete erfassen.")
    audit = commands.add_parser("audit")
    audit.add_argument("--inventory", type=Path, required=True)
    audit.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            if args.python:
                command = [
                    args.python,
                    str(Path(__file__).resolve()),
                    "capture",
                    "--output",
                    str(args.output.resolve()),
                    "--project-root",
                    str(args.project_root.resolve()),
                ]
                if args.lock:
                    command.extend(["--lock", str(args.lock.resolve())])
                return subprocess.run(command, check=False).returncode
            inventory = installed_inventory(args.project_root)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if args.lock:
                compare_lock(inventory, args.lock)
            return 0
        return audit_inventory(json.loads(args.inventory.read_text(encoding="utf-8")), args.output)
    except (AuditError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Dependency-Audit fehlgeschlagen: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
