#!/usr/bin/env python3
"""Apply and verify one exact upstream CPython security backport in owned runtimes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
METADATA = ROOT / "packaging/python/cpython-security.json"
RECEIPT_RELATIVE = Path("share/e-rechnung-pruefer/cpython-security.json")
VERSION = "3.14.7"
ADVISORY = "CVE-2026-15806"
COMMIT = "a0d023fbd23773e24b35d8368789470e22cda5d8"
BEFORE = "f3464032de00c1fbc839f4657577065ea6efb30d99e5a86e204b1aefdcb77430"
AFTER = "48fcaa1b047f09f53abf37a36184686a96d56ddaacc63ae6f9ebe9db1827f784"


class SecurityPatchError(RuntimeError):
    """The exact source, applied correction or runtime could not be established."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def security_metadata() -> dict[str, Any]:
    metadata: dict[str, Any] = json.loads(METADATA.read_bytes())
    expected = {
        "schema_version": 1,
        "cpython_version": VERSION,
        "advisory": ADVISORY,
        "upstream_commit": COMMIT,
        "relative_file": "urllib/request.py",
        "before_sha256": BEFORE,
        "after_sha256": AFTER,
        "patch_file": "urllib-request-CVE-2026-15806.patch",
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise SecurityPatchError("Unexpected CPython security metadata")
    patch = METADATA.with_name(metadata["patch_file"]).read_bytes()
    if sha256(patch) != metadata.get("patch_sha256"):
        raise SecurityPatchError("CPython security patch hash mismatch")
    return metadata


def patch_bytes(source: bytes) -> bytes:
    """No fuzzy matching: original bytes, every hunk and final bytes must match."""
    metadata = security_metadata()
    digest = sha256(source)
    if digest == metadata["before_sha256"]:
        crlf = False
    elif digest == metadata["before_crlf_sha256"]:
        crlf = True
    else:
        raise SecurityPatchError("Unknown CPython source hash; refusing to patch")
    canonical = source.replace(b"\r\n", b"\n") if crlf else source
    if sha256(canonical) != BEFORE:
        raise SecurityPatchError("CPython canonical source hash mismatch")
    original = canonical.decode("utf-8").splitlines(keepends=True)
    patch = METADATA.with_name(metadata["patch_file"]).read_text(encoding="utf-8").splitlines(keepends=True)
    if patch[:2] != ["--- a/Lib/urllib/request.py\n", "+++ b/Lib/urllib/request.py\n"]:
        raise SecurityPatchError("Unexpected patch target")
    result: list[str] = []
    position = 0
    index = 2
    while index < len(patch):
        header = re.fullmatch(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@\n", patch[index])
        if header is None:
            raise SecurityPatchError("Malformed patch hunk")
        start, old_count, new_start, new_count = map(int, header.groups())
        if start - 1 < position or start - 1 > len(original):
            raise SecurityPatchError("Overlapping or out-of-range patch hunk")
        result.extend(original[position : start - 1])
        position = start - 1
        if len(result) != new_start - 1:
            raise SecurityPatchError("Patch output offset mismatch")
        consumed = produced = 0
        index += 1
        while index < len(patch) and not patch[index].startswith("@@ "):
            line = patch[index]
            if line[:1] in {" ", "-"}:
                if position >= len(original) or original[position] != line[1:]:
                    raise SecurityPatchError("Patch context differs from pinned source")
                position += 1
                consumed += 1
            if line[:1] in {" ", "+"}:
                result.append(line[1:])
                produced += 1
            if line[:1] not in {" ", "+", "-"}:
                raise SecurityPatchError("Unexpected patch syntax")
            index += 1
        if (consumed, produced) != (old_count, new_count):
            raise SecurityPatchError("Patch hunk count mismatch")
    result.extend(original[position:])
    fixed = "".join(result).encode("utf-8")
    if sha256(fixed) != AFTER:
        raise SecurityPatchError("Patched canonical bytes differ from upstream fix")
    if crlf:
        fixed = fixed.replace(b"\n", b"\r\n")
    if sha256(fixed) != metadata["after_crlf_sha256" if crlf else "after_sha256"]:
        raise SecurityPatchError("Patched native bytes differ from expected line endings")
    return fixed


def make_receipt(before: bytes, after: bytes) -> dict[str, Any]:
    metadata = security_metadata()
    if patch_bytes(before) != after:
        raise SecurityPatchError("Receipt source/result mismatch")
    return {
        "schema_version": 1,
        "cpython_version": VERSION,
        "advisory": ADVISORY,
        "upstream_commit": COMMIT,
        "relative_file": "urllib/request.py",
        "input_sha256": sha256(before),
        "output_sha256": sha256(after),
        "canonical_before_sha256": BEFORE,
        "canonical_after_sha256": AFTER,
        "patch_sha256": metadata["patch_sha256"],
        "metadata_sha256": sha256(METADATA.read_bytes()),
        "helper_sha256": sha256(Path(__file__).read_bytes()),
        "line_endings": "crlf" if b"\r\n" in before else "lf",
    }


def regular_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
        raise SecurityPatchError(f"Expected independent regular file: {path}")


def safe_parent_directories(path: Path) -> None:
    for parent in path.absolute().parents:
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SecurityPatchError(f"Symlink/reparse point in CPython output parent: {parent}")
        if not stat.S_ISDIR(info.st_mode):
            raise SecurityPatchError(f"Non-directory CPython output parent: {parent}")


def validate_receipt(receipt: dict[str, Any], *, target: Path | None = None) -> dict[str, Any]:
    metadata = security_metadata()
    endings = receipt.get("line_endings")
    if endings not in {"lf", "crlf"}:
        raise SecurityPatchError("Invalid receipt line endings")
    expected = {
        "schema_version": 1,
        "cpython_version": VERSION,
        "advisory": ADVISORY,
        "upstream_commit": COMMIT,
        "relative_file": "urllib/request.py",
        "input_sha256": metadata["before_crlf_sha256" if endings == "crlf" else "before_sha256"],
        "output_sha256": metadata["after_crlf_sha256" if endings == "crlf" else "after_sha256"],
        "canonical_before_sha256": BEFORE,
        "canonical_after_sha256": AFTER,
        "patch_sha256": metadata["patch_sha256"],
        "metadata_sha256": sha256(METADATA.read_bytes()),
        "helper_sha256": sha256(Path(__file__).read_bytes()),
        "line_endings": endings,
    }
    if receipt != expected:
        raise SecurityPatchError("CPython security receipt does not match pinned provenance")
    if target is not None:
        regular_file(target)
        data = target.read_bytes()
        if sha256(data) != expected["output_sha256"] or sha256(data.replace(b"\r\n", b"\n")) != AFTER:
            raise SecurityPatchError("CPython security target bytes mismatch")
    return dict(receipt)


def run_security_regressions(module: ModuleType) -> dict[str, Any]:
    """Synthetic credentials only; never opens sockets or makes an HTTP request."""
    cases: list[str] = []

    def require(condition: bool, name: str) -> None:
        if not condition:
            raise SecurityPatchError(f"CPython URL scheme security regression: {name}")
        cases.append(name)

    for manager_class in (module.HTTPPasswordMgr, module.HTTPPasswordMgrWithPriorAuth):
        for source_scheme, other_scheme in (("https", "http"), ("http", "https")):
            manager = manager_class()
            manager.add_password("synthetic-realm", f"{source_scheme}://example.invalid/area/", "synthetic", "secret")
            prefix = manager_class.__name__ + "-" + source_scheme
            require(
                manager.find_user_password("synthetic-realm", f"{other_scheme}://example.invalid/area/item")
                == (None, None),
                prefix + "-scheme-isolation",
            )
            require(
                manager.find_user_password("synthetic-realm", f"{source_scheme}://example.invalid/area/item")
                == ("synthetic", "secret"),
                prefix + "-same-scheme",
            )
            require(
                manager.find_user_password("synthetic-realm", f"{source_scheme}://other.invalid/area/") == (None, None),
                prefix + "-host-isolation",
            )
            require(
                manager.find_user_password("synthetic-realm", f"{source_scheme}://example.invalid/elsewhere/")
                == (None, None),
                prefix + "-path-isolation",
            )
            expected_realm = (None, None) if manager_class is module.HTTPPasswordMgr else ("synthetic", "secret")
            require(
                manager.find_user_password("another-realm", f"{source_scheme}://example.invalid/area/")
                == expected_realm,
                prefix + "-realm-compatibility",
            )
        manager = manager_class()
        manager.add_password("synthetic-realm", "https://example.invalid/", "synthetic", "secret")
        require(
            manager.find_user_password("synthetic-realm", "example.invalid") == ("synthetic", "secret"),
            manager_class.__name__ + "-schemeless-lookup",
        )
        for registered in ("schemeless.invalid", "//schemeless.invalid/"):
            manager = manager_class()
            manager.add_password("synthetic-realm", registered, "synthetic", "secret")
            for scheme in ("http", "https"):
                require(
                    manager.find_user_password("synthetic-realm", f"{scheme}://schemeless.invalid/")
                    == ("synthetic", "secret"),
                    manager_class.__name__ + "-schemeless-registration-" + registered + "-" + scheme,
                )
        require(
            manager.reduce_uri("http://example.invalid/path") == ("example.invalid:80", "/path"),
            manager_class.__name__ + "-legacy-reduced-uri",
        )
        require(
            manager.is_suburi(("example.invalid", "/path"), ("example.invalid", "/path/item")),
            manager_class.__name__ + "-legacy-suburi",
        )
    manager = module.HTTPPasswordMgrWithPriorAuth()
    manager.add_password(None, "https://example.invalid/", "synthetic", "secret", is_authenticated=True)
    handler = module.HTTPBasicAuthHandler(manager)
    request = module.Request("http://example.invalid/")
    handler.http_request(request)
    require(not request.has_header("Authorization"), "prior-auth-no-https-to-http-header")
    secure = module.Request("https://example.invalid/")
    handler.http_request(secure)
    require(secure.has_header("Authorization"), "prior-auth-same-scheme-header")
    require(manager.is_authenticated("http://example.invalid/") is not True, "prior-auth-state-scheme-isolation")
    return {"passed": True, "cases": cases}


def apply_file(target: Path, receipt_path: Path) -> dict[str, Any]:
    safe_parent_directories(target)
    safe_parent_directories(receipt_path)
    regular_file(target)
    backup = receipt_path.with_suffix(".before.source")
    if receipt_path.exists() or receipt_path.is_symlink() or backup.exists() or backup.is_symlink():
        raise SecurityPatchError("Existing CPython patch evidence must not be overwritten")
    original = target.read_bytes()
    fixed = patch_bytes(original)
    receipt = make_receipt(original, fixed)
    cache = target.parent / "__pycache__"
    if cache.is_symlink():
        raise SecurityPatchError("Bytecode directory must not be a symlink")
    bytecode = list(cache.glob("request.*.pyc")) + [target.with_suffix(".pyc")]
    bytecode = [path for path in bytecode if path.exists() or path.is_symlink()]
    for path in bytecode:
        regular_file(path)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with backup.open("xb") as handle:
        handle.write(original)
    descriptor, temporary = tempfile.mkstemp(prefix=".request-security-", dir=target.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(fixed)
        temporary_path.chmod(stat.S_IMODE(target.stat().st_mode))
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)
    for path in bytecode:
        path.unlink()
    with receipt_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return validate_receipt(receipt, target=target)


def require_version() -> None:
    if sys.implementation.name != "cpython" or sys.version_info[:3] != (3, 14, 7):
        raise SecurityPatchError("Backport requires exactly CPython 3.14.7")


def runtime_target() -> Path:
    return Path(sysconfig.get_path("stdlib")) / "urllib/request.py"


def verify_runtime() -> dict[str, Any]:
    import urllib.request

    require_version()
    target = runtime_target()
    prefix = Path(sys.base_prefix).resolve(strict=True)
    if not target.resolve(strict=True).is_relative_to(prefix) or Path(urllib.request.__file__).resolve(
        strict=True
    ) != target.resolve(strict=True):
        raise SecurityPatchError("Loaded urllib.request is outside the bound interpreter")
    receipt_path = prefix / RECEIPT_RELATIVE
    regular_file(receipt_path)
    raw = receipt_path.read_bytes()
    receipt = validate_receipt(json.loads(raw), target=target)
    behavior = run_security_regressions(urllib.request)
    return {
        "cpython_version": VERSION,
        "module_path": str(target),
        "relative_file": "urllib/request.py",
        "output_sha256": sha256(target.read_bytes()),
        "receipt_sha256": sha256(raw),
        "receipt": receipt,
        "behavior_passed": behavior["passed"],
        "cases": behavior["cases"],
    }


def apply_current(prefix: Path, receipt_path: Path) -> dict[str, Any]:
    require_version()
    prefix = prefix.resolve(strict=True)
    target = runtime_target()
    if prefix != Path(sys.base_prefix).resolve(strict=True) or not target.resolve(strict=True).is_relative_to(prefix):
        raise SecurityPatchError("Explicit prefix does not bind the current CPython standard library")
    if receipt_path.absolute() != prefix / RECEIPT_RELATIVE:
        raise SecurityPatchError("Receipt must remain inside the explicitly bound interpreter")
    apply_file(target, receipt_path)
    # Verify in a fresh process: cached pre-patch imports must not provide false evidence.
    result = subprocess.run(
        [sys.executable, "-I", str(Path(__file__).resolve()), "verify-runtime"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def clone_runtime(destination: Path) -> dict[str, Any]:
    require_version()
    if sys.platform not in {"win32", "linux"}:
        raise SecurityPatchError("Private CPython cloning is supported only on native Windows/Linux builders")
    source = Path(sys.base_prefix).resolve(strict=True)
    destination = destination.absolute()
    safe_parent_directories(destination)
    if destination.exists() or destination.is_symlink() or destination.resolve().is_relative_to(source):
        raise SecurityPatchError("Private interpreter destination must be new and outside its source")
    # Establish the pristine known input before copying anything.
    patch_bytes(runtime_target().read_bytes())
    shutil.copytree(source, destination, symlinks=False, ignore=shutil.ignore_patterns("__pycache__", "site-packages"))
    executable = destination / ("python.exe" if sys.platform == "win32" else "bin/python3")
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}}
    result = subprocess.run(
        [
            str(executable),
            "-I",
            str(Path(__file__).resolve()),
            "apply-current",
            "--prefix",
            str(destination),
            "--receipt",
            str(destination / RECEIPT_RELATIVE),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    proof = json.loads(result.stdout)
    proof.update(executable=str(executable), private_prefix=str(destination))
    return proof


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    clone = commands.add_parser("clone")
    clone.add_argument("--destination", type=Path, required=True)
    clone.add_argument("--output", type=Path)
    apply = commands.add_parser("apply-current")
    apply.add_argument("--prefix", type=Path, required=True)
    apply.add_argument("--receipt", type=Path, required=True)
    verify = commands.add_parser("verify-runtime")
    verify.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "clone":
            result = clone_runtime(args.destination)
        elif args.command == "apply-current":
            result = apply_current(args.prefix, args.receipt)
        else:
            result = verify_runtime()
        text = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if getattr(args, "output", None):
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as output:
                output.write(text)
        print(text, end="")
        return 0
    except (SecurityPatchError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"CPython-Sicherheitsprüfung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
