#!/usr/bin/env python3
"""Prove retained runtime payload and positive Trivy package coverage using stdlib.

Coverage is separate from vulnerability policy: findings remain reported and the
Trivy/pip-audit gates must still pass. CPython provenance and interpreter identity
are checked here, not CPython vulnerability coverage. Run verify-payload in the
read-only final image; the controller separately verifies the three Docker mounts
and binds /app, the manifest itself and builder-inventory.json to that image.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import json
import os
import posixpath
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, cast

MANIFEST_PATH = "/usr/share/e-rechnung-pruefer/runtime-rootfs-manifest.json"
BUILDER_INVENTORY_PATH = "/usr/share/e-rechnung-pruefer/builder-inventory.json"
ENGINE_NETWORK_FILES = ("/etc/hosts", "/etc/hostname", "/etc/resolv.conf")
ENGINE_NETWORK_REASON = "container engine supplies network configuration at startup"
RUNTIME_ROOTS = ("/usr", "/opt", "/etc", "/var", "/lib", "/lib64", "/bin", "/sbin", "/home")
BOOTSTRAP_NAMES = {"pip", "setuptools", "wheel", "packaging", "pkg-resources", "ensurepip", "-distutils-hack"}
PYTHON_VERSION = "3.14.7"
PYTHON_COUNT = 27


class CoverageError(ValueError):
    """Missing or contradictory coverage evidence is not a clean scan."""


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def require_digest(value: Any) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise CoverageError("Missing or invalid SHA-256 binding")


def image_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or posixpath.normpath(value) != value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise CoverageError(f"Noncanonical image path: {value!r}")
    return value


def canonical_name(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise CoverageError("Invalid distribution name")
    return re.sub(r"[-_.]+", "-", value).lower()


def trivy_debian_version(package: dict[str, Any], prefix: str = "") -> str:
    # Trivy's dpkg analyzer parses binary and source versions independently:
    # https://github.com/aquasecurity/trivy/blob/v0.74.0/pkg/fanal/analyzer/pkg/dpkg/dpkg.go
    # Epoch=0 and Release="" are omitted from JSON (types/package.go). Never
    # recover missing components from the manifest or the original package ID.
    version = package.get(prefix + "Version")
    epoch = package.get(prefix + "Epoch", 0)
    release = package.get(prefix + "Release", "")
    if (
        not isinstance(version, str)
        or not version
        or any(character.isspace() for character in version)
        or type(epoch) is not int
        or epoch < 0
        or not isinstance(release, str)
        or any(character.isspace() for character in release)
    ):
        raise CoverageError("Incomplete or malformed Trivy Debian version components")
    return (f"{epoch}:" if epoch else "") + version + ("-" + release if release else "")


def package_identity(package: dict[str, Any], *, trivy: bool = False) -> tuple[str, ...]:
    values: tuple[Any, ...]
    if trivy:
        values = (
            package.get("Name"),
            trivy_debian_version(package),
            package.get("Arch"),
            package.get("SrcName"),
            trivy_debian_version(package, "Src"),
        )
        if package.get("ID") != f"{values[0]}@{values[1]}":
            raise CoverageError("Trivy Debian package ID contradicts reconstructed binary version")
    else:
        keys = ("name", "version", "architecture", "source_name", "source_version")
        values = tuple(package.get(key) for key in keys)
    if any(not isinstance(value, str) or not value or any(c.isspace() for c in value) for value in values):
        raise CoverageError("Incomplete Debian binary/source identity")
    return cast(tuple[str, ...], values)


def forbidden_payload(path: str, *, wheel_metadata_paths: frozenset[str] = frozenset()) -> bool:
    parts = PurePosixPath(path).parts
    for index, part in enumerate(parts):
        normalized = re.sub(r"[-_.]+", "-", part).lower()
        if part == "WHEEL" and index == len(parts) - 1 and path in wheel_metadata_paths:
            # The wheel format requires this metadata filename; it is not the
            # wheel distribution. Only a fully bound immediate dist-info file
            # qualifies, never a module, directory, nested or unmanifested file.
            continue
        if index == 2 and parts[1:3] == ("app", "packaging"):
            # Repository data: runtime locks, KoSIT lock and Windows recipes.
            # Only this component is exempt, never nested tools or wheel bytes.
            if re.fullmatch(r"/app/packaging/(?:__init__\.py[co]?|__pycache__/__init__[^/]*\.pyc)", path):
                return True
            continue
        if normalized in BOOTSTRAP_NAMES or re.fullmatch(r"pip(?:\d+(?:\.\d+)*)?(?:\.exe)?", part.lower()):
            return True
        if re.fullmatch(
            r"(?:pip|setuptools|wheel|packaging|pkg_resources)[-_].+\.(?:dist-info|egg-info|whl)", part, re.I
        ):
            return True
    return False


def bound_wheel_metadata(files: dict[str, Any], metadata_paths: dict[str, str]) -> frozenset[str]:
    """Bind the standard WHEEL file to its METADATA, RECORD and original wheel."""
    result = set()
    for metadata_path in metadata_paths.values():
        directory = posixpath.dirname(metadata_path)
        path = directory + "/WHEEL"
        if path not in files:
            continue
        metadata, record, wheel = files[metadata_path], files[directory + "/RECORD"], files[path]
        origin = metadata["provenance"]
        if (
            not metadata_path.endswith(".dist-info/METADATA")
            or not metadata_path.startswith("/opt/runtime/lib/python3.14/site-packages/")
            or any(entry.get("type") != "file" for entry in (metadata, record, wheel))
            or origin.get("kind") != "wheel"
            or record.get("provenance") != origin
            or wheel.get("provenance") != origin
            or record.get("sha256") != origin.get("record_sha256")
        ):
            raise CoverageError(f"Standard WHEEL metadata is not bound to its distribution: {path}")
        result.add(path)
    return frozenset(result)


def manifest_inventory(manifest: dict[str, Any]) -> tuple[set[tuple[str, ...]], dict[str, str], dict[str, str], str]:
    if manifest.get("schema_version") != 1 or manifest.get("manifest_self_excluded") is not True:
        raise CoverageError("Unsupported or incomplete rootfs manifest")
    base = manifest.get("base_image")
    if not isinstance(base, str) or not re.fullmatch(r"python:3\.14\.7-slim-trixie@sha256:[a-f0-9]{64}", base):
        raise CoverageError("CPython origin is not the pinned official stable image")
    if manifest.get("cpython_version") != PYTHON_VERSION:
        raise CoverageError("Unexpected CPython version")
    for key in ("builder_script_sha256", "runtime_lock_sha256", "runtime_metadata_sha256"):
        require_digest(manifest.get(key))
    packages, files = manifest.get("packages"), manifest.get("files")
    if not isinstance(packages, dict) or not packages or not isinstance(files, dict) or not files:
        raise CoverageError("Empty Debian or file manifest")
    expected_os = set()
    for key, package in packages.items():
        identity = package_identity(package)
        if key != identity[0] + ":" + identity[2] or identity in expected_os:
            raise CoverageError("Ambiguous Debian identity in manifest")
        expected_os.add(identity)
        path = image_path(package["status_path"])
        if path != "/var/lib/dpkg/status.d/" + key:
            raise CoverageError("Debian status path is not bound to its package")
        require_digest(package.get("status_sha256"))
        if files.get(path, {}).get("sha256") != package["status_sha256"]:
            raise CoverageError("Debian status bytes are not bound to the file manifest")
    architectures = {identity[2] for identity in expected_os} - {"all"}
    if len(architectures) != 1 or not architectures <= {"amd64", "arm64"}:
        raise CoverageError("Missing or ambiguous native Debian architecture")
    architecture = next(iter(architectures))
    owners: set[str] = set()
    python: dict[str, str] = {}
    wheels: dict[str, tuple[str, str, str]] = {}
    metadata_paths: dict[str, str] = {}
    records: dict[str, str] = {}
    cpython: set[str] = set()
    for path, entry in files.items():
        image_path(path)
        kind = entry.get("type")
        if kind not in {"file", "directory", "symlink"}:
            raise CoverageError(f"Unknown runtime file type: {path}")
        if kind in {"file", "directory"}:
            mode = entry.get("mode")
            if type(mode) is not int or not 0 <= mode <= 0o1777:
                raise CoverageError(f"Invalid or set-id runtime mode: {path}")
        if kind == "directory":
            continue
        if kind == "file":
            require_digest(entry.get("sha256"))
            if type(entry.get("size")) is not int or entry["size"] < 0 or type(entry.get("elf")) is not bool:
                raise CoverageError(f"Incomplete runtime file metadata: {path}")
            if entry["elf"] and not isinstance(entry.get("elf_dependencies"), list):
                raise CoverageError(f"Missing ELF dependency inventory: {path}")
        elif not isinstance(entry.get("target"), str) or not entry["target"]:
            raise CoverageError(f"Missing symlink target: {path}")
        origin = entry.get("provenance", {})
        origin_kind = origin.get("kind")
        if origin_kind in {"debian", "debian-generated", "debian-generated-symlink"}:
            attributed = origin.get("packages")
            if not isinstance(attributed, list) or not attributed or not set(attributed) <= packages.keys():
                raise CoverageError(f"Unknown or missing Debian payload owner: {path}")
            owners.update(attributed)
        elif origin_kind == "wheel":
            name = canonical_name(origin.get("name"))
            version = origin.get("version")
            if name in BOOTSTRAP_NAMES or not isinstance(version, str) or not version:
                raise CoverageError("Unexpected Python runtime distribution")
            require_digest(origin.get("wheel_sha256"))
            require_digest(origin.get("record_sha256"))
            if not isinstance(origin.get("wheel_filename"), str) or not origin["wheel_filename"].endswith(".whl"):
                raise CoverageError("Missing original wheel filename")
            identity = (version, origin["wheel_sha256"], origin["wheel_filename"])
            if name in wheels and wheels[name] != identity:
                raise CoverageError("Conflicting original wheel identities")
            wheels[name], python[name] = identity, version
            if path.endswith(".dist-info/METADATA"):
                if name in metadata_paths or not path.startswith("/opt/runtime/lib/python3.14/site-packages/"):
                    raise CoverageError("Duplicate or misplaced installed distribution metadata")
                metadata_paths[name] = path
            if path.endswith(".dist-info/RECORD"):
                if name in records or entry.get("sha256") != origin["record_sha256"]:
                    raise CoverageError("Duplicate or unbound installed RECORD")
                records[name] = path
        elif origin_kind in {"cpython", "cpython-venv", "merged-usr-layout"}:
            if origin.get("base_image") != base or (
                origin_kind != "merged-usr-layout" and origin.get("version") != PYTHON_VERSION
            ):
                raise CoverageError("Unbound CPython or base-layout payload")
            if origin_kind == "cpython" and kind == "file":
                cpython.add(path)
        elif origin_kind == "generated":
            allowed = {"/etc/passwd", "/etc/group", *ENGINE_NETWORK_FILES, "/var/lib/dpkg/status"}
            allowed.update(package["status_path"] for package in packages.values())
            if path not in allowed or not isinstance(origin.get("reason"), str) or not origin["reason"]:
                raise CoverageError(f"Unapproved generated runtime file: {path}")
        else:
            raise CoverageError(f"Missing or unknown runtime file provenance: {path}")
        if path in ENGINE_NETWORK_FILES and (
            origin != {"kind": "generated", "reason": ENGINE_NETWORK_REASON}
            or kind != "file"
            or entry["size"] != 0
            or entry["sha256"] != sha256(b"")
        ):
            raise CoverageError("Docker network-file exception has conflicting provenance")
    if owners != packages.keys():
        raise CoverageError("Debian inventory includes packages without retained payload")
    if len(python) != PYTHON_COUNT or python.keys() != metadata_paths.keys() or python.keys() != records.keys():
        raise CoverageError("Missing complete metadata/RECORD for all 27 Python distributions")
    for name, path in metadata_paths.items():
        if posixpath.dirname(path) != posixpath.dirname(records[name]):
            raise CoverageError("Installed METADATA and RECORD refer to different distributions")
    required_python = {"/usr/local/bin/python3.14", "/usr/local/lib/libpython3.14.so.1.0"}
    if not required_python <= cpython or not all(files[path].get("elf") for path in required_python):
        raise CoverageError("Interpreter and libpython bytes are not inventoried")
    if files.get("/var/lib/dpkg/status", {}).get("type") != "file":
        raise CoverageError("Combined original Debian status is missing")
    wheel_metadata_paths = bound_wheel_metadata(files, metadata_paths)
    for path in files:
        if forbidden_payload(path, wheel_metadata_paths=wheel_metadata_paths):
            raise CoverageError(f"Bootstrap payload in manifest: {path}")
    return expected_os, python, metadata_paths, architecture


def check_coverage(manifest: dict[str, Any], scan: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    expected_os, expected_python, metadata_paths, architecture = manifest_inventory(manifest)
    actual_python: dict[str, str] = {}
    if (
        inventory.get("schema_version") != 1
        or "excluded_editable" not in inventory
        or inventory["excluded_editable"] is not None
    ):
        raise CoverageError("Incomplete or selectively excluded final Python inventory")
    for package in inventory.get("packages", []):
        name, version = canonical_name(package.get("name")), package.get("version")
        if name in actual_python or not isinstance(version, str):
            raise CoverageError("Duplicate or malformed Python inventory")
        actual_python[name] = version
    inventory_hash = sha256(json.dumps(sorted(actual_python.items()), separators=(",", ":")).encode())
    if actual_python != expected_python or inventory.get("inventory_sha256") != inventory_hash:
        raise CoverageError("Final Python inventory does not match the complete runtime manifest")
    environment = inventory.get("environment", {})
    machine = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        str(environment.get("machine", "")).lower()
    )
    if (
        environment.get("python") != PYTHON_VERSION
        or environment.get("implementation") != "CPython"
        or environment.get("sys_platform") != "linux"
        or environment.get("gil_disabled") is not False
        or machine != architecture
    ):
        raise CoverageError("Final interpreter, architecture or ABI differs from the manifest")
    metadata = scan.get("Metadata", {})
    operating_system = metadata.get("OS", {})
    image_id = metadata.get("ImageID", "")
    if (
        scan.get("SchemaVersion") != 2
        or scan.get("ArtifactType") != "container_image"
        or operating_system.get("Family") != "debian"
        or not re.fullmatch(r"13(?:\.\d+)*", str(operating_system.get("Name", "")))
        or metadata.get("ImageConfig", {}).get("architecture") != architecture
        or not isinstance(image_id, str)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id)
    ):
        raise CoverageError("Trivy image/OS/architecture evidence is incomplete or contradictory")
    observed_os: set[tuple[str, ...]] = set()
    installed_metadata: set[str] = set()
    findings = 0
    results = scan.get("Results")
    if not isinstance(results, list) or not results:
        raise CoverageError("Trivy results are empty")
    for result in results:
        findings += len(result.get("Vulnerabilities", []))
        if result.get("Class") == "os-pkgs":
            if result.get("Type") != "debian":
                raise CoverageError("Unexpected OS analyzer result")
            for package in result.get("Packages", []):
                identity = package_identity(package, trivy=True)
                if package.get("AnalyzedBy") != "dpkg":
                    raise CoverageError("Unproved Debian scan identity")
                # The preserved status and status.d stanzas may both be scanned.
                # Only fully identical binary/source identities deduplicate; a
                # conflicting version/source remains an extra set member below.
                observed_os.add(identity)
        elif result.get("Class") == "lang-pkgs" and result.get("Type") == "python-pkg":
            for package in result.get("Packages", []):
                name = canonical_name(package.get("Name"))
                if name in BOOTSTRAP_NAMES or expected_python.get(name) != package.get("Version"):
                    raise CoverageError("Unexpected Python package or vendored bootstrap in image scan")
                if package.get("AnalyzedBy") == "python-pkg":
                    path = "/" + str(package.get("FilePath", "")).lstrip("/")
                    if name in installed_metadata or path != metadata_paths[name]:
                        raise CoverageError("Scanned Python package lacks unique bound installation metadata")
                    installed_metadata.add(name)
                elif package.get("AnalyzedBy") != "sbom":
                    raise CoverageError("Unknown Python inventory analyzer")
    if observed_os != expected_os:
        raise CoverageError(
            f"Debian scan coverage mismatch: missing={sorted(expected_os - observed_os)}, extra={sorted(observed_os - expected_os)}"
        )
    if installed_metadata != expected_python.keys():
        raise CoverageError("Trivy did not positively identify every installed Python distribution")
    return {
        "coverage_passed": True,
        "debian_packages": len(expected_os),
        "python_distributions": len(expected_python),
        "cpython_version": PYTHON_VERSION,
        "cpython_vulnerability_coverage": "not_provided_by_this_check",
        "architecture": architecture,
        "image_id": image_id,
        "trivy_image_vulnerabilities": findings,
    }


def resolve_image(root: Path, path: str, *, follow_final: bool = True) -> Path:
    pending = list(PurePosixPath(image_path(path)).parts[1:])
    resolved: list[str] = []
    links = 0
    while pending:
        part = pending.pop(0)
        candidate = root.joinpath(*resolved, part)
        if candidate.is_symlink() and (pending or follow_final):
            links += 1
            if links > 40:
                raise CoverageError(f"Runtime symlink cycle: {path}")
            target = os.readlink(candidate)
            joined = target if target.startswith("/") else posixpath.join("/" + "/".join(resolved), target)
            pending = list(PurePosixPath(image_path(posixpath.normpath(joined))).parts[1:]) + pending
            resolved = []
        else:
            resolved.append(part)
    return root.joinpath(*resolved)


def status_identity(payload: bytes) -> tuple[str, ...]:
    fields = email.parser.Parser().parsestr(payload.decode("utf-8"))
    for key in ("Package", "Version", "Architecture", "Status", "Source"):
        if len(fields.get_all(key, [])) > 1:
            raise CoverageError("Duplicate Debian status identity field")
    if fields.get("Status") != "install ok installed":
        raise CoverageError("Retained Debian status is not installed")
    name, version, architecture = fields.get("Package"), fields.get("Version"), fields.get("Architecture")
    source = fields.get("Source", name or "")
    match = re.fullmatch(r"([^\s()]+)(?: \(([^\s()]+)\))?", source)
    if not match:
        raise CoverageError("Malformed Debian source identity")
    return package_identity(
        {
            "name": name,
            "version": version,
            "architecture": architecture,
            "source_name": match[1],
            "source_version": match[2] or version,
        }
    )


def verify_payload(manifest: dict[str, Any], root: Path = Path("/")) -> dict[str, Any]:
    expected_os, _, metadata_paths, _ = manifest_inventory(manifest)
    root = root.resolve(strict=True)
    files = manifest["files"]
    wheel_metadata_paths = bound_wheel_metadata(files, metadata_paths)
    for path, entry in files.items():
        actual = resolve_image(root, path, follow_final=False)
        try:
            information = actual.lstat()
        except OSError as exc:
            raise CoverageError(f"Runtime payload is missing or inaccessible: {path}") from exc
        kind = entry["type"]
        if kind == "symlink":
            if not stat.S_ISLNK(information.st_mode) or os.readlink(actual) != entry["target"]:
                raise CoverageError(f"Runtime symlink differs: {path}")
            resolve_image(root, path).stat()
            continue
        if kind == "directory":
            if not stat.S_ISDIR(information.st_mode):
                raise CoverageError(f"Runtime directory differs: {path}")
        elif not stat.S_ISREG(information.st_mode):
            raise CoverageError(f"Runtime file type differs: {path}")
        if path in ENGINE_NETWORK_FILES:
            continue  # Fixed engine mounts, separately verified by the caller.
        if stat.S_IMODE(information.st_mode) != entry["mode"]:
            raise CoverageError(f"Runtime mode differs: {path}")
        for field, observed in (("uid", information.st_uid), ("gid", information.st_gid)):
            if field in entry and entry[field] != observed:
                raise CoverageError(f"Runtime ownership differs: {path}")
        if kind == "file":
            with actual.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                hashed = hashlib.file_digest(handle, "sha256").hexdigest()
                finished = os.fstat(handle.fileno())
            after = actual.lstat()
            file_identities = {
                (item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns)
                for item in (information, opened, finished, after)
            }
            if len(file_identities) != 1 or hashed != entry["sha256"] or information.st_size != entry["size"]:
                raise CoverageError(f"Runtime payload hash/identity differs: {path}")
    for package in manifest["packages"].values():
        content = resolve_image(root, package["status_path"]).read_bytes()
        if status_identity(content) != package_identity(package):
            raise CoverageError("Original Debian status contradicts manifest source identity")
    combined = resolve_image(root, "/var/lib/dpkg/status").read_bytes()
    identities = [status_identity(block + b"\n") for block in combined.split(b"\n\n") if block.strip()]
    if len(identities) != len(expected_os) or set(identities) != expected_os:
        raise CoverageError("Combined Debian status does not describe the retained package set")
    extras_allowed = {MANIFEST_PATH, BUILDER_INVENTORY_PATH}
    engine_symlinks = []

    def walk_error(error: OSError) -> None:
        raise CoverageError(f"Runtime subtree could not be inventoried: {error}") from error

    for prefix in (*RUNTIME_ROOTS, "/app"):
        start = root / prefix.lstrip("/")
        if start.is_symlink() or not start.exists():
            continue
        for directory, directories, names in os.walk(start, followlinks=False, onerror=walk_error):
            for name in (*directories, *names):
                actual = Path(directory) / name
                path = "/" + actual.relative_to(root).as_posix()
                if forbidden_payload(path, wheel_metadata_paths=wheel_metadata_paths):
                    raise CoverageError(f"Forbidden bootstrap payload found: {path}")
                if path == "/etc/mtab" and path not in files:
                    # Docker's init layer adds this exact symlink to containers:
                    # https://github.com/moby/moby/blob/master/daemon/initlayer/setup_unix.go
                    # A manifested path must pass normal validation; no other
                    # path, file type or equivalent/relative target qualifies.
                    if not stat.S_ISLNK(actual.lstat().st_mode) or os.readlink(actual) != "/proc/mounts":
                        raise CoverageError("Docker runtime /etc/mtab is not the exact /proc/mounts symlink")
                    engine_symlinks.append({"path": path, "type": "symlink", "target": "/proc/mounts"})
                    continue
                if (
                    prefix != "/app"
                    and (actual.is_symlink() or not actual.is_dir())
                    and path not in files
                    and path not in extras_allowed
                ):
                    raise CoverageError(f"Unmanifested runtime payload found: {path}")
    return {
        "payload_passed": True,
        "files_verified": len(files) - sum(path in files for path in ENGINE_NETWORK_FILES),
        "engine_network_exceptions": [path for path in ENGINE_NETWORK_FILES if path in files],
        "engine_symlink_exceptions": engine_symlinks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("verify-payload", "check"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--output", type=Path)
        if name == "verify-payload":
            command.add_argument("--root", type=Path, default=Path("/"))
        else:
            command.add_argument("--trivy-image", type=Path, required=True)
            command.add_argument("--inventory", type=Path, required=True)
    args = parser.parse_args(argv)
    result: dict[str, Any]
    exit_code = 0
    try:
        manifest_bytes = args.manifest.read_bytes()
        manifest = json.loads(manifest_bytes)
        inputs = {"manifest_sha256": sha256(manifest_bytes)}
        if args.command == "verify-payload":
            result = verify_payload(manifest, args.root)
        else:
            trivy_bytes, inventory_bytes = args.trivy_image.read_bytes(), args.inventory.read_bytes()
            inputs.update(trivy_image_sha256=sha256(trivy_bytes), inventory_sha256=sha256(inventory_bytes))
            result = check_coverage(manifest, json.loads(trivy_bytes), json.loads(inventory_bytes))
        result["inputs"] = inputs
    except (CoverageError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        result = {"coverage_passed": False, "payload_passed": False, "error": str(exc)}
        exit_code = 1
    serialized = json.dumps(result, sort_keys=True, indent=2) + "\n"
    print(serialized, end="")
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
        except OSError as exc:
            print(f"Could not preserve coverage evidence: {exc}", file=sys.stderr)
            return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
