from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import windows_install_conflicts as conflicts


class _ContextKey:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


@contextmanager
def _hive(value):
    yield value


def _patch_clean_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conflicts.sys, "platform", "win32")
    monkeypatch.setattr(conflicts, "_running_desktop_processes", lambda: ())
    monkeypatch.setattr(conflicts, "_desktop_mutex_exists", lambda: False)


def test_conflict_check_is_windows_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conflicts.sys, "platform", "darwin")
    with pytest.raises(OSError, match="ausschließlich unter Windows"):
        conflicts.assert_no_desktop_installation()


@pytest.mark.parametrize("source", ["process", "mutex"])
def test_conflict_check_rejects_running_desktop_before_profile_inventory(
    source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conflicts.sys, "platform", "win32")
    monkeypatch.setattr(
        conflicts,
        "_running_desktop_processes",
        lambda: (1234,) if source == "process" else (),
    )
    monkeypatch.setattr(conflicts, "_desktop_mutex_exists", lambda: source == "mutex")

    def profile_reader():
        raise AssertionError("profiles must not be read")

    monkeypatch.setattr(conflicts, "_profile_paths", profile_reader)
    with pytest.raises(RuntimeError, match="laufende Desktop-Version"):
        conflicts.assert_no_desktop_installation()


def test_conflict_check_rejects_default_install_directory_even_without_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_clean_runtime(monkeypatch)
    profile = tmp_path / "User"
    install = profile / "AppData" / "Local" / "Programs" / conflicts.DESKTOP_INSTALL_DIRECTORY_NAME
    install.mkdir(parents=True)
    (install / "unins000.exe").write_text("partial", encoding="utf-8")
    monkeypatch.setattr(conflicts, "_profile_paths", lambda: (("S-1-5-21-1", profile),))

    with pytest.raises(RuntimeError, match="Teilinstallation"):
        conflicts.assert_no_desktop_installation()


@pytest.mark.parametrize(("registered", "autostart"), [(True, False), (False, True)])
def test_conflict_check_rejects_registry_or_autostart_in_any_profile(
    registered: bool,
    autostart: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_clean_runtime(monkeypatch)
    profile = tmp_path / "User"
    monkeypatch.setattr(conflicts, "_profile_paths", lambda: (("S-1-5-21-1", profile),))
    monkeypatch.setattr(conflicts, "_safe_directory_exists", lambda _path: False)
    monkeypatch.setattr(conflicts, "_profile_hive", lambda _sid, _path: _hive(object()))
    monkeypatch.setattr(conflicts, "_registered_desktop_present", lambda _hive: registered)
    monkeypatch.setattr(conflicts, "_desktop_autostart_present", lambda _hive: autostart)

    with pytest.raises(RuntimeError, match="Desktop-Version oder deren Autostart"):
        conflicts.assert_no_desktop_installation()


def test_conflict_check_accepts_profiles_without_desktop_footprints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_clean_runtime(monkeypatch)
    profile = tmp_path / "User"
    monkeypatch.setattr(conflicts, "_profile_paths", lambda: (("S-1-5-21-1", profile),))
    monkeypatch.setattr(conflicts, "_safe_directory_exists", lambda _path: False)
    monkeypatch.setattr(conflicts, "_profile_hive", lambda _sid, _path: _hive(object()))
    monkeypatch.setattr(conflicts, "_registered_desktop_present", lambda _hive: False)
    monkeypatch.setattr(conflicts, "_desktop_autostart_present", lambda _hive: False)
    conflicts.assert_no_desktop_installation()


def test_no_follow_path_checks_empty_product_directory_as_present(tmp_path: Path) -> None:
    product = tmp_path / conflicts.DESKTOP_INSTALL_DIRECTORY_NAME
    assert conflicts._safe_directory_exists(product) is False
    product.mkdir()
    assert conflicts._safe_directory_exists(product) is True


def test_profile_paths_accept_only_canonical_fixed_drive_locations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive_type = Mock(return_value=conflicts.DRIVE_FIXED)
    monkeypatch.setattr(conflicts, "_native_drive_type", drive_type)
    assert conflicts._validated_local_fixed_path(r"C:\Users\Test") == Path(r"C:\Users\Test")
    drive_type.assert_called_once_with("C:\\")

    for unsafe in (
        r"\\server\share\User",
        r"\\?\C:\Users\Test",
        r"C:relative\path",
        r"C:\Users\Test\..\Admin",
        r"C:\Users\Test\file:stream",
        r"C:\Users\CON",
        "C:\\Users\\Test ",
    ):
        with pytest.raises(RuntimeError):
            conflicts._validated_local_fixed_path(unsafe)

    drive_type.return_value = 4
    with pytest.raises(RuntimeError, match="festen lokalen Laufwerk"):
        conflicts._validated_local_fixed_path(r"D:\Users\Test")


def test_no_follow_path_check_rejects_redirected_product_directory(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    redirected = tmp_path / conflicts.DESKTOP_INSTALL_DIRECTORY_NAME
    try:
        redirected.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks sind in dieser Testumgebung nicht verfügbar.")
    with pytest.raises(RuntimeError, match="Reparse-Point oder Junction"):
        conflicts._safe_directory_exists(redirected)


def _no_more_items() -> OSError:
    error = OSError("done")
    error.winerror = conflicts.ERROR_NO_MORE_ITEMS  # type: ignore[attr-defined]
    return error


def test_profile_list_includes_local_and_entra_users_but_filters_system_sids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hklm = object()
    profile_list = _ContextKey()
    keys = {
        "S-1-5-18": _ContextKey(),
        "S-1-5-21-1-2-3-1001": _ContextKey(),
        "S-1-12-1-111-222-333-444": _ContextKey(),
    }
    paths = {
        keys["S-1-5-21-1-2-3-1001"]: (r"C:\Users\Local", 2),
        keys["S-1-12-1-111-222-333-444"]: (r"D:\Users\Entra", 2),
    }

    def open_key(root, subkey, _reserved=0, _access=0):
        if root is hklm and subkey == conflicts.PROFILE_LIST_KEY:
            return profile_list
        if root is profile_list:
            return keys[subkey]
        raise AssertionError((root, subkey))

    fake_winreg = SimpleNamespace(
        HKEY_LOCAL_MACHINE=hklm,
        KEY_QUERY_VALUE=1,
        KEY_ENUMERATE_SUB_KEYS=8,
        REG_SZ=1,
        REG_EXPAND_SZ=2,
        OpenKey=Mock(side_effect=open_key),
        EnumKey=Mock(side_effect=[*keys, _no_more_items()]),
        QueryValueEx=Mock(side_effect=lambda key, _name: paths[key]),
    )
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake_winreg)
    monkeypatch.setattr(conflicts, "_validated_local_fixed_path", lambda value: Path(value))

    assert conflicts._profile_paths() == (
        ("S-1-5-21-1-2-3-1001", Path(r"C:\Users\Local")),
        ("S-1-12-1-111-222-333-444", Path(r"D:\Users\Entra")),
    )
    assert fake_winreg.QueryValueEx.call_count == 2


@pytest.mark.parametrize(
    ("enum_error", "profile_value", "message"),
    [
        (OSError("enumeration failed"), None, "nicht vollständig inventarisiert"),
        (None, ("", 1), "ungültigen Profilpfad"),
        (None, (r"\\server\profile", 99), "ungültigen Profilpfad"),
    ],
)
def test_profile_list_fails_closed_for_enumeration_and_invalid_profiles(
    enum_error: OSError | None,
    profile_value: tuple[str, int] | None,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hklm = object()
    profile_list = _ContextKey()
    profile = _ContextKey()
    enum_side_effect = [enum_error] if enum_error is not None else ["S-1-5-21-1", _no_more_items()]
    fake_winreg = SimpleNamespace(
        HKEY_LOCAL_MACHINE=hklm,
        KEY_QUERY_VALUE=1,
        KEY_ENUMERATE_SUB_KEYS=8,
        REG_SZ=1,
        REG_EXPAND_SZ=2,
        OpenKey=Mock(side_effect=lambda root, subkey, *_args: profile_list if root is hklm else profile),
        EnumKey=Mock(side_effect=enum_side_effect),
        QueryValueEx=Mock(return_value=profile_value),
    )
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake_winreg)
    with pytest.raises(RuntimeError, match=message):
        conflicts._profile_paths()


def test_profile_hive_prefers_loaded_hive_and_uses_offline_reader_only_when_needed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hku = object()
    loaded = _ContextKey()
    offline = object()
    fake_winreg = SimpleNamespace(
        HKEY_USERS=hku,
        KEY_QUERY_VALUE=1,
        KEY_ENUMERATE_SUB_KEYS=8,
        OpenKey=Mock(return_value=loaded),
    )
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake_winreg)
    offline_reader = Mock(side_effect=lambda _path: _hive(offline))
    monkeypatch.setattr(conflicts, "_offline_profile_hive", offline_reader)

    with conflicts._profile_hive("S-1-5-21-1", tmp_path) as result:
        assert result is loaded
    offline_reader.assert_not_called()

    fake_winreg.OpenKey.side_effect = FileNotFoundError
    (tmp_path / "NTUSER.DAT").write_bytes(b"hive")
    with conflicts._profile_hive("S-1-5-21-1", tmp_path) as result:
        assert result is offline
    offline_reader.assert_called_once_with(tmp_path / "NTUSER.DAT")


def test_offline_profile_hive_uses_read_only_application_handle_and_closes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hive_path = tmp_path / "NTUSER.DAT"
    handle = Mock()

    def load_app_key(path, output, access, options, reserved):
        assert path == str(hive_path)
        assert access == 0x20019
        assert options == reserved == 0
        output._obj.value = 73  # noqa: SLF001 - ctypes output adapter
        return 0

    loader = Mock(side_effect=load_app_key)
    advapi = SimpleNamespace(RegLoadAppKeyW=loader)
    fake_winreg = SimpleNamespace(
        KEY_READ=0x20019,
        HKEYType=Mock(return_value=handle),
    )
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake_winreg)
    monkeypatch.setattr(conflicts, "_safe_regular_file_exists", lambda _path: True)
    monkeypatch.setattr(
        conflicts,
        "ctypes_windows",
        SimpleNamespace(WinDLL=Mock(return_value=advapi)),
    )

    with conflicts._offline_profile_hive(hive_path) as opened:
        assert opened is handle
    fake_winreg.HKEYType.assert_called_once_with(73)
    handle.Close.assert_called_once_with()

    loader.side_effect = lambda *_args: 5
    with pytest.raises(OSError, match="read-only geöffnet"):
        with conflicts._offline_profile_hive(hive_path):
            pass


@pytest.mark.parametrize("name", ["NTUSER.DAT", "NTUSER.MAN"])
def test_offline_profile_selects_exactly_one_no_follow_hive(
    name: str,
    tmp_path: Path,
) -> None:
    selected = tmp_path / name
    selected.write_bytes(b"hive")
    assert conflicts._select_offline_profile_hive(tmp_path) == selected


@pytest.mark.parametrize("names", [(), ("NTUSER.DAT", "NTUSER.MAN")])
def test_offline_profile_rejects_missing_or_ambiguous_hive(
    names: tuple[str, ...],
    tmp_path: Path,
) -> None:
    for name in names:
        (tmp_path / name).write_bytes(b"hive")
    with pytest.raises(RuntimeError, match="keinen eindeutig prüfbaren NTUSER-Hive"):
        conflicts._select_offline_profile_hive(tmp_path)


def test_offline_profile_rejects_redirected_hive(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"hive")
    redirected = tmp_path / "NTUSER.DAT"
    try:
        redirected.symlink_to(target)
    except OSError:
        pytest.skip("Symlinks sind in dieser Testumgebung nicht verfügbar.")
    with pytest.raises(RuntimeError, match="Reparse-Point oder Junction"):
        conflicts._select_offline_profile_hive(tmp_path)


def test_offline_profile_hive_fails_closed_if_selected_file_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conflicts, "_safe_regular_file_exists", lambda _path: False)
    loader = Mock()
    monkeypatch.setattr(
        conflicts,
        "ctypes_windows",
        SimpleNamespace(WinDLL=loader),
    )
    with pytest.raises(RuntimeError, match="nicht mehr sicher lesbar"):
        with conflicts._offline_profile_hive(tmp_path / "NTUSER.DAT"):
            pass
    loader.assert_not_called()


def test_registered_desktop_detects_custom_location_and_partial_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _ContextKey()
    validate = Mock(return_value=Path(r"D:\Custom"))
    fake_winreg = SimpleNamespace(
        KEY_QUERY_VALUE=1,
        REG_SZ=1,
        OpenKey=Mock(return_value=key),
        QueryValueEx=Mock(return_value=(r"D:\Custom", 1)),
    )
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake_winreg)
    monkeypatch.setattr(conflicts, "_validated_local_fixed_path", validate)
    assert conflicts._registered_desktop_present(object()) is True
    validate.assert_called_once_with(r"D:\Custom")

    fake_winreg.QueryValueEx.side_effect = FileNotFoundError
    assert conflicts._registered_desktop_present(object()) is True


@pytest.mark.parametrize("value", [("", 1), (r"D:\Custom", 99)])
def test_registered_desktop_rejects_invalid_install_location(
    value: tuple[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_winreg = SimpleNamespace(
        KEY_QUERY_VALUE=1,
        REG_SZ=1,
        OpenKey=Mock(return_value=_ContextKey()),
        QueryValueEx=Mock(return_value=value),
    )
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake_winreg)
    with pytest.raises(RuntimeError, match="ungültigen Installationspfad"):
        conflicts._registered_desktop_present(object())


def test_autostart_presence_is_read_only_and_invalid_values_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_winreg = SimpleNamespace(
        KEY_QUERY_VALUE=1,
        REG_SZ=1,
        OpenKey=Mock(return_value=_ContextKey()),
        QueryValueEx=Mock(return_value=(r'"D:\Custom\E-Rechnungs-Pruefer.exe" --background', 1)),
    )
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake_winreg)
    assert conflicts._desktop_autostart_present(object()) is True
    fake_winreg.QueryValueEx.return_value = ("", 1)
    with pytest.raises(RuntimeError, match="ungültigen Wert"):
        conflicts._desktop_autostart_present(object())


def test_native_process_inventory_filters_names_and_closes_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = iter((("other.exe", 7), (conflicts.DESKTOP_EXECUTABLE_NAME, 42)))
    error = {"value": 0}

    def populate(_snapshot, pointer) -> bool:
        try:
            name, process_id = next(rows)
        except StopIteration:
            error["value"] = conflicts.ERROR_NO_MORE_FILES
            return False
        entry = pointer._obj  # noqa: SLF001 - ctypes byref adapter
        entry.szExeFile = name
        entry.th32ProcessID = process_id
        return True

    kernel32 = SimpleNamespace(
        CreateToolhelp32Snapshot=Mock(return_value=991),
        Process32FirstW=Mock(side_effect=populate),
        Process32NextW=Mock(side_effect=populate),
        CloseHandle=Mock(return_value=True),
    )
    windows = SimpleNamespace(
        WinDLL=Mock(return_value=kernel32),
        set_last_error=Mock(side_effect=lambda value: error.update(value=value)),
        get_last_error=Mock(side_effect=lambda: error["value"]),
    )
    monkeypatch.setattr(conflicts, "ctypes_windows", windows)

    assert conflicts._running_desktop_processes() == (42,)
    kernel32.CloseHandle.assert_called_once_with(991)

    rows = iter(())
    kernel32.CloseHandle.return_value = False
    error["value"] = 6
    windows.set_last_error.side_effect = None
    with pytest.raises(OSError, match="nicht freigegeben"):
        conflicts._running_desktop_processes()


def test_desktop_mutex_distinguishes_absence_errors_and_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_mutex = Mock(return_value=None)
    close_handle = Mock(return_value=True)
    error = Mock(return_value=conflicts.ERROR_FILE_NOT_FOUND)
    kernel32 = SimpleNamespace(OpenMutexW=open_mutex, CloseHandle=close_handle)
    monkeypatch.setattr(
        conflicts,
        "ctypes_windows",
        SimpleNamespace(WinDLL=Mock(return_value=kernel32), get_last_error=error),
    )
    assert conflicts._desktop_mutex_exists() is False

    error.return_value = 5
    with pytest.raises(OSError, match="Mutex konnte nicht geprüft"):
        conflicts._desktop_mutex_exists()

    open_mutex.return_value = 73
    assert conflicts._desktop_mutex_exists() is True
    close_handle.assert_called_with(73)

    close_handle.return_value = False
    error.return_value = 6
    with pytest.raises(OSError, match="nicht freigegeben"):
        conflicts._desktop_mutex_exists()
