#!/usr/bin/env python3
"""Derive and verify pip-free Docker runtime locks without resolving packages.

The original Docker lock remains the verified builder/wheel source. This separate
format records exactly one permitted projection: remove its explicit pip bootstrap
root and pip distribution. Native wheel-byte verification still uses the parent
lock before installation; this tool performs no downloads or installations.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = Path(__file__).resolve()
SCHEMA_VERSION = 1
KIND = "docker-runtime-lock"
PYTHON_VERSION = "3.14.7"
PARENT_PROFILES = {"docker-amd64", "docker-arm64"}


def load_support(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH.with_name(name + ".py"))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Lock-Prüfwerkzeug konnte nicht geladen werden: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lock = load_support("dependency_lock")
audit = load_support("dependency_audit")


def sidecar(path: Path) -> Path:
    return Path(str(path) + ".metadata.json")


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def metadata_bytes(metadata: dict[str, Any]) -> bytes:
    return (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode("utf-8")


def derive_runtime_lock(parent_lock: Path, project_root: Path = PROJECT_ROOT) -> tuple[bytes, dict[str, Any]]:
    """Validate the original generator bindings and preserve every retained wheel."""
    parent_bytes = parent_lock.read_bytes()
    parent_metadata_bytes = sidecar(parent_lock).read_bytes()
    parent = lock.check_lock(parent_lock, project_root)
    if parent["profile"] not in PARENT_PROFILES or parent["target"]["python_full_version"] != PYTHON_VERSION:
        raise lock.LockError("Runtime-Ableitung benötigt ein Dockerprofil mit exakt CPython 3.14.7.")
    bootstrap = parent["bootstrap"]
    if (
        len(bootstrap) != 1
        or lock.canonicalize_name(lock.plain_requirement(bootstrap[0]).name) != "pip"
        or parent["additional_requirements"]
    ):
        raise lock.LockError("Erlaubt ist ausschließlich die einzelne Pip-Bootstraproot ohne Zusatzanforderungen.")
    project = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    if any(
        lock.canonicalize_name(lock.plain_requirement(value).name) == "pip"
        for value in project["project"]["dependencies"]
    ):
        raise lock.LockError("Pip ist eine Projektabhängigkeit und darf nicht aus der Runtime entfernt werden.")
    pip_root = str(lock.plain_requirement(bootstrap[0]))
    if parent["roots"].count(pip_root) != 1:
        raise lock.LockError("Die einzelne Pip-Bootstraproot fehlt im ursprünglichen Profil.")
    removed = [package for package in parent["packages"] if package["name"] == "pip"]
    if len(removed) != 1:
        raise lock.LockError("Das ursprüngliche Profil muss genau eine Pip-Distribution enthalten.")
    roots = [value for value in parent["roots"] if value != pip_root]
    packages = [package for package in parent["packages"] if package["name"] != "pip"]
    # This rejects pip required by any remaining dependency, including extras and
    # target markers, as well as any newly unreachable package. No ignore list.
    lock.verify_closure(roots, packages, parent["target"])
    profile = parent["profile"].replace("docker-", "docker-runtime-", 1)
    text = f"# Derived {profile}; parent wheel metadata and hashes are unchanged.\n"
    text += "".join(
        f"{package['name']}=={package['version']} --hash=sha256:{package['sha256']}\n"
        for package in sorted(packages, key=lambda package: package["name"])
    )
    payload = text.encode("utf-8")
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "profile": profile,
        "target": parent["target"],
        "compatible_tags": parent["compatible_tags"],
        "roots": roots,
        "packages": packages,
        "removed": removed,
        "lock_sha256": digest(payload),
        "parent": {
            "profile": parent["profile"],
            "lock_sha256": digest(parent_bytes),
            "metadata_sha256": digest(parent_metadata_bytes),
            "generator": parent["generator"],
            "inputs": parent["inputs"],
        },
        "derivation": {
            "policy": "remove-pip-bootstrap-only",
            "script_sha256": lock.source_digest(SCRIPT_PATH),
            "source_normalization": "utf8-lf",
        },
    }
    if parent_bytes != parent_lock.read_bytes() or parent_metadata_bytes != sidecar(parent_lock).read_bytes():
        raise lock.LockError("Ursprünglicher Lock oder Sidecar wurde während der Ableitung verändert.")
    return payload, metadata


def check_runtime_lock(runtime_lock: Path, parent_lock: Path, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Recompute the complete projection; do not trust editable runtime metadata."""
    expected_bytes, expected_metadata = derive_runtime_lock(parent_lock, project_root)
    if runtime_lock.read_bytes() != expected_bytes:
        raise lock.LockError("Runtime-Lock stimmt nicht exakt mit der erlaubten Parent-Ableitung überein.")
    if sidecar(runtime_lock).read_bytes() != metadata_bytes(expected_metadata):
        raise lock.LockError("Runtime-Sidecar oder Herkunftsbindung stimmt nicht mit der Ableitung überein.")
    return expected_metadata


def verify_runtime_inventory(metadata: dict[str, Any], inventory: dict[str, Any]) -> None:
    """Compare a final-image capture against metadata returned by check_runtime_lock."""
    audit.validate_inventory(inventory)
    if "excluded_editable" not in inventory or inventory["excluded_editable"] is not None:
        raise lock.LockError("Docker-Runtime-Inventare dürfen keine editable-Distribution ausnehmen.")
    lock.compare_target_inventory(metadata, inventory)


def write_runtime_lock(output: Path, parent_lock: Path, project_root: Path = PROJECT_ROOT) -> None:
    payload, metadata = derive_runtime_lock(parent_lock, project_root)
    artifacts = {output: payload, sidecar(output): metadata_bytes(metadata)}
    protected = {parent_lock.resolve(), sidecar(parent_lock).resolve()}
    for path, content in artifacts.items():
        if path.resolve() in protected or path.is_symlink():
            raise lock.LockError("Ursprüngliche Locks oder Symlinkziele dürfen nicht überschrieben werden.")
        if path.exists() and path.read_bytes() != content:
            raise lock.LockError(f"Abweichende Ausgabedatei wird nicht überschrieben: {path}")
    output.parent.mkdir(parents=True, exist_ok=True)
    for path, content in artifacts.items():
        # Exclusive creation also prevents replacing a file introduced after the
        # preflight. An interrupted pair fails check; it cannot pass partially.
        if not path.exists():
            with path.open("xb") as handle:
                handle.write(content)
    check_runtime_lock(output, parent_lock, project_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("derive", "check", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--parent", type=Path, required=True)
        command.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
        if name == "derive":
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--lock", type=Path, required=True)
        if name == "verify":
            command.add_argument("--inventory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "derive":
            write_runtime_lock(args.output, args.parent, args.project_root)
        else:
            metadata = check_runtime_lock(args.lock, args.parent, args.project_root)
            if args.command == "verify":
                inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
                if not isinstance(inventory, dict):
                    raise lock.LockError("Runtime-Inventar muss ein JSON-Objekt sein.")
                verify_runtime_inventory(metadata, inventory)
        print("Docker-Runtime-Lock erfolgreich geprüft.")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Docker-Runtime-Lock fehlgeschlagen: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
