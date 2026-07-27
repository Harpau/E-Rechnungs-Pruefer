from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

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


def test_conflict_check_reads_offline_profile_after_checking_fixed_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_clean_runtime(monkeypatch)
    profile = tmp_path / "User"
    directory_check = Mock(return_value=False)
    offline = conflicts._OfflineProfilePath(profile / "NTUSER.DAT")
    profile_reader = Mock(side_effect=lambda _sid, _path: _hive(offline))
    isolated_check = Mock(return_value=False)
    monkeypatch.setattr(conflicts, "_profile_paths", lambda: (("S-1-5-21-1", profile),))
    monkeypatch.setattr(conflicts, "_safe_directory_exists", directory_check)
    monkeypatch.setattr(conflicts, "_profile_hive", profile_reader)
    monkeypatch.setattr(conflicts, "_inspect_offline_profile_hive_isolated", isolated_check)
    monkeypatch.setattr(conflicts.time, "monotonic", Mock(side_effect=(100.0, 101.0, 200.0, 201.0)))

    conflicts.assert_no_desktop_installation()

    directory_check.assert_called_once_with(
        profile / "AppData" / "Local" / "Programs" / conflicts.DESKTOP_INSTALL_DIRECTORY_NAME
    )
    profile_reader.assert_called_once_with("S-1-5-21-1", profile)
    isolated_check.assert_called_once_with(profile / "NTUSER.DAT", timeout_seconds=30)

    isolated_check.return_value = True
    with pytest.raises(RuntimeError, match="Desktop-Version oder deren Autostart"):
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
    fake_winreg = SimpleNamespace(
        HKEY_USERS=hku,
        KEY_QUERY_VALUE=1,
        KEY_ENUMERATE_SUB_KEYS=8,
        OpenKey=Mock(return_value=loaded),
    )
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake_winreg)
    selector = Mock(return_value=tmp_path / "NTUSER.DAT")
    monkeypatch.setattr(conflicts, "_select_offline_profile_hive", selector)

    with conflicts._profile_hive("S-1-5-21-1", tmp_path) as result:
        assert result is loaded
    fake_winreg.OpenKey.assert_called_once_with(hku, "S-1-5-21-1", 0, 9)
    selector.assert_not_called()

    fake_winreg.OpenKey.side_effect = FileNotFoundError
    with conflicts._profile_hive("S-1-5-21-1", tmp_path) as result:
        assert result == conflicts._OfflineProfilePath(tmp_path / "NTUSER.DAT")
    selector.assert_called_once_with(tmp_path)


def test_profile_hive_fails_closed_for_loaded_access_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_winreg = SimpleNamespace(
        HKEY_USERS=object(),
        KEY_QUERY_VALUE=1,
        KEY_ENUMERATE_SUB_KEYS=8,
        OpenKey=Mock(side_effect=PermissionError("denied")),
    )
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake_winreg)

    with pytest.raises(RuntimeError, match="geladenes Benutzerprofil"):
        with conflicts._profile_hive("S-1-5-21-1", tmp_path):
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


def test_offline_profile_rejects_redirected_or_hardlinked_hive(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"hive")
    redirected = tmp_path / "NTUSER.DAT"
    try:
        redirected.symlink_to(target)
    except OSError:
        pytest.skip("Symlinks sind in dieser Testumgebung nicht verfügbar.")
    with pytest.raises(RuntimeError, match="Reparse-Point oder Junction"):
        conflicts._select_offline_profile_hive(tmp_path)

    redirected.unlink()
    try:
        redirected.hardlink_to(target)
    except OSError:
        pytest.skip("Hardlinks sind in dieser Testumgebung nicht verfügbar.")
    with pytest.raises(RuntimeError, match="keine eindeutige reguläre Datei"):
        conflicts._select_offline_profile_hive(tmp_path)


@contextmanager
def _ordinary_reader(path: Path):
    with path.open("rb") as reader:
        yield reader


def test_offline_hive_snapshot_is_bounded_and_identity_checked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hive_path = tmp_path / "NTUSER.DAT"
    payload = b"x" * 4096
    hive_path.write_bytes(payload)
    monkeypatch.setattr(conflicts, "_locked_binary_reader", _ordinary_reader)

    assert conflicts._read_safe_hive_bytes(hive_path) == payload

    identity_check = conflicts._same_file_identity
    identities = iter((True, False))
    monkeypatch.setattr(conflicts, "_same_file_identity", lambda _left, _right: next(identities))
    with pytest.raises(RuntimeError, match="während des Lesens verändert"):
        conflicts._read_safe_hive_bytes(hive_path)

    monkeypatch.setattr(conflicts, "_same_file_identity", identity_check)
    monkeypatch.setattr(conflicts, "OFFLINE_HIVE_MAX_BYTES", 1024)
    with pytest.raises(RuntimeError, match="unzulässige Größe"):
        conflicts._read_safe_hive_bytes(hive_path)


def _valid_hive_header() -> bytearray:
    data = bytearray(8192)
    data[:4] = b"regf"
    conflicts.struct.pack_into("<I", data, 0x04, 7)
    conflicts.struct.pack_into("<I", data, 0x08, 7)
    conflicts.struct.pack_into("<I", data, 0x1C, 0)
    conflicts.struct.pack_into("<I", data, 0x20, 1)
    conflicts.struct.pack_into("<I", data, 0x24, 0x20)
    conflicts.struct.pack_into("<I", data, 0x28, 4096)
    conflicts.struct.pack_into("<I", data, 0x2C, 1)
    conflicts.struct.pack_into("<I", data, 0x1FC, conflicts._registry_header_checksum(bytes(data)))
    data[conflicts.REGISTRY_HEADER_BYTES : conflicts.REGISTRY_HEADER_BYTES + 4] = b"hbin"
    conflicts.struct.pack_into("<I", data, conflicts.REGISTRY_HEADER_BYTES + 4, 0)
    conflicts.struct.pack_into(
        "<I",
        data,
        conflicts.REGISTRY_HEADER_BYTES + 8,
        conflicts.REGISTRY_HEADER_BYTES,
    )
    return data


def test_offline_hive_header_rejects_dirty_corrupt_and_out_of_bounds_snapshots() -> None:
    valid = _valid_hive_header()
    conflicts._validate_hive_snapshot(bytes(valid))

    mutations = (
        (0x00, b"bad!"),
        (0x08, (8).to_bytes(4, "little")),
        (0x1FC, (0).to_bytes(4, "little")),
        (0x28, (8192).to_bytes(4, "little")),
    )
    for offset, replacement in mutations:
        candidate = valid.copy()
        candidate[offset : offset + len(replacement)] = replacement
        with pytest.raises(RuntimeError):
            conflicts._validate_hive_snapshot(bytes(candidate))


def test_offline_hive_rejects_inconsistent_hbin_chain() -> None:
    valid = _valid_hive_header()
    assert conflicts._validated_hive_bin_ranges(bytes(valid)) == ((4128, 8192),)

    mutations = (
        (conflicts.REGISTRY_HEADER_BYTES, b"xxxx"),
        (conflicts.REGISTRY_HEADER_BYTES + 4, (8).to_bytes(4, "little")),
        (conflicts.REGISTRY_HEADER_BYTES + 8, (2048).to_bytes(4, "little")),
    )
    for offset, replacement in mutations:
        candidate = valid.copy()
        candidate[offset : offset + len(replacement)] = replacement
        with pytest.raises(RuntimeError, match="HBin"):
            conflicts._validated_hive_bin_ranges(bytes(candidate))


def test_regipy_adapter_is_version_bound_and_uses_only_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRegistryHive:
        pass

    class FakeKey:
        def __init__(self, cell, _stream):
            self.cell = cell
            self.name = "ROOT"
            self.subkey_count = 0
            self.values_count = 0
            self.header = SimpleNamespace(
                key_name_size=0,
                flags=SimpleNamespace(KEY_HIVE_ENTRY=True),
            )

    @contextmanager
    def boomerang(stream):
        yield stream

    package = SimpleNamespace(__version__=conflicts.REGIPY_VERSION)
    parsed_header = SimpleNamespace(file_name="NTUSER.DAT")
    registry = SimpleNamespace(
        Cell=lambda **values: SimpleNamespace(**values),
        RegistryHive=FakeRegistryHive,
        NKRecord=FakeKey,
        boomerang_stream=boomerang,
        REGF_HEADER=SimpleNamespace(parse_stream=lambda _stream: parsed_header),
    )
    monkeypatch.setattr(
        conflicts.importlib,
        "import_module",
        lambda name: package if name == "regipy" else registry,
    )

    snapshot = _valid_hive_header()
    root_cell_offset = conflicts.REGISTRY_HEADER_BYTES + 0x20
    conflicts.struct.pack_into("<i", snapshot, root_cell_offset, -88)
    snapshot[root_cell_offset + 4 : root_cell_offset + 6] = b"nk"
    opened = conflicts._regipy_hive_from_bytes(bytes(snapshot))
    assert isinstance(opened.hive._stream, conflicts.BytesIO)
    assert opened.hive._stream.getvalue() == bytes(snapshot)
    assert opened.hive.root.name == "ROOT"
    assert opened.hive.root.cell.offset == root_cell_offset + 6
    opened.hive._stream.close()

    package.__version__ = "unexpected"
    with pytest.raises(RuntimeError, match="unerwartete Version"):
        conflicts._regipy_hive_from_bytes(bytes(snapshot))


def test_regipy_adapter_uses_header_root_and_rejects_invalid_root_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cells: list[SimpleNamespace] = []

    class FakeRegistryHive:
        pass

    class FakeKey:
        def __init__(self, cell, _stream):
            cells.append(cell)
            self.name = "ROOT"
            self.header = SimpleNamespace(
                key_name_size=0,
                flags=SimpleNamespace(KEY_HIVE_ENTRY=True),
            )

    @contextmanager
    def boomerang(stream):
        yield stream

    registry = SimpleNamespace(
        Cell=lambda **values: SimpleNamespace(**values),
        RegistryHive=FakeRegistryHive,
        NKRecord=FakeKey,
        boomerang_stream=boomerang,
        REGF_HEADER=SimpleNamespace(parse_stream=lambda _stream: SimpleNamespace(file_name="NTUSER.DAT")),
    )
    package = SimpleNamespace(__version__=conflicts.REGIPY_VERSION)
    monkeypatch.setattr(
        conflicts.importlib,
        "import_module",
        lambda name: package if name == "regipy" else registry,
    )

    snapshot = _valid_hive_header()
    conflicts.struct.pack_into("<I", snapshot, 0x24, 0x80)
    conflicts.struct.pack_into("<I", snapshot, 0x1FC, 0)
    conflicts.struct.pack_into("<I", snapshot, 0x1FC, conflicts._registry_header_checksum(bytes(snapshot)))
    decoy_offset = conflicts.REGISTRY_HEADER_BYTES + 0x20
    root_offset = conflicts.REGISTRY_HEADER_BYTES + 0x80
    for offset in (decoy_offset, root_offset):
        conflicts.struct.pack_into("<i", snapshot, offset, -88)
        snapshot[offset + 4 : offset + 6] = b"nk"

    opened = conflicts._regipy_hive_from_bytes(bytes(snapshot))
    opened.hive._stream.close()
    assert cells == [
        SimpleNamespace(cell_type="nk", offset=root_offset + 6, size=84),
    ]

    for cell_size, signature in ((88, b"nk"), (-88, b"xx"), (-81, b"nk")):
        candidate = snapshot.copy()
        conflicts.struct.pack_into("<i", candidate, root_offset, cell_size)
        candidate[root_offset + 4 : root_offset + 6] = signature
        with pytest.raises(RuntimeError, match="Root-Key"):
            conflicts._regipy_hive_from_bytes(bytes(candidate))


def test_offline_hive_inspection_isolated_with_timeout_and_exit_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hive_path = tmp_path / "NTUSER.DAT"
    run = Mock(side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=10)])
    monkeypatch.setattr(conflicts.subprocess, "run", run)
    monkeypatch.setattr(conflicts, "_offline_worker_environment", lambda: {"SAFE": "1"})
    monkeypatch.setattr(conflicts.sys, "frozen", True, raising=False)
    monkeypatch.setattr(conflicts.sys, "executable", r"C:\Program Files\Product\client.exe")

    assert conflicts._inspect_offline_profile_hive_isolated(hive_path) is False
    assert conflicts._inspect_offline_profile_hive_isolated(hive_path) is True
    assert run.call_args_list == [
        call(
            [
                r"C:\Program Files\Product\client.exe",
                "--inspect-offline-profile-hive",
                str(hive_path),
            ],
            check=False,
            stdin=conflicts.subprocess.DEVNULL,
            stdout=conflicts.subprocess.DEVNULL,
            stderr=conflicts.subprocess.DEVNULL,
            timeout=conflicts.OFFLINE_HIVE_INSPECTION_TIMEOUT_SECONDS,
            creationflags=getattr(conflicts.subprocess, "CREATE_NO_WINDOW", 0),
            env={"SAFE": "1"},
        ),
        call(
            [
                r"C:\Program Files\Product\client.exe",
                "--inspect-offline-profile-hive",
                str(hive_path),
            ],
            check=False,
            stdin=conflicts.subprocess.DEVNULL,
            stdout=conflicts.subprocess.DEVNULL,
            stderr=conflicts.subprocess.DEVNULL,
            timeout=conflicts.OFFLINE_HIVE_INSPECTION_TIMEOUT_SECONDS,
            creationflags=getattr(conflicts.subprocess, "CREATE_NO_WINDOW", 0),
            env={"SAFE": "1"},
        ),
    ]

    run.side_effect = subprocess_timeout = conflicts.subprocess.TimeoutExpired("worker", 30)
    with pytest.raises(RuntimeError, match="Zeitgrenze"):
        conflicts._inspect_offline_profile_hive_isolated(hive_path)
    assert subprocess_timeout.timeout == 30

    run.side_effect = None
    run.return_value = SimpleNamespace(returncode=1)
    with pytest.raises(RuntimeError, match="isolierten Prüfprozess"):
        conflicts._inspect_offline_profile_hive_isolated(hive_path)


def test_offline_inventory_has_an_overall_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_clean_runtime(monkeypatch)
    profile = tmp_path / "User"
    offline = conflicts._OfflineProfilePath(profile / "NTUSER.DAT")
    monkeypatch.setattr(conflicts, "_profile_paths", lambda: (("S-1-5-21-1", profile),))
    monkeypatch.setattr(conflicts, "_safe_directory_exists", lambda _path: False)
    monkeypatch.setattr(conflicts, "_profile_hive", lambda _sid, _path: _hive(offline))
    isolated = Mock()
    monkeypatch.setattr(conflicts, "_inspect_offline_profile_hive_isolated", isolated)
    monkeypatch.setattr(conflicts.time, "monotonic", Mock(side_effect=(100.0, 161.0)))

    with pytest.raises(RuntimeError, match="Gesamtzeitgrenze"):
        conflicts.assert_no_desktop_installation()
    isolated.assert_not_called()


def test_offline_worker_environment_removes_pyinstaller_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYINSTALLER_RESET_ENVIRONMENT", "1")
    monkeypatch.setenv("ERP_SAFE_SENTINEL", "preserved")
    environment = conflicts._offline_worker_environment()
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in environment
    assert environment["ERP_SAFE_SENTINEL"] == "preserved"


def test_direct_offline_hive_worker_combines_both_registry_footprints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offline = object()
    monkeypatch.setattr(conflicts, "_offline_profile_hive", lambda _path: _hive(offline))
    registered = Mock(side_effect=(False, True))
    autostart = Mock(return_value=True)
    monkeypatch.setattr(conflicts, "_registered_desktop_present", registered)
    monkeypatch.setattr(conflicts, "_desktop_autostart_present", autostart)

    assert conflicts.inspect_offline_profile_hive(Path("first")) is True
    assert conflicts.inspect_offline_profile_hive(Path("second")) is True
    assert registered.call_count == 2
    autostart.assert_called_once_with(offline)


class _OfflineKey:
    def __init__(self, name: str, *, children=(), values=(), subkey_count=None, values_count=None):
        self.name = name
        self._children = tuple(children)
        self._values = tuple(values)
        self.subkey_count = len(self._children) if subkey_count is None else subkey_count
        self.values_count = len(self._values) if values_count is None else values_count

    def iter_subkeys(self):
        yield from self._children

    def get_values(self, *, trim_values: bool):
        assert trim_values is False
        return list(self._values)


def _offline_tree(path: str, values: tuple[SimpleNamespace, ...]) -> conflicts._OfflineProfileHive:
    child = _OfflineKey(path.split("\\")[-1], values=values)
    for part in reversed(path.split("\\")[:-1]):
        child = _OfflineKey(part, children=(child,))
    root = _OfflineKey("ROOT", children=(child,))
    return conflicts._OfflineProfileHive(
        hive=SimpleNamespace(root=root),
        key_type=_OfflineKey,
    )


def test_offline_registry_detects_custom_uninstall_and_autostart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = SimpleNamespace(
        name="InstallLocation",
        value=r"D:\Legacy\Desktop",
        value_type="REG_SZ",
        is_corrupted=False,
    )
    uninstall = _offline_tree(conflicts.DESKTOP_UNINSTALL_KEY, (location,))
    validate = Mock(return_value=Path(r"D:\Legacy\Desktop"))
    monkeypatch.setattr(conflicts, "_validated_local_fixed_path", validate)
    assert conflicts._registered_desktop_present(uninstall) is True
    validate.assert_called_once_with(r"D:\Legacy\Desktop")

    run_value = SimpleNamespace(
        name=conflicts.AUTOSTART_VALUE_NAME,
        value=r'"D:\Legacy\Desktop\E-Rechnungs-Pruefer.exe" --background',
        value_type="REG_SZ",
        is_corrupted=False,
    )
    autostart = _offline_tree(conflicts.AUTOSTART_KEY, (run_value,))
    assert conflicts._desktop_autostart_present(autostart) is True


def test_offline_registry_missing_well_formed_keys_is_not_a_conflict() -> None:
    root = conflicts._OfflineProfileHive(
        hive=SimpleNamespace(root=_OfflineKey("ROOT")),
        key_type=_OfflineKey,
    )
    assert conflicts._registered_desktop_present(root) is False
    assert conflicts._desktop_autostart_present(root) is False


@pytest.mark.parametrize(
    "corrupt_root",
    [
        _OfflineKey("ROOT", children=(_OfflineKey("Software"),), subkey_count=2),
        _OfflineKey("ROOT", children=(_OfflineKey("Software"), _OfflineKey("software"))),
    ],
)
def test_offline_registry_fails_closed_for_incomplete_or_ambiguous_keys(
    corrupt_root: _OfflineKey,
) -> None:
    root = conflicts._OfflineProfileHive(
        hive=SimpleNamespace(root=corrupt_root),
        key_type=_OfflineKey,
    )
    with pytest.raises(RuntimeError):
        conflicts._registered_desktop_present(root)


@pytest.mark.parametrize(
    "value",
    [
        SimpleNamespace(name="InstallLocation", value="", value_type="REG_SZ", is_corrupted=False),
        SimpleNamespace(name="InstallLocation", value=r"D:\Custom", value_type="REG_BINARY", is_corrupted=False),
        SimpleNamespace(name="InstallLocation", value=r"D:\Custom", value_type="REG_SZ", is_corrupted=True),
    ],
)
def test_offline_registry_fails_closed_for_invalid_values(
    value: SimpleNamespace,
) -> None:
    root = _offline_tree(conflicts.DESKTOP_UNINSTALL_KEY, (value,))
    with pytest.raises(RuntimeError):
        conflicts._registered_desktop_present(root)


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
