#!/usr/bin/env python3
"""Assemble a traced Debian/CPython runtime; run only in the native trusted builder.

This is an allowlist copier, not a package uninstaller. A failure leaves an
incomplete output directory which must never be used as an image stage. ldd is
only run against the pinned base, Debian packages and hash-verified wheel files.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import csv
import email.parser
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import struct
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_BASE_IMAGE = "python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6"
MANIFEST_PATH = "/usr/share/e-rechnung-pruefer/runtime-rootfs-manifest.json"
PYTHON_VERSION = "3.14.7"
PYTHON_STDLIB = "/usr/local/lib/python3.14"
RUNTIME = "/opt/runtime"
SITE_PACKAGES = RUNTIME + "/lib/python3.14/site-packages"
BOOTSTRAP_PACKAGES = {"pip", "setuptools", "wheel", "packaging"}


class RootfsError(RuntimeError):
    """Runtime closure or provenance could not be proved."""


class WheelEntryPoints(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def normalize(path: str) -> str:
    if not path.startswith("/") or "\0" in path or "\n" in path:
        raise RootfsError(f"Invalid absolute image path: {path!r}")
    return posixpath.normpath(path)


def image_path(root: Path, path: str) -> Path:
    return root / normalize(path).lstrip("/")


def resolve_image(root: Path, path: str, *, follow_final: bool = True) -> str:
    """Resolve symlinks against the image root, never against the host root."""
    pending = list(PurePosixPath(normalize(path)).parts[1:])
    resolved: list[str] = []
    count = 0
    while pending:
        part = pending.pop(0)
        current = "/" + "/".join([*resolved, part])
        source = image_path(root, current)
        if source.is_symlink() and (pending or follow_final):
            count += 1
            if count > 40:
                raise RootfsError(f"Symlink cycle: {path}")
            target = os.readlink(source)
            absolute = target if target.startswith("/") else posixpath.join("/" + "/".join(resolved), target)
            pending = list(PurePosixPath(normalize(absolute)).parts[1:]) + pending
            resolved = []
        else:
            resolved.append(part)
    return "/" + "/".join(resolved)


def parse_status(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        stanza = block.rstrip("\n") + "\n"
        fields = email.parser.Parser().parsestr(stanza)
        if fields.get("Status") != "install ok installed":
            continue
        name, architecture = fields.get("Package"), fields.get("Architecture")
        if not name or not architecture or not fields.get("Version"):
            raise RootfsError("Incomplete installed dpkg status stanza")
        key = f"{name}:{architecture}"
        if key in result:
            raise RootfsError(f"duplicate dpkg status: {key}")
        result[key] = stanza
    return result


def command(arguments: list[str], *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, env=env, check=False)
    if result.returncode or result.stderr.strip():
        raise RootfsError(f"Command failed ({result.returncode}): {arguments!r}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def debian_inventory(source: Path) -> tuple[dict[str, list[str]], dict[str, str]]:
    if source != Path("/"):
        raise RootfsError("Native dpkg inventory requires the live builder filesystem")
    statuses = parse_status((source / "var/lib/dpkg/status").read_text())
    names = command(["dpkg-query", "--show", "--showformat=${binary:Package}\n"]).splitlines()
    owners: dict[str, list[str]] = {}
    for name in names:
        matches = [key for key in statuses if key == name or key.split(":")[0] == name]
        if not matches:
            continue  # dpkg may also list removed packages with residual conffiles.
        if len(matches) != 1:
            raise RootfsError(f"Ambiguous Debian package identity: {name}")
        key = matches[0]
        for path in command(["dpkg-query", "--listfiles", name]).splitlines():
            if not path.startswith("/"):
                raise RootfsError(f"Unexpected dpkg-query file entry: {path}")
            for candidate in {normalize(path), resolve_image(source, path, follow_final=False)}:
                if key not in owners.setdefault(candidate, []):
                    owners[candidate].append(key)
    if not statuses or not owners:
        raise RootfsError("Empty Debian inventory")
    return owners, statuses


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_provenance(
    source: Path, lock: Path, metadata_path: Path, wheelhouse: Path | None = None
) -> dict[str, dict[str, Any]]:
    metadata = json.loads(metadata_path.read_text())
    if metadata.get("lock_sha256") != sha256(lock):
        raise RootfsError("Runtime lock/metadata hash mismatch")
    locked: dict[str, tuple[str, str]] = {}
    for line in lock.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+) --hash=sha256:([a-f0-9]{64})", line)
        if not match:
            raise RootfsError(f"Noncanonical runtime lock line: {line}")
        name, version, digest = match.groups()
        name = canonical_name(name)
        if name in locked or name in BOOTSTRAP_PACKAGES:
            raise RootfsError(f"Duplicate or forbidden runtime package: {name}")
        locked[name] = (version, digest)
    artifacts: dict[str, dict[str, Any]] = {}
    for item in metadata["packages"]:
        name = canonical_name(item["name"])
        if name in artifacts or locked.get(name) != (item["version"], item["sha256"]):
            raise RootfsError(f"Wheel metadata does not match runtime lock: {name}")
        artifacts[name] = item
    if not locked or set(artifacts) != set(locked):
        raise RootfsError("Incomplete runtime wheel provenance")
    site = image_path(source, SITE_PACKAGES)
    result: dict[str, dict[str, Any]] = {}
    installed: set[str] = set()
    for info in sorted(site.glob("*.dist-info")):
        fields = email.parser.Parser().parsestr((info / "METADATA").read_text())
        name = canonical_name(fields.get("Name", ""))
        if name in installed or name not in locked or fields.get("Version") != locked[name][0]:
            raise RootfsError(f"Unexpected installed distribution: {name}")
        installed.add(name)
        record = info / "RECORD"
        original_files: dict[str, bytes] = {}
        console_scripts: set[str] = set()
        if wheelhouse is not None:
            wheel = wheelhouse / artifacts[name]["filename"]
            if wheel.parent != wheelhouse or sha256(wheel) != locked[name][1]:
                raise RootfsError(f"Original wheel hash mismatch: {name}")
            with zipfile.ZipFile(wheel) as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    if member.filename.startswith("/") or ".." in PurePosixPath(member.filename).parts:
                        raise RootfsError(f"Unsafe original wheel member: {member.filename}")
                    relative = member.filename
                    data_match = re.fullmatch(r"[^/]+\.data/(purelib|platlib)/(.+)", relative)
                    if data_match:
                        relative = data_match[2]
                    elif ".data/" in relative:
                        raise RootfsError(f"Unsupported wheel relocation: {member.filename}")
                    path = posixpath.join(SITE_PACKAGES, relative)
                    if path in original_files:
                        raise RootfsError(f"Duplicate original wheel member: {path}")
                    original_files[path] = archive.read(member)
                    if relative.endswith(".dist-info/entry_points.txt"):
                        config = WheelEntryPoints(interpolation=None)
                        config.read_string(original_files[path].decode())
                        for section in ("console_scripts", "gui_scripts"):
                            if config.has_section(section):
                                for script in config[section]:
                                    if not re.fullmatch(r"[A-Za-z0-9_.-]+", script):
                                        raise RootfsError(f"Unsafe wheel entry point: {script}")
                                    console_scripts.add(RUNTIME + "/bin/" + script)
        if (info / "direct_url.json").exists():
            raise RootfsError(f"Unapproved direct URL runtime distribution: {name}")
        with record.open(newline="") as stream:
            for row in csv.reader(stream):
                if len(row) != 3:
                    raise RootfsError(f"Malformed RECORD: {record}")
                relative, digest, size = row
                if relative.startswith("/"):
                    raise RootfsError(f"Absolute RECORD path: {relative}")
                path = normalize(posixpath.join(SITE_PACKAGES, relative))
                if not path.startswith(RUNTIME + "/") or path in result:
                    raise RootfsError(f"Escaping or colliding RECORD path: {path}")
                file = image_path(source, path)
                if file.is_symlink() or not file.is_file():
                    raise RootfsError(f"Missing or symlinked RECORD payload: {path}")
                if digest:
                    if not digest.startswith("sha256="):
                        raise RootfsError(f"Unsupported RECORD hash: {path}")
                    actual = base64.urlsafe_b64encode(bytes.fromhex(sha256(file))).rstrip(b"=").decode()
                    if actual != digest.removeprefix("sha256="):
                        raise RootfsError(f"RECORD hash mismatch: {path}")
                elif file != record:
                    # pip creates metadata files without wheel hashes. It may
                    # not omit hashes for executable or library payloads.
                    generated = {"INSTALLER", "REQUESTED"}
                    if file.parent != info or file.name not in generated:
                        raise RootfsError(f"Unhashed RECORD payload: {path}")
                if size and file.stat().st_size != int(size):
                    raise RootfsError(f"RECORD size mismatch: {path}")
                if wheelhouse is not None:
                    if path in original_files:
                        if file != record and file.read_bytes() != original_files.pop(path):
                            raise RootfsError(f"Installed payload differs from original wheel: {path}")
                        original_files.pop(path, None)
                    elif path in console_scripts:
                        console_scripts.remove(path)
                    elif file.parent != info or file.name not in {"INSTALLER", "REQUESTED"}:
                        raise RootfsError(f"Runtime payload not supplied by original wheel: {path}")
                result[path] = {
                    "kind": "wheel",
                    "name": name,
                    "version": locked[name][0],
                    "wheel_sha256": locked[name][1],
                    "wheel_filename": artifacts[name]["filename"],
                    "record_sha256": sha256(record),
                }
        if original_files or console_scripts:
            raise RootfsError(f"Original wheel payload missing from installation: {name}")
    if installed != set(locked):
        raise RootfsError(f"Runtime inventory differs from lock: {sorted(installed ^ set(locked))}")
    return result


def skip_cpython(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return any(part in {"site-packages", "ensurepip", "__pycache__"} or part.startswith("config-") for part in parts)


def skip_private_tls(path: str) -> bool:
    return path == "/etc/ssl/private" or path.startswith("/etc/ssl/private/")


def parse_ldd(text: str) -> list[str]:
    dependencies = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.fullmatch(r"linux-(?:vdso|gate)\.so\.\d+ \(0x[0-9a-f]+\)", line):
            continue
        match = re.fullmatch(r"(?:\S+ => )?(/\S+) \(0x[0-9a-f]+\)", line)
        if not match:
            raise RootfsError(f"Unresolved or unexpected ldd output: {line}")
        dependencies.append(match[1])
    if not dependencies:
        raise RootfsError(f"No dynamic ELF closure proved: {text!r}")
    return dependencies


def elf_linkage(path: Path) -> tuple[bool, str | None]:
    """Read DT_NEEDED/PT_INTERP, including a loader with no dependencies.

    Native supported builders produce ELF64 little-endian x86-64 or AArch64.
    A malformed header must not turn an unsuccessful ldd into an empty closure.
    """
    data = path.read_bytes()
    if len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01":
        raise RootfsError(f"Unsupported ELF header: {path}")
    if struct.unpack_from("<H", data, 18)[0] not in {62, 183}:
        raise RootfsError(f"Unsupported ELF architecture: {path}")
    offset = struct.unpack_from("<Q", data, 32)[0]
    entry_size, count = struct.unpack_from("<HH", data, 54)
    if entry_size != 56 or count == 0 or offset + count * entry_size > len(data):
        raise RootfsError(f"Invalid ELF program headers: {path}")
    needed = False
    interpreter = None
    for index in range(count):
        kind, _, start, _, _, size, _, _ = struct.unpack_from("<IIQQQQQQ", data, offset + index * entry_size)
        if start + size > len(data):
            raise RootfsError(f"Truncated ELF segment: {path}")
        if kind == 3:
            value = data[start : start + size]
            if not value.endswith(b"\0") or b"\0" in value[:-1] or interpreter is not None:
                raise RootfsError(f"Invalid ELF interpreter: {path}")
            interpreter = normalize(value[:-1].decode("ascii"))
        elif kind == 2:
            if size % 16:
                raise RootfsError(f"Malformed ELF dynamic table: {path}")
            terminated = False
            for position in range(start, start + size, 16):
                tag, _ = struct.unpack_from("<qQ", data, position)
                if tag == 0:
                    terminated = True
                    break
                needed = needed or tag == 1
            if not terminated:
                raise RootfsError(f"Unterminated ELF dynamic table: {path}")
    return needed, interpreter


class RootfsBuilder:
    def __init__(
        self,
        source: Path,
        output: Path,
        *,
        owners: dict[str, list[str]],
        statuses: dict[str, str],
        base_image: str,
        runtime_files: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.source = source.absolute()
        self.output = output.absolute()
        if self.output.is_symlink() or self.output == self.source or self.output == Path("/"):
            raise RootfsError("Unsafe rootfs output path")
        if self.output.exists() and any(self.output.iterdir()):
            raise RootfsError("Rootfs output must be empty")
        if not re.fullmatch(r"python:3\.14\.7-slim-trixie@sha256:[a-f0-9]{64}", base_image):
            raise RootfsError("CPython provenance requires a pinned official 3.14.7-slim-trixie base")
        self.output.mkdir(parents=True, exist_ok=True)
        self.owners = owners
        self.statuses = statuses
        self.base_image = base_image
        self.runtime_files = runtime_files or {}
        self.entries: dict[str, dict[str, Any]] = {}
        self.packages: set[str] = set()
        self._copying: set[str] = set()

    def package(self, name: str) -> str:
        matches = [key for key in self.statuses if key.split(":")[0] == name]
        if len(matches) != 1:
            raise RootfsError(f"Missing or ambiguous generated-file producer: {name}")
        return matches[0]

    def provenance(self, path: str, *, symlink: bool = False) -> dict[str, Any]:
        owners = self.owners.get(path, [])
        if owners:
            if any(owner not in self.statuses for owner in owners):
                raise RootfsError(f"No installed status for payload: {path}")
            self.packages.update(owners)
            return {"kind": "debian", "packages": sorted(owners)}
        if path in self.runtime_files:
            return self.runtime_files[path]
        if path.startswith(PYTHON_STDLIB + "/") and not skip_cpython(path):
            return {"kind": "cpython", "version": PYTHON_VERSION, "base_image": self.base_image}
        if re.fullmatch(r"/usr/local/(?:bin/python(?:3(?:\.14)?)?|lib/libpython3(?:\.14)?\.so(?:\.1\.0)?)", path):
            return {"kind": "cpython", "version": PYTHON_VERSION, "base_image": self.base_image}
        if path == RUNTIME + "/bin/𝜋thon":
            # CPython 3.14's UTF-8 venv alias is generated by setup_python:
            # https://github.com/python/cpython/blob/v3.14.7/Lib/venv/__init__.py#L294-L302
            interpreter = "/usr/local/bin/python3.14"
            if (
                not symlink
                or os.readlink(image_path(self.source, path)) not in {"python", "python3", "python3.14"}
                or resolve_image(self.source, path) != interpreter
                or not image_path(self.source, interpreter).is_file()
            ):
                raise RootfsError("CPython 3.14 pi alias does not target the bound interpreter")
            return {"kind": "cpython-venv", "version": PYTHON_VERSION, "base_image": self.base_image}
        if path == RUNTIME + "/pyvenv.cfg" or re.fullmatch(
            re.escape(RUNTIME) + r"/bin/(?:python(?:3(?:\.14)?)?|activate(?:\.csh|\.fish)?|Activate\.ps1)", path
        ):
            return {"kind": "cpython-venv", "version": PYTHON_VERSION, "base_image": self.base_image}
        if path == RUNTIME + "/.gitignore":
            if (
                image_path(self.source, path).read_text()
                != "# Created by venv; see https://docs.python.org/3/library/venv.html\n*\n"
            ):
                raise RootfsError("Unexpected venv .gitignore content")
            return {"kind": "cpython-venv", "version": PYTHON_VERSION, "base_image": self.base_image}
        if path == RUNTIME + "/lib64" and symlink and os.readlink(image_path(self.source, path)) == "lib":
            return {"kind": "cpython-venv", "version": PYTHON_VERSION, "base_image": self.base_image}
        generated = {
            "/etc/ssl/certs/ca-certificates.crt": "ca-certificates",
            "/etc/ssl/certs/java/cacerts": "ca-certificates-java",
            "/etc/ld.so.cache": "libc-bin",
        }
        if path in generated:
            package = self.package(generated[path])
            if path.startswith("/etc/ssl/certs/"):
                anchors = list((self.source / "usr/share/ca-certificates").rglob("*.crt"))
                if not anchors or not (self.source / "etc/ca-certificates.conf").is_file():
                    raise RootfsError(f"Missing CA inputs for generated truststore: {path}")
                for anchor in anchors:
                    anchor_path = "/" + str(anchor.relative_to(self.source))
                    if not self.owners.get(anchor_path):
                        raise RootfsError(f"Untraced CA input: {anchor_path}")
                self.package("ca-certificates")
                custom = self.source / "usr/local/share/ca-certificates"
                if custom.exists() and any(item.is_file() for item in custom.rglob("*")):
                    raise RootfsError("Custom trust anchors are not allowed in the runtime builder")
            self.packages.add(package)
            return {"kind": "debian-generated", "packages": [package], "generator": generated[path]}
        if path == "/etc/nsswitch.conf":
            template = self.source / "usr/share/libc-bin/nsswitch.conf"
            if not template.is_file() or image_path(self.source, path).read_bytes() != template.read_bytes():
                raise RootfsError("Unowned nsswitch.conf differs from the Debian libc-bin template")
            package = self.package("libc-bin")
            self.packages.add(package)
            return {
                "kind": "debian-generated",
                "packages": [package],
                "generator": "libc-bin postinst",
                "template_sha256": sha256(template),
            }
        if symlink:
            resolved = resolve_image(self.source, path)
            if path in {"/bin", "/sbin", "/lib", "/lib64"} and resolved == "/usr" + path:
                return {"kind": "merged-usr-layout", "base_image": self.base_image}
            if path.startswith(("/etc/alternatives/", "/etc/ssl/certs/")) or path in {
                "/usr/bin/java",
                "/etc/localtime",
            }:
                target_owners = self.owners.get(resolve_image(self.source, path, follow_final=True), [])
                if target_owners:
                    self.packages.update(target_owners)
                    return {"kind": "debian-generated-symlink", "packages": sorted(target_owners), "target": resolved}
        raise RootfsError(f"No approved provenance for runtime payload: {path}")

    def destination(self, path: str) -> tuple[str, Path]:
        canonical = resolve_image(self.source, path, follow_final=False)
        # Never let a preserved absolute symlink redirect a host-side write.
        return canonical, image_path(self.output, canonical)

    def ensure_directory(self, path: str) -> None:
        path = normalize(path)
        if path == "/":
            return
        self.ensure_directory(posixpath.dirname(path))
        canonical, destination = self.destination(path)
        original = image_path(self.source, canonical)
        if original.is_symlink():
            self.copy_path(path)
            return
        if not original.is_dir():
            raise RootfsError(f"Missing runtime directory: {path}")
        if canonical not in self.entries:
            destination.mkdir(exist_ok=True)
            mode = stat.S_IMODE(original.stat().st_mode)
            destination.chmod(mode)
            self.entries[canonical] = {"type": "directory", "mode": mode}

    def copy_path(self, path: str, *, recursive: bool = False, exclude: Callable[[str], bool] | None = None) -> None:
        path = normalize(path)
        if exclude and exclude(path):
            return
        if path in self._copying:
            raise RootfsError(f"Symlink copy cycle: {path}")
        self._copying.add(path)
        try:
            self.ensure_directory(posixpath.dirname(path))
            canonical, destination = self.destination(path)
            original = image_path(self.source, canonical)
            try:
                information = original.lstat()
            except FileNotFoundError as exc:
                raise RootfsError(f"Missing runtime payload: {path}") from exc
            if stat.S_ISDIR(information.st_mode):
                self.ensure_directory(path)
                if recursive:
                    for child in sorted(original.iterdir()):
                        self.copy_path(posixpath.join(path, child.name), recursive=True, exclude=exclude)
                return
            if canonical in self.entries:
                return
            link = stat.S_ISLNK(information.st_mode)
            if not link and not stat.S_ISREG(information.st_mode):
                raise RootfsError(f"Special runtime file forbidden: {path}")
            provenance = self.provenance(canonical, symlink=link)
            if destination.exists() or destination.is_symlink():
                raise RootfsError(f"Rootfs destination collision: {canonical}")
            if link:
                target = os.readlink(original)
                destination.symlink_to(target)
                self.entries[canonical] = {"type": "symlink", "target": target, "provenance": provenance}
                target_path = normalize(
                    target if target.startswith("/") else posixpath.join(posixpath.dirname(path), target)
                )
                self.copy_path(target_path, recursive=recursive, exclude=exclude)
            else:
                shutil.copyfile(original, destination)
                mode = stat.S_IMODE(information.st_mode)
                if mode & (stat.S_ISUID | stat.S_ISGID):
                    raise RootfsError(f"Set-id runtime file forbidden: {path}")
                destination.chmod(mode)
                with original.open("rb") as stream:
                    elf = stream.read(4) == b"\x7fELF"
                self.entries[canonical] = {
                    "type": "file",
                    "mode": mode,
                    "size": information.st_size,
                    "sha256": sha256(destination),
                    "elf": elf,
                    "provenance": provenance,
                }
        finally:
            self._copying.remove(path)

    def copy_elf_closure(self, inspect: Callable[[str], list[str]]) -> None:
        inspected: set[str] = set()
        while True:
            pending = [path for path, entry in self.entries.items() if entry.get("elf") and path not in inspected]
            if not pending:
                return
            for path in pending:
                dependencies = inspect(path)
                for dependency in dependencies:
                    self.copy_path(dependency)
                self.entries[path]["elf_dependencies"] = dependencies
                inspected.add(path)

    def generated(self, path: str, data: bytes, reason: str, *, mode: int = 0o644) -> None:
        path = normalize(path)
        # Generated locations are fixed by this program, never caller supplied.
        destination = image_path(self.output, path)
        for parent in destination.parents:
            if parent == self.output:
                break
            if parent.is_symlink():
                raise RootfsError(f"Generated-file parent is a symlink: {path}")
        if destination.exists() or destination.is_symlink() or path in self.entries:
            raise RootfsError(f"Generated payload collision: {path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        destination.chmod(mode)
        self.entries[path] = {
            "type": "file",
            "mode": mode,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "elf": False,
            "provenance": {"kind": "generated", "reason": reason},
        }

    def copy_package_metadata(self) -> None:
        processed: set[str] = set()
        while self.packages - processed:
            for package in sorted(self.packages - processed):
                self.copy_path(f"/usr/share/doc/{package.split(':')[0]}/copyright")
                processed.add(package)
        combined = ""
        for package in sorted(self.packages):
            stanza = self.statuses[package]
            combined += stanza + "\n"
            self.generated(f"/var/lib/dpkg/status.d/{package}", stanza.encode(), "unmodified installed dpkg stanza")
        self.generated("/var/lib/dpkg/status", combined.encode(), "unmodified retained-package dpkg stanzas")

    def runtime_roots(self) -> None:
        for path in ("/usr/local/bin/python", "/usr/local/bin/python3", "/usr/local/bin/python3.14"):
            self.copy_path(path)
        for library_path in sorted((self.source / "usr/local/lib").glob("libpython*.so*")):
            self.copy_path("/" + str(library_path.relative_to(self.source)))
        self.copy_path(PYTHON_STDLIB, recursive=True, exclude=skip_cpython)
        self.copy_path(RUNTIME, recursive=True)
        required = (
            "/usr/bin/java",
            "/usr/lib/jvm",
            "/etc/ssl",
            "/usr/share/ca-certificates",
            "/etc/ca-certificates.conf",
            "/usr/share/zoneinfo",
            "/etc/localtime",
            "/etc/nsswitch.conf",
            "/etc/os-release",
            "/etc/ld.so.cache",
            "/usr/lib/ssl/cert.pem",
            "/usr/lib/ssl/certs",
            "/usr/lib/ssl/openssl.cnf",
        )
        for path in required:
            self.copy_path(path, recursive=True, exclude=skip_private_tls if path == "/etc/ssl" else None)
        # The existing headless-Java baseline need not install system fonts.
        # Application Noto fonts are copied with /app and tested separately.
        for path in ("/etc/fonts", "/usr/share/fontconfig", "/usr/share/fonts"):
            candidate = image_path(self.source, path)
            if candidate.exists() or candidate.is_symlink():
                self.copy_path(path, recursive=True)
        # These providers are dlopened and are not guaranteed to appear in ldd.
        providers = (
            "libnss_dns.so*",
            "libnss_files.so*",
            "libnss3.so",
            "libsmime3.so",
            "libnssutil3.so",
            "libsoftokn3.so",
            "libfreebl3.so",
            "libfreeblpriv3.so",
        )
        for pattern in providers:
            for library_path in sorted((self.source / "usr/lib").glob("**/" + pattern)):
                self.copy_path("/" + str(library_path.relative_to(self.source)))
        for provider_path in sorted((self.source / "usr/lib").glob("**/ossl-modules")):
            self.copy_path("/" + str(provider_path.relative_to(self.source)), recursive=True)

    def account_and_mount_points(self) -> None:
        self.generated(
            "/etc/passwd",
            b"root:x:0:0:root:/root:/sbin/nologin\nappuser:x:10001:10001::/home/appuser:/sbin/nologin\n",
            "fixed nonroot runtime account",
        )
        self.generated("/etc/group", b"root:x:0:\nappuser:x:10001:\n", "fixed nonroot runtime group")
        for path in ("/etc/hosts", "/etc/hostname", "/etc/resolv.conf"):
            self.generated(path, b"", "container engine supplies network configuration at startup")
        for path, mode, uid, gid in (
            ("/tmp", 0o1777, 0, 0),
            ("/home/appuser", 0o755, 10001, 10001),
            ("/dev", 0o755, 0, 0),
            ("/app", 0o755, 10001, 10001),
            ("/app/vendor", 0o755, 10001, 10001),
        ):
            destination = image_path(self.output, path)
            destination.mkdir(parents=True, exist_ok=True)
            destination.chmod(mode)
            os.chown(destination, uid, gid)
            self.entries[path] = {"type": "directory", "mode": mode, "uid": uid, "gid": gid}

    def write_manifest(self, *, lock: Path, metadata: Path) -> None:
        for path, entry in self.entries.items():
            if entry.get("elf") and "elf_dependencies" not in entry:
                raise RootfsError(f"ELF was not inspected: {path}")
            if entry["type"] != "directory" and "provenance" not in entry:
                raise RootfsError(f"Untraced runtime file: {path}")
            if entry["type"] == "symlink":
                resolved = resolve_image(self.output, path)
                if not image_path(self.output, resolved).exists():
                    raise RootfsError(f"Dangling output symlink: {path}")
        package_metadata = {}
        for package in sorted(self.packages):
            fields = email.parser.Parser().parsestr(self.statuses[package])
            name, version = fields["Package"], fields["Version"]
            source = fields.get("Source", name)
            source_match = re.fullmatch(r"([^\s()]+)(?: \(([^()]+)\))?", source)
            if not source_match:
                raise RootfsError(f"Malformed Source field: {package}")
            package_metadata[package] = {
                "name": name,
                "version": version,
                "architecture": fields["Architecture"],
                "source_name": source_match[1],
                "source_version": source_match[2] or version,
                "status_sha256": hashlib.sha256(self.statuses[package].encode()).hexdigest(),
                "status_path": f"/var/lib/dpkg/status.d/{package}",
            }
        manifest = {
            "schema_version": 1,
            "base_image": self.base_image,
            "cpython_version": PYTHON_VERSION,
            "builder_script_sha256": sha256(Path(__file__)),
            "runtime_lock_sha256": sha256(lock),
            "runtime_metadata_sha256": sha256(metadata),
            "packages": package_metadata,
            "files": dict(sorted(self.entries.items())),
            "manifest_self_excluded": True,
        }
        self.generated(
            MANIFEST_PATH,
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
            "rootfs provenance manifest; its digest is captured by the image audit",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-lock", type=Path, required=True)
    parser.add_argument("--runtime-metadata", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, default=Path("/wheels"))
    parser.add_argument("--base-image", default=DEFAULT_BASE_IMAGE)
    arguments = parser.parse_args(argv)
    try:
        if sys.platform != "linux" or sys.version_info[:3] != (3, 14, 7) or os.geteuid() != 0:
            raise RootfsError("Run as root using native CPython 3.14.7 inside the pinned Linux builder")
        source = Path("/")
        owners, statuses = debian_inventory(source)
        runtime_files = runtime_provenance(
            source, arguments.runtime_lock, arguments.runtime_metadata, arguments.wheelhouse
        )
        build = RootfsBuilder(
            source,
            arguments.output,
            owners=owners,
            statuses=statuses,
            base_image=arguments.base_image,
            runtime_files=runtime_files,
        )
        build.runtime_roots()
        java_paths = sorted(Path("/usr/lib/jvm").glob("*/lib")) + sorted(Path("/usr/lib/jvm").glob("*/lib/server"))
        environment = {key: value for key, value in os.environ.items() if not key.startswith("LD_")}
        environment.update(
            LC_ALL="C", LD_LIBRARY_PATH=":".join(["/usr/local/lib", *(str(path) for path in java_paths)])
        )

        def inspect(path: str) -> list[str]:
            needed, interpreter = elf_linkage(Path(path))
            if not needed:
                return [interpreter] if interpreter else []
            dependencies = parse_ldd(command(["ldd", path], env=environment))
            if interpreter and interpreter not in dependencies:
                dependencies.append(interpreter)
            return dependencies

        build.copy_elf_closure(inspect)
        build.copy_package_metadata()
        # Copyright symlinks may extend the package closure. Check any newly
        # introduced ELF bytes too rather than assuming metadata is harmless.
        build.copy_elf_closure(inspect)
        build.account_and_mount_points()
        build.write_manifest(lock=arguments.runtime_lock, metadata=arguments.runtime_metadata)
        print(
            json.dumps(
                {"manifest": MANIFEST_PATH, "files": len(build.entries), "debian_packages": len(build.packages)},
                sort_keys=True,
            )
        )
        return 0
    except (RootfsError, OSError, ValueError, KeyError) as exc:
        print(f"Runtime rootfs build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
