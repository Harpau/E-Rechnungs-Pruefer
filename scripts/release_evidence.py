#!/usr/bin/env python3
"""Create and verify a canonical, fail-closed release-evidence inventory."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypedDict, cast

FORMAT_VERSION = "e-rechnungs-pruefer-release-evidence-inventory-v1"
HASH_ALGORITHM = "sha256"
BUFFER_SIZE = 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class InventoryError(ValueError):
    """Raised when evidence cannot be inventoried or verified safely."""


class InventoryEntry(TypedDict):
    path: str
    type: Literal["directory", "file"]
    mode: str
    size: int
    sha256: str | None


class InventoryDocument(TypedDict):
    format: str
    hash_algorithm: str
    root: str
    entries: list[InventoryEntry]


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _mode(value: int) -> str:
    return f"{stat.S_IMODE(value):04o}"


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns)


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise InventoryError(f"Metadaten können nicht sicher gelesen werden: {path}: {exc}") from exc


def _file_digest(path: Path, before: os.stat_result) -> str:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InventoryError(f"Datei kann nicht sicher gelesen werden: {path}: {exc}") from exc

    digest = sha256()
    try:
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
                raise InventoryError(f"Datei hat sich vor dem Hashen geändert: {path}")
            for chunk in iter(lambda: handle.read(BUFFER_SIZE), b""):
                digest.update(chunk)
            after_read = os.fstat(handle.fileno())
    except Exception:
        # os.fdopen owns the descriptor after it succeeds. It does not own it if
        # construction itself fails, so close it here when necessary.
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise

    after_path = _lstat(path)
    if _identity(after_read) != _identity(opened) or _identity(after_path) != _identity(opened):
        raise InventoryError(f"Datei hat sich während des Hashens geändert: {path}")
    return digest.hexdigest()


def _relative_path(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    return relative.as_posix() if relative.parts else "."


def _entry(path: Path, root: Path, metadata: os.stat_result) -> InventoryEntry:
    relative = _relative_path(path, root)
    if stat.S_ISDIR(metadata.st_mode):
        return {"path": relative, "type": "directory", "mode": _mode(metadata.st_mode), "size": 0, "sha256": None}
    if stat.S_ISREG(metadata.st_mode):
        return {
            "path": relative,
            "type": "file",
            "mode": _mode(metadata.st_mode),
            "size": metadata.st_size,
            "sha256": _file_digest(path, metadata),
        }
    if stat.S_ISLNK(metadata.st_mode):
        kind = "symbolischer Link"
    else:
        kind = "Sonderdatei"
    raise InventoryError(f"Nicht unterstützter Dateityp ({kind}): {relative}")


def _scan_directory(
    path: Path,
    root: Path,
    excluded: frozenset[Path],
    entries: list[InventoryEntry],
) -> None:
    before = _lstat(path)
    if not stat.S_ISDIR(before.st_mode):
        entries.append(_entry(path, root, before))
        return

    entries.append(_entry(path, root, before))
    try:
        children = sorted(path.iterdir(), key=lambda child: child.name)
    except OSError as exc:
        raise InventoryError(f"Verzeichnis kann nicht sicher gelesen werden: {path}: {exc}") from exc

    for child in children:
        if child in excluded:
            continue
        metadata = _lstat(child)
        if stat.S_ISDIR(metadata.st_mode):
            _scan_directory(child, root, excluded, entries)
        else:
            entries.append(_entry(child, root, metadata))

    after = _lstat(path)
    if _identity(after) != _identity(before):
        raise InventoryError(f"Verzeichnis hat sich während der Inventarisierung geändert: {path}")


def _excluded_paths(root: Path, paths: Iterable[Path]) -> frozenset[Path]:
    excluded: set[Path] = set()
    for path in paths:
        absolute = _absolute(path)
        try:
            relative = absolute.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            raise InventoryError("Der Evidence-Root selbst kann kein Ausgabepfad sein.")
        excluded.add(root / relative)
    return frozenset(excluded)


def _validated_root(root: Path) -> Path:
    absolute_root = _absolute(root)
    root_metadata = _lstat(absolute_root)
    if stat.S_ISLNK(root_metadata.st_mode):
        raise InventoryError(f"Der Evidence-Root darf kein symbolischer Link sein: {absolute_root}")
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise InventoryError(f"Der Evidence-Root ist kein Verzeichnis: {absolute_root}")
    return absolute_root


def build_inventory(root: Path, *, excluded_paths: Iterable[Path] = ()) -> InventoryDocument:
    """Build an in-memory inventory without following links or accepting special files."""
    absolute_root = _validated_root(root)

    entries: list[InventoryEntry] = []
    excluded = _excluded_paths(absolute_root, excluded_paths)
    _scan_directory(absolute_root, absolute_root, excluded, entries)
    entries.sort(key=lambda item: item["path"])
    return {
        "format": FORMAT_VERSION,
        "hash_algorithm": HASH_ALGORITHM,
        "root": ".",
        "entries": entries,
    }


def canonical_inventory_bytes(document: InventoryDocument) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _assert_safe_output(path: Path, *, overwrite: bool) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise InventoryError(f"Ausgabepfad kann nicht geprüft werden: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InventoryError(f"Ausgabepfad ist keine reguläre Datei: {path}")
    if not overwrite:
        raise InventoryError(f"Ausgabedatei existiert bereits (verwende --force): {path}")


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def create_inventory(
    root: Path,
    inventory_path: Path,
    checksum_path: Path,
    *,
    overwrite: bool = False,
) -> InventoryDocument:
    """Create a canonical inventory and a detached checksum file atomically per file."""
    inventory = _absolute(inventory_path)
    checksum = _absolute(checksum_path)
    if inventory == checksum:
        raise InventoryError("Inventar und Prüfsumme müssen unterschiedliche Ausgabepfade verwenden.")

    _validated_root(root)
    inventory.parent.mkdir(parents=True, exist_ok=True)
    checksum.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_output(inventory, overwrite=overwrite)
    _assert_safe_output(checksum, overwrite=overwrite)

    document = build_inventory(root, excluded_paths=(inventory, checksum))
    content = canonical_inventory_bytes(document)
    checksum_content = f"{sha256(content).hexdigest()}  {inventory.name}\n".encode()
    _atomic_write(inventory, content)
    _atomic_write(checksum, checksum_content)
    return document


def _read_regular_bytes(path: Path) -> bytes:
    metadata = _lstat(path)
    if not stat.S_ISREG(metadata.st_mode):
        raise InventoryError(f"Erwartete reguläre Datei fehlt oder ist unsicher: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InventoryError(f"Datei kann nicht gelesen werden: {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(metadata):
                raise InventoryError(f"Datei hat sich vor dem Lesen geändert: {path}")
            content = handle.read()
            after_read = os.fstat(handle.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    after_path = _lstat(path)
    if _identity(after_read) != _identity(opened) or _identity(after_path) != _identity(opened):
        raise InventoryError(f"Datei hat sich während des Lesens geändert: {path}")
    return content


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InventoryError(f"Doppelter JSON-Schlüssel im Inventar: {key!r}")
        result[key] = value
    return result


def _parse_entry(value: object, index: int) -> InventoryEntry:
    if not isinstance(value, dict) or set(value) != {"path", "type", "mode", "size", "sha256"}:
        raise InventoryError(f"Ungültiger Inventareintrag an Position {index}.")
    path = value["path"]
    entry_type = value["type"]
    mode = value["mode"]
    size = value["size"]
    digest = value["sha256"]
    if not isinstance(path, str) or not path:
        raise InventoryError(f"Ungültiger relativer Pfad an Position {index}.")
    pure_path = PurePosixPath(path)
    if path != "." and (pure_path.is_absolute() or ".." in pure_path.parts or pure_path.as_posix() != path):
        raise InventoryError(f"Ungültiger relativer Pfad an Position {index}.")
    if entry_type not in {"directory", "file"}:
        raise InventoryError(f"Ungültiger Dateityp für {path!r}.")
    if not isinstance(mode, str) or re.fullmatch(r"[0-7]{4}", mode) is None:
        raise InventoryError(f"Ungültiger Dateimodus für {path!r}.")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise InventoryError(f"Ungültige Dateigröße für {path!r}.")
    if entry_type == "directory":
        if size != 0 or digest is not None:
            raise InventoryError(f"Verzeichniseintrag enthält Dateidaten: {path!r}.")
    elif not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
        raise InventoryError(f"Ungültiger SHA-256-Wert für {path!r}.")
    return cast(InventoryEntry, value)


def _parse_inventory(content: bytes) -> InventoryDocument:
    try:
        decoded = content.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InventoryError(f"Inventar ist kein gültiges UTF-8-JSON: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {"format", "hash_algorithm", "root", "entries"}:
        raise InventoryError("Inventar besitzt nicht das erwartete Schema.")
    if value["format"] != FORMAT_VERSION or value["hash_algorithm"] != HASH_ALGORITHM or value["root"] != ".":
        raise InventoryError("Inventarformat, Hashalgorithmus oder Rootbindung ist ungültig.")
    raw_entries = value["entries"]
    if not isinstance(raw_entries, list):
        raise InventoryError("Inventareinträge müssen eine Liste sein.")
    entries = [_parse_entry(entry, index) for index, entry in enumerate(raw_entries)]
    paths = [entry["path"] for entry in entries]
    if paths != sorted(paths) or len(paths) != len(set(paths)) or not paths or paths[0] != ".":
        raise InventoryError("Inventarpfade sind nicht eindeutig und kanonisch sortiert.")
    document: InventoryDocument = {
        "format": FORMAT_VERSION,
        "hash_algorithm": HASH_ALGORITHM,
        "root": ".",
        "entries": entries,
    }
    if canonical_inventory_bytes(document) != content:
        raise InventoryError("Inventar ist nicht kanonisch serialisiert.")
    return document


def _verify_detached_checksum(inventory: Path, checksum: Path, inventory_content: bytes) -> None:
    checksum_content = _read_regular_bytes(checksum)
    expected = f"{sha256(inventory_content).hexdigest()}  {inventory.name}\n".encode()
    if checksum_content != expected:
        raise InventoryError("Detached SHA-256-Datei stimmt nicht mit dem Inventar überein.")


def verify_inventory(root: Path, inventory_path: Path, checksum_path: Path) -> list[str]:
    """Return state differences after validating inventory format and detached checksum."""
    inventory = _absolute(inventory_path)
    checksum = _absolute(checksum_path)
    if inventory == checksum:
        raise InventoryError("Inventar und Prüfsumme müssen unterschiedliche Pfade verwenden.")
    inventory_content = _read_regular_bytes(inventory)
    _verify_detached_checksum(inventory, checksum, inventory_content)
    expected_document = _parse_inventory(inventory_content)
    actual_document = build_inventory(root, excluded_paths=(inventory, checksum))

    expected = {entry["path"]: entry for entry in expected_document["entries"]}
    actual = {entry["path"]: entry for entry in actual_document["entries"]}
    differences: list[str] = []
    for path in sorted(expected.keys() - actual.keys()):
        differences.append(f"Fehlt: {path}")
    for path in sorted(actual.keys() - expected.keys()):
        differences.append(f"Unerwartet: {path}")
    for path in sorted(expected.keys() & actual.keys()):
        expected_entry = expected[path]
        actual_entry = actual[path]
        changed: list[str] = []
        if expected_entry["type"] != actual_entry["type"]:
            changed.append("type")
        if expected_entry["mode"] != actual_entry["mode"]:
            changed.append("mode")
        if expected_entry["size"] != actual_entry["size"]:
            changed.append("size")
        if expected_entry["sha256"] != actual_entry["sha256"]:
            changed.append("sha256")
        if changed:
            differences.append(f"Geändert: {path} ({', '.join(changed)})")
    return differences


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("root", type=Path, help="Zu inventarisierender Evidence-Root")
        subparser.add_argument("--inventory", type=Path, required=True, help="Pfad des kanonischen JSON-Inventars")
        subparser.add_argument("--checksum", type=Path, required=True, help="Pfad der detached SHA-256-Datei")
        if command == "create":
            subparser.add_argument("--force", action="store_true", help="Vorhandene reguläre Ausgabedateien ersetzen")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            document = create_inventory(args.root, args.inventory, args.checksum, overwrite=args.force)
            print(f"Evidence-Inventar erzeugt: {args.inventory} ({len(document['entries'])} Einträge)")
            print(f"Detached SHA-256 erzeugt: {args.checksum}")
            return 0
        differences = verify_inventory(args.root, args.inventory, args.checksum)
    except (InventoryError, OSError) as exc:
        print(f"Evidence-Prüfung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    if differences:
        print("Evidence-Prüfung fehlgeschlagen:", file=sys.stderr)
        for difference in differences:
            print(f"- {difference}", file=sys.stderr)
        return 1
    print("Evidence-Inventar ist vollständig und unverändert.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
