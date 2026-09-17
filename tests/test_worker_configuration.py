from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app.configuration import Settings, settings_from_snapshot, settings_to_snapshot


def test_configuration_import_does_not_import_loader_or_read_environment(tmp_path):
    script = """
import sys
from app.configuration import Settings
assert 'app.settings' not in sys.modules
assert Settings().max_upload_bytes == 25 * 1024 * 1024
assert Settings().kosit_java_bin == 'java'
"""
    environment = dict(os.environ, MAX_UPLOAD_BYTES="not-an-integer", KOSIT_JAVA_BIN="private-token-sentinel")
    subprocess.run([sys.executable, "-c", script], env=environment, check=True, timeout=10)


def test_snapshot_round_trip_preserves_paths_and_dataclass_replace(tmp_path):
    expected = replace(
        Settings(), kosit_validator_jar=tmp_path / "validator.jar", kosit_scenarios=(tmp_path / "scenarios.xml",)
    )
    snapshot = settings_to_snapshot(expected)
    assert settings_from_snapshot(json.loads(json.dumps(snapshot))) == expected
    assert isinstance(settings_from_snapshot(snapshot).kosit_validator_jar, Path)
    assert "token" not in snapshot


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("token", "secret"),
        ("max_upload_bytes", True),
        ("max_upload_bytes", 0),
        ("max_technical_seconds", float("nan")),
        ("max_technical_seconds", float("inf")),
        ("max_technical_seconds", 10**1000),
        ("kosit_enabled", 1),
        ("kosit_timeout_seconds", 301),
        ("kosit_java_bin", "java\x00bad"),
        ("kosit_validator_jar", "relative.jar"),
        ("kosit_scenarios", "not-a-list"),
        ("port", False),
        ("port", 65536),
        ("schema_version", 2),
    ],
)
def test_snapshot_rejects_untrusted_types_paths_and_unknown_keys(key, value):
    snapshot = settings_to_snapshot(Settings())
    snapshot[key] = value
    with pytest.raises(ValueError):
        settings_from_snapshot(snapshot)


def test_snapshot_rejects_missing_keys():
    snapshot = settings_to_snapshot(Settings())
    del snapshot["host"]
    with pytest.raises(ValueError):
        settings_from_snapshot(snapshot)


def test_explicit_settings_loader_keeps_environment_and_discovery(monkeypatch, tmp_path):
    from app import settings as loader

    monkeypatch.setattr(loader, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "12345")
    monkeypatch.setenv("KOSIT_JAVA_BIN", "configured-java")
    (tmp_path / ".env").write_text("HOST=192.0.2.1\n", encoding="utf-8")
    monkeypatch.delenv("HOST", raising=False)
    assert loader.load_settings(load_env_files=False).host == "127.0.0.1"
    loaded = loader.load_settings()
    assert loaded.host == "192.0.2.1"
    assert loaded.max_upload_bytes == 12345
    assert loaded.kosit_java_bin == "configured-java"
