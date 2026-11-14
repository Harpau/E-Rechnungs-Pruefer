#!/usr/bin/env python3
"""Install the exactly pinned KoSIT validator, XRechnung configuration and Factur-X EXTENDED artefacts.

Nothing is downloaded at application runtime. This explicit setup script reads
versions, download URLs and mandatory SHA-256 digests from the repository lock,
verifies every artefact in a staging directory, replaces ``vendor/kosit`` only
after everything was verified, keeps the previous installation when the
replacement fails, and writes ``.env.kosit`` atomically.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import kosit_components as components  # noqa: E402

PROJECT_ROOT = SCRIPT_DIR.parent
VENDOR_ROOT = PROJECT_ROOT / "vendor" / "kosit"
DEFAULT_LOCK_FILE = PROJECT_ROOT / "packaging" / "kosit" / "components.lock.json"
EXTENDED_SCENARIOS_SOURCE = PROJECT_ROOT / "packaging" / "kosit" / "facturx-extended" / "scenarios.xml"
REQUIRED_COMPONENTS = ("validator", "xrechnung", "facturx_extended")
REQUIRED_STANDARDS = (
    "xrechnung",
    "xrechnung_configuration",
    "cen_en16931",
    "xrechnung_schematron",
    "facturx_extended",
    "facturx_extended_source",
)
USER_AGENT = "e-rechnung-pruefer-kosit-installer/2.2.1"


class InstallError(RuntimeError):
    """Raised when an official KoSIT component cannot be installed safely."""


def load_lock(path: Path) -> dict[str, Any]:
    try:
        payload = components.load_lock(path, required=REQUIRED_COMPONENTS)
        standards = payload["standards"]
        for field in REQUIRED_STANDARDS:
            value = standards[field]
            if not isinstance(value, str) or not value:
                raise InstallError(f"Ungültiges Feld standards.{field} in der KoSIT-Sperrdatei.")
    except components.ComponentLockError as exc:
        raise InstallError(f"KoSIT-Sperrdatei kann nicht gelesen werden: {exc}") from exc
    except (KeyError, TypeError) as exc:
        raise InstallError(f"KoSIT-Sperrdatei kann nicht gelesen werden: {exc}") from exc
    return payload


def download(url: str, target: Path) -> None:
    try:
        components.download(url, target, user_agent=USER_AGENT)
    except components.ComponentLockError as exc:
        raise InstallError(str(exc)) from exc


def sha256_file(path: Path) -> str:
    return components.sha256_file(path)


def verify_locked_digest(path: Path, expected: str) -> str:
    try:
        return components.verify_file(path, expected_sha256=expected)
    except components.ComponentLockError as exc:
        raise InstallError(str(exc)) from exc


def manifest_attributes(jar_path: Path) -> dict[str, str]:
    """Read and unfold attributes from META-INF/MANIFEST.MF."""
    try:
        with zipfile.ZipFile(jar_path) as archive:
            raw = archive.read("META-INF/MANIFEST.MF")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise InstallError(f"{jar_path.name} ist kein lesbares JAR mit Manifest: {exc}") from exc

    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    unfolded: list[str] = []
    for line in text.split("\n"):
        if line.startswith(" ") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)

    attributes: dict[str, str] = {}
    for line in unfolded:
        key, separator, value = line.partition(":")
        if separator:
            attributes[key.strip().lower()] = value.strip()
    return attributes


def require_executable_jar(jar_path: Path) -> str:
    main_class = manifest_attributes(jar_path).get("main-class")
    if not main_class:
        raise InstallError(
            f"{jar_path.name} enthält kein Main-Class-Manifestattribut. "
            "Benötigt wird das offizielle '*-standalone.jar', nicht das Bibliotheks-JAR."
        )
    return main_class


def find_validator_jar(root: Path) -> Path:
    candidates = [
        path
        for path in root.rglob("*-standalone.jar")
        if not any(token in path.name.lower() for token in ("sources", "javadoc", "tests"))
    ]
    executable: list[Path] = []
    for candidate in candidates:
        try:
            require_executable_jar(candidate)
        except InstallError:
            continue
        executable.append(candidate)
    if not executable:
        raise InstallError("Es wurde kein ausführbares KoSIT-Standalone-JAR gefunden.")
    return max(executable, key=lambda path: path.stat().st_size)


def safe_extract(zip_path: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    target_resolved = target.resolve()
    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise InstallError(f"ZIP-Artefakt kann nicht geöffnet werden: {exc}") from exc
    with archive:
        for member in archive.infolist():
            destination = (target / member.filename).resolve()
            if target_resolved not in destination.parents and destination != target_resolved:
                raise InstallError(f"Unsicherer Pfad im ZIP-Archiv: {member.filename}")
        archive.extractall(target)


def find_scenarios(root: Path) -> Path:
    candidates = [path for path in root.rglob("scenarios.xml") if "src" not in path.parts]
    if not candidates:
        candidates = list(root.rglob("scenarios.xml"))
    if not candidates:
        raise InstallError("In der XRechnung-Konfiguration wurde keine scenarios.xml gefunden.")
    candidates.sort(key=lambda path: (0 if (path.parent / "resources").is_dir() else 1, len(path.parts)))
    return candidates[0]


def configuration_root(scenarios: Path, extraction_root: Path) -> Path:
    current = scenarios.parent
    extraction_root = extraction_root.resolve()
    while True:
        if (current / "resources").is_dir():
            return current
        if current.resolve() == extraction_root or current.parent == current:
            return scenarios.parent
        current = current.parent


def _portable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def write_env(jar: Path, scenarios: Path, repository: Path, extended_repository: Path | None = None) -> Path:
    """Write ``.env.kosit`` atomically so a failure never leaves a truncated file."""

    env_path = PROJECT_ROOT / ".env.kosit"
    lines = [
        "# Automatisch erzeugt durch scripts/install_kosit.py",
        "KOSIT_ENABLED=true",
        f"KOSIT_VALIDATOR_JAR={_portable_path(jar)}",
        f"KOSIT_SCENARIOS={_portable_path(scenarios)}",
        f"KOSIT_REPOSITORIES={_portable_path(repository)}",
    ]
    if extended_repository is not None:
        lines.extend(
            [
                f"KOSIT_FACTURX_EXTENDED_SCENARIOS={_portable_path(extended_repository / 'scenarios.xml')}",
                f"KOSIT_FACTURX_EXTENDED_REPOSITORIES={_portable_path(extended_repository)}",
            ]
        )
    lines.extend(["KOSIT_TIMEOUT_SECONDS=60", ""])
    temporary = env_path.with_name(env_path.name + ".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temporary, env_path)
    return env_path


def install(force: bool, *, lock_file: Path = DEFAULT_LOCK_FILE) -> None:
    if VENDOR_ROOT.exists() and not force:
        raise InstallError(
            f"{VENDOR_ROOT} existiert bereits. Mit --force kann die Installation sicher aktualisiert werden."
        )

    locked = load_lock(lock_file)
    validator_component = locked["components"]["validator"]
    config_component = locked["components"]["xrechnung"]
    extended_component = locked["components"]["facturx_extended"]
    standards = locked["standards"]

    print(f"Validator:   {validator_component['version']} – {validator_component['filename']}")
    print(f"XRechnung:   {config_component['version']} – {config_component['filename']}")
    print(f"CEN-Regeln:  {standards['cen_en16931']}")
    print(f"Factur-X:    {extended_component['version']} – Commit {extended_component['commit'][:7]}")

    with tempfile.TemporaryDirectory(prefix="kosit-download-") as temp:
        temp_path = Path(temp)
        stage_root = temp_path / "kosit"
        validator_dir = stage_root / "validator"
        config_dir = stage_root / "xrechnung"
        validator_jar = validator_dir / validator_component["filename"]
        config_zip = temp_path / config_component["filename"]

        print("Lade ausführbares Validator-Standalone-JAR herunter …")
        download(validator_component["url"], validator_jar)
        validator_sha256 = verify_locked_digest(validator_jar, validator_component["sha256"])
        main_class = require_executable_jar(validator_jar)

        print("Lade XRechnung-Konfiguration herunter …")
        download(config_component["url"], config_zip)
        config_sha256 = verify_locked_digest(config_zip, config_component["sha256"])
        print("Entpacke XRechnung-Konfiguration …")
        safe_extract(config_zip, config_dir)

        staged_jar = find_validator_jar(validator_dir)
        staged_scenarios = find_scenarios(config_dir)
        staged_repository = configuration_root(staged_scenarios, config_dir)

        print("Lade Factur-X-EXTENDED-Artefakte herunter …")
        try:
            staged_extended = components.stage_facturx_extended(
                extended_component,
                stage_root,
                scenarios_source=EXTENDED_SCENARIOS_SOURCE,
                report_xsl_source=staged_repository / "resources" / "default-report.xsl",
                user_agent=USER_AGENT,
                downloader=download,
            )
        except components.ComponentLockError as exc:
            raise InstallError(str(exc)) from exc

        jar_relative = staged_jar.relative_to(stage_root)
        scenarios_relative = staged_scenarios.relative_to(stage_root)
        repository_relative = staged_repository.relative_to(stage_root)
        extended_relative = staged_extended.relative_to(stage_root)

        # Everything is verified. Only now is the existing installation touched,
        # and every failure from here on restores it together with .env.kosit.
        replacement = components.DirectoryReplacement(VENDOR_ROOT, stage_root)
        try:
            replacement.apply()
            env_path = write_env(
                VENDOR_ROOT / jar_relative,
                VENDOR_ROOT / scenarios_relative,
                VENDOR_ROOT / repository_relative,
                VENDOR_ROOT / extended_relative,
            )
        except Exception as exc:
            replacement.rollback()
            raise InstallError(
                f"Der Austausch wurde zurückgenommen; die bisherige Installation und .env.kosit bleiben erhalten: {exc}"
            ) from exc
        replacement.finish()

    jar = VENDOR_ROOT / jar_relative
    scenarios = VENDOR_ROOT / scenarios_relative
    repository = VENDOR_ROOT / repository_relative
    extended = VENDOR_ROOT / extended_relative

    print("\nKoSIT wurde eingerichtet.")
    print(f"JAR:          {jar}")
    print(f"Main-Class:   {main_class}")
    print(f"JAR SHA-256:  {validator_sha256}")
    print(f"Szenarien:    {scenarios}")
    print(f"Ressourcen:   {repository}")
    print(f"Config SHA-256: {config_sha256}")
    print(f"Factur-X EXTENDED: {extended} ({extended_component['tag']}, {extended_component['commit'][:7]})")
    print(f"Konfiguration: {env_path}")
    if shutil.which("java") is None:
        print("\nHinweis: Java ist noch nicht im PATH. Benötigt wird eine unterstützte Java-Laufzeit.")
    print("\nAnwendung neu starten; danach ist die KoSIT-Prüfung automatisch aktiv.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Vorhandene KoSIT-Dateien ersetzen")
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)
    args = parser.parse_args()
    try:
        install(args.force, lock_file=args.lock_file)
        return 0
    except (InstallError, components.ComponentLockError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
