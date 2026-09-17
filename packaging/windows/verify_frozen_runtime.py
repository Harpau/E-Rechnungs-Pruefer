"""Verify the patched stdlib actually embedded in all three Windows executables.

Runs with the private, verified Windows build interpreter and its locked PyInstaller.
It reads the EXEs as archives; it never launches an executable or installs a package.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import CodeType, ModuleType
from typing import Any

EXECUTABLE_NAMES = (
    "E-Rechnungs-Pruefer.exe",
    "E-Rechnungs-Pruefer-Dienst.exe",
    "E-Rechnungs-Pruefer-Oeffnen.exe",
)
APPLICATION_MODULES = {
    "app.main": "app/main.py",
    "app.configuration": "app/configuration.py",
    "app.http_upload": "app/http_upload.py",
    "app.report_templates": "app/report_templates.py",
    "app.server_runtime": "app/server_runtime.py",
    "app.upload_ingress": "app/upload_ingress.py",
    "app.processing": "app/processing/__init__.py",
    **{
        f"app.processing.{name}": f"app/processing/{name}.py"
        for name in (
            "bootstrap",
            "budgets",
            "kosit_runtime",
            "manager",
            "native",
            "operations",
            "posix",
            "protocol",
            "ready",
            "result",
            "supervisor",
            "watchdog",
            "windows",
            "worker",
        )
    },
}


class FrozenRuntimeError(RuntimeError):
    """Missing, altered or unverified frozen security payload."""


def security_helper() -> Any:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    return importlib.import_module("scripts.cpython_security")


def archive_reader(path: Path) -> Any:
    readers = importlib.import_module("PyInstaller.archive.readers")
    return readers.CArchiveReader(str(path))


def _constant(value: Any) -> Any:
    if isinstance(value, CodeType):
        return ["code", _code_fields(value)]
    if value is None or value is Ellipsis:
        return [type(value).__name__]
    if type(value) in (str, bool, int):
        return [type(value).__name__, value]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, complex):
        return ["complex", value.real.hex(), value.imag.hex()]
    if isinstance(value, tuple):
        return ["tuple", [_constant(item) for item in value]]
    if isinstance(value, slice):
        return ["slice", _constant(value.start), _constant(value.stop), _constant(value.step)]
    if isinstance(value, frozenset):
        return ["frozenset", sorted((_constant(item) for item in value), key=_serialized)]
    raise FrozenRuntimeError("Unbekannter Konstantentyp im eingefrorenen Code.")


def _serialized(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _code_fields(code: CodeType) -> dict[str, Any]:
    # Only the build-machine filename differs legitimately. Preserve bytecode,
    # typed constants, names and every executable/position/exception-table field.
    fields = (
        "co_argcount",
        "co_posonlyargcount",
        "co_kwonlyargcount",
        "co_nlocals",
        "co_stacksize",
        "co_flags",
        "co_name",
        "co_qualname",
        "co_firstlineno",
        "co_names",
        "co_varnames",
        "co_freevars",
        "co_cellvars",
    )
    result = {field: getattr(code, field) for field in fields}
    result.update(
        co_code=code.co_code.hex(),
        co_linetable=code.co_linetable.hex(),
        co_exceptiontable=code.co_exceptiontable.hex(),
        co_consts=[_constant(value) for value in code.co_consts],
    )
    return result


def code_digest(code: CodeType) -> str:
    return hashlib.sha256(_serialized(_code_fields(code)).encode("utf-8")).hexdigest()


def read_frozen_code(path: Path, module_name: str = "urllib.request") -> CodeType:
    try:
        archive = archive_reader(path)
        pyz_names = [name for name, entry in archive.toc.items() if entry[-1] == "z"]
        if pyz_names != ["PYZ.pyz"]:
            raise FrozenRuntimeError("Genau ein eingebettetes PYZ.pyz ist erforderlich.")
        # CArchiveReader's embedded reader otherwise defaults to check_pymagic=False.
        raw = archive.extract("PYZ.pyz")
        if raw[:8] != b"PYZ\0" + importlib.util.MAGIC_NUMBER:
            raise FrozenRuntimeError("PYZ-Pythonmagic stimmt nicht mit dem geprüften Interpreter überein.")
        pyz = archive.open_embedded_archive("PYZ.pyz")
        expected_type = 1 if module_name == "app.processing" else 0
        if pyz.toc.get(module_name, (None,))[0] != expected_type:
            raise FrozenRuntimeError(f"Das eingefrorene Modul fehlt oder hat den falschen Typ: {module_name}")
        code = pyz.extract(module_name)
        if not isinstance(code, CodeType):
            raise FrozenRuntimeError(f"Das eingefrorene Modul enthält keinen Code: {module_name}")
        return code
    except FrozenRuntimeError:
        raise
    except Exception as exc:
        raise FrozenRuntimeError(f"Eingefrorener Code kann nicht gelesen werden: {path.name}") from exc


def verify_application_code(path: Path, *, source_root: Path | None = None) -> dict[str, Any]:
    """Read-only app payload binding; never execute frozen processing code."""
    root = source_root or Path(__file__).resolve().parents[2]
    records = []
    for name, relative in APPLICATION_MODULES.items():
        source_path = root / relative
        before = file_record(source_path)
        source = source_path.read_bytes()
        expected = compile(source, relative, "exec", dont_inherit=True, optimize=1)
        expected_digest = code_digest(expected)
        actual = read_frozen_code(path, name)
        if code_digest(actual) != expected_digest:
            raise FrozenRuntimeError(f"Eingefrorener Anwendungscode weicht von der Build-Source ab: {name}")
        if hashlib.sha256(source).hexdigest() != before["sha256"] or file_record(source_path) != before:
            raise FrozenRuntimeError(f"Anwendungs-Source wurde während der Prüfung verändert: {name}")
        records.append({"name": name, "source_sha256": before["sha256"], "code_sha256": expected_digest})
    return {"passed": True, "modules": records}


def file_record(path: Path) -> dict[str, Any]:
    path = path.absolute()
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400:
        raise FrozenRuntimeError(f"Keine reguläre Artefaktdatei: {path.name}")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        finished = os.fstat(stream.fileno())
    after = path.lstat()
    # Windows path-stat and handle-stat differ in extension-derived execute bits;
    # compare their identities, and full modes within each same-API pair.
    identities = {
        (
            item.st_dev,
            item.st_ino,
            item.st_mode & ~0o111 if sys.platform == "win32" else item.st_mode,
            item.st_size,
            item.st_mtime_ns,
        )
        for item in (before, opened, finished, after)
    }
    if len(identities) != 1 or before.st_mode != after.st_mode or opened.st_mode != finished.st_mode:
        raise FrozenRuntimeError(f"Artefaktdatei wurde während der Prüfung verändert: {path.name}")
    return {"path": str(path), "name": path.name, "size": before.st_size, "sha256": checksum}


def verify_binaries(paths: list[Path]) -> dict[str, Any]:
    if len(paths) != 3 or {path.name for path in paths} != set(EXECUTABLE_NAMES):
        raise FrozenRuntimeError("Genau die drei anwendungseigenen Windows-EXEs sind erforderlich.")
    helper = security_helper()
    runtime = helper.verify_runtime()
    if runtime.get("behavior_passed") is not True:
        raise FrozenRuntimeError("Die Build-Laufzeit wurde nicht erfolgreich verifiziert.")
    source = helper.runtime_target().read_bytes()
    source_sha256 = hashlib.sha256(source).hexdigest()
    if source_sha256 != runtime.get("output_sha256"):
        raise FrozenRuntimeError("Die verifizierte Source wurde vor dem Frozen-Abgleich verändert.")
    expected = compile(source, "urllib/request.py", "exec", dont_inherit=True, optimize=1)
    expected_digest = code_digest(expected)
    records = []
    for path in paths:
        before = file_record(path)
        code = read_frozen_code(path)
        if code_digest(code) != expected_digest:
            raise FrozenRuntimeError(f"Eingefrorener Code weicht vom geprüften Backport ab: {path.name}")
        # Execute only after the full code comparison. The helper's regressions
        # use synthetic redirects and never issue a network request.
        module = ModuleType("_einvoice_verified_frozen_urllib_request")
        module.__package__ = "urllib"
        exec(code, module.__dict__)
        behavior = helper.run_security_regressions(module)
        if behavior.get("passed") is not True:
            raise FrozenRuntimeError(f"Sicherheitsregression im eingefrorenen Code fehlgeschlagen: {path.name}")
        application = verify_application_code(path) if path.name in EXECUTABLE_NAMES[:2] else None
        if file_record(path) != before:
            raise FrozenRuntimeError(f"Artefaktdatei wurde während der Codeprüfung verändert: {path.name}")
        record = {**before, "code_sha256": expected_digest, "behavior": behavior}
        if application is not None:
            record["application"] = application
        records.append(record)
    return {
        "schema_version": 1,
        "passed": True,
        "cve": "CVE-2026-15806",
        "python_version": sys.version.split()[0],
        "source_sha256": source_sha256,
        "runtime_receipt_sha256": runtime["receipt_sha256"],
        "artifacts": records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = verify_binaries(args.executable)
        serialized = json.dumps(result, sort_keys=True, indent=2) + "\n"
        # Evidence is new per build; do not overwrite an older successful proof.
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(serialized)
        print(serialized, end="")
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        print(f"Frozen-Runtime-Prüfung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
