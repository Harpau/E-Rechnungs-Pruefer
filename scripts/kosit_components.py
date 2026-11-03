"""Shared helpers for the locked KoSIT and Factur-X validation components.

The module is intentionally dependency-free and only uses the standard library so
that both ``install_kosit.py`` and ``prepare_windows_components.py`` can load it
without the application package being installed.

Two component kinds exist in the lock files (``schema_version`` 2):

* ``artifact``: a single downloadable file with URL and SHA-256 (validator JAR,
  XRechnung configuration ZIP, Windows Java runtime);
* ``fileset``: several raw files of a Git repository bound to one commit, each
  with target path, size and SHA-256 (Factur-X EXTENDED validation artefacts).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

LOCK_SCHEMA_VERSION = 2
FACTURX_EXTENDED_DIRECTORY = "facturx-extended"
FACTURX_EXTENDED_REPORT_TARGET = PurePosixPath("resources/report/default-report.xsl")
FACTURX_EXTENDED_SCENARIOS_TARGET = PurePosixPath("scenarios.xml")
SOURCE_NOTE_NAME = "SOURCE.txt"
DOWNLOAD_TIMEOUT_SECONDS = 180

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_RAW_BASE_URL_RE = re.compile(r"^https://raw\.githubusercontent\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/([0-9a-f]{40})/$")
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}

Downloader = Callable[[str, Path], None]


class ComponentLockError(RuntimeError):
    """Raised when a lock file or a locked component cannot be used safely."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_hex_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _require_text(component: dict[str, Any], name: str, field_name: str) -> str:
    value = component.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ComponentLockError(f"Ungültiges Feld {name}.{field_name} in der Komponenten-Sperrdatei.")
    return value


def validate_artifact(name: str, component: dict[str, Any]) -> None:
    for field_name in ("version", "filename", "url", "sha256"):
        _require_text(component, name, field_name)
    if Path(component["filename"]).name != component["filename"]:
        raise ComponentLockError(f"Unsicherer Dateiname für {name} in der Komponenten-Sperrdatei.")
    if not component["url"].startswith("https://"):
        raise ComponentLockError(f"Unsichere Download-URL für {name} in der Komponenten-Sperrdatei.")
    if not is_hex_digest(component["sha256"]):
        raise ComponentLockError(f"Ungültige SHA-256-Prüfsumme für {name}.")


def _validate_relative_path(name: str, value: object, *, field_name: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ComponentLockError(f"Ungültiger Pfad in {name}.files[].{field_name}.")
    if "\\" in value or value.startswith("/") or value.endswith("/"):
        raise ComponentLockError(f"Unsicherer Pfad in {name}.files[].{field_name}: {value}")
    path = PurePosixPath(value)
    if path.is_absolute() or path.drive or path.anchor:
        raise ComponentLockError(f"Unsicherer Pfad in {name}.files[].{field_name}: {value}")
    for segment in path.parts:
        if segment in {"", ".", ".."} or not _SAFE_SEGMENT_RE.fullmatch(segment):
            raise ComponentLockError(f"Unsicherer Pfad in {name}.files[].{field_name}: {value}")
        stem = segment.split(".", 1)[0].lower()
        if stem in _WINDOWS_RESERVED_NAMES or re.fullmatch(r"[A-Za-z]:.*", segment):
            raise ComponentLockError(f"Unsicherer Pfad in {name}.files[].{field_name}: {value}")
    return path


def validate_fileset(name: str, component: dict[str, Any]) -> None:
    if component.get("kind") != "fileset":
        raise ComponentLockError(f"Komponente {name} muss die Art 'fileset' haben.")
    for field_name in ("version", "source", "tag", "commit", "base_url"):
        _require_text(component, name, field_name)
    if not component["source"].startswith("https://"):
        raise ComponentLockError(f"Unsichere Quelle für {name} in der Komponenten-Sperrdatei.")
    commit = component["commit"]
    if not _COMMIT_RE.fullmatch(commit):
        raise ComponentLockError(f"Komponente {name} muss an einen 40-stelligen Commit gebunden sein.")
    match = _RAW_BASE_URL_RE.fullmatch(component["base_url"])
    if match is None or match.group(1) != commit:
        raise ComponentLockError(
            f"Komponente {name} muss Rohdateien über HTTPS vom gebundenen Commit beziehen, nicht von Branches oder Tags."
        )
    files = component.get("files")
    if not isinstance(files, list) or not files:
        raise ComponentLockError(f"Komponente {name} enthält keine Dateiliste.")
    seen_targets: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ComponentLockError(f"Ungültiger Dateieintrag in {name}.")
        _validate_relative_path(name, entry.get("path"), field_name="path")
        target = _validate_relative_path(name, entry.get("target"), field_name="target")
        key = target.as_posix().casefold()
        if key in seen_targets:
            raise ComponentLockError(f"Doppeltes Ziel in {name}: {target.as_posix()}")
        seen_targets.add(key)
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ComponentLockError(f"Ungültige Größe für {target.as_posix()} in {name}.")
        if not is_hex_digest(entry.get("sha256")):
            raise ComponentLockError(f"Ungültige SHA-256-Prüfsumme für {target.as_posix()} in {name}.")


def load_lock(path: Path, *, required: tuple[str, ...]) -> dict[str, Any]:
    """Load a schema-2 lock file and validate every required component."""

    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ComponentLockError(f"Komponenten-Sperrdatei kann nicht gelesen werden: {exc}") from exc
    if not isinstance(payload, dict):
        raise ComponentLockError("Die Komponenten-Sperrdatei muss ein JSON-Objekt enthalten.")
    if payload.get("schema_version") != LOCK_SCHEMA_VERSION:
        raise ComponentLockError("Unbekannte Schema-Version der Komponenten-Sperrdatei.")
    components = payload.get("components")
    if not isinstance(components, dict):
        raise ComponentLockError("Die Komponenten-Sperrdatei enthält keine Komponenten.")
    for name in required:
        component = components.get(name)
        if not isinstance(component, dict):
            raise ComponentLockError(f"Komponente {name} fehlt in der Komponenten-Sperrdatei.")
        kind = component.get("kind", "artifact")
        if kind == "artifact":
            validate_artifact(name, component)
        elif kind == "fileset":
            validate_fileset(name, component)
        else:
            raise ComponentLockError(f"Unbekannte Komponentenart {kind!r} für {name}.")
    return payload


def download(url: str, target: Path, *, user_agent: str) -> None:
    """Download ``url`` to ``target`` via HTTPS without any redirect to other schemes."""

    if not url.startswith("https://"):
        raise ComponentLockError(f"Nur HTTPS-Downloads sind zulässig: {url}")
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response, target.open("wb") as output:
            shutil.copyfileobj(response, output)
    except (OSError, urllib.error.URLError) as exc:
        target.unlink(missing_ok=True)
        raise ComponentLockError(f"Download fehlgeschlagen: {url}: {exc}") from exc


def verify_file(path: Path, *, expected_sha256: str, expected_size: int | None = None) -> str:
    """Verify size and SHA-256 of ``path``; delete it and raise on mismatch."""

    try:
        actual_size = path.stat().st_size
    except OSError as exc:
        raise ComponentLockError(f"Datei fehlt nach dem Download: {path.name}") from exc
    if expected_size is not None and actual_size != expected_size:
        path.unlink(missing_ok=True)
        raise ComponentLockError(
            f"Größenprüfung für {path.name} fehlgeschlagen: erwartet {expected_size} Bytes, erhalten {actual_size}."
        )
    actual = sha256_file(path)
    if actual.lower() != expected_sha256.lower():
        path.unlink(missing_ok=True)
        raise ComponentLockError(
            f"SHA-256-Prüfung für {path.name} fehlgeschlagen: erwartet {expected_sha256}, erhalten {actual}."
        )
    return actual


def fetch_fileset(
    component: dict[str, Any],
    destination: Path,
    *,
    user_agent: str,
    cache_dir: Path | None = None,
    downloader: Downloader | None = None,
) -> list[dict[str, Any]]:
    """Download every file of a validated ``fileset`` into ``destination``.

    With ``cache_dir`` a verified copy is kept below the cache and reused when
    its hash still matches. Every file is verified again at its final place.
    """

    fetch: Downloader = downloader or (lambda url, target: download(url, target, user_agent=user_agent))
    base_url = component["base_url"]
    fetched: list[dict[str, Any]] = []
    for entry in component["files"]:
        target = destination / PurePosixPath(entry["target"])
        target.parent.mkdir(parents=True, exist_ok=True)
        url = base_url + entry["path"]
        if cache_dir is not None:
            cached = cache_dir / FACTURX_EXTENDED_DIRECTORY / PurePosixPath(entry["target"])
            if not (
                cached.is_file() and cached.stat().st_size == entry["size"] and sha256_file(cached) == entry["sha256"]
            ):
                cached.unlink(missing_ok=True)
                temporary = cached.with_name(cached.name + ".tmp")
                fetch(url, temporary)
                verify_file(temporary, expected_sha256=entry["sha256"], expected_size=entry["size"])
                temporary.replace(cached)
            shutil.copy2(cached, target)
        else:
            fetch(url, target)
        verify_file(target, expected_sha256=entry["sha256"], expected_size=entry["size"])
        fetched.append({"target": entry["target"], "size": entry["size"], "sha256": entry["sha256"]})
    return fetched


def write_source_note(destination: Path, component: dict[str, Any], files: list[dict[str, Any]]) -> Path:
    lines = [
        f"Komponente: {component['version']}",
        f"Quelle: {component['source']}",
        f"Tag: {component['tag']}",
        f"Commit: {component['commit']}",
        f"Basis-URL: {component['base_url']}",
        f"Bereitgestellt: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "SHA-256  Größe  Ziel",
    ]
    lines.extend(f"{item['sha256']}  {item['size']}  {item['target']}" for item in files)
    note = destination / SOURCE_NOTE_NAME
    note.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return note


def stage_facturx_extended(
    component: dict[str, Any],
    stage_root: Path,
    *,
    scenarios_source: Path,
    report_xsl_source: Path,
    user_agent: str,
    cache_dir: Path | None = None,
    downloader: Downloader | None = None,
) -> Path:
    """Build the complete ``facturx-extended`` repository below ``stage_root``."""

    validate_fileset("facturx_extended", component)
    destination = stage_root / FACTURX_EXTENDED_DIRECTORY
    if destination.exists():
        raise ComponentLockError(f"Staging-Verzeichnis existiert bereits: {destination}")
    destination.mkdir(parents=True)
    files = fetch_fileset(component, destination, user_agent=user_agent, cache_dir=cache_dir, downloader=downloader)
    for source, target in (
        (scenarios_source, destination / FACTURX_EXTENDED_SCENARIOS_TARGET),
        (report_xsl_source, destination / FACTURX_EXTENDED_REPORT_TARGET),
    ):
        if not source.is_file():
            raise ComponentLockError(f"Erforderliche Datei fehlt: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        files.append(
            {
                "target": target.relative_to(destination).as_posix(),
                "size": target.stat().st_size,
                "sha256": sha256_file(target),
            }
        )
    write_source_note(destination, component, files)
    return destination


@dataclass
class DirectoryReplacement:
    """Replace ``target`` by ``staged`` with a backup that allows a full rollback."""

    target: Path
    staged: Path
    backup: Path | None = field(default=None, init=False)
    applied: bool = field(default=False, init=False)

    def apply(self) -> None:
        if self.applied:
            raise ComponentLockError(f"Der Austausch von {self.target} wurde bereits durchgeführt.")
        self.target.parent.mkdir(parents=True, exist_ok=True)
        if self.target.exists():
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup = self.target.with_name(f"{self.target.name}.previous-{stamp}")
            counter = 0
            while backup.exists():
                counter += 1
                backup = self.target.with_name(f"{self.target.name}.previous-{stamp}-{counter}")
            self.target.rename(backup)
            self.backup = backup
        self.applied = True
        shutil.move(str(self.staged), str(self.target))

    def rollback(self) -> None:
        if not self.applied:
            return
        if self.target.exists():
            shutil.rmtree(self.target, ignore_errors=True)
        if self.backup is not None and self.backup.exists():
            self.backup.rename(self.target)
            self.backup = None
        self.applied = False

    def finish(self) -> None:
        if self.backup is not None and self.backup.exists():
            shutil.rmtree(self.backup)
        self.backup = None


__all__ = [
    "DOWNLOAD_TIMEOUT_SECONDS",
    "FACTURX_EXTENDED_DIRECTORY",
    "FACTURX_EXTENDED_REPORT_TARGET",
    "FACTURX_EXTENDED_SCENARIOS_TARGET",
    "LOCK_SCHEMA_VERSION",
    "SOURCE_NOTE_NAME",
    "ComponentLockError",
    "DirectoryReplacement",
    "Downloader",
    "download",
    "fetch_fileset",
    "is_hex_digest",
    "load_lock",
    "sha256_file",
    "stage_facturx_extended",
    "validate_artifact",
    "validate_fileset",
    "verify_file",
    "write_source_note",
]
