#!/usr/bin/env python3
"""Download the official KoSIT standalone validator and XRechnung configuration.

Nothing is downloaded at application runtime. This explicit setup script uses
GitHub's release API, verifies the downloaded artefacts when a SHA-256 digest is
published, installs them below ``vendor/kosit`` and writes ``.env.kosit``.
"""

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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = PROJECT_ROOT / "vendor" / "kosit"
USER_AGENT = "e-rechnung-pruefer-kosit-installer/1.3.0"


class InstallError(RuntimeError):
    """Raised when an official KoSIT component cannot be installed safely."""


def api_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise InstallError(f"GitHub-API konnte nicht erreicht oder ausgewertet werden: {exc}") from exc


def latest_release(repo: str) -> dict:
    data = api_json(f"https://api.github.com/repos/{repo}/releases/latest")
    if not data.get("assets"):
        raise InstallError(f"Die aktuelle Veröffentlichung von {repo} enthält keine Download-Artefakte.")
    return data


def choose_validator_asset(release: dict) -> dict:
    """Select the executable standalone JAR, never the library JAR or ZIP."""
    candidates = []
    for asset in release.get("assets", []):
        name = str(asset.get("name", "")).lower()
        if (
            name.endswith("-standalone.jar")
            and "validator" in name
            and not any(token in name for token in ("sources", "javadoc", "tests"))
        ):
            candidates.append(asset)
    if not candidates:
        raise InstallError(
            f"Kein ausführbares '*-standalone.jar' in Validator-Release {release.get('tag_name')} gefunden."
        )
    return max(candidates, key=lambda asset: int(asset.get("size", 0)))


def choose_configuration_asset(release: dict) -> dict:
    assets = [
        asset
        for asset in release.get("assets", [])
        if str(asset.get("name", "")).lower().endswith(".zip") and "source" not in str(asset.get("name", "")).lower()
    ]
    if not assets:
        raise InstallError(f"Kein ZIP-Artefakt in XRechnung-Release {release.get('tag_name')} gefunden.")

    def score(asset: dict) -> tuple[int, int]:
        name = str(asset.get("name", "")).lower()
        points = 0
        if "validator-configuration-xrechnung" in name:
            points += 60
        if "xrechnung" in name:
            points += 25
        if "configuration" in name:
            points += 10
        return points, int(asset.get("size", 0))

    return max(assets, key=score)


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=180) as response, target.open("wb") as output:
            shutil.copyfileobj(response, output)
    except urllib.error.URLError as exc:
        raise InstallError(f"Download fehlgeschlagen: {exc}") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_published_digest(path: Path, asset: dict) -> str:
    actual = sha256_file(path)
    published = str(asset.get("digest") or "").strip().lower()
    if published:
        algorithm, separator, expected = published.partition(":")
        if separator and algorithm == "sha256" and expected:
            if actual.lower() != expected.lower():
                raise InstallError(
                    f"SHA-256-Prüfung für {path.name} fehlgeschlagen: erwartet {expected}, erhalten {actual}."
                )
    return actual


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


def write_env(jar: Path, scenarios: Path, repository: Path) -> Path:
    env_path = PROJECT_ROOT / ".env.kosit"
    env_path.write_text(
        "\n".join(
            [
                "# Automatisch erzeugt durch scripts/install_kosit.py",
                "KOSIT_ENABLED=true",
                f"KOSIT_VALIDATOR_JAR={_portable_path(jar)}",
                f"KOSIT_SCENARIOS={_portable_path(scenarios)}",
                f"KOSIT_REPOSITORIES={_portable_path(repository)}",
                "KOSIT_TIMEOUT_SECONDS=60",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return env_path


def install(force: bool) -> None:
    if VENDOR_ROOT.exists() and not force:
        raise InstallError(
            f"{VENDOR_ROOT} existiert bereits. Mit --force kann die Installation sicher aktualisiert werden."
        )

    print("Ermittle aktuelle KoSIT-Veröffentlichungen …")
    validator_release = latest_release("itplr-kosit/validator")
    config_release = latest_release("itplr-kosit/validator-configuration-xrechnung")
    validator_asset = choose_validator_asset(validator_release)
    config_asset = choose_configuration_asset(config_release)

    print(f"Validator:   {validator_release.get('tag_name')} – {validator_asset.get('name')}")
    print(f"XRechnung:   {config_release.get('tag_name')} – {config_asset.get('name')}")

    with tempfile.TemporaryDirectory(prefix="kosit-download-") as temp:
        temp_path = Path(temp)
        stage_root = temp_path / "kosit"
        validator_dir = stage_root / "validator"
        config_dir = stage_root / "xrechnung"
        validator_jar = validator_dir / Path(str(validator_asset["name"])).name
        config_zip = temp_path / "xrechnung.zip"

        print("Lade ausführbares Validator-Standalone-JAR herunter …")
        download(validator_asset["browser_download_url"], validator_jar)
        validator_sha256 = verify_published_digest(validator_jar, validator_asset)
        main_class = require_executable_jar(validator_jar)

        print("Lade XRechnung-Konfiguration herunter …")
        download(config_asset["browser_download_url"], config_zip)
        config_sha256 = verify_published_digest(config_zip, config_asset)
        print("Entpacke XRechnung-Konfiguration …")
        safe_extract(config_zip, config_dir)

        staged_jar = find_validator_jar(validator_dir)
        staged_scenarios = find_scenarios(config_dir)
        staged_repository = configuration_root(staged_scenarios, config_dir)
        jar_relative = staged_jar.relative_to(stage_root)
        scenarios_relative = staged_scenarios.relative_to(stage_root)
        repository_relative = staged_repository.relative_to(stage_root)

        VENDOR_ROOT.parent.mkdir(parents=True, exist_ok=True)
        if VENDOR_ROOT.exists():
            shutil.rmtree(VENDOR_ROOT)
        shutil.move(str(stage_root), str(VENDOR_ROOT))

    jar = VENDOR_ROOT / jar_relative
    scenarios = VENDOR_ROOT / scenarios_relative
    repository = VENDOR_ROOT / repository_relative
    env_path = write_env(jar, scenarios, repository)

    print("\nKoSIT wurde eingerichtet.")
    print(f"JAR:          {jar}")
    print(f"Main-Class:   {main_class}")
    print(f"JAR SHA-256:  {validator_sha256}")
    print(f"Szenarien:    {scenarios}")
    print(f"Ressourcen:   {repository}")
    print(f"Config SHA-256: {config_sha256}")
    print(f"Konfiguration: {env_path}")
    if shutil.which("java") is None:
        print("\nHinweis: Java ist noch nicht im PATH. Benötigt wird eine unterstützte Java-Laufzeit.")
    print("\nAnwendung neu starten; danach ist die KoSIT-Prüfung automatisch aktiv.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Vorhandene KoSIT-Dateien ersetzen")
    args = parser.parse_args()
    try:
        install(args.force)
        return 0
    except InstallError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
