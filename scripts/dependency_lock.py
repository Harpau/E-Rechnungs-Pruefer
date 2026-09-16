#!/usr/bin/env python3
"""Native wheel-only release locks; verification never resolves newer versions.

refresh resolves explicit profile roots and records actual wheel bytes/metadata.
check validates a committed lock offline on any host. verify additionally checks
the native target and wheel bytes; --installed compares the complete environment.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import unquote, urlsplit

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import compatible_tags as generic_tags
from packaging.tags import cpython_tags, parse_tag, sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
PROFILES = {
    "windows-release": ("win32", "AMD64", "Windows", "nt"),
    "source-release": ("linux", "x86_64", "Linux", "posix"),
    "docker-amd64": ("linux", "x86_64", "Linux", "posix"),
    "docker-arm64": ("linux", "aarch64", "Linux", "posix"),
}
TARGET_KEYS = ("sys_platform", "platform_machine", "implementation_name", "python_full_version")


class LockError(ValueError):
    """Reject incomplete, incompatible or unbound release dependencies."""


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def source_digest(path: Path) -> str:
    """Git may check text out with CRLF on Windows; bind its normalized UTF-8 source."""
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def target_environment(profile: str, python_version: str) -> dict[str, str]:
    version = Version(python_version)
    if version.is_prerelease or len(version.release) != 3 or version.release[0] != 3:
        raise LockError("Das Ziel benötigt eine exakte stabile Python-Patchversion.")
    system, machine, system_name, os_name = PROFILES[profile]
    return {
        "implementation_name": "cpython",
        "implementation_version": str(version),
        "os_name": os_name,
        "platform_machine": machine,
        "platform_python_implementation": "CPython",
        "platform_release": "",
        "platform_system": system_name,
        "platform_version": "",
        "python_full_version": str(version),
        "python_version": ".".join(map(str, version.release[:2])),
        "sys_platform": system,
    }


def host_environment() -> dict[str, str]:
    env = {key: str(value) for key, value in default_environment().items()}
    machine = env["platform_machine"].lower()
    if machine in ("x86_64", "amd64"):
        env["platform_machine"] = "AMD64" if sys.platform == "win32" else "x86_64"
    elif machine in ("aarch64", "arm64"):
        env["platform_machine"] = "aarch64"
    return env


def require_native(target: dict[str, str]) -> None:
    current = host_environment()
    if any(current[key] != target.get(key) for key in TARGET_KEYS) or sysconfig.get_config_var("Py_GIL_DISABLED"):
        raise LockError("Interpreter, Betriebssystem, Architektur oder ABI stimmt nicht mit dem nativen Ziel überein.")


def validate_target_tags(tags: set[str], target: dict[str, str]) -> None:
    parsed = {tag for value in tags for tag in parse_tag(value)}
    platforms = {tag.platform for tag in parsed}
    for value in platforms:
        if value == "any":
            continue
        if target["sys_platform"] == "win32":
            valid = value == "win_amd64"
        else:
            valid = bool(
                re.fullmatch(r"(?:linux|manylinux[0-9_]+|musllinux[0-9_]+)_" + target["platform_machine"], value)
            )
        if not valid:
            raise LockError("Plattformtags gehören zu einem anderen Ziel.")
    python = tuple(Version(target["python_full_version"]).release[:2])
    interpreter = "cp" + "".join(map(str, python))
    allowed = {
        *cpython_tags(python_version=python, abis=[interpreter], platforms=sorted(platforms)),
        *generic_tags(python_version=python, interpreter=interpreter, platforms=sorted(platforms)),
    }
    if not parsed or not parsed <= allowed:
        raise LockError("Interpreter-/ABI-Tags gehören zu einem anderen Ziel.")


def plain_requirement(value: str) -> Requirement:
    requirement = Requirement(value)
    if requirement.url is not None:
        raise LockError("URL-/VCS-Abhängigkeiten sind in Releaseprofilen nicht erlaubt.")
    return requirement


def profile_inputs(
    root: Path, profile: str, bootstrap: list[str], additional: list[str]
) -> tuple[list[str], dict[str, str]]:
    project_path = root / "pyproject.toml"
    project = tomllib.loads(project_path.read_text(encoding="utf-8"))
    inputs = {"pyproject.toml": source_digest(project_path)}
    roots = list(project["project"]["dependencies"])
    pinned: set[str] = set()
    for value in bootstrap:
        requirement = plain_requirement(value)
        specifiers = list(requirement.specifier)
        if (
            requirement.marker
            or requirement.extras
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise LockError("Bootstrapwerkzeuge müssen exakt und ohne Marker gepinnt sein.")
        pinned.add(canonicalize_name(requirement.name))
    mandatory = {"pip"} if profile.startswith("docker-") else {"pip", "setuptools", "wheel"}
    if not mandatory <= pinned:
        raise LockError(f"Explizite Bootstrap-Pins fehlen: {', '.join(sorted(mandatory - pinned))}")
    if not profile.startswith("docker-"):
        roots.extend(project["build-system"]["requires"])
    dev = project["project"].get("optional-dependencies", {}).get("dev", [])
    if profile == "source-release":
        roots.extend(dev)
    elif profile == "windows-release":
        wanted = {"pytest", "httpx", "httpx2"}
        tests = [value for value in dev if canonicalize_name(Requirement(value).name) in wanted]
        if {canonicalize_name(Requirement(value).name) for value in tests} != wanted:
            raise LockError("Windows-Testeingaben sind unvollständig.")
        roots.extend(tests)
        build_path = root / "packaging/windows/requirements-build.txt"
        inputs["packaging/windows/requirements-build.txt"] = source_digest(build_path)
        roots.extend(
            line.strip()
            for line in build_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    roots.extend(bootstrap)
    roots.extend(additional)
    return sorted({str(plain_requirement(value)) for value in roots}), inputs


def package_map(packages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for package in packages:
        name = canonicalize_name(package["name"])
        version = Version(package["version"])
        if name in result or version.is_prerelease or version.is_devrelease:
            raise LockError(f"Doppelter Name oder nicht stabile Version: {name}")
        result[name] = package
    if not result:
        raise LockError("Paketmenge ist leer.")
    return result


def verify_closure(roots: list[str], packages: list[dict[str, Any]], environment: dict[str, str]) -> None:
    locked = package_map(packages)
    for name, package in locked.items():
        if not SpecifierSet(package.get("requires_python") or "").contains(environment["python_full_version"]):
            raise LockError(f"Python-Version wird von {name} nicht unterstützt.")
    pending = [plain_requirement(value) for value in roots]
    active: dict[str, set[str]] = {}
    while pending:
        requirement = pending.pop()
        if requirement.marker and not requirement.marker.evaluate(environment | {"extra": ""}):
            continue
        name = canonicalize_name(requirement.name)
        if name not in locked:
            raise LockError(f"Transitive Abhängigkeit oder aktiviertes Extra fehlt: {name}")
        package = locked[name]
        if not requirement.specifier.contains(package["version"], prereleases=False):
            raise LockError(f"Unvereinbarer Pin für {name}: {requirement}")
        extras = {"", *(canonicalize_name(extra) for extra in requirement.extras)}
        new_extras = extras - active.get(name, set())
        active.setdefault(name, set()).update(extras)
        for extra in new_extras:
            for value in package.get("requires_dist", []):
                dependency = plain_requirement(value)
                if dependency.marker and not dependency.marker.evaluate(environment | {"extra": extra}):
                    continue
                # The parent extra was evaluated here; preserve child extras but
                # do not re-evaluate the parent expression against an empty extra.
                dependency.marker = None
                pending.append(dependency)
    if set(locked) != set(active):
        raise LockError(f"Nicht vom Profil erreichbare Pakete: {', '.join(sorted(set(locked) - set(active)))}")


def parse_lock(text: str) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+) --hash=sha256:([a-f0-9]{64})", line)
        if not match:
            raise LockError("Lock muss markerfrei, exakt gepinnt und SHA-256-gehasht sein.")
        name = canonicalize_name(match[1])
        version = Version(match[2])
        if name in result or version.is_prerelease or version.is_devrelease:
            raise LockError(f"Doppelter Name oder nicht stabile Version: {name}")
        result[name] = (str(version), match[3])
    if not result:
        raise LockError("Lock ist leer.")
    return result


def read_wheel(path: Path, digest: str, compatible_tags: set[str]) -> dict[str, Any]:
    if sha256(path) != digest:
        raise LockError(f"SHA-256 stimmt nicht: {path.name}")
    name, version, _, filename_tags = parse_wheel_filename(path.name)
    tags = {str(tag) for tag in filename_tags}
    if not tags & compatible_tags:
        raise LockError(f"Wheel ist nicht zum Ziel kompatibel: {path.name}")
    with zipfile.ZipFile(path) as archive:
        members = archive.namelist()
        metadata_paths = [p for p in members if re.fullmatch(r"[^/]+\.dist-info/METADATA", p)]
        wheel_paths = [p for p in members if re.fullmatch(r"[^/]+\.dist-info/WHEEL", p)]
        if len(metadata_paths) != 1 or len(wheel_paths) != 1:
            raise LockError("Wheel enthält keine eindeutigen Metadaten.")
        if metadata_paths[0].rsplit("/", 1)[0] != wheel_paths[0].rsplit("/", 1)[0]:
            raise LockError("Wheel-Metadaten gehören zu unterschiedlichen Distributionen.")
        if any(archive.getinfo(p).file_size > 2_000_000 for p in [*metadata_paths, *wheel_paths]):
            raise LockError("Wheel-Metadaten überschreiten die Größenbegrenzung.")
        parser = email.parser.BytesParser()
        metadata = parser.parsebytes(archive.read(metadata_paths[0]))
        wheel = parser.parsebytes(archive.read(wheel_paths[0]))
    if canonicalize_name(metadata["Name"]) != name or Version(metadata["Version"]) != version:
        raise LockError("Wheel-Dateiname und Paketmetadaten widersprechen sich.")
    wheel_tags = {str(tag) for value in wheel.get_all("Tag", []) for tag in parse_tag(value)}
    if wheel_tags != tags:
        raise LockError("Wheel-Dateiname und interne ABI-/Plattformtags widersprechen sich.")
    return {
        "name": name,
        "version": str(version),
        "filename": path.name,
        "sha256": digest,
        "tags": sorted(tags),
        "requires_python": metadata.get("Requires-Python", ""),
        "requires_dist": sorted(metadata.get_all("Requires-Dist", [])),
    }


def safe_wheel_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "files.pythonhosted.org"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise LockError("Wheelquelle ist kein unveränderlicher öffentlicher PyPI-Dateilink.")
    filename = unquote(parsed.path.rsplit("/", 1)[-1])
    if Path(filename).name != filename or "/" in filename or "\\" in filename or not filename.endswith(".whl"):
        raise LockError("Ungültiger Wheel-Dateiname oder Quelldistribution.")
    parse_wheel_filename(filename)
    return filename


def fetch_wheel(url: str, digest: str, wheelhouse: Path) -> Path:
    filename = safe_wheel_url(url)
    target = wheelhouse / filename
    if target.exists():
        if sha256(target) != digest:
            raise LockError(f"SHA-256 des vorhandenen Wheels stimmt nicht: {filename}")
        return target
    wheelhouse.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as response:
        safe_wheel_url(response.url)
        with tempfile.NamedTemporaryFile(dir=wheelhouse, delete=False) as output:
            temporary = Path(output.name)
            try:
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 256 * 1024 * 1024:
                        raise LockError("Wheel überschreitet die Downloadgrößenbegrenzung.")
                    output.write(chunk)
                output.close()
                if sha256(temporary) != digest:
                    raise LockError(f"SHA-256 des Downloads stimmt nicht: {filename}")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
    return target


def resolver_command(report: Path, roots: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pip",
        "--isolated",
        "install",
        "--dry-run",
        "--ignore-installed",
        "--only-binary=:all:",
        "--no-input",
        "--index-url",
        "https://pypi.org/simple",
        "--report",
        str(report),
        "-r",
        str(roots),
    ]


def validate_report(report: dict[str, Any], target: dict[str, str]) -> list[dict[str, Any]]:
    if report.get("version") != "1":
        raise LockError("Nicht unterstütztes pip-Berichtsschema.")
    environment = report.get("environment", {})
    if any(environment.get(key) != target[key] for key in TARGET_KEYS):
        raise LockError("pip-Bericht gehört zu einem anderen Ziel.")
    items = report.get("install")
    if not isinstance(items, list) or not items:
        raise LockError("pip-Bericht ist leer.")
    for item in items:
        if item.get("is_yanked") or item.get("is_direct"):
            raise LockError("Zurückgezogene oder direkt adressierte Pakete sind nicht erlaubt.")
        safe_wheel_url(item["download_info"]["url"])
        digest = item["download_info"]["archive_info"]["hashes"]["sha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise LockError("pip-Bericht enthält keinen gültigen SHA-256.")
    return items


def write_lock(output: Path, metadata: dict[str, Any]) -> None:
    text = f"# Native release lock: {metadata['profile']}, CPython {metadata['target']['python_full_version']}.\n"
    text += "# Generated by scripts/dependency_lock.py; update lock and metadata together.\n"
    text += "".join(f"{p['name']}=={p['version']} --hash=sha256:{p['sha256']}\n" for p in metadata["packages"])
    metadata["lock_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8", newline="\n")
    Path(str(output) + ".metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def refresh(args: argparse.Namespace) -> None:
    target = target_environment(args.profile, args.python_version)
    require_native(target)
    roots, inputs = profile_inputs(args.project_root, args.profile, args.bootstrap, args.require)
    target = host_environment()
    compatible_tags = {str(tag) for tag in sys_tags()}
    with tempfile.TemporaryDirectory(prefix="einvoice-lock-") as temporary:
        report_path = Path(temporary) / "report.json"
        roots_path = Path(temporary) / "roots.txt"
        roots_path.write_text("\n".join(roots) + "\n", encoding="utf-8")
        env = {name: value for name, value in os.environ.items() if not name.startswith("PIP_")}
        env["PIP_CONFIG_FILE"] = os.devnull
        subprocess.run(resolver_command(report_path, roots_path), env=env, check=True)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        items = validate_report(report, target)
        packages = []
        wheelhouse = args.wheelhouse or Path(temporary) / "wheels"
        for item in items:
            download = item["download_info"]
            digest = download["archive_info"]["hashes"]["sha256"]
            path = fetch_wheel(download["url"], digest, wheelhouse)
            package = read_wheel(path, digest, compatible_tags)
            reported = item["metadata"]
            if package["name"] != canonicalize_name(reported["name"]) or Version(package["version"]) != Version(
                reported["version"]
            ):
                raise LockError("pip-Bericht und Wheel-Metadaten widersprechen sich.")
            packages.append(package | {"url": download["url"]})
        packages.sort(key=lambda p: p["name"])
        verify_closure(roots, packages, target)
        write_lock(
            args.output,
            {
                "schema_version": SCHEMA_VERSION,
                "profile": args.profile,
                "target": target,
                "compatible_tags": sorted(compatible_tags),
                "roots": roots,
                "bootstrap": args.bootstrap,
                "additional_requirements": args.require,
                "inputs": inputs,
                "generator": {
                    "script_sha256": source_digest(Path(__file__)),
                    "source_normalization": "utf8-lf",
                    "pip": report["pip_version"],
                    "packaging": importlib.metadata.version("packaging"),
                    "python": platform.python_version(),
                },
                "packages": packages,
            },
        )


def check_lock(path: Path, project_root: Path | None = None) -> dict[str, Any]:
    metadata = json.loads(Path(str(path) + ".metadata.json").read_text(encoding="utf-8"))
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise LockError("Nicht unterstütztes Lock-Schema.")
    locked = parse_lock(path.read_text(encoding="utf-8"))
    if sha256(path) != metadata.get("lock_sha256"):
        raise LockError("Lockhash und Sidecar stimmen nicht überein.")
    target = metadata["target"]
    expected_target = target_environment(metadata["profile"], target["python_full_version"])
    if any(
        target.get(key) != value
        for key, value in expected_target.items()
        if key not in {"platform_release", "platform_version"}
    ):
        raise LockError("Sidecar-Ziel widerspricht dem Profil.")
    supported = set(metadata["compatible_tags"])
    if not supported:
        raise LockError("Kompatible Ziel-Tags fehlen.")
    validate_target_tags(supported, target)
    packages = metadata["packages"]
    mapped = package_map(packages)
    if {name: (p["version"], p["sha256"]) for name, p in mapped.items()} != locked:
        raise LockError("Lock-Paketmenge und Sidecar stimmen nicht überein.")
    for package in packages:
        if safe_wheel_url(package["url"]) != package["filename"]:
            raise LockError("Wheel-URL und Dateiname stimmen nicht überein.")
        name, version, _, tags = parse_wheel_filename(package["filename"])
        if name != package["name"] or str(version) != package["version"]:
            raise LockError("Wheel-Dateiname und gesperrte Identität stimmen nicht überein.")
        actual_tags = {str(tag) for tag in tags}
        if actual_tags != set(package["tags"]) or not actual_tags & supported:
            raise LockError("Wheel-Tags sind nicht zum Ziel kompatibel.")
    verify_closure(metadata["roots"], packages, target)
    if project_root is not None:
        expected_roots, expected_inputs = profile_inputs(
            project_root, metadata["profile"], metadata["bootstrap"], metadata["additional_requirements"]
        )
        if metadata["roots"] != expected_roots or metadata["inputs"] != expected_inputs:
            raise LockError("Gespeicherte Profileingaben stimmen nicht mit dem Projekt überein.")
        for relative, digest in metadata["inputs"].items():
            if relative not in {"pyproject.toml", "packaging/windows/requirements-build.txt"}:
                raise LockError("Unzulässiger Eingabepfad im Sidecar.")
            if source_digest(project_root / relative) != digest:
                raise LockError(f"Lock-Eingabe wurde nach Auflösung geändert: {relative}")
        if source_digest(Path(__file__)) != metadata["generator"]["script_sha256"]:
            raise LockError("Generatorrevision und Sidecar stimmen nicht überein.")
    return metadata


def compare_inventory(locked: dict[str, tuple[str, str]], packages: list[dict[str, str]]) -> None:
    actual = {canonicalize_name(p["name"]): str(Version(p["version"])) for p in packages}
    if len(actual) != len(packages) or actual != {name: version for name, (version, _) in locked.items()}:
        raise LockError("Installierte Pakete stimmen nicht exakt mit dem Lock überein.")


def compare_target_inventory(metadata: dict[str, Any], inventory: dict[str, Any]) -> None:
    env = inventory["environment"]
    target = metadata["target"]
    machine = env.get("machine", "").lower()
    expected_machine = target["platform_machine"].lower()
    if machine in {"amd64", "x86_64"}:
        machine = "x86_64"
    if expected_machine in {"amd64", "x86_64"}:
        expected_machine = "x86_64"
    if (
        env.get("python") != target["python_full_version"]
        or env.get("implementation") != "CPython"
        or env.get("sys_platform") != target["sys_platform"]
        or machine != expected_machine
        or env.get("gil_disabled") is not False
    ):
        raise LockError("Erfasstes Inventar gehört nicht zum gebundenen Ziel.")
    expected = {p["name"]: (p["version"], p.get("sha256", "")) for p in metadata["packages"]}
    compare_inventory(expected, inventory["packages"])


def audit_support() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dependency_audit", Path(__file__).with_name("dependency_audit.py"))
    if spec is None or spec.loader is None:
        raise LockError("Inventarwerkzeug konnte nicht geladen werden.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify(args: argparse.Namespace) -> None:
    metadata = check_lock(args.lock, args.project_root)
    require_native(metadata["target"])
    supported = {str(tag) for tag in sys_tags()}
    with tempfile.TemporaryDirectory(prefix="einvoice-verify-") as temporary:
        wheelhouse = args.wheelhouse or Path(temporary)
        for package in metadata["packages"]:
            path = fetch_wheel(package["url"], package["sha256"], wheelhouse)
            actual = read_wheel(path, package["sha256"], supported)
            if actual != {key: value for key, value in package.items() if key != "url"}:
                raise LockError(f"Wheelbytes und Sidecar-Metadaten widersprechen sich: {package['name']}")
    if args.installed or args.inventory:
        # Import only for the installed comparison; standalone offline checks do
        # not need the auditor and never invoke pip or a resolver.
        dependency_audit = audit_support()

        if args.inventory:
            inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
        elif args.python:
            with tempfile.TemporaryDirectory(prefix="einvoice-inventory-") as temporary:
                path = Path(temporary) / "inventory.json"
                subprocess.run(
                    [
                        args.python,
                        str(Path(__file__).with_name("dependency_audit.py").resolve()),
                        "capture",
                        "--project-root",
                        str(args.project_root.resolve()),
                        "--output",
                        str(path),
                    ],
                    check=True,
                )
                inventory = json.loads(path.read_text(encoding="utf-8"))
        else:
            inventory = dependency_audit.installed_inventory(args.project_root)
        dependency_audit.validate_inventory(inventory)
        compare_target_inventory(metadata, inventory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    refresh_parser = commands.add_parser("refresh")
    refresh_parser.add_argument("--profile", choices=PROFILES, required=True)
    refresh_parser.add_argument("--python-version", required=True)
    refresh_parser.add_argument("--output", type=Path, required=True)
    refresh_parser.add_argument("--bootstrap", action="append", default=[])
    refresh_parser.add_argument("--require", action="append", default=[])
    refresh_parser.add_argument("--wheelhouse", type=Path)
    refresh_parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    for name in ("check", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--lock", type=Path, required=True)
        command.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
        if name == "verify":
            command.add_argument("--wheelhouse", type=Path)
            inventories = command.add_mutually_exclusive_group()
            inventories.add_argument("--installed", action="store_true")
            inventories.add_argument("--inventory", type=Path)
            command.add_argument(
                "--python", help="Zielinterpreter für --installed; dort werden keine Werkzeuge installiert."
            )
    args = parser.parse_args(argv)
    try:
        if args.command == "refresh":
            refresh(args)
        elif args.command == "check":
            check_lock(args.lock, args.project_root)
        else:
            if args.python and not args.installed:
                raise LockError("--python benötigt --installed.")
            verify(args)
        print("Dependency-Lock erfolgreich geprüft.")
        return 0
    except (
        LockError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"Dependency-Lock fehlgeschlagen: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
