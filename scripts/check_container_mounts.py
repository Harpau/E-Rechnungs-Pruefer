#!/usr/bin/env python3
"""Bind Docker's three generated network files by inode/device, using only stdlib.

Capture container facts with its default non-root identity and host facts with
read-only sudo access to the three Inspect source paths. No file contents are
read. The mountinfo root is a filesystem-relative path, not a Docker host path:
https://man7.org/linux/man-pages/man5/proc_pid_mountinfo.5.html
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import stat
import sys
from pathlib import Path
from typing import Any

NETWORK_FILES = {"/etc/hosts": "HostsPath", "/etc/hostname": "HostnamePath", "/etc/resolv.conf": "ResolvConfPath"}


class MountError(ValueError):
    """Engine mount identities are missing, ambiguous or contradictory."""


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def validate_binding(container_id: str, image_id: str, inspect_sha256: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", container_id) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise MountError("Full container and image identities are required")
    if not re.fullmatch(r"[a-f0-9]{64}", inspect_sha256):
        raise MountError("Missing exact Inspect SHA-256")


def validate_inspect(inspection: dict[str, Any], container_id: str, image_id: str) -> None:
    if inspection.get("Id") != container_id or inspection.get("Image") != image_id:
        raise MountError("Inspect does not describe the bound container and image")
    if inspection.get("Config", {}).get("User") != "appuser":
        raise MountError("Bound container must retain its default non-root user")
    configuration = inspection.get("HostConfig", {})
    if configuration.get("ReadonlyRootfs") is not True or configuration.get("NetworkMode") != "none":
        raise MountError("Bound container must be read-only and offline")
    for key in NETWORK_FILES.values():
        source = inspection.get(key)
        if (
            not isinstance(source, str)
            or not source.startswith("/")
            or posixpath.normpath(source) != source
            or "\0" in source
        ):
            raise MountError(f"Missing or noncanonical Inspect source: {key}")


def read_inspect(path: Path, container_id: str, image_id: str) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    data = json.loads(payload)
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise MountError("Inspect must contain exactly one container")
    inspect_sha256 = digest(payload)
    validate_binding(container_id, image_id, inspect_sha256)
    validate_inspect(data[0], container_id, image_id)
    return data[0], inspect_sha256


def unescape_mount_path(value: str) -> str:
    escapes = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
    return re.sub(r"\\(040|011|012|134)", lambda match: escapes[match[1]], value)


def network_mounts(raw: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, str) or not raw.strip():
        raise MountError("Raw container mountinfo is missing")
    for line in raw.splitlines():
        sections = line.split(" - ")
        if len(sections) != 2:
            raise MountError("Malformed mountinfo separator")
        fields, filesystem = sections[0].split(), sections[1].split()
        if len(fields) < 6 or len(filesystem) < 3:
            raise MountError("Incomplete mountinfo record")
        target = unescape_mount_path(fields[4])
        if any(target.startswith(expected + "/") for expected in NETWORK_FILES):
            raise MountError("Unexpected nested network-file mount")
        if target not in NETWORK_FILES:
            continue
        if target in result:
            raise MountError(f"Duplicate network-file mountpoint: {target}")
        if not re.fullmatch(r"\d+:\d+", fields[2]) or not fields[0].isdigit() or not fields[1].isdigit():
            raise MountError("Invalid mount ID or filesystem device")
        major, minor = (int(value) for value in fields[2].split(":"))
        result[target] = {
            "mount_id": int(fields[0]),
            "parent_id": int(fields[1]),
            "device_major": major,
            "device_minor": minor,
            "root": unescape_mount_path(fields[3]),
            "target": target,
            "options": fields[5],
            "filesystem": filesystem[0],
            "source": unescape_mount_path(filesystem[1]),
        }
    if result.keys() != NETWORK_FILES.keys():
        raise MountError("Exactly three dedicated engine network-file mountpoints are required")
    return result


def regular_file_fact(path: Path) -> dict[str, Any]:
    information = path.lstat()
    if not stat.S_ISREG(information.st_mode):
        raise MountError(f"Engine network path is a symlink or non-regular file: {path}")
    return {"path": str(path), "device": information.st_dev, "inode": information.st_ino, "mode": information.st_mode}


def capture_container(container_id: str, image_id: str, inspect_sha256: str) -> dict[str, Any]:
    validate_binding(container_id, image_id, inspect_sha256)
    if sys.platform != "linux" or os.getuid() != 10001 or os.getgid() != 10001:
        raise MountError("Container capture requires the native default UID/GID 10001")
    raw = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    network_mounts(raw)
    return {
        "schema_version": 1,
        "role": "container",
        "container_id": container_id,
        "image_id": image_id,
        "inspect_sha256": inspect_sha256,
        "mountinfo": raw,
        "files": {target: regular_file_fact(Path(target)) for target in NETWORK_FILES},
    }


def capture_host(inspection: dict[str, Any], inspect_sha256: str, container_id: str, image_id: str) -> dict[str, Any]:
    validate_binding(container_id, image_id, inspect_sha256)
    validate_inspect(inspection, container_id, image_id)
    return {
        "schema_version": 1,
        "role": "host",
        "container_id": container_id,
        "image_id": image_id,
        "inspect_sha256": inspect_sha256,
        "mountinfo": Path("/proc/self/mountinfo").read_text(encoding="utf-8"),
        "files": {target: regular_file_fact(Path(inspection[key])) for target, key in NETWORK_FILES.items()},
    }


def validate_fact(fact: dict[str, Any], path: str) -> tuple[int, int]:
    if fact.get("path") != path or any(type(fact.get(key)) is not int for key in ("device", "inode", "mode")):
        raise MountError("File identity has an unexpected path or missing stat data")
    if fact["device"] < 0 or fact["inode"] <= 0 or not stat.S_ISREG(fact["mode"]):
        raise MountError("File identity is not a regular file with a valid inode/device")
    return fact["device"], fact["inode"]


def check_bindings(
    inspection: dict[str, Any],
    inspect_sha256: str,
    host: dict[str, Any],
    container: dict[str, Any],
    container_id: str,
    image_id: str,
) -> dict[str, Any]:
    validate_binding(container_id, image_id, inspect_sha256)
    validate_inspect(inspection, container_id, image_id)
    for role, evidence in (("host", host), ("container", container)):
        if (
            evidence.get("schema_version") != 1
            or evidence.get("role") != role
            or evidence.get("container_id") != container_id
            or evidence.get("image_id") != image_id
            or evidence.get("inspect_sha256") != inspect_sha256
            or not isinstance(evidence.get("files"), dict)
            or evidence["files"].keys() != NETWORK_FILES.keys()
            or not isinstance(evidence.get("mountinfo"), str)
            or not evidence["mountinfo"].strip()
        ):
            raise MountError(f"Incomplete or unbound {role} evidence")
    mounted = network_mounts(container["mountinfo"])
    bindings = {}
    for target, key in NETWORK_FILES.items():
        source = inspection[key]
        host_identity = validate_fact(host["files"][target], source)
        container_identity = validate_fact(container["files"][target], target)
        record = mounted[target]
        if host_identity != container_identity:
            raise MountError(f"Host source and container target are different files: {target}")
        if (os.major(container_identity[0]), os.minor(container_identity[0])) != (
            record["device_major"],
            record["device_minor"],
        ):
            raise MountError(f"Mountinfo device contradicts stat identity: {target}")
        bindings[target] = {
            "source": source,
            "device": host_identity[0],
            "inode": host_identity[1],
            "mount_id": record["mount_id"],
            "mount_root": record["root"],
            "filesystem": record["filesystem"],
        }
    return {
        "passed": True,
        "container_id": container_id,
        "image_id": image_id,
        "inspect_sha256": inspect_sha256,
        "bindings": bindings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("capture-container", "capture-host", "check"):
        command = commands.add_parser(name)
        command.add_argument("--container-id", required=True)
        command.add_argument("--image-id", required=True)
        command.add_argument("--output", type=Path)
        if name == "capture-container":
            command.add_argument("--inspect-sha256", required=True)
        else:
            command.add_argument("--inspect", type=Path, required=True)
        if name == "check":
            command.add_argument("--host-facts", type=Path, required=True)
            command.add_argument("--container-facts", type=Path, required=True)
    args = parser.parse_args(argv)
    exit_code = 0
    try:
        if args.command == "capture-container":
            result = capture_container(args.container_id, args.image_id, args.inspect_sha256)
        else:
            inspection, inspect_sha256 = read_inspect(args.inspect, args.container_id, args.image_id)
            if args.command == "capture-host":
                result = capture_host(inspection, inspect_sha256, args.container_id, args.image_id)
            else:
                host_bytes, container_bytes = args.host_facts.read_bytes(), args.container_facts.read_bytes()
                result = check_bindings(
                    inspection,
                    inspect_sha256,
                    json.loads(host_bytes),
                    json.loads(container_bytes),
                    args.container_id,
                    args.image_id,
                )
                result["inputs"] = {
                    "host_facts_sha256": digest(host_bytes),
                    "container_facts_sha256": digest(container_bytes),
                }
    except (MountError, OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        result = {"passed": False, "error": str(exc)}
        exit_code = 1
    serialized = json.dumps(result, sort_keys=True, indent=2) + "\n"
    print(serialized, end="")
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
        except OSError as exc:
            print(f"Could not preserve mount evidence: {exc}", file=sys.stderr)
            return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
