from __future__ import annotations

from pathlib import Path

import pytest

from app import settings as settings_module


def test_load_env_file_ignores_noise_unquotes_values_and_preserves_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
        # Kommentar

        KEINE_ZUWEISUNG
        =wird-ignoriert
        TEST_ENV_EXISTING=aus-datei
        TEST_ENV_DOUBLE = "doppelt gequotet"
        TEST_ENV_SINGLE = 'einfach gequotet'
        TEST_ENV_EMPTY =
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_ENV_EXISTING", "aus-umgebung")
    for name in ("TEST_ENV_DOUBLE", "TEST_ENV_SINGLE", "TEST_ENV_EMPTY"):
        monkeypatch.delenv(name, raising=False)

    settings_module._load_env_file(env_file)

    assert settings_module.os.environ["TEST_ENV_EXISTING"] == "aus-umgebung"
    assert settings_module.os.environ["TEST_ENV_DOUBLE"] == "doppelt gequotet"
    assert settings_module.os.environ["TEST_ENV_SINGLE"] == "einfach gequotet"
    assert settings_module.os.environ["TEST_ENV_EMPTY"] == ""
    assert "" not in settings_module.os.environ


def test_load_env_file_ignores_missing_file(tmp_path: Path) -> None:
    settings_module._load_env_file(tmp_path / "nicht-vorhanden.env")


@pytest.mark.parametrize("value", ["1", " true ", "YES", "On"])
def test_bool_env_accepts_true_values(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_BOOL_SETTING", value)

    assert settings_module._bool_env("TEST_BOOL_SETTING", False) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "beliebig", ""])
def test_bool_env_treats_other_values_as_false(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_BOOL_SETTING", value)

    assert settings_module._bool_env("TEST_BOOL_SETTING", True) is False


@pytest.mark.parametrize("default", [True, False])
def test_bool_env_uses_default_when_variable_is_missing(
    default: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_BOOL_SETTING", raising=False)

    assert settings_module._bool_env("TEST_BOOL_SETTING", default) is default


def test_resolve_and_split_paths_use_project_root_and_semicolon_separator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "projekt"
    absolute_path = tmp_path / "absolut" / "datei.xml"
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", project_root)

    assert settings_module._resolve_path(" config/datei.xml ") == project_root / "config/datei.xml"
    assert settings_module._resolve_path(f" {absolute_path} ") == absolute_path
    assert settings_module._split_paths(None) == ()
    assert settings_module._split_paths("") == ()
    assert settings_module._split_paths(f" eins ;; {absolute_path};zwei ") == (
        project_root / "eins",
        absolute_path,
        project_root / "zwei",
    )


def test_discover_validator_jar_returns_none_without_vendor_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", tmp_path)

    assert settings_module._discover_validator_jar() is None


def test_discover_java_bin_prefers_bundled_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = "java.exe" if settings_module.os.name == "nt" else "java"
    java = tmp_path / "runtime/java/bin" / executable
    java.parent.mkdir(parents=True)
    java.write_bytes(b"")
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", tmp_path)

    assert settings_module._discover_java_bin() == str(java)


def test_discover_java_bin_falls_back_to_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", tmp_path)

    assert settings_module._discover_java_bin() == "java"


def test_discovery_returns_empty_when_vendor_directories_contain_no_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "vendor/kosit/validator").mkdir(parents=True)
    (tmp_path / "vendor/kosit/xrechnung").mkdir(parents=True)
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", tmp_path)

    assert settings_module._discover_validator_jar() is None
    assert settings_module._discover_scenarios() == ()


def test_discover_validator_jar_selects_largest_executable_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator_dir = tmp_path / "vendor/kosit/validator/releases"
    validator_dir.mkdir(parents=True)
    smaller = validator_dir / "validator-2.0-standalone.jar"
    larger = validator_dir / "validator-1.0-standalone.jar"
    ignored = validator_dir / "validator-3.0-sources-standalone.jar"
    library = validator_dir / "validator-4.0.jar"
    smaller.write_bytes(b"klein")
    larger.write_bytes(b"deutlich-groesser")
    ignored.write_bytes(b"x" * 100)
    library.write_bytes(b"x" * 200)
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", tmp_path)

    assert settings_module._discover_validator_jar() == larger


def test_discover_scenarios_returns_empty_without_vendor_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", tmp_path)

    assert settings_module._discover_scenarios() == ()


def test_discover_scenarios_prefers_distribution_with_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "vendor/kosit/xrechnung"
    shorter = base / "kurz/scenarios.xml"
    preferred = base / "distribution/config/scenarios.xml"
    shorter.parent.mkdir(parents=True)
    preferred.parent.mkdir(parents=True)
    (preferred.parent / "resources").mkdir()
    shorter.write_text("kurz", encoding="utf-8")
    preferred.write_text("bevorzugt", encoding="utf-8")
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", tmp_path)

    assert settings_module._discover_scenarios() == (preferred,)


def test_discover_scenarios_uses_src_only_as_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = tmp_path / "vendor/kosit/xrechnung/src/test/scenarios.xml"
    scenario.parent.mkdir(parents=True)
    scenario.write_text("fallback", encoding="utf-8")
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", tmp_path)

    assert settings_module._discover_scenarios() == (scenario,)


def test_discover_repositories_prefers_resources_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vendor/kosit/xrechnung/distribution"
    scenario = root / "config/scenarios.xml"
    scenario.parent.mkdir(parents=True)
    (root / "resources").mkdir()
    scenario.write_text("test", encoding="utf-8")
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", tmp_path)

    assert settings_module._discover_repositories((scenario,)) == (root,)


def test_discover_repositories_falls_back_to_scenario_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = tmp_path / "vendor/kosit/xrechnung/scenarios.xml"
    scenario.parent.mkdir(parents=True)
    scenario.write_text("test", encoding="utf-8")
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", tmp_path)

    assert settings_module._discover_repositories((scenario,)) == (scenario.parent,)
