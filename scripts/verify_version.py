#!/usr/bin/env python3
"""Verify that all release-relevant version declarations are synchronized."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")


def _read_app_version() -> str:
    content = (PROJECT_ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
    if not match:
        raise ValueError("In app/__init__.py wurde keine __version__-Angabe gefunden.")
    return match.group(1)


def _read_installer_version() -> str:
    content = (PROJECT_ROOT / "scripts" / "install_kosit.py").read_text(encoding="utf-8")
    match = re.search(r'USER_AGENT\s*=\s*["\']e-rechnung-pruefer-kosit-installer/([^"\']+)["\']', content)
    if not match:
        raise ValueError("Im KoSIT-Installer wurde keine Version im USER_AGENT gefunden.")
    return match.group(1)


def collect_versions() -> dict[str, str]:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {
        "VERSION": (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        "pyproject.toml": str(pyproject["project"]["version"]),
        "app/__init__.py": _read_app_version(),
        "scripts/install_kosit.py": _read_installer_version(),
    }


def verify() -> str:
    versions = collect_versions()
    unique = set(versions.values())
    if len(unique) != 1:
        details = ", ".join(f"{name}={value!r}" for name, value in versions.items())
        raise ValueError(f"Versionsangaben stimmen nicht überein: {details}")
    version = next(iter(unique))
    if not SEMVER_PATTERN.fullmatch(version):
        raise ValueError(f"Die Version {version!r} entspricht nicht dem erwarteten SemVer-Format.")
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print", action="store_true", dest="print_only", help="Nur die geprüfte Version ausgeben")
    args = parser.parse_args()
    try:
        version = verify()
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"Versionsprüfung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    print(version if args.print_only else f"Versionsangaben sind konsistent: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
