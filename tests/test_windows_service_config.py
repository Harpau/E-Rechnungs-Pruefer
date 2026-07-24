from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import windows_service_config
from app.desktop_security import API_TOKEN_ENV
from app.windows_service_config import (
    BASE_TOKEN_PRINCIPALS,
    CONFIG_FILE_NAME,
    DEFAULT_SERVICE_PORT,
    RUNTIME_DIRECTORY_NAME,
    SERVICE_ACCOUNT,
    SERVICE_DATA_DIRECTORY_NAME,
    SERVICE_MODE_ENV,
    SERVICE_NAME,
    SERVICE_SID,
    SERVICE_SID_ACCOUNT,
    TOKEN_FILE_NAME,
    ServiceConfiguration,
    ServicePaths,
    TokenStore,
    activate_service_environment,
    load_or_create_configuration,
    service_shutdown_timeout,
    validate_machine_path,
)


def test_service_paths_use_machine_data_directory() -> None:
    paths = ServicePaths.from_environment({"PROGRAMDATA": r"C:\ProgramData"})

    assert paths.data_directory == Path(r"C:\ProgramData") / SERVICE_DATA_DIRECTORY_NAME
    assert paths.configuration == paths.data_directory / CONFIG_FILE_NAME
    assert paths.token == paths.data_directory / TOKEN_FILE_NAME
    assert paths.log.parent.parent == paths.data_directory
    assert paths.runtime_directory == paths.data_directory / RUNTIME_DIRECTORY_NAME


def test_windows_service_paths_ignore_environment_override_and_use_known_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted = Path(r"C:\TrustedProgramData")
    monkeypatch.setattr(windows_service_config.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", r"C:\AttackerControlled")
    monkeypatch.setattr(windows_service_config, "_windows_program_data_directory", lambda: trusted)

    assert ServicePaths.from_environment().data_directory == trusted / SERVICE_DATA_DIRECTORY_NAME


def test_service_identity_is_local_service_with_service_specific_acl() -> None:
    assert SERVICE_NAME == "ERechnungsPrueferService"
    assert SERVICE_ACCOUNT == r"NT AUTHORITY\LocalService"
    assert SERVICE_SID_ACCOUNT == rf"NT SERVICE\{SERVICE_NAME}"
    assert SERVICE_SID == "S-1-5-80-3900036394-2548317589-2626916927-3141249857-921448814"
    assert set(BASE_TOKEN_PRINCIPALS) == {"SYSTEM", "BUILTIN\\Administrators", SERVICE_SID_ACCOUNT}
    assert not {"Everyone", "BUILTIN\\Users", "Authenticated Users"} & set(BASE_TOKEN_PRINCIPALS)


def test_machine_configuration_is_created_atomically_and_strictly_validated(tmp_path: Path) -> None:
    path = tmp_path / CONFIG_FILE_NAME
    protected: list[Path] = []

    configuration = load_or_create_configuration(path, protect=lambda candidate: protected.append(candidate))

    assert configuration == ServiceConfiguration()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "kosit_enabled": True,
        "kosit_timeout_seconds": 60,
        "port": DEFAULT_SERVICE_PORT,
        "schema_version": 1,
    }
    assert protected and protected[-1].name.endswith(".tmp")
    assert not list(tmp_path.glob("*.tmp"))

    path.write_text('{"schema_version":1,"port":8080,"host":"0.0.0.0"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="Unbekannte Konfigurationsfelder"):
        load_or_create_configuration(path)


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "port": 8080, "kosit_enabled": True, "kosit_timeout_seconds": 60},
        {"schema_version": 1, "port": 0, "kosit_enabled": True, "kosit_timeout_seconds": 60},
        {"schema_version": 1, "port": 8080, "kosit_enabled": True, "kosit_timeout_seconds": 301},
    ],
)
def test_machine_configuration_rejects_unsafe_values(tmp_path: Path, payload: dict[str, object]) -> None:
    path = tmp_path / CONFIG_FILE_NAME
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError):
        load_or_create_configuration(path)


def test_service_environment_is_activated_before_app_import() -> None:
    environment = {
        "EINVOICE_DESKTOP_TOKEN": "darf-nicht-vererbt-werden",
        "HOST": "0.0.0.0",
    }
    configuration = ServiceConfiguration(port=18080, kosit_enabled=False, kosit_timeout_seconds=45)
    token = "t" * 43

    activate_service_environment(configuration, token, environ=environment)

    assert environment[SERVICE_MODE_ENV] == "1"
    assert environment["HOST"] == "127.0.0.1"
    assert environment["PORT"] == "18080"
    assert environment["EINVOICE_DESKTOP_PORT"] == "18080"
    assert environment[API_TOKEN_ENV] == token
    assert environment["KOSIT_ENABLED"] == "false"
    assert environment["KOSIT_TIMEOUT_SECONDS"] == "45"
    assert "EINVOICE_DESKTOP_TOKEN" not in environment
    assert service_shutdown_timeout(configuration) == 60.0


def test_token_store_creates_persists_and_rotates_with_acl_before_replace(tmp_path: Path) -> None:
    path = tmp_path / TOKEN_FILE_NAME
    generated = iter(("a" * 43, "b" * 43))
    protected_directories: list[Path] = []
    protected_files: list[Path] = []

    def protect_file(candidate: Path) -> None:
        assert candidate.exists()
        assert candidate != path
        protected_files.append(candidate)

    store = TokenStore(
        path,
        token_factory=lambda: next(generated),
        protect_directory=lambda candidate: protected_directories.append(candidate),
        protect_file=protect_file,
    )

    first = store.load_or_create()
    persisted = store.load_or_create()
    rotated = store.rotate()

    assert first == persisted == "a" * 43
    assert rotated == "b" * 43
    assert path.read_text(encoding="ascii") == rotated + "\n"
    assert protected_directories == [tmp_path, tmp_path]
    assert len(protected_files) == 2
    assert not list(tmp_path.glob("*.tmp"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_token_migration_requires_explicit_consent_and_preserves_target_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "service" / TOKEN_FILE_NAME
    target.parent.mkdir()
    target.write_text("a" * 43 + "\n", encoding="ascii")
    store = TokenStore(target)

    with pytest.raises(RuntimeError, match="ausdrückliche Zustimmung"):
        store.import_value("m" * 43, consent=False)
    assert target.read_text(encoding="ascii") == "a" * 43 + "\n"

    assert store.import_value("m" * 43, consent=True) == "m" * 43
    assert target.read_text(encoding="ascii") == "m" * 43 + "\n"


def test_windows_machine_path_rejects_reparse_parent_before_file_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redirected_directory = tmp_path / "redirected"
    redirected_directory.mkdir()
    target = redirected_directory / CONFIG_FILE_NAME
    target.write_text("{}", encoding="utf-8")
    real_lstat = os.lstat

    def fake_lstat(candidate: os.PathLike[str]) -> os.stat_result | SimpleNamespace:
        if Path(candidate) == redirected_directory:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700,
                st_file_attributes=windows_service_config._WINDOWS_REPARSE_POINT_ATTRIBUTE,
                st_nlink=1,
            )
        return real_lstat(candidate)

    monkeypatch.setattr(windows_service_config.sys, "platform", "win32")
    monkeypatch.setattr(windows_service_config.os, "lstat", fake_lstat)

    with pytest.raises(RuntimeError, match="Reparse-Point oder Junction"):
        validate_machine_path(target, directory=False)


def test_windows_machine_file_rejects_unexpected_hardlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = tmp_path / TOKEN_FILE_NAME
    token.write_text("t" * 43 + "\n", encoding="ascii")
    os.link(token, tmp_path / "token-alias.txt")
    monkeypatch.setattr(windows_service_config.sys, "platform", "win32")

    with pytest.raises(RuntimeError, match="Hardlink-Anzahl"):
        TokenStore(token).load()


def test_windows_atomic_token_write_rechecks_target_path_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = tmp_path / TOKEN_FILE_NAME
    token.write_text("a" * 43 + "\n", encoding="ascii")
    os.link(token, tmp_path / "token-alias.txt")
    monkeypatch.setattr(windows_service_config.sys, "platform", "win32")

    def protect(_path: Path) -> None:
        pass

    store = TokenStore(
        token,
        token_factory=lambda: "b" * 43,
        protect_directory=protect,
        protect_file=protect,
    )

    with pytest.raises(RuntimeError, match="Hardlink-Anzahl"):
        store.rotate()

    assert token.read_text(encoding="ascii") == "a" * 43 + "\n"


def test_windows_atomic_publish_uses_replace_existing_and_write_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = tmp_path / ".api-token.txt.0123456789abcdef.tmp"
    destination = tmp_path / TOKEN_FILE_NAME
    move = Mock(return_value=1)
    kernel32 = SimpleNamespace(MoveFileExW=move)
    monkeypatch.setattr(windows_service_config.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_service_config.ctypes,
        "WinDLL",
        Mock(return_value=kernel32),
        raising=False,
    )

    windows_service_config._replace_file_durable(temporary, destination)

    move.assert_called_once_with(
        str(temporary),
        str(destination),
        windows_service_config._MOVEFILE_REPLACE_EXISTING | windows_service_config._MOVEFILE_WRITE_THROUGH,
    )
