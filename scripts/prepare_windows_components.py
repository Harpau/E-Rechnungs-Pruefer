#!/usr/bin/env python3
"""Prepare locked Java and KoSIT components for the Windows package."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK_FILE = PROJECT_ROOT / "packaging" / "windows" / "components.lock.json"
DEFAULT_CACHE_DIR = PROJECT_ROOT / ".cache" / "windows-components"
USER_AGENT = f"e-rechnung-pruefer-windows-builder/{(PROJECT_ROOT / 'VERSION').read_text(encoding='utf-8').strip()}"


class ComponentError(RuntimeError):
    """Raised when a locked package component cannot be prepared safely."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_lock(path: Path) -> dict[str, dict[str, str]]:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ComponentError("Die Komponenten-Sperrdatei muss ein JSON-Objekt enthalten.")
        if payload.get("schema_version") != 1:
            raise ComponentError("Unbekannte Schema-Version der Komponenten-Sperrdatei.")
        components = payload["components"]
        for name in ("java", "validator", "xrechnung"):
            component = components[name]
            for field in ("version", "filename", "url", "sha256"):
                value = component[field]
                if not isinstance(value, str) or not value:
                    raise ComponentError(f"Ungültiges Feld {name}.{field} in der Komponenten-Sperrdatei.")
            digest = component["sha256"]
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest.lower()):
                raise ComponentError(f"Ungültige SHA-256-Prüfsumme für {name}.")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ComponentError(f"Komponenten-Sperrdatei kann nicht gelesen werden: {exc}") from exc
    return components


def _download_locked(component: dict[str, str], cache_dir: Path) -> Path:
    target = cache_dir / Path(component["filename"]).name
    expected = component["sha256"].lower()
    if target.is_file() and sha256_file(target).lower() == expected:
        return target
    target.unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    request = urllib.request.Request(component["url"], headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
    except (OSError, urllib.error.URLError) as exc:
        temporary.unlink(missing_ok=True)
        raise ComponentError(f"Download von {component['filename']} fehlgeschlagen: {exc}") from exc

    actual = sha256_file(temporary).lower()
    if actual != expected:
        temporary.unlink(missing_ok=True)
        raise ComponentError(
            f"SHA-256-Prüfung für {component['filename']} fehlgeschlagen: erwartet {expected}, erhalten {actual}."
        )
    temporary.replace(target)
    return target


def _safe_extract(archive_path: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    target_resolved = target.resolve()
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ComponentError(f"ZIP-Artefakt kann nicht geöffnet werden: {archive_path.name}: {exc}") from exc
    with archive:
        for member in archive.infolist():
            destination = (target / member.filename).resolve()
            if destination != target_resolved and target_resolved not in destination.parents:
                raise ComponentError(f"Unsicherer Pfad in {archive_path.name}: {member.filename}")
        archive.extractall(target)


def _find_java_root(extraction_root: Path) -> Path:
    candidates = list(extraction_root.rglob("bin/java.exe"))
    if len(candidates) != 1:
        raise ComponentError("Das Java-Archiv enthält keine eindeutige bin/java.exe.")
    return candidates[0].parent.parent


def _prepare_tree(components: dict[str, dict[str, str]], cache_dir: Path, stage_root: Path) -> None:
    java_archive = _download_locked(components["java"], cache_dir)
    validator_jar = _download_locked(components["validator"], cache_dir)
    xrechnung_archive = _download_locked(components["xrechnung"], cache_dir)

    java_extraction = stage_root / "java-extracted"
    _safe_extract(java_archive, java_extraction)
    java_root = _find_java_root(java_extraction)
    (stage_root / "runtime").mkdir()
    shutil.move(str(java_root), str(stage_root / "runtime" / "java"))

    validator_target = stage_root / "vendor" / "kosit" / "validator" / validator_jar.name
    validator_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(validator_jar, validator_target)
    _safe_extract(xrechnung_archive, stage_root / "vendor" / "kosit" / "xrechnung")


def prepare(lock_file: Path, cache_dir: Path) -> None:
    components = _load_lock(lock_file)
    for name in ("java", "validator", "xrechnung"):
        print(f"{name}: {components[name]['version']}")

    with tempfile.TemporaryDirectory(prefix="einvoice-windows-components-") as temporary:
        stage_root = Path(temporary) / "stage"
        stage_root.mkdir()
        _prepare_tree(components, cache_dir, stage_root)

        java_target = PROJECT_ROOT / "runtime" / "java"
        kosit_target = PROJECT_ROOT / "vendor" / "kosit"
        java_target.parent.mkdir(parents=True, exist_ok=True)
        kosit_target.parent.mkdir(parents=True, exist_ok=True)
        if java_target.exists():
            shutil.rmtree(java_target)
        if kosit_target.exists():
            shutil.rmtree(kosit_target)
        shutil.move(str(stage_root / "runtime" / "java"), str(java_target))
        shutil.move(str(stage_root / "vendor" / "kosit"), str(kosit_target))

    print(f"Java vorbereitet: {java_target}")
    print(f"KoSIT vorbereitet: {kosit_target}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    args = parser.parse_args()
    try:
        prepare(args.lock_file, args.cache_dir)
        return 0
    except ComponentError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
