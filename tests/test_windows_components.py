from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "prepare_windows_components.py"
SPEC = importlib.util.spec_from_file_location("prepare_windows_components", SCRIPT_PATH)
assert SPEC and SPEC.loader
components = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(components)


def test_repository_component_lock_is_valid() -> None:
    locked = components._load_lock(PROJECT_ROOT / "packaging/windows/components.lock.json")

    assert locked["validator"]["version"] == "KoSIT Validator 1.6.2"
    assert locked["xrechnung"]["version"].endswith("2026-01-31")
    assert locked["java"]["filename"].endswith("windows_hotspot_21.0.11_10.zip")


def test_load_lock_rejects_unknown_schema_and_invalid_digest(tmp_path: Path) -> None:
    path = tmp_path / "lock.json"
    path.write_text(json.dumps({"schema_version": 2, "components": {}}), encoding="utf-8")
    with pytest.raises(components.ComponentError, match="Schema-Version"):
        components._load_lock(path)

    component = {"version": "1", "filename": "a", "url": "https://example.test/a", "sha256": "falsch"}
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "components": {"java": component, "validator": component, "xrechnung": component},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(components.ComponentError, match="Prüfsumme"):
        components._load_lock(path)


def test_safe_extract_rejects_parent_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../ausbruch.txt", "nicht entpacken")

    with pytest.raises(components.ComponentError, match="Unsicherer Pfad"):
        components._safe_extract(archive, tmp_path / "target")

    assert not (tmp_path / "ausbruch.txt").exists()


def test_find_java_root_requires_one_windows_java_executable(tmp_path: Path) -> None:
    root = tmp_path / "jdk-21-jre"
    java = root / "bin/java.exe"
    java.parent.mkdir(parents=True)
    java.write_bytes(b"test")

    assert components._find_java_root(tmp_path) == root

    second = tmp_path / "anderes/bin/java.exe"
    second.parent.mkdir(parents=True)
    second.write_bytes(b"test")
    with pytest.raises(components.ComponentError, match="eindeutige"):
        components._find_java_root(tmp_path)
