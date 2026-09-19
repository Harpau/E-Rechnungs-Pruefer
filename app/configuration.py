"""Environment-free configuration values and the explicit worker snapshot contract."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Settings:
    max_upload_bytes: int = 25 * 1024 * 1024
    max_technical_rows: int = 100_000
    max_xml_structure_items: int = 100_000
    max_technical_seconds: float = 5.0
    kosit_enabled: bool = True
    kosit_java_bin: str = "java"
    kosit_validator_jar: Path | None = None
    kosit_scenarios: tuple[Path, ...] = ()
    kosit_repositories: tuple[Path, ...] = ()
    kosit_timeout_seconds: int = 60
    host: str = "127.0.0.1"
    port: int = 8080


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError(f"Ungültiger Konfigurationswert: {name}.")
    return value


def _path(value: Any, name: str) -> Path:
    path = Path(_string(value, name))
    if not path.is_absolute():
        raise ValueError(f"Konfigurationspfad muss absolut sein: {name}.")
    return path


def settings_from_snapshot(payload: Mapping[str, Any]) -> Settings:
    """Validate an already decoded snapshot without discovery, file reads or environment access."""
    expected = {field.name for field in fields(Settings)} | {"schema_version"}
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise ValueError("Unvollständiger oder unbekannter Konfigurationssnapshot.")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("Unbekannte Konfigurationssnapshot-Version.")
    values = dict(payload)
    del values["schema_version"]
    for name in ("max_upload_bytes", "max_technical_rows", "max_xml_structure_items", "kosit_timeout_seconds", "port"):
        value = values[name]
        if type(value) is not int or not 1 <= value <= 2**31 - 1:
            raise ValueError(f"Ungültige positive ganze Zahl: {name}.")
    if values["kosit_timeout_seconds"] > 300 or values["port"] > 65535:
        raise ValueError("Ungültiger KoSIT-Timeout oder Port.")
    seconds = values["max_technical_seconds"]
    if type(seconds) not in (int, float) or not 0 < seconds <= 3600 or not math.isfinite(seconds):
        raise ValueError("Ungültige technische Verarbeitungsfrist.")
    values["max_technical_seconds"] = float(seconds)
    if type(values["kosit_enabled"]) is not bool:
        raise ValueError("Ungültiger KoSIT-Aktivierungswert.")
    for name in ("kosit_java_bin", "host"):
        values[name] = _string(values[name], name)
    if values["kosit_validator_jar"] is not None:
        values["kosit_validator_jar"] = _path(values["kosit_validator_jar"], "kosit_validator_jar")
    for name in ("kosit_scenarios", "kosit_repositories"):
        entries = values[name]
        if not isinstance(entries, list) or len(entries) > 32:
            raise ValueError(f"Ungültige Konfigurationspfadliste: {name}.")
        values[name] = tuple(_path(entry, name) for entry in entries)
    return Settings(**values)


def settings_to_snapshot(settings: Settings) -> dict[str, Any]:
    """Return only the fixed non-secret setting fields; reject invalid values before IPC."""
    result: dict[str, Any] = {"schema_version": 1}
    for field in fields(Settings):
        value = getattr(settings, field.name)
        if isinstance(value, Path):
            value = str(value)
        elif isinstance(value, tuple):
            value = [str(path) for path in value]
        result[field.name] = value
    settings_from_snapshot(result)
    return result
