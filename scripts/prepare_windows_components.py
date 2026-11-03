#!/usr/bin/env python3
"""Prepare locked Java, KoSIT and Factur-X EXTENDED components for the Windows package.

All components are downloaded into a cache, verified, assembled in a staging tree
and only then moved to ``runtime/java`` and ``vendor/kosit``. A failure during the
replacement restores both previous trees.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import kosit_components as shared  # noqa: E402

PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_LOCK_FILE = PROJECT_ROOT / "packaging" / "windows" / "components.lock.json"
DEFAULT_CACHE_DIR = PROJECT_ROOT / ".cache" / "windows-components"
EXTENDED_SCENARIOS_SOURCE = PROJECT_ROOT / "packaging" / "kosit" / "facturx-extended" / "scenarios.xml"
REQUIRED_COMPONENTS = ("java", "validator", "xrechnung", "facturx_extended")
USER_AGENT = f"e-rechnung-pruefer-windows-builder/{(PROJECT_ROOT / 'VERSION').read_text(encoding='utf-8').strip()}"


class ComponentError(RuntimeError):
    """Raised when a locked package component cannot be prepared safely."""


def sha256_file(path: Path) -> str:
    return shared.sha256_file(path)


def _load_lock(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = shared.load_lock(path, required=REQUIRED_COMPONENTS)
    except shared.ComponentLockError as exc:
        raise ComponentError(str(exc)) from exc
    components: dict[str, dict[str, Any]] = payload["components"]
    return components


def _download_file(url: str, target: Path) -> None:
    try:
        shared.download(url, target, user_agent=USER_AGENT)
    except shared.ComponentLockError as exc:
        raise ComponentError(str(exc)) from exc


def _download_locked(component: dict[str, Any], cache_dir: Path) -> Path:
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


def _find_default_report(configuration_root: Path) -> Path:
    candidates = [path for path in configuration_root.rglob("default-report.xsl") if "src" not in path.parts]
    if len(candidates) != 1:
        raise ComponentError("Die XRechnung-Konfiguration enthält keine eindeutige resources/default-report.xsl.")
    return candidates[0]


def _prepare_tree(components: dict[str, dict[str, Any]], cache_dir: Path, stage_root: Path) -> None:
    java_archive = _download_locked(components["java"], cache_dir)
    validator_jar = _download_locked(components["validator"], cache_dir)
    xrechnung_archive = _download_locked(components["xrechnung"], cache_dir)

    java_extraction = stage_root / "java-extracted"
    _safe_extract(java_archive, java_extraction)
    java_root = _find_java_root(java_extraction)
    (stage_root / "runtime").mkdir()
    shutil.move(str(java_root), str(stage_root / "runtime" / "java"))

    kosit_root = stage_root / "vendor" / "kosit"
    validator_target = kosit_root / "validator" / validator_jar.name
    validator_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(validator_jar, validator_target)
    _safe_extract(xrechnung_archive, kosit_root / "xrechnung")

    try:
        shared.stage_facturx_extended(
            components["facturx_extended"],
            kosit_root,
            scenarios_source=EXTENDED_SCENARIOS_SOURCE,
            report_xsl_source=_find_default_report(kosit_root / "xrechnung"),
            user_agent=USER_AGENT,
            cache_dir=cache_dir,
            downloader=_download_file,
        )
    except shared.ComponentLockError as exc:
        raise ComponentError(str(exc)) from exc


def prepare(lock_file: Path, cache_dir: Path) -> None:
    components = _load_lock(lock_file)
    for name in REQUIRED_COMPONENTS:
        print(f"{name}: {components[name]['version']}")

    java_target = PROJECT_ROOT / "runtime" / "java"
    kosit_target = PROJECT_ROOT / "vendor" / "kosit"
    with tempfile.TemporaryDirectory(prefix="einvoice-windows-components-") as temporary:
        stage_root = Path(temporary) / "stage"
        stage_root.mkdir()
        _prepare_tree(components, cache_dir, stage_root)

        # Both trees are complete and verified. Replace them together and restore
        # both when either replacement fails.
        java_replacement = shared.DirectoryReplacement(java_target, stage_root / "runtime" / "java")
        kosit_replacement = shared.DirectoryReplacement(kosit_target, stage_root / "vendor" / "kosit")
        try:
            java_replacement.apply()
            kosit_replacement.apply()
        except Exception as exc:
            kosit_replacement.rollback()
            java_replacement.rollback()
            raise ComponentError(
                f"Der Austausch wurde zurückgenommen; bisherige Java- und KoSIT-Bäume bleiben erhalten: {exc}"
            ) from exc
        kosit_replacement.finish()
        java_replacement.finish()

    print(f"Java vorbereitet: {java_target}")
    print(f"KoSIT vorbereitet: {kosit_target}")
    print(f"Factur-X EXTENDED vorbereitet: {kosit_target / shared.FACTURX_EXTENDED_DIRECTORY}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    args = parser.parse_args()
    try:
        prepare(args.lock_file, args.cache_dir)
        return 0
    except (ComponentError, shared.ComponentLockError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
