from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, call

import pytest

from app import windows_desktop_migration as migration
from app import windows_open_client


class _ContextKey:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class _NamedContextKey(_ContextKey):
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeAcl:
    def __init__(self) -> None:
        self.aces: list[tuple[tuple[int, int], int, str]] = []

    def AddAccessAllowedAceEx(self, _revision: int, flags: int, mask: int, sid: str) -> None:
        self.aces.append(((0, flags), int(mask), sid))

    def AddAccessAllowedAce(self, _revision: int, mask: int, sid: str) -> None:
        self.aces.append(((0, 0), int(mask), sid))

    def GetAceCount(self) -> int:
        return len(self.aces)

    def GetAce(self, index: int):
        return self.aces[index]


class _FakeDescriptor:
    def __init__(self) -> None:
        self.owner: str | None = None
        self.dacl: _FakeAcl | None = None
        self.control = 0

    def SetSecurityDescriptorOwner(self, owner: str, _defaulted: int) -> None:
        self.owner = owner

    def SetSecurityDescriptorDacl(self, _present: int, dacl: _FakeAcl, _defaulted: int) -> None:
        self.dacl = dacl

    def SetSecurityDescriptorControl(self, _mask: int, value: int) -> None:
        self.control = value

    def GetSecurityDescriptorOwner(self) -> str | None:
        return self.owner

    def GetSecurityDescriptorDacl(self) -> _FakeAcl | None:
        return self.dacl

    def GetSecurityDescriptorControl(self) -> tuple[int, int]:
        return self.control, 1


class _FakeSecurityAttributes:
    SECURITY_DESCRIPTOR: _FakeDescriptor


class _FakeToken:
    def Close(self) -> None:
        return None


class _FakeFileHandle:
    def __init__(self, path: Path) -> None:
        self.file = path.open("xb")


def _fake_migration_security_modules(*, current_sid: str = "S-1-5-21-1000"):
    descriptors: dict[str, _FakeDescriptor] = {}
    ntsecuritycon = SimpleNamespace(
        FILE_ADD_FILE=0x02,
        FILE_ALL_ACCESS=0x0F,
        FILE_GENERIC_EXECUTE=0x20,
        FILE_GENERIC_READ=0x01,
        FILE_READ_ATTRIBUTES=0x80,
        FILE_TRAVERSE=0x20,
        DELETE=0x10000,
    )
    win32security = SimpleNamespace(
        ACCESS_ALLOWED_ACE_TYPE=0,
        ACL_REVISION_DS=4,
        CONTAINER_INHERIT_ACE=2,
        DACL_SECURITY_INFORMATION=4,
        OBJECT_INHERIT_ACE=1,
        OWNER_SECURITY_INFORMATION=1,
        PROTECTED_DACL_SECURITY_INFORMATION=0x80000000,
        SE_DACL_PROTECTED=0x1000,
        SE_FILE_OBJECT=1,
        TokenUser=1,
        ACL=_FakeAcl,
        SECURITY_DESCRIPTOR=_FakeDescriptor,
        ConvertStringSidToSid=lambda value: value,
        ConvertSidToStringSid=lambda value: value,
        LookupAccountSid=lambda _system, sid: (sid, "TEST", 1),
        GetNamedSecurityInfo=lambda path, _kind, _information: descriptors[path],
        OpenProcessToken=lambda _process, _access: _FakeToken(),
        GetTokenInformation=lambda _token, _kind: (current_sid,),
    )
    pywintypes = SimpleNamespace(SECURITY_ATTRIBUTES=_FakeSecurityAttributes)
    win32api = SimpleNamespace(GetCurrentProcess=lambda: object())
    win32con = SimpleNamespace(
        CREATE_NEW=1,
        FILE_ATTRIBUTE_NORMAL=0x80,
        FILE_ATTRIBUTE_TEMPORARY=2,
        GENERIC_WRITE=4,
        TOKEN_QUERY=8,
    )

    def create_directory(path: str, attributes: _FakeSecurityAttributes) -> None:
        Path(path).mkdir()
        descriptors[path] = attributes.SECURITY_DESCRIPTOR

    def create_file(
        path: str,
        _access: int,
        _share: int,
        attributes: _FakeSecurityAttributes,
        _creation: int,
        _flags: int,
        _template,
    ) -> _FakeFileHandle:
        handle = _FakeFileHandle(Path(path))
        descriptors[path] = attributes.SECURITY_DESCRIPTOR
        return handle

    def move_file(source: str, target: str, _flags: int) -> None:
        Path(source).rename(target)
        descriptors[target] = descriptors.pop(source)

    def set_named_security_info(
        path: str,
        _kind: int,
        _information: int,
        _owner,
        _group,
        dacl: _FakeAcl,
        _sacl,
    ) -> None:
        descriptors[path].dacl = dacl
        descriptors[path].control = win32security.SE_DACL_PROTECTED

    win32security.SetNamedSecurityInfo = set_named_security_info
    win32file = SimpleNamespace(
        CreateDirectoryW=create_directory,
        CreateFile=create_file,
        MoveFileEx=move_file,
        WriteFile=lambda handle, payload: handle.file.write(payload),
        FlushFileBuffers=lambda handle: handle.file.flush(),
        CloseHandle=lambda handle: handle.file.close(),
    )
    modules = (pywintypes, win32api, win32con, win32file, win32security, ntsecuritycon)
    return modules, descriptors


def _private_receipt_descriptor(modules, *, owner_sid: str) -> _FakeDescriptor:
    _pywintypes, _win32api, _win32con, _win32file, win32security, ntsecuritycon = modules
    descriptor = _FakeDescriptor()
    descriptor.owner = owner_sid
    descriptor.control = win32security.SE_DACL_PROTECTED
    descriptor.dacl = _FakeAcl()
    for sid in (migration.SYSTEM_SID, migration.ADMINISTRATORS_SID, owner_sid):
        descriptor.dacl.AddAccessAllowedAce(1, ntsecuritycon.FILE_ALL_ACCESS, sid)
    return descriptor


def _transaction(
    receipt: migration.MigrationReceipt,
    *,
    phase: migration.MigrationPhase,
    reader_sid: str = "S-1-5-21-test",
    transaction_id: str = "a" * 32,
) -> tuple[migration.MigrationSeal, migration.MigrationPhaseRecord]:
    return (
        migration.MigrationSeal(
            schema_version=migration.MIGRATION_SCHEMA_VERSION,
            transaction_id=transaction_id,
            reader_sid=reader_sid,
            token_sha256=None,
            receipt=receipt,
        ),
        migration.MigrationPhaseRecord(
            schema_version=migration.MIGRATION_PHASE_SCHEMA_VERSION,
            transaction_id=transaction_id,
            generation=migration.MIGRATION_PHASE_GENERATIONS[phase],
            phase=phase,
        ),
    )


def test_autostart_validation_accepts_only_the_registered_desktop_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migration, "desktop_executable", lambda: Path(r"C:\Programme\Pruefer\app.exe"))
    expected = r'"C:\Programme\Pruefer\app.exe" --background'

    assert migration.validate_autostart_command(expected) == expected
    with pytest.raises(RuntimeError, match="nicht eindeutig"):
        migration.validate_autostart_command(expected + " --fremd")


def test_internal_migration_failure_never_opens_a_message_box(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_open_client,
        "plan_desktop_migration",
        Mock(side_effect=RuntimeError("Testfehler")),
    )
    message_box = Mock()
    monkeypatch.setattr(windows_open_client, "_show_message", message_box)

    result = windows_open_client.main(["--plan-desktop-migration", "--receipt", "receipt.json"])

    assert result == 1
    message_box.assert_not_called()


def test_internal_machine_inventory_action_is_ui_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    verify = Mock()
    monkeypatch.setattr(windows_open_client, "verify_no_legacy_desktop_conflicts", verify)
    message_box = Mock()
    monkeypatch.setattr(windows_open_client, "_show_message", message_box)
    monkeypatch.setattr(windows_open_client, "is_process_elevated", Mock(return_value=True))

    assert windows_open_client.main(["--verify-applied-desktop-migration"]) == 0
    verify.assert_called_once_with()
    message_box.assert_not_called()


def test_internal_sealed_rollback_and_cleanup_actions_are_routed_without_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollback = Mock()
    clear = Mock()
    message_box = Mock()
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(windows_open_client, "rollback_desktop_migration", rollback)
    monkeypatch.setattr(windows_open_client, "clear_desktop_migration_seal", clear)
    monkeypatch.setattr(windows_open_client, "is_process_elevated", Mock(return_value=True))
    monkeypatch.setattr(windows_open_client, "_show_message", message_box)

    assert windows_open_client.main(["--rollback-desktop-migration"]) == 0
    assert windows_open_client.main(["--clear-desktop-migration-seal"]) == 0

    rollback.assert_called_once_with()
    clear.assert_called_once_with()
    message_box.assert_not_called()

    clear.reset_mock()
    monkeypatch.setattr(windows_open_client, "is_process_elevated", Mock(return_value=False))
    assert windows_open_client.main(["--clear-desktop-migration-seal"]) == 1
    clear.assert_not_called()
    message_box.assert_not_called()


def test_migration_receipt_is_limited_to_plan_and_seal() -> None:
    with pytest.raises(SystemExit):
        windows_open_client._parse_arguments(["--preflight-machine", "--receipt", "receipt.json"])
    with pytest.raises(SystemExit):
        windows_open_client._parse_arguments(["--apply-desktop-migration", "--receipt", "receipt.json"])


def test_custom_desktop_install_location_is_taken_from_v130_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _ContextKey()
    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        REG_SZ=1,
        OpenKey=Mock(return_value=key),
        QueryValueEx=Mock(return_value=(r"D:\Eigene Installation", 1)),
    )
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Test\AppData\Local")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert migration.desktop_executable() == Path(r"D:\Eigene Installation") / "E-Rechnungs-Pruefer.exe"
    fake_winreg.OpenKey.assert_called_once_with(
        fake_winreg.HKEY_CURRENT_USER,
        migration.DESKTOP_UNINSTALL_KEY,
        0,
        fake_winreg.KEY_QUERY_VALUE,
    )


def test_registered_desktop_without_install_location_is_not_treated_as_absent() -> None:
    fake_winreg = SimpleNamespace(
        KEY_QUERY_VALUE=1,
        OpenKey=Mock(return_value=_ContextKey()),
        QueryValueEx=Mock(side_effect=FileNotFoundError),
    )

    with pytest.raises(RuntimeError, match="keinen Installationspfad"):
        migration._registered_install_location(object(), winreg=fake_winreg)


def test_elevated_inventory_accepts_only_canonical_fixed_drive_paths() -> None:
    fixed_drive = Mock(return_value=migration.DRIVE_FIXED)

    assert (
        migration._normalize_local_fixed_windows_path(
            r"C:\Users\Test\AppData\Local",
            drive_type=fixed_drive,
        )
        == r"C:\Users\Test\AppData\Local"
    )
    fixed_drive.assert_called_once_with("C:\\")

    for unsafe in (
        r"\\server\share\E-Rechnungs-Pruefer",
        r"\\?\C:\Users\Test",
        r"C:relative\path",
        r"C:\Users\Test\..\Admin",
        r"C:\Users\Test\file:stream",
        r"C:\Users\Test\NUL.txt",
        "C:\\Users\\Test\\trailing. ",
        r"C:\Users\Test\*.exe",
        r"C:/Users/Test",
    ):
        probe = Mock(return_value=migration.DRIVE_FIXED)
        with pytest.raises(RuntimeError):
            migration._normalize_local_fixed_windows_path(unsafe, drive_type=probe)

    with pytest.raises(RuntimeError, match="festen lokalen Laufwerk"):
        migration._normalize_local_fixed_windows_path(
            r"Z:\Users\Test",
            drive_type=lambda _root: 4,
        )


def test_native_path_inventory_holds_no_follow_handles_for_every_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handles = iter((101, 102, 103))
    create_file = Mock(side_effect=lambda *_args: next(handles))
    close_handle = Mock(return_value=True)
    information_calls = 0

    def get_information(_handle, pointer) -> bool:
        nonlocal information_calls
        information_calls += 1
        information = pointer._obj  # noqa: SLF001 - ctypes byref test adapter
        information.file_attributes = migration.FILE_ATTRIBUTE_DIRECTORY if information_calls < 3 else 0
        information.number_of_links = 1
        assert close_handle.call_count == 0
        return True

    kernel32 = SimpleNamespace(
        CreateFileW=create_file,
        GetFileInformationByHandle=Mock(side_effect=get_information),
        CloseHandle=close_handle,
    )
    monkeypatch.setattr(migration.os, "name", "nt")
    monkeypatch.setattr(
        migration,
        "_validated_local_fixed_path",
        lambda value: migration.PureWindowsPath(value),
    )
    monkeypatch.setattr(migration.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)
    monkeypatch.setattr(migration.ctypes, "get_last_error", lambda: 0, raising=False)
    path = cast(Path, migration.PureWindowsPath(r"C:\Users\Test\E-Rechnungs-Pruefer.exe"))

    with migration._locked_local_path(path, directory=False) as exists:
        assert exists is True
        assert close_handle.call_count == 0

    assert create_file.call_count == 3
    for candidate in create_file.call_args_list:
        assert candidate.args[2] == migration.FILE_SHARE_READ
        assert candidate.args[5] == (migration.FILE_FLAG_OPEN_REPARSE_POINT | migration.FILE_FLAG_BACKUP_SEMANTICS)
    assert close_handle.call_args_list == [call(103), call(102), call(101)]


def test_native_hive_reader_uses_held_no_follow_backup_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = 707
    create_file = Mock(return_value=handle)
    close_handle = Mock(return_value=True)

    def get_information(_handle, pointer) -> bool:
        information = pointer._obj  # noqa: SLF001 - ctypes byref test adapter
        information.file_attributes = 0
        information.number_of_links = 1
        information.file_size_high = 0
        information.file_size_low = 4
        return True

    def read_file(_handle, buffer, _maximum_bytes, bytes_read, _overlapped) -> bool:
        migration.ctypes.memmove(buffer, b"hive", 4)
        bytes_read._obj.value = 4  # noqa: SLF001 - ctypes byref test adapter
        return True

    kernel32 = SimpleNamespace(
        CreateFileW=create_file,
        GetFileInformationByHandle=Mock(side_effect=get_information),
        ReadFile=Mock(side_effect=read_file),
        CloseHandle=close_handle,
    )
    monkeypatch.setattr(migration.os, "name", "nt")
    monkeypatch.setattr(migration.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)
    monkeypatch.setattr(migration.ctypes, "get_last_error", lambda: 0, raising=False)
    path = cast(Path, migration.PureWindowsPath(r"C:\Users\Test\NTUSER.DAT"))

    with migration._open_locked_profile_hive_reader(path) as reader:
        assert reader.size == 4
        assert reader.read(4) == b"hive"
        close_handle.assert_not_called()

    create_file.assert_called_once_with(
        str(path),
        migration.GENERIC_READ,
        migration.FILE_SHARE_READ,
        None,
        migration.OPEN_EXISTING,
        migration.FILE_FLAG_OPEN_REPARSE_POINT | migration.FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    close_handle.assert_called_once_with(handle)


def test_private_transfer_file_is_exclusive_and_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "transfer" / "token.txt"

    migration._write_private(target, b"secret\n")

    assert target.read_bytes() == b"secret\n"
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        migration._write_private(target, b"overwrite")
    assert target.read_bytes() == b"secret\n"


def test_private_transfer_writer_does_not_recreate_an_existing_protected_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "prepared-leaf"
    parent.mkdir()
    target = parent / "receipt.json"
    mkdir = Mock(side_effect=AssertionError("existing protected parent must not be recreated"))
    monkeypatch.setattr(Path, "mkdir", mkdir)

    migration._write_private(target, b"receipt\n")

    assert target.read_bytes() == b"receipt\n"
    mkdir.assert_not_called()


def _fake_transfer_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, tuple, dict[str, _FakeDescriptor]]:
    program_data = tmp_path / "ProgramData"
    program_data.mkdir()
    root = program_data / migration.DESKTOP_MIGRATION_TRANSFER_ROOT_NAME
    modules, descriptors = _fake_migration_security_modules()
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_migration_windows_modules", lambda: modules)
    monkeypatch.setattr(migration, "_desktop_migration_transfer_root", lambda: root)
    return root, modules, descriptors


def test_transfer_staging_prepares_least_privilege_client_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, modules, descriptors = _fake_transfer_environment(tmp_path, monkeypatch)
    _pywintypes, _win32api, _win32con, _win32file, _win32security, ntsecuritycon = modules
    source = tmp_path / "E-Rechnungs-Pruefer-Oeffnen-source.exe"
    source.write_bytes(b"native-client")
    leaf = root / "is-A1b2.tmp"
    client_name = "E-Rechnungs-Pruefer-Oeffnen.exe"

    client = migration.prepare_desktop_migration_transfer(leaf, source, client_name)

    assert client == leaf / client_name
    assert client.read_bytes() == b"native-client"
    assert set(path.name for path in leaf.iterdir()) == {client_name}
    root_aces = descriptors[str(root)].dacl
    leaf_aces = descriptors[str(leaf)].dacl
    client_aces = descriptors[str(client)].dacl
    assert root_aces is not None
    assert leaf_aces is not None
    assert client_aces is not None
    assert root_aces.aces[-1][1:] == (ntsecuritycon.FILE_TRAVERSE, migration.INTERACTIVE_SID)
    assert leaf_aces.aces[-1][1:] == (
        ntsecuritycon.FILE_TRAVERSE | ntsecuritycon.FILE_READ_ATTRIBUTES | ntsecuritycon.FILE_ADD_FILE,
        migration.INTERACTIVE_SID,
    )
    assert client_aces.aces[-1][1:] == (
        ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_EXECUTE,
        migration.INTERACTIVE_SID,
    )
    assert all(header[1] == 0 for dacl in (root_aces, leaf_aces, client_aces) for header, _mask, _sid in dacl.aces)

    assert migration.prepare_desktop_migration_transfer(leaf, source, client_name) == client
    source.write_bytes(b"different-client")
    with pytest.raises(RuntimeError, match="stimmt nicht"):
        migration.prepare_desktop_migration_transfer(leaf, source, client_name)
    assert client.read_bytes() == b"native-client"


def test_transfer_staging_validates_owner_inventory_and_cleans_nonrecursively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, modules, descriptors = _fake_transfer_environment(tmp_path, monkeypatch)
    source = tmp_path / "client-source.exe"
    source.write_bytes(b"native-client")
    leaf = root / "is-C3d4.tmp"
    client_name = "E-Rechnungs-Pruefer-Oeffnen.exe"
    client = migration.prepare_desktop_migration_transfer(leaf, source, client_name)
    receipt = leaf / migration.DESKTOP_MIGRATION_TRANSFER_RECEIPT_NAME
    token = leaf / migration.DESKTOP_MIGRATION_TRANSFER_TOKEN_NAME
    receipt.write_text("{}\n", encoding="utf-8")
    token.write_text("t" * 43 + "\n", encoding="ascii")
    descriptors[str(receipt)] = _private_receipt_descriptor(modules, owner_sid="S-1-5-21-1000")
    descriptors[str(token)] = _private_receipt_descriptor(modules, owner_sid="S-1-5-21-1000")

    migration.validate_desktop_migration_transfer(leaf, receipt, token, client_name)

    descriptors[str(token)] = _private_receipt_descriptor(modules, owner_sid="S-1-5-21-2000")
    with pytest.raises(RuntimeError, match="unterschiedlichen Benutzeridentitäten"):
        migration.validate_desktop_migration_transfer(leaf, receipt, token, client_name)
    descriptors[str(token)] = _private_receipt_descriptor(modules, owner_sid="S-1-5-21-1000")

    unknown = leaf / "unknown.bin"
    unknown.write_bytes(b"untrusted")
    with pytest.raises(RuntimeError, match="unerwartete Einträge"):
        migration.clear_desktop_migration_transfer(leaf, client_name)
    assert {client, receipt, token, unknown} <= set(leaf.iterdir())

    unknown.unlink()
    migration.clear_desktop_migration_transfer(leaf, client_name)

    assert not leaf.exists()
    assert not root.exists()


def test_transfer_staging_rejects_aliases_wrong_paths_and_receipt_residue_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, modules, descriptors = _fake_transfer_environment(tmp_path, monkeypatch)
    source = tmp_path / "client-source.exe"
    source.write_bytes(b"native-client")
    leaf = root / "is-E5f6.tmp"
    client_name = "E-Rechnungs-Pruefer-Oeffnen.exe"
    client = migration.prepare_desktop_migration_transfer(leaf, source, client_name)
    receipt = leaf / migration.DESKTOP_MIGRATION_TRANSFER_RECEIPT_NAME
    receipt.write_text("{}\n", encoding="utf-8")
    descriptors[str(receipt)] = _private_receipt_descriptor(modules, owner_sid="S-1-5-21-1000")

    with pytest.raises(RuntimeError, match="nicht ausschließlich"):
        migration.prepare_desktop_migration_transfer(leaf, source, client_name)
    with pytest.raises(RuntimeError, match="erwarteten Desktop-Transferpfad"):
        migration.validate_desktop_migration_transfer(
            leaf,
            tmp_path / migration.DESKTOP_MIGRATION_TRANSFER_RECEIPT_NAME,
            None,
            client_name,
        )

    outside_alias = tmp_path / "client-hardlink.exe"
    os.link(client, outside_alias)
    with pytest.raises(RuntimeError, match="Hardlink"):
        migration.validate_desktop_migration_transfer(leaf, receipt, None, client_name)


def test_locked_transfer_reader_rejects_aliases_and_bounds_token_size(tmp_path: Path) -> None:
    source = tmp_path / "desktop-token.txt"
    source.write_text("m" * 43 + "\n", encoding="ascii")

    assert migration.read_desktop_migration_token(source) == "m" * 43

    hardlink = tmp_path / "hardlink.txt"
    os.link(source, hardlink)
    with pytest.raises(RuntimeError, match="eindeutige reguläre Datei"):
        migration.read_desktop_migration_token(hardlink)

    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"x" * (migration.MAXIMUM_MIGRATION_TOKEN_BYTES + 1))
    with pytest.raises(RuntimeError, match="zulässige Größe"):
        migration.read_desktop_migration_token(oversized)


def test_protected_migration_seal_binds_transaction_reader_token_and_initial_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader_sid = "S-1-5-21-1000"
    modules, descriptors = _fake_migration_security_modules(current_sid=reader_sid)
    state_directory = tmp_path / migration.MIGRATION_STATE_DIRECTORY_NAME
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    receipt_path = tmp_path / "receipt.json"
    token_path = tmp_path / "token.txt"
    receipt = migration.MigrationReceipt(
        autostart_command=None,
        was_running=False,
        executable=str(tmp_path / migration.DESKTOP_EXECUTABLE_NAME),
        disabled_executable=None,
    )
    token_path.write_text("t" * 43 + "\n", encoding="ascii")
    receipt_path.write_text(
        json.dumps(
            receipt.__dict__
            if hasattr(receipt, "__dict__")
            else {
                "autostart_command": receipt.autostart_command,
                "was_running": receipt.was_running,
                "executable": receipt.executable,
                "disabled_executable": receipt.disabled_executable,
            }
        ),
        encoding="utf-8",
    )
    descriptors[str(receipt_path)] = _private_receipt_descriptor(modules, owner_sid=reader_sid)
    descriptors[str(token_path)] = _private_receipt_descriptor(modules, owner_sid=reader_sid)
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_migration_windows_modules", lambda: modules)
    monkeypatch.setattr(migration, "_migration_state_paths", lambda: (state_directory, seal_path))
    monkeypatch.setattr(migration.secrets, "token_hex", lambda _length: "a" * 32)

    migration.seal_desktop_migration(receipt_path=receipt_path, token_transfer_path=token_path)

    phase_path = state_directory / migration.MIGRATION_PHASE_FILE_NAME
    sealed_token_path = state_directory / migration.MIGRATION_TOKEN_FILE_NAME
    for protected_path in (state_directory, seal_path, phase_path):
        descriptor = descriptors[str(protected_path)]
        assert descriptor.dacl is not None
        reader_aces = [(mask, sid) for _header, mask, sid in descriptor.dacl.aces if sid == reader_sid]
        assert reader_aces == [(modules[5].FILE_GENERIC_READ, reader_sid)]
        assert reader_aces[0][0] & modules[5].DELETE == 0
    sealed_token_descriptor = descriptors[str(sealed_token_path)]
    assert sealed_token_descriptor.dacl is not None
    assert all(sid != reader_sid for _header, _mask, sid in sealed_token_descriptor.dacl.aces)

    receipt_path.write_text('{"attacker":true}', encoding="utf-8")
    token_path.write_text("u" * 43 + "\n", encoding="ascii")
    loaded_seal, loaded_phase = migration._load_migration_transaction(require_current_user=True)

    assert loaded_seal.receipt == receipt
    assert loaded_seal.reader_sid == reader_sid
    assert loaded_seal.transaction_id == "a" * 32
    assert loaded_seal.token_sha256 == migration.hashlib.sha256(("t" * 43 + "\n").encode("ascii")).hexdigest()
    assert loaded_phase == migration.MigrationPhaseRecord(
        schema_version=migration.MIGRATION_PHASE_SCHEMA_VERSION,
        transaction_id="a" * 32,
        generation=0,
        phase=migration.MigrationPhase.ROLLBACKABLE,
    )
    assert sealed_token_path.read_text(encoding="ascii") == "t" * 43 + "\n"
    assert set(migration._migration_state_entries(state_directory)) == {
        migration.MIGRATION_SEAL_FILE_NAME,
        migration.MIGRATION_PHASE_FILE_NAME,
        migration.MIGRATION_TOKEN_FILE_NAME,
    }

    migration.clear_desktop_migration_seal()

    assert not state_directory.exists()


def test_receipt_parser_rejects_duplicate_fields_and_seal_rejects_wrong_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = (
        b'{"autostart_command":null,"was_running":false,"was_running":true,"executable":"x","disabled_executable":null}'
    )
    with pytest.raises(RuntimeError, match="ungültig"):
        migration._decode_receipt(duplicate)

    reader_sid = "S-1-5-21-1000"
    modules, descriptors = _fake_migration_security_modules(current_sid="S-1-5-21-2000")
    state_directory = tmp_path / migration.MIGRATION_STATE_DIRECTORY_NAME
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    receipt_path = tmp_path / "receipt.json"
    receipt = migration.MigrationReceipt(None, False, str(tmp_path / migration.DESKTOP_EXECUTABLE_NAME), None)
    receipt_path.write_text(
        json.dumps(
            {
                "autostart_command": None,
                "was_running": False,
                "executable": receipt.executable,
                "disabled_executable": None,
            }
        ),
        encoding="utf-8",
    )
    descriptors[str(receipt_path)] = _private_receipt_descriptor(modules, owner_sid=reader_sid)
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_migration_windows_modules", lambda: modules)
    monkeypatch.setattr(migration, "_migration_state_paths", lambda: (state_directory, seal_path))

    migration.seal_desktop_migration(receipt_path=receipt_path, token_transfer_path=None)

    with pytest.raises(RuntimeError, match="anderen Benutzeridentität"):
        migration._load_migration_transaction(require_current_user=True)

    migration.clear_desktop_migration_seal()

    assert not state_directory.exists()


def test_apply_uses_only_rollbackable_seal_and_finishes_partial_steps_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / migration.DESKTOP_EXECUTABLE_NAME
    executable.write_bytes(b"desktop-v1.3")
    disabled = executable.with_name(executable.name + migration.DISABLED_SUFFIX)
    command = f'"{executable}" --background'
    receipt = migration.MigrationReceipt(command, True, str(executable), str(disabled))
    key = _ContextKey()
    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        KEY_SET_VALUE=2,
        REG_SZ=1,
        OpenKey=Mock(return_value=key),
        QueryValueEx=Mock(return_value=(command, 1)),
        DeleteValue=Mock(),
    )
    stop = Mock(return_value=True)
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(migration, "_stop_desktop_backend", stop)
    monkeypatch.setattr(
        migration,
        "_load_migration_transaction",
        lambda **_kwargs: _transaction(receipt, phase=migration.MigrationPhase.ROLLBACKABLE),
    )

    migration.apply_desktop_migration()

    assert not executable.exists()
    assert disabled.read_bytes() == b"desktop-v1.3"
    fake_winreg.DeleteValue.assert_called_once_with(key, migration.AUTOSTART_VALUE_NAME)
    stop.assert_called_once_with()

    fake_winreg.QueryValueEx.side_effect = FileNotFoundError
    migration.apply_desktop_migration()
    assert disabled.read_bytes() == b"desktop-v1.3"


def test_apply_rejects_wrong_phase_before_desktop_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / migration.DESKTOP_EXECUTABLE_NAME
    executable.write_bytes(b"desktop-v1.3")
    disabled = executable.with_name(executable.name + migration.DISABLED_SUFFIX)
    receipt = migration.MigrationReceipt(None, False, str(executable), str(disabled))
    stop = Mock()
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_stop_desktop_backend", stop)
    monkeypatch.setattr(
        migration,
        "_load_migration_transaction",
        lambda **_kwargs: _transaction(receipt, phase=migration.MigrationPhase.SERVICE_TRANSITION),
    )

    with pytest.raises(RuntimeError, match="Phase"):
        migration.apply_desktop_migration()

    stop.assert_not_called()
    assert executable.read_bytes() == b"desktop-v1.3"
    assert not disabled.exists()


def test_owner_probe_requires_the_current_reader_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load = Mock()
    monkeypatch.setattr(migration, "_load_migration_transaction", load)

    migration.verify_desktop_migration_owner()

    load.assert_called_once_with(require_current_user=True)


def test_public_desktop_binding_is_canonical_optional_and_token_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    token_path = state_directory / migration.MIGRATION_TOKEN_FILE_NAME
    token_path.write_bytes(b"sealed")
    receipt = migration.MigrationReceipt(None, False, r"C:\App\E-Rechnungs-Pruefer.exe", None)
    base_seal, phase = _transaction(receipt, phase=migration.MigrationPhase.ROLLBACKABLE)
    seal = migration.MigrationSeal(
        schema_version=base_seal.schema_version,
        transaction_id=base_seal.transaction_id,
        reader_sid=base_seal.reader_sid,
        token_sha256="b" * 64,
        receipt=base_seal.receipt,
    )
    monkeypatch.setattr(migration, "_migration_state_paths", lambda: (state_directory, seal_path))
    monkeypatch.setattr(
        migration,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    load = Mock(return_value=(seal, phase))
    monkeypatch.setattr(migration, "_load_migration_transaction", load)

    binding = migration.load_desktop_migration_binding()

    assert binding == migration.DesktopMigrationBinding(
        transaction_id=seal.transaction_id,
        reader_sid=seal.reader_sid,
        seal_sha256=migration.hashlib.sha256(migration._encode_migration_seal(seal)).hexdigest(),
        token_sha256=seal.token_sha256,
        receipt=receipt,
        phase=migration.MigrationPhase.ROLLBACKABLE,
    )
    assert migration.protected_desktop_migration_token_path() == token_path
    assert load.call_args_list == [
        call(require_current_user=False),
        call(require_current_user=False),
    ]

    token_path.unlink()
    state_directory.rmdir()
    assert migration.load_desktop_migration_binding() is None


def test_partial_seal_detection_accepts_only_valid_prephase_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    receipt = migration.MigrationReceipt(None, False, r"C:\App\E-Rechnungs-Pruefer.exe", None)
    base_seal, _phase = _transaction(receipt, phase=migration.MigrationPhase.ROLLBACKABLE)
    seal = migration.MigrationSeal(
        schema_version=base_seal.schema_version,
        transaction_id=base_seal.transaction_id,
        reader_sid=base_seal.reader_sid,
        token_sha256="b" * 64,
        receipt=base_seal.receipt,
    )
    monkeypatch.setattr(migration, "_migration_state_paths", lambda: (state_directory, seal_path))
    monkeypatch.setattr(
        migration,
        "validate_machine_path",
        lambda path, *, directory: path == state_directory and directory and path.exists(),
    )
    monkeypatch.setattr(migration, "_verify_migration_state_path", Mock(return_value=seal.reader_sid))
    entries = {migration.MIGRATION_SEAL_FILE_NAME}
    monkeypatch.setattr(migration, "_migration_state_entries", lambda _path: tuple(entries))
    monkeypatch.setattr(migration, "_load_migration_seal_envelope", lambda **_kwargs: seal)
    validate_token = Mock()
    monkeypatch.setattr(migration, "_validate_sealed_migration_token", validate_token)

    assert migration.desktop_migration_state_is_partial() is True
    validate_token.assert_not_called()

    entries.add(migration.MIGRATION_TOKEN_FILE_NAME)
    assert migration.desktop_migration_state_is_partial() is True
    validate_token.assert_called_once_with(state_directory, seal)

    entries.add("fremd.txt")
    with pytest.raises(RuntimeError, match="unerwartete Einträge"):
        migration.desktop_migration_state_is_partial()

    entries.clear()
    assert migration.desktop_migration_state_is_partial() is True


@pytest.mark.parametrize("payload", [b"", b'{"schema_version":1'])
def test_partial_detection_and_cleanup_accept_interrupted_atomic_seal_scratch(
    payload: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    transaction_id = "a" * 32
    scratch = state_directory / (f"{migration.MIGRATION_SEAL_TEMP_FILE_PREFIX}{transaction_id}-{'b' * 32}.tmp")
    scratch.write_bytes(payload)
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_migration_state_paths", lambda: (state_directory, seal_path))
    monkeypatch.setattr(
        migration,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    monkeypatch.setattr(
        migration,
        "_verify_migration_state_path",
        Mock(return_value="S-1-5-21-test"),
    )

    assert migration.desktop_migration_state_is_partial() is True
    assert scratch.read_bytes() == payload

    migration.clear_desktop_migration_seal()

    assert not state_directory.exists()


def test_partial_detection_accepts_interrupted_token_and_phase_scratch_only_before_fixed_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    receipt = migration.MigrationReceipt(None, False, r"C:\App\E-Rechnungs-Pruefer.exe", None)
    base_seal, _phase = _transaction(receipt, phase=migration.MigrationPhase.ROLLBACKABLE)
    seal = migration.MigrationSeal(
        schema_version=base_seal.schema_version,
        transaction_id=base_seal.transaction_id,
        reader_sid=base_seal.reader_sid,
        token_sha256="b" * 64,
        receipt=base_seal.receipt,
    )
    seal_path.write_bytes(migration._encode_migration_seal(seal))
    token_scratch = state_directory / (
        f"{migration.MIGRATION_TOKEN_TEMP_FILE_PREFIX}{seal.transaction_id}-{'c' * 32}.tmp"
    )
    token_scratch.write_bytes(b"")
    monkeypatch.setattr(migration, "_migration_state_paths", lambda: (state_directory, seal_path))
    monkeypatch.setattr(
        migration,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    monkeypatch.setattr(
        migration,
        "_verify_migration_state_path",
        Mock(return_value=seal.reader_sid),
    )

    assert migration.desktop_migration_state_is_partial() is True

    token_scratch.unlink()
    token_path = state_directory / migration.MIGRATION_TOKEN_FILE_NAME
    token_payload = b"t" * 43 + b"\n"
    token_path.write_bytes(token_payload)
    seal = migration.MigrationSeal(
        schema_version=seal.schema_version,
        transaction_id=seal.transaction_id,
        reader_sid=seal.reader_sid,
        token_sha256=migration.hashlib.sha256(token_payload).hexdigest(),
        receipt=seal.receipt,
    )
    seal_path.write_bytes(migration._encode_migration_seal(seal))
    phase_scratch = state_directory / (
        f"{migration.MIGRATION_PHASE_TEMP_FILE_PREFIX}{seal.transaction_id}-{'d' * 32}.tmp"
    )
    phase_scratch.write_bytes(b'{"partial":')

    assert migration.desktop_migration_state_is_partial() is True


def test_partial_cleanup_never_deletes_unknown_or_corrupt_authoritative_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    seal_path.write_bytes(b"")
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_migration_state_paths", lambda: (state_directory, seal_path))
    monkeypatch.setattr(
        migration,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    monkeypatch.setattr(
        migration,
        "_verify_migration_state_path",
        Mock(return_value="S-1-5-21-test"),
    )

    with pytest.raises(RuntimeError, match="Migrationsbeleg"):
        migration.desktop_migration_state_is_partial()
    with pytest.raises(RuntimeError, match="Migrationsbeleg"):
        migration.clear_desktop_migration_seal()
    assert seal_path.read_bytes() == b""

    seal_path.unlink()
    unknown = state_directory / "fremd.txt"
    unknown.write_bytes(b"keep")
    with pytest.raises(RuntimeError, match="ohne Seal|unerwartete Einträge"):
        migration.clear_desktop_migration_seal()
    assert unknown.read_bytes() == b"keep"


def test_verify_applied_desktop_migration_uses_only_sealed_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verify = Mock()
    monkeypatch.setattr(migration, "verify_no_legacy_desktop_conflicts", verify)

    migration.verify_applied_desktop_migration()

    verify.assert_called_once_with()


def test_transaction_loader_tolerates_only_valid_bound_hard_kill_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = migration.MigrationReceipt(None, False, r"C:\App\E-Rechnungs-Pruefer.exe", None)
    seal, phase = _transaction(receipt, phase=migration.MigrationPhase.ROLLBACKABLE)
    state_directory = tmp_path / "state"
    temporary_name = f"{migration.MIGRATION_PHASE_TEMP_FILE_PREFIX}{seal.transaction_id}-{'b' * 32}.tmp"
    snapshot_name = f"{migration.PROFILE_HIVE_SNAPSHOT_DIRECTORY_PREFIX}{seal.transaction_id}-{'c' * 32}"
    temporary_phase = migration.MigrationPhaseRecord(
        schema_version=migration.MIGRATION_PHASE_SCHEMA_VERSION,
        transaction_id=seal.transaction_id,
        generation=migration.MIGRATION_PHASE_GENERATIONS[migration.MigrationPhase.SERVICE_TRANSITION],
        phase=migration.MigrationPhase.SERVICE_TRANSITION,
    )
    entries = {
        migration.MIGRATION_SEAL_FILE_NAME,
        migration.MIGRATION_PHASE_FILE_NAME,
        temporary_name,
        snapshot_name,
    }
    monkeypatch.setattr(
        migration,
        "_migration_state_paths",
        lambda: (state_directory, state_directory / migration.MIGRATION_SEAL_FILE_NAME),
    )
    monkeypatch.setattr(migration, "_load_migration_seal_envelope", lambda **_kwargs: seal)
    monkeypatch.setattr(migration, "_migration_state_entries", lambda _path: tuple(entries))
    monkeypatch.setattr(migration, "_verify_migration_state_path", Mock())
    temporary_payload = [migration._encode_migration_phase(temporary_phase)]
    monkeypatch.setattr(
        migration,
        "_read_locked_bytes",
        lambda path, **_kwargs: (
            temporary_payload[0] if path.name == temporary_name else migration._encode_migration_phase(phase)
        ),
    )
    monkeypatch.setattr(migration, "validate_machine_path", lambda path, *, directory: directory)
    validate_snapshot = Mock(return_value=())
    monkeypatch.setattr(migration, "_validate_profile_hive_recovery_tail", validate_snapshot)

    assert migration._load_migration_transaction(require_current_user=False) == (seal, phase)
    validate_snapshot.assert_called_once_with(
        state_directory / snapshot_name,
        expected_transaction_id=seal.transaction_id,
    )

    mount = f"{migration.PROFILE_AUDIT_MOUNT_PREFIX}{seal.transaction_id}_{'d' * 24}"
    monkeypatch.setattr(migration, "_profile_audit_mounts", lambda _transaction_id: (mount,))
    validate_snapshot.reset_mock()
    assert migration._load_migration_transaction(require_current_user=False) == (seal, phase)
    validate_snapshot.assert_not_called()

    temporary_payload[0] = b'{"schema_version":'
    assert migration._load_migration_transaction(require_current_user=False) == (seal, phase)

    temporary_payload[0] = b"{}"
    with pytest.raises(RuntimeError, match="Migrationsphase"):
        migration._load_migration_transaction(require_current_user=False)
    temporary_payload[0] = migration._encode_migration_phase(temporary_phase)

    entries.add("fremd.txt")
    with pytest.raises(RuntimeError, match="unerwartete Einträge"):
        migration._load_migration_transaction(require_current_user=False)


@pytest.mark.parametrize(
    "terminal_phase",
    [
        migration.MigrationPhase.SERVICE_ROLLBACK_COMPLETE,
        migration.MigrationPhase.SERVICE_COMMITTED,
    ],
)
def test_terminal_transaction_and_cleanup_tolerate_already_removed_sealed_token(
    terminal_phase: migration.MigrationPhase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    receipt = migration.MigrationReceipt(None, False, r"C:\App\E-Rechnungs-Pruefer.exe", None)
    base_seal, phase = _transaction(receipt, phase=terminal_phase)
    seal = migration.MigrationSeal(
        schema_version=base_seal.schema_version,
        transaction_id=base_seal.transaction_id,
        reader_sid=base_seal.reader_sid,
        token_sha256="b" * 64,
        receipt=base_seal.receipt,
    )
    seal_path.write_bytes(migration._encode_migration_seal(seal))
    (state_directory / migration.MIGRATION_PHASE_FILE_NAME).write_bytes(migration._encode_migration_phase(phase))
    phase_temporary = state_directory / (
        f"{migration.MIGRATION_PHASE_TEMP_FILE_PREFIX}{seal.transaction_id}-{'c' * 32}.tmp"
    )
    phase_temporary.write_bytes(b"")
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_migration_state_paths", lambda: (state_directory, seal_path))
    monkeypatch.setattr(
        migration,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    monkeypatch.setattr(
        migration,
        "_verify_migration_state_path",
        Mock(return_value=seal.reader_sid),
    )

    assert migration._load_migration_transaction(require_current_user=False) == (seal, phase)
    with pytest.raises(RuntimeError, match="nicht mehr verfügbar"):
        migration.protected_desktop_migration_token_path()

    migration.clear_desktop_migration_seal()

    assert not state_directory.exists()


@pytest.mark.parametrize(
    "nonterminal_phase",
    [
        migration.MigrationPhase.ROLLBACKABLE,
        migration.MigrationPhase.SERVICE_TRANSITION,
    ],
)
def test_nonterminal_transaction_missing_sealed_token_remains_fail_closed(
    nonterminal_phase: migration.MigrationPhase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    receipt = migration.MigrationReceipt(None, False, r"C:\App\E-Rechnungs-Pruefer.exe", None)
    base_seal, phase = _transaction(receipt, phase=nonterminal_phase)
    seal = migration.MigrationSeal(
        schema_version=base_seal.schema_version,
        transaction_id=base_seal.transaction_id,
        reader_sid=base_seal.reader_sid,
        token_sha256="b" * 64,
        receipt=base_seal.receipt,
    )
    seal_path.write_bytes(migration._encode_migration_seal(seal))
    phase_path = state_directory / migration.MIGRATION_PHASE_FILE_NAME
    phase_path.write_bytes(migration._encode_migration_phase(phase))
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_migration_state_paths", lambda: (state_directory, seal_path))
    monkeypatch.setattr(
        migration,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    monkeypatch.setattr(
        migration,
        "_verify_migration_state_path",
        Mock(return_value=seal.reader_sid),
    )

    with pytest.raises(RuntimeError, match="versiegelten Token"):
        migration._load_migration_transaction(require_current_user=False)
    with pytest.raises(RuntimeError, match="versiegelten Token"):
        migration.clear_desktop_migration_seal()

    assert seal_path.exists()
    assert phase_path.exists()


def test_corrupt_fixed_initial_phase_is_never_treated_as_partial_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    receipt = migration.MigrationReceipt(None, False, r"C:\App\E-Rechnungs-Pruefer.exe", None)
    seal, _phase = _transaction(receipt, phase=migration.MigrationPhase.ROLLBACKABLE)
    seal_path.write_bytes(migration._encode_migration_seal(seal))
    phase_path = state_directory / migration.MIGRATION_PHASE_FILE_NAME
    phase_path.write_bytes(b"")
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_migration_state_paths", lambda: (state_directory, seal_path))
    monkeypatch.setattr(
        migration,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    monkeypatch.setattr(
        migration,
        "_verify_migration_state_path",
        Mock(return_value=seal.reader_sid),
    )

    assert migration.desktop_migration_state_is_partial() is False
    with pytest.raises(RuntimeError, match="Migrationsphase"):
        migration.clear_desktop_migration_seal()

    assert seal_path.exists()
    assert phase_path.read_bytes() == b""


def test_profile_hive_recovery_tail_allows_secured_registry_support_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction_id = "e" * 32
    snapshot_directory = tmp_path / (f"{migration.PROFILE_HIVE_SNAPSHOT_DIRECTORY_PREFIX}{transaction_id}-{'f' * 32}")
    snapshot_directory.mkdir()
    monkeypatch.setattr(migration, "_verify_migration_state_path", Mock())
    verify_file = Mock()
    monkeypatch.setattr(migration, "_verify_profile_hive_support_file", verify_file)

    assert (
        migration._validate_profile_hive_recovery_tail(
            snapshot_directory,
            expected_transaction_id=transaction_id,
        )
        == ()
    )

    partial = snapshot_directory / migration.PROFILE_HIVE_SNAPSHOT_FILE_NAME
    partial.write_bytes(b"partial")
    assert migration._validate_profile_hive_recovery_tail(
        snapshot_directory,
        expected_transaction_id=transaction_id,
    ) == (partial,)
    verify_file.assert_called_once_with(partial)

    support = snapshot_directory / "NTUSER.DAT.LOG1"
    support.write_bytes(b"x")
    assert set(
        migration._validate_profile_hive_recovery_tail(
            snapshot_directory,
            expected_transaction_id=transaction_id,
        )
    ) == {partial, support}
    assert {candidate.args[0] for candidate in verify_file.call_args_list[-2:]} == {
        partial,
        support,
    }


def test_profile_audit_mount_inventory_is_read_only_and_rejects_foreign_names() -> None:
    transaction_id = "a" * 32
    expected_mount = f"{migration.PROFILE_AUDIT_MOUNT_PREFIX}{transaction_id}_{'b' * 24}"
    hku = object()

    def no_more_items() -> OSError:
        error = OSError("done")
        error.winerror = migration.ERROR_NO_MORE_ITEMS  # type: ignore[attr-defined]
        return error

    fake_winreg = SimpleNamespace(
        HKEY_USERS=hku,
        EnumKey=Mock(side_effect=["S-1-5-21-1000", expected_mount, no_more_items()]),
        UnloadKey=Mock(),
    )

    assert migration._profile_audit_mounts(transaction_id, _winreg=fake_winreg) == (expected_mount,)
    fake_winreg.UnloadKey.assert_not_called()

    foreign_mount = f"{migration.PROFILE_AUDIT_MOUNT_PREFIX}{'c' * 32}_{'d' * 24}"
    fake_winreg.EnumKey = Mock(side_effect=[foreign_mount, no_more_items()])
    with pytest.raises(RuntimeError, match="fremder oder ungültiger"):
        migration._profile_audit_mounts(transaction_id, _winreg=fake_winreg)
    fake_winreg.UnloadKey.assert_not_called()


def test_mutating_profile_audit_recovery_unloads_bound_mount_before_snapshot_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction_id = "a" * 32
    state_directory = tmp_path / "state"
    snapshot_directory = state_directory / (
        f"{migration.PROFILE_HIVE_SNAPSHOT_DIRECTORY_PREFIX}{transaction_id}-{'c' * 32}"
    )
    snapshot_directory.mkdir(parents=True)
    snapshot = snapshot_directory / migration.PROFILE_HIVE_SNAPSHOT_FILE_NAME
    snapshot.write_bytes(b"hive")
    support = snapshot_directory / "NTUSER.DAT.LOG1"
    support.write_bytes(b"log")
    mount = f"{migration.PROFILE_AUDIT_MOUNT_PREFIX}{transaction_id}_{'d' * 24}"
    hku = object()
    events: list[str] = []

    def no_more_items() -> OSError:
        error = OSError("done")
        error.winerror = migration.ERROR_NO_MORE_ITEMS  # type: ignore[attr-defined]
        return error

    fake_winreg = SimpleNamespace(
        HKEY_USERS=hku,
        EnumKey=Mock(side_effect=[mount, no_more_items()]),
        UnloadKey=Mock(side_effect=lambda _root, _name: events.append("unload")),
    )
    monkeypatch.setattr(
        migration,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    validate_directory = Mock(side_effect=lambda *_args, **_kwargs: events.append("validate-directory"))
    monkeypatch.setattr(
        migration,
        "_validate_profile_hive_recovery_directory",
        validate_directory,
    )
    validate_tail = Mock(side_effect=lambda *_args, **_kwargs: events.append("validate-files"))
    monkeypatch.setattr(migration, "_validate_profile_hive_recovery_tail", validate_tail)
    monkeypatch.setattr(
        migration,
        "_enable_registry_hive_privileges",
        Mock(side_effect=lambda: events.append("privileges")),
    )
    remove = Mock(side_effect=lambda *_args, **_kwargs: events.append("remove"))
    monkeypatch.setattr(migration, "_remove_profile_hive_snapshot", remove)

    migration._recover_orphaned_profile_audit_state(
        state_directory,
        transaction_id=transaction_id,
        winreg=fake_winreg,
    )

    assert events == [
        "validate-directory",
        "privileges",
        "unload",
        "validate-files",
        "remove",
    ]
    fake_winreg.UnloadKey.assert_called_once_with(hku, mount)
    remove.assert_called_once_with(snapshot, expected_transaction_id=transaction_id)

    unknown = state_directory / "fremd.txt"
    unknown.write_bytes(b"keep")
    fake_winreg.UnloadKey.reset_mock()
    remove.reset_mock()
    with pytest.raises(RuntimeError, match="unerwartete Einträge"):
        migration._recover_orphaned_profile_audit_state(
            state_directory,
            transaction_id=transaction_id,
            winreg=fake_winreg,
        )
    fake_winreg.UnloadKey.assert_not_called()
    remove.assert_not_called()
    assert unknown.read_bytes() == b"keep"


def test_phase_transitions_are_monotone_transaction_bound_and_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = migration.MigrationReceipt(None, False, r"C:\App\E-Rechnungs-Pruefer.exe", None)
    seal, phase = _transaction(receipt, phase=migration.MigrationPhase.ROLLBACKABLE)
    write = Mock()
    remove_temporaries = Mock()
    monkeypatch.setattr(migration, "_load_migration_transaction", lambda **_kwargs: (seal, phase))
    monkeypatch.setattr(
        migration,
        "_remove_abandoned_migration_phase_temporaries",
        remove_temporaries,
    )
    monkeypatch.setattr(migration, "_write_atomic_migration_phase", write)

    advanced = migration.advance_desktop_migration_phase(migration.MigrationPhase.SERVICE_TRANSITION)

    assert advanced == migration.MigrationPhaseRecord(
        schema_version=migration.MIGRATION_PHASE_SCHEMA_VERSION,
        transaction_id=seal.transaction_id,
        generation=1,
        phase=migration.MigrationPhase.SERVICE_TRANSITION,
    )
    remove_temporaries.assert_called_once_with(seal, phase)
    write.assert_called_once_with(advanced, reader_sid=seal.reader_sid)

    with pytest.raises(RuntimeError, match="Phasenübergang"):
        migration.advance_desktop_migration_phase(migration.MigrationPhase.SERVICE_COMMITTED)

    duplicate = (
        b'{"schema_version":1,"transaction_id":"'
        + seal.transaction_id.encode("ascii")
        + b'","generation":0,"generation":1,"phase":"rollbackable"}'
    )
    with pytest.raises(RuntimeError, match="ungültig"):
        migration._decode_migration_phase(duplicate)


def test_next_desktop_phase_discards_only_revalidated_abandoned_phase_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = migration.MigrationReceipt(None, False, r"C:\App\E-Rechnungs-Pruefer.exe", None)
    seal, current = _transaction(receipt, phase=migration.MigrationPhase.SERVICE_TRANSITION)
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    complete = state_directory / (f"{migration.MIGRATION_PHASE_TEMP_FILE_PREFIX}{seal.transaction_id}-{'b' * 32}.tmp")
    truncated = state_directory / (f"{migration.MIGRATION_PHASE_TEMP_FILE_PREFIX}{seal.transaction_id}-{'c' * 32}.tmp")
    next_phase = migration.MigrationPhaseRecord(
        schema_version=migration.MIGRATION_PHASE_SCHEMA_VERSION,
        transaction_id=seal.transaction_id,
        generation=migration.MIGRATION_PHASE_GENERATIONS[migration.MigrationPhase.SERVICE_COMMITTED],
        phase=migration.MigrationPhase.SERVICE_COMMITTED,
    )
    complete.write_bytes(migration._encode_migration_phase(next_phase))
    truncated.write_bytes(b'{"schema_version":')
    monkeypatch.setattr(
        migration,
        "_load_migration_transaction",
        lambda **_kwargs: (seal, current),
    )
    monkeypatch.setattr(
        migration,
        "_migration_state_paths",
        lambda: (state_directory, state_directory / migration.MIGRATION_SEAL_FILE_NAME),
    )
    monkeypatch.setattr(migration, "_verify_migration_state_path", Mock())

    migration._remove_abandoned_migration_phase_temporaries(seal, current)

    assert not complete.exists()
    assert not truncated.exists()


def test_phase_update_writes_a_secure_sibling_before_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    record = migration.MigrationPhaseRecord(
        schema_version=migration.MIGRATION_PHASE_SCHEMA_VERSION,
        transaction_id="a" * 32,
        generation=1,
        phase=migration.MigrationPhase.SERVICE_TRANSITION,
    )
    operations: list[str] = []

    def write_secure(path: Path, payload: bytes, *, reader_sid: str | None) -> None:
        assert reader_sid == "S-1-5-21-test"
        operations.append("write")
        path.write_bytes(payload)

    def replace(source: Path, target: Path) -> None:
        operations.append("replace")
        os.replace(source, target)

    monkeypatch.setattr(migration, "_migration_state_paths", lambda: (state_directory, seal_path))
    monkeypatch.setattr(migration, "_write_secure_migration_file", write_secure)
    monkeypatch.setattr(migration, "_atomic_replace_migration_file", replace)
    monkeypatch.setattr(migration, "_verify_migration_state_path", Mock())
    monkeypatch.setattr(migration.secrets, "token_hex", lambda _length: "b" * 32)

    migration._write_atomic_migration_phase(record, reader_sid="S-1-5-21-test")

    assert operations == ["write", "replace"]
    phase_path = state_directory / migration.MIGRATION_PHASE_FILE_NAME
    assert migration._decode_migration_phase(phase_path.read_bytes()) == record
    assert not any(path.name.endswith(".tmp") for path in state_directory.iterdir())


def test_initial_seal_and_phase_publish_only_after_secure_transaction_bound_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    receipt = migration.MigrationReceipt(None, False, r"C:\App\E-Rechnungs-Pruefer.exe", None)
    transaction_id = "a" * 32
    events: list[tuple[str, str]] = []

    def write_secure(path: Path, payload: bytes, *, reader_sid: str | None) -> None:
        events.append(("write", path.name))
        path.write_bytes(payload)

    def publish(source: Path, target: Path) -> None:
        events.append(("publish", target.name))
        source.rename(target)

    monkeypatch.setattr(migration, "_write_secure_migration_file", write_secure)
    monkeypatch.setattr(migration, "_atomic_publish_migration_file", publish)
    monkeypatch.setattr(migration, "_verify_migration_state_path", Mock())
    monkeypatch.setattr(migration, "_read_locked_bytes", lambda path, **_kwargs: path.read_bytes())
    monkeypatch.setattr(migration.secrets, "token_hex", lambda _length: "b" * 32)

    seal = migration._store_migration_seal(
        seal_path,
        receipt,
        reader_sid="S-1-5-21-test",
        transaction_id=transaction_id,
    )
    phase = migration._store_initial_migration_phase(
        state_directory,
        transaction_id=transaction_id,
        reader_sid=seal.reader_sid,
    )

    assert events == [
        (
            "write",
            f"{migration.MIGRATION_SEAL_TEMP_FILE_PREFIX}{transaction_id}-{'b' * 32}.tmp",
        ),
        ("publish", migration.MIGRATION_SEAL_FILE_NAME),
        (
            "write",
            f"{migration.MIGRATION_PHASE_TEMP_FILE_PREFIX}{transaction_id}-{'b' * 32}.tmp",
        ),
        ("publish", migration.MIGRATION_PHASE_FILE_NAME),
    ]
    assert migration._decode_migration_seal(seal_path.read_bytes()) == seal
    assert (
        migration._decode_migration_phase((state_directory / migration.MIGRATION_PHASE_FILE_NAME).read_bytes()) == phase
    )


def test_state_cleanup_rejects_unknown_root_entry_before_deleting_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader_sid = "S-1-5-21-1000"
    modules, descriptors = _fake_migration_security_modules(current_sid=reader_sid)
    state_directory = tmp_path / migration.MIGRATION_STATE_DIRECTORY_NAME
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "autostart_command": None,
                "was_running": False,
                "executable": str(tmp_path / migration.DESKTOP_EXECUTABLE_NAME),
                "disabled_executable": None,
            }
        ),
        encoding="utf-8",
    )
    descriptors[str(receipt_path)] = _private_receipt_descriptor(modules, owner_sid=reader_sid)
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_migration_windows_modules", lambda: modules)
    monkeypatch.setattr(migration, "_migration_state_paths", lambda: (state_directory, seal_path))
    monkeypatch.setattr(migration.secrets, "token_hex", lambda _length: "b" * 32)
    migration.seal_desktop_migration(receipt_path=receipt_path, token_transfer_path=None)
    unknown = state_directory / "fremd.txt"
    unknown.write_text("nicht löschen", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unerwartete Einträge"):
        migration.clear_desktop_migration_seal()

    assert seal_path.exists()
    assert unknown.read_text(encoding="utf-8") == "nicht löschen"


def test_state_cleanup_accepts_only_transaction_bound_protected_hive_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader_sid = "S-1-5-21-1000"
    transaction_id = "e" * 32
    modules, descriptors = _fake_migration_security_modules(current_sid=reader_sid)
    state_directory = tmp_path / migration.MIGRATION_STATE_DIRECTORY_NAME
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "autostart_command": None,
                "was_running": False,
                "executable": str(tmp_path / migration.DESKTOP_EXECUTABLE_NAME),
                "disabled_executable": None,
            }
        ),
        encoding="utf-8",
    )
    descriptors[str(receipt_path)] = _private_receipt_descriptor(modules, owner_sid=reader_sid)
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_migration_windows_modules", lambda: modules)
    monkeypatch.setattr(migration, "_migration_state_paths", lambda: (state_directory, seal_path))
    monkeypatch.setattr(migration.secrets, "token_hex", lambda _length: transaction_id)
    migration.seal_desktop_migration(receipt_path=receipt_path, token_transfer_path=None)

    snapshot_directory = migration._create_profile_hive_snapshot_directory(
        state_directory,
        state_reader_sid=reader_sid,
        transaction_id=transaction_id,
    )
    snapshot = snapshot_directory / migration.PROFILE_HIVE_SNAPSHOT_FILE_NAME
    attributes = migration._migration_security_attributes(directory=False, reader_sid=None)
    handle = modules[3].CreateFile(
        str(snapshot),
        modules[2].GENERIC_WRITE,
        0,
        attributes,
        modules[2].CREATE_NEW,
        modules[2].FILE_ATTRIBUTE_TEMPORARY,
        None,
    )
    modules[3].WriteFile(handle, b"synthetic-hive")
    modules[3].CloseHandle(handle)
    mount = f"{migration.PROFILE_AUDIT_MOUNT_PREFIX}{transaction_id}_{'a' * 24}"
    hku = object()
    unload = Mock()
    fake_winreg = SimpleNamespace(HKEY_USERS=hku, UnloadKey=unload)
    monkeypatch.setattr(migration, "_profile_audit_mounts", lambda _transaction_id: (mount,))
    enable_privileges = Mock()
    monkeypatch.setattr(migration, "_enable_registry_hive_privileges", enable_privileges)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    migration.clear_desktop_migration_seal()

    assert not state_directory.exists()
    enable_privileges.assert_called_once_with()
    unload.assert_called_once_with(hku, mount)


def test_offline_hive_is_copied_under_component_locks_before_registry_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader_sid = "S-1-5-21-1000"
    modules, descriptors = _fake_migration_security_modules(current_sid=reader_sid)
    state_directory = tmp_path / migration.MIGRATION_STATE_DIRECTORY_NAME
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text("{}", encoding="utf-8")
    descriptors[str(receipt_path)] = _private_receipt_descriptor(modules, owner_sid=reader_sid)
    profile = tmp_path / "profile"
    profile.mkdir()
    source = profile / "NTUSER.DAT"
    source.write_bytes(b"synthetic-offline-hive")
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_migration_windows_modules", lambda: modules)
    monkeypatch.setattr(migration, "_migration_state_paths", lambda: (state_directory, seal_path))
    transaction_id = "c" * 32
    monkeypatch.setattr(migration.secrets, "token_hex", lambda _length: "d" * 32)
    migration._prepare_migration_state(receipt_path)

    snapshot = migration._snapshot_profile_hive(
        profile,
        state_directory,
        state_reader_sid=reader_sid,
        transaction_id=transaction_id,
    )

    assert snapshot.read_bytes() == source.read_bytes()
    assert snapshot.parent.parent == state_directory
    assert snapshot.parent.name.startswith(migration.PROFILE_HIVE_SNAPSHOT_DIRECTORY_PREFIX)
    support_file = snapshot.with_name(snapshot.name + ".LOG1")
    support_file.write_bytes(b"synthetic-registry-log")
    _pywintypes, _win32api, _win32con, _win32file, win32security, ntsecuritycon = modules
    support_descriptor = _FakeDescriptor()
    support_descriptor.owner = migration.ADMINISTRATORS_SID
    support_descriptor.dacl = _FakeAcl()
    inherited_ace = 0x10
    for sid in (migration.SYSTEM_SID, migration.ADMINISTRATORS_SID):
        support_descriptor.dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS,
            inherited_ace,
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )
    descriptors[str(support_file)] = support_descriptor
    snapshot_directory = snapshot.parent
    migration._remove_profile_hive_snapshot(snapshot, expected_transaction_id=transaction_id)
    assert not snapshot.exists()
    assert not support_file.exists()
    assert not snapshot_directory.exists()

    (profile / "NTUSER.MAN").write_bytes(b"second-hive")
    with pytest.raises(RuntimeError, match="keinen eindeutig prüfbaren"):
        migration._snapshot_profile_hive(
            profile,
            state_directory,
            state_reader_sid=reader_sid,
            transaction_id=transaction_id,
        )


def test_registry_hive_privileges_are_enabled_and_token_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = Mock()
    win32security = SimpleNamespace(
        SE_BACKUP_NAME="backup",
        SE_RESTORE_NAME="restore",
        SE_PRIVILEGE_ENABLED=2,
        OpenProcessToken=Mock(return_value=token),
        LookupPrivilegeValue=Mock(side_effect=lambda _system, name: f"luid-{name}"),
        AdjustTokenPrivileges=Mock(),
    )
    monkeypatch.setitem(sys.modules, "ntsecuritycon", SimpleNamespace(TOKEN_ADJUST_PRIVILEGES=1, TOKEN_QUERY=2))
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(GetCurrentProcess=Mock(return_value="process")))
    monkeypatch.setitem(sys.modules, "win32security", win32security)

    migration._enable_registry_hive_privileges()

    win32security.AdjustTokenPrivileges.assert_called_once_with(
        token,
        False,
        [("luid-backup", 2), ("luid-restore", 2)],
    )
    token.Close.assert_called_once_with()


def test_native_process_inventory_finds_only_legacy_executable_and_closes_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = 991
    rows = iter((("other.exe", 7), (migration.DESKTOP_EXECUTABLE_NAME, 42)))
    last_error = {"value": 0}

    def populate(pointer) -> bool:
        try:
            name, process_id = next(rows)
        except StopIteration:
            last_error["value"] = migration.ERROR_NO_MORE_FILES
            return False
        entry = pointer._obj  # noqa: SLF001 - ctypes byref test adapter
        entry.szExeFile = name
        entry.th32ProcessID = process_id
        return True

    kernel32 = SimpleNamespace(
        CreateToolhelp32Snapshot=Mock(return_value=snapshot),
        Process32FirstW=Mock(side_effect=lambda _snapshot, pointer: populate(pointer)),
        Process32NextW=Mock(side_effect=lambda _snapshot, pointer: populate(pointer)),
        CloseHandle=Mock(return_value=True),
    )
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)
    monkeypatch.setattr(
        migration.ctypes,
        "get_last_error",
        lambda: last_error["value"],
        raising=False,
    )

    assert migration._running_legacy_desktop_processes() == (42,)

    kernel32.CreateToolhelp32Snapshot.assert_called_once_with(migration.TH32CS_SNAPPROCESS, 0)
    kernel32.CloseHandle.assert_called_once_with(snapshot)


def test_plan_migration_is_read_only_and_writes_the_validated_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / migration.DESKTOP_EXECUTABLE_NAME
    executable.write_bytes(b"desktop-v1.3")
    expected_command = f'"{executable}" --background'
    key = _ContextKey()
    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        KEY_SET_VALUE=2,
        REG_SZ=1,
        OpenKey=Mock(return_value=key),
        QueryValueEx=Mock(return_value=(expected_command, 1)),
        DeleteValue=Mock(),
    )
    receipt = tmp_path / "receipt.json"
    written: dict[Path, bytes] = {}

    def write_private(path: Path, payload: bytes) -> None:
        written[path] = payload
        path.write_bytes(payload)

    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(migration, "desktop_executable", lambda: executable)
    running = Mock(return_value=True)
    stop = Mock(side_effect=AssertionError("Der Plan darf den Desktop nicht stoppen."))
    monkeypatch.setattr(migration, "_desktop_backend_is_running", running)
    monkeypatch.setattr(migration, "_stop_desktop_backend", stop)
    monkeypatch.setattr(migration, "_write_private", write_private)

    migration.plan_desktop_migration(receipt_path=receipt, token_transfer_path=None)

    payload = json.loads(written[receipt])
    assert payload == {
        "autostart_command": expected_command,
        "disabled_executable": str(executable.with_name(executable.name + migration.DISABLED_SUFFIX)),
        "executable": str(executable),
        "was_running": True,
    }
    assert executable.read_bytes() == b"desktop-v1.3"
    assert not executable.with_name(executable.name + migration.DISABLED_SUFFIX).exists()
    fake_winreg.DeleteValue.assert_not_called()
    running.assert_called_once_with()
    stop.assert_not_called()


def test_plan_migration_transfers_valid_token_only_with_explicit_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _ContextKey()
    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        KEY_SET_VALUE=2,
        REG_SZ=1,
        OpenKey=Mock(return_value=key),
        QueryValueEx=Mock(side_effect=FileNotFoundError),
        DeleteValue=Mock(),
    )
    desktop_token = tmp_path / "desktop-token.txt"
    desktop_token.write_text("t" * 43 + "\n", encoding="ascii")
    receipt = tmp_path / "receipt.json"
    transfer = tmp_path / "transfer.txt"
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(migration, "desktop_token_file", lambda: desktop_token)
    monkeypatch.setattr(migration, "desktop_executable", lambda: tmp_path / migration.DESKTOP_EXECUTABLE_NAME)
    monkeypatch.setattr(migration, "_desktop_backend_is_running", Mock(return_value=False))
    monkeypatch.setattr(migration, "_write_private", lambda path, payload: path.write_bytes(payload))

    migration.plan_desktop_migration(receipt_path=receipt, token_transfer_path=transfer)

    assert transfer.read_text(encoding="ascii") == "t" * 43 + "\n"
    assert json.loads(receipt.read_text(encoding="utf-8"))["was_running"] is False
    fake_winreg.DeleteValue.assert_not_called()


def test_plan_migration_cleans_transfer_files_without_unsealed_rollback_after_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _ContextKey()
    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        KEY_SET_VALUE=2,
        REG_SZ=1,
        OpenKey=Mock(return_value=key),
        QueryValueEx=Mock(side_effect=FileNotFoundError),
    )
    desktop_token = tmp_path / "desktop-token.txt"
    desktop_token.write_text("t" * 43 + "\n", encoding="ascii")
    receipt = tmp_path / "receipt.json"
    transfer = tmp_path / "transfer.txt"
    writes = 0

    def failing_write(path: Path, payload: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            path.write_bytes(payload)
            return
        raise OSError("simulierter Schreibfehler")

    rollback = Mock(side_effect=AssertionError("Ein read-only Plan benötigt keinen unversiegelten Rollback."))
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(migration, "desktop_token_file", lambda: desktop_token)
    monkeypatch.setattr(migration, "desktop_executable", lambda: tmp_path / migration.DESKTOP_EXECUTABLE_NAME)
    monkeypatch.setattr(migration, "_desktop_backend_is_running", Mock(return_value=False))
    monkeypatch.setattr(migration, "_write_private", failing_write)
    monkeypatch.setattr(migration, "rollback_desktop_migration", rollback)

    with pytest.raises(OSError, match="Schreibfehler"):
        migration.plan_desktop_migration(receipt_path=receipt, token_transfer_path=transfer)

    rollback.assert_not_called()
    assert not receipt.exists()
    assert not transfer.exists()


def test_sealed_rollback_is_idempotent_and_restores_autostart_and_running_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / migration.DESKTOP_EXECUTABLE_NAME
    executable.write_bytes(b"test")
    command = f'"{executable}" --background'
    receipt = migration.MigrationReceipt(
        autostart_command=command,
        disabled_executable=str(executable.with_name(executable.name + migration.DISABLED_SUFFIX)),
        was_running=True,
        executable=str(executable),
    )
    disabled = executable.with_name(executable.name + migration.DISABLED_SUFFIX)
    executable.replace(disabled)
    key = _ContextKey()
    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        KEY_SET_VALUE=2,
        REG_SZ=1,
        OpenKey=Mock(return_value=key),
        CreateKeyEx=Mock(return_value=key),
        QueryValueEx=Mock(side_effect=FileNotFoundError),
        SetValueEx=Mock(),
    )
    restart = Mock()
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(migration, "desktop_executable", lambda: executable)
    monkeypatch.setattr(migration, "_restart_desktop", restart)
    monkeypatch.setattr(migration, "_desktop_backend_is_running", Mock(return_value=False))
    runtime_proof = Mock(return_value=True)
    monkeypatch.setattr(migration, "_desktop_runtime_has_start_proof", runtime_proof)
    monkeypatch.setattr(
        migration,
        "_load_migration_transaction",
        lambda **_kwargs: _transaction(receipt, phase=migration.MigrationPhase.ROLLBACKABLE),
    )

    migration.rollback_desktop_migration(require_seal=True)

    fake_winreg.SetValueEx.assert_called_once_with(
        key,
        migration.AUTOSTART_VALUE_NAME,
        0,
        fake_winreg.REG_SZ,
        command,
    )
    restart.assert_called_once_with(executable)
    assert executable.read_bytes() == b"test"
    assert not disabled.exists()

    fake_winreg.QueryValueEx.side_effect = None
    fake_winreg.QueryValueEx.return_value = (command, fake_winreg.REG_SZ)
    monkeypatch.setattr(migration, "_desktop_backend_is_running", Mock(return_value=True))
    migration.rollback_desktop_migration(require_seal=True)
    assert restart.call_count == 1
    runtime_proof.assert_called_once_with()


def test_rollback_retry_rejects_mutex_only_previous_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / migration.DESKTOP_EXECUTABLE_NAME
    executable.write_bytes(b"restored")
    disabled = executable.with_name(executable.name + migration.DISABLED_SUFFIX)
    receipt = migration.MigrationReceipt(
        autostart_command=None,
        disabled_executable=str(disabled),
        was_running=True,
        executable=str(executable),
    )
    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        OpenKey=Mock(side_effect=FileNotFoundError),
    )
    restart = Mock()
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(
        migration,
        "_load_migration_transaction",
        lambda **_kwargs: _transaction(receipt, phase=migration.MigrationPhase.ROLLBACKABLE),
    )
    monkeypatch.setattr(migration, "_desktop_backend_is_running", Mock(return_value=True))
    monkeypatch.setattr(migration, "_desktop_runtime_has_start_proof", Mock(return_value=False))
    monkeypatch.setattr(migration, "_restart_desktop", restart)

    with pytest.raises(RuntimeError, match="keinen belastbaren Startnachweis"):
        migration.rollback_desktop_migration(require_seal=True)

    restart.assert_not_called()
    assert executable.read_bytes() == b"restored"
    assert not disabled.exists()


def test_commit_migration_removes_the_quarantined_legacy_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / migration.DESKTOP_EXECUTABLE_NAME
    disabled = executable.with_name(executable.name + migration.DISABLED_SUFFIX)
    disabled.write_bytes(b"desktop-v1.3")
    receipt = migration.MigrationReceipt(
        autostart_command=None,
        disabled_executable=str(disabled),
        was_running=False,
        executable=str(executable),
    )
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "desktop_executable", Mock(side_effect=AssertionError("mutable HKCU read")))
    monkeypatch.setattr(
        migration,
        "_load_migration_transaction",
        lambda **_kwargs: _transaction(receipt, phase=migration.MigrationPhase.SERVICE_COMMITTED),
    )
    monkeypatch.setattr(migration, "_desktop_backend_is_running", Mock(return_value=False))
    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        OpenKey=Mock(side_effect=FileNotFoundError),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    migration.commit_desktop_migration(require_seal=True)
    migration.commit_desktop_migration(require_seal=True)

    assert not disabled.exists()


def test_sealed_rollback_uses_receipt_bound_path_without_rereading_hkcu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / migration.DESKTOP_EXECUTABLE_NAME
    disabled = executable.with_name(executable.name + migration.DISABLED_SUFFIX)
    disabled.write_bytes(b"desktop-v1.3")
    receipt = migration.MigrationReceipt(None, False, str(executable), str(disabled))
    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        OpenKey=Mock(side_effect=FileNotFoundError),
    )
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(
        migration,
        "_load_migration_transaction",
        lambda **_kwargs: _transaction(receipt, phase=migration.MigrationPhase.ROLLBACKABLE),
    )
    monkeypatch.setattr(migration, "desktop_executable", Mock(side_effect=AssertionError("mutable HKCU read")))
    monkeypatch.setattr(migration, "_desktop_backend_is_running", Mock(return_value=False))

    migration.rollback_desktop_migration(tmp_path / "attacker-receipt.json", require_seal=True)

    assert executable.read_bytes() == b"desktop-v1.3"
    assert not disabled.exists()


def test_rollback_rejects_unsealed_receipts_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"unexpected":true}', encoding="utf-8")
    disabled = tmp_path / (migration.DESKTOP_EXECUTABLE_NAME + migration.DISABLED_SUFFIX)
    disabled.write_bytes(b"legacy")
    monkeypatch.setattr(migration.sys, "platform", "win32")

    with pytest.raises(RuntimeError, match="geschützten"):
        migration.rollback_desktop_migration(receipt, require_seal=False)

    assert disabled.read_bytes() == b"legacy"


@pytest.mark.parametrize(
    ("operation", "phase"),
    [
        ("rollback", migration.MigrationPhase.SERVICE_COMMITTED),
        ("commit", migration.MigrationPhase.ROLLBACKABLE),
    ],
)
def test_desktop_terminal_actions_reject_the_wrong_phase_before_mutation(
    operation: str,
    phase: migration.MigrationPhase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / migration.DESKTOP_EXECUTABLE_NAME
    disabled = executable.with_name(executable.name + migration.DISABLED_SUFFIX)
    disabled.write_bytes(b"legacy")
    receipt = migration.MigrationReceipt(None, False, str(executable), str(disabled))
    fake_winreg = SimpleNamespace()
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(
        migration,
        "_load_migration_transaction",
        lambda **_kwargs: _transaction(receipt, phase=phase),
    )

    with pytest.raises(RuntimeError, match="Phase|Dienst-Commit"):
        if operation == "rollback":
            migration.rollback_desktop_migration(require_seal=True)
        else:
            migration.commit_desktop_migration(require_seal=True)

    assert disabled.read_bytes() == b"legacy"
    assert not executable.exists()


def test_restart_desktop_requires_exact_existing_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / migration.DESKTOP_EXECUTABLE_NAME
    with pytest.raises(RuntimeError, match="nicht mehr vorhanden"):
        migration._restart_desktop(executable)

    executable.write_bytes(b"test")
    process = Mock(pid=1234)
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr(migration.subprocess, "Popen", popen)
    monkeypatch.setattr(migration, "_desktop_runtime_path", lambda: tmp_path / "runtime.json")
    migration._restart_desktop(
        executable,
        _runtime_reader=lambda _path: (process.pid, 8765),
        _health_probe=lambda _port: True,
        _mutex_probe=lambda: True,
    )

    popen.assert_called_once_with(
        [str(executable), "--background"],
        close_fds=True,
        creationflags=getattr(migration.subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_existing_desktop_start_proof_binds_runtime_pid_port_and_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_path = tmp_path / "runtime.json"
    read_runtime = Mock(return_value=(4321, 8765))
    running_processes = Mock(return_value=(1234, 4321))
    health = Mock(return_value=True)
    monkeypatch.setattr(migration, "_desktop_runtime_path", lambda: runtime_path)
    monkeypatch.setattr(migration, "_read_desktop_runtime_identity", read_runtime)
    monkeypatch.setattr(migration, "_running_legacy_desktop_processes", running_processes)
    monkeypatch.setattr(migration, "_desktop_health_is_ready", health)

    assert migration._desktop_runtime_has_start_proof() is True
    read_runtime.assert_called_once_with(runtime_path)
    running_processes.assert_called_once_with()
    health.assert_called_once_with(8765)

    running_processes.return_value = (1234,)
    health.reset_mock()
    assert migration._desktop_runtime_has_start_proof() is False
    health.assert_not_called()

    running_processes.return_value = (4321,)
    health.return_value = False
    assert migration._desktop_runtime_has_start_proof() is False


def test_restart_desktop_requires_matching_runtime_mutex_and_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / migration.DESKTOP_EXECUTABLE_NAME
    executable.write_bytes(b"test")
    runtime_path = tmp_path / "runtime.json"
    monkeypatch.setattr(migration, "_desktop_runtime_path", lambda: runtime_path)
    process = Mock(pid=4321)
    process.poll.return_value = None
    health = Mock(return_value=True)
    mutex = Mock(return_value=True)

    migration._restart_desktop(
        executable,
        _popen=Mock(return_value=process),
        _runtime_reader=Mock(return_value=(process.pid, 8765)),
        _health_probe=health,
        _mutex_probe=mutex,
        _monotonic=Mock(side_effect=[0.0, 0.0]),
        _sleep=Mock(),
    )

    health.assert_called_once_with(8765)
    mutex.assert_called_once_with()

    runtime_path.write_text(
        json.dumps({"pid": process.pid, "port": 8765, "token": "t" * 43}),
        encoding="utf-8",
    )
    assert migration._read_desktop_runtime_identity(runtime_path) == (process.pid, 8765)


def test_restart_desktop_fails_on_early_exit_and_stale_or_unhealthy_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / migration.DESKTOP_EXECUTABLE_NAME
    executable.write_bytes(b"test")
    monkeypatch.setattr(migration, "_desktop_runtime_path", lambda: tmp_path / "runtime.json")
    exited = Mock(pid=100)
    exited.poll.return_value = 7
    with pytest.raises(RuntimeError, match="vor dem Startnachweis beendet"):
        migration._restart_desktop(
            executable,
            _popen=Mock(return_value=exited),
            _runtime_reader=Mock(),
            _monotonic=Mock(side_effect=[0.0, 0.0]),
            _sleep=Mock(),
        )

    for runtime, health, mutex in (
        ((999, 8765), True, True),
        ((100, 8765), False, True),
        ((100, 8765), True, False),
    ):
        running = Mock(pid=100)
        running.poll.return_value = None
        with pytest.raises(RuntimeError, match="nicht rechtzeitig"):
            migration._restart_desktop(
                executable,
                timeout_seconds=0.5,
                poll_seconds=0,
                _popen=Mock(return_value=running),
                _runtime_reader=Mock(return_value=runtime),
                _health_probe=Mock(return_value=health),
                _mutex_probe=Mock(return_value=mutex),
                _monotonic=Mock(side_effect=[0.0, 0.0, 1.0]),
                _sleep=Mock(),
            )


def test_machine_inventory_allows_only_current_users_quarantined_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = tmp_path / "current" / migration.DESKTOP_EXECUTABLE_NAME
    other = tmp_path / "other" / migration.DESKTOP_EXECUTABLE_NAME
    current.parent.mkdir()
    other.parent.mkdir()
    current.with_name(current.name + migration.DISABLED_SUFFIX).write_bytes(b"quarantine")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "autostart_command": None,
                "disabled_executable": str(current.with_name(current.name + migration.DISABLED_SUFFIX)),
                "was_running": False,
                "executable": str(current),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(
        migration,
        "desktop_executable",
        lambda: tmp_path / "elevated-admin-profile" / migration.DESKTOP_EXECUTABLE_NAME,
    )
    monkeypatch.setattr(migration, "_running_legacy_desktop_processes", lambda: ())
    monkeypatch.setattr(migration, "_profile_installation_candidates", lambda **_kwargs: (current, other))
    sealed_receipt = migration.MigrationReceipt(
        autostart_command=None,
        disabled_executable=str(current.with_name(current.name + migration.DISABLED_SUFFIX)),
        was_running=False,
        executable=str(current),
    )
    monkeypatch.setattr(
        migration,
        "_load_migration_transaction",
        lambda **_kwargs: _transaction(
            sealed_receipt,
            phase=migration.MigrationPhase.ROLLBACKABLE,
        ),
    )
    store_seal = Mock()
    monkeypatch.setattr(migration, "_store_migration_seal", store_seal)
    monkeypatch.setattr(
        migration,
        "_migration_state_paths",
        lambda: (tmp_path / "state", tmp_path / "state" / migration.MIGRATION_SEAL_FILE_NAME),
    )

    migration.verify_no_legacy_desktop_conflicts(receipt)

    store_seal.assert_not_called()

    other.write_bytes(b"legacy")
    with pytest.raises(RuntimeError, match="weiteren Benutzerprofil"):
        migration.verify_no_legacy_desktop_conflicts(receipt)


def test_machine_inventory_rejects_every_running_legacy_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_running_legacy_desktop_processes", lambda: (42, 91))
    monkeypatch.setattr(migration, "_profile_installation_candidates", lambda **_kwargs: ())
    monkeypatch.setattr(
        migration,
        "_load_migration_transaction",
        lambda **_kwargs: _transaction(
            migration.MigrationReceipt(None, False, str(tmp_path / migration.DESKTOP_EXECUTABLE_NAME), None),
            phase=migration.MigrationPhase.ROLLBACKABLE,
        ),
    )

    with pytest.raises(RuntimeError, match="42, 91"):
        migration.verify_no_legacy_desktop_conflicts(tmp_path / "receipt.json")


def test_machine_inventory_rejects_a_receipt_that_hides_its_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / migration.DESKTOP_EXECUTABLE_NAME
    executable.with_name(executable.name + migration.DISABLED_SUFFIX).write_bytes(b"quarantine")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "autostart_command": None,
                "disabled_executable": None,
                "was_running": False,
                "executable": str(executable),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_running_legacy_desktop_processes", lambda: ())
    monkeypatch.setattr(
        migration,
        "_load_migration_transaction",
        lambda **_kwargs: _transaction(
            migration.MigrationReceipt(None, False, str(executable), None),
            phase=migration.MigrationPhase.ROLLBACKABLE,
        ),
    )

    with pytest.raises(RuntimeError, match="verschweigt"):
        migration.verify_no_legacy_desktop_conflicts(receipt)


def test_machine_inventory_includes_entra_profiles_and_excludes_only_system_profiles() -> None:
    assert migration._profile_sid_is_in_scope("S-1-5-21-1-2-3-1001")
    assert migration._profile_sid_is_in_scope("S-1-12-1-111-222-333-444")
    for sid in migration.SYSTEM_PROFILE_SIDS:
        assert not migration._profile_sid_is_in_scope(sid)


def test_profile_autostart_inventory_is_fail_closed() -> None:
    key = _ContextKey()
    fake_winreg = SimpleNamespace(
        KEY_QUERY_VALUE=1,
        REG_SZ=1,
        OpenKey=Mock(return_value=key),
        QueryValueEx=Mock(return_value=(r'"D:\Custom\E-Rechnungs-Pruefer.exe" --background', 1)),
    )

    assert migration._registered_autostart(object(), winreg=fake_winreg) is not None
    fake_winreg.QueryValueEx.return_value = ("", 1)
    with pytest.raises(RuntimeError, match="ungültigen Wert"):
        migration._registered_autostart(object(), winreg=fake_winreg)


def test_profile_inventory_loads_and_releases_an_offline_entra_hive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "S-1-12-1-111-222-333-444"
    profile_path = tmp_path / "entra-profile"
    profile_path.mkdir()
    (profile_path / "NTUSER.DAT").write_bytes(b"synthetic registry hive")
    snapshot_directory = tmp_path / "protected-state"
    snapshot_directory.mkdir()
    snapshot = snapshot_directory / "profile-hive.dat"
    snapshot.write_bytes(b"protected synthetic registry hive")
    profile_list = _NamedContextKey("profile-list")
    profile = _NamedContextKey("profile")
    hive = _NamedContextKey("hive")
    hklm = object()
    hku = object()
    loaded_mounts: list[str] = []

    def no_more_items() -> OSError:
        error = OSError("done")
        error.winerror = migration.ERROR_NO_MORE_ITEMS  # type: ignore[attr-defined]
        return error

    def open_key(root, subkey, _reserved=0, _access=0):
        if root is hklm and subkey == migration.PROFILE_LIST_KEY:
            return profile_list
        if root is profile_list and subkey == sid:
            return profile
        if root is hku and subkey == sid:
            raise FileNotFoundError
        if root is hku and subkey in loaded_mounts:
            return hive
        if root is hive and subkey in {migration.DESKTOP_UNINSTALL_KEY, migration.AUTOSTART_KEY}:
            raise FileNotFoundError
        raise AssertionError((root, subkey))

    def query_value(key, name):
        if key is profile and name == "ProfileImagePath":
            return str(profile_path), 2
        raise AssertionError((key, name))

    def load_key(root, mount, hive_path):
        assert root is hku
        assert Path(hive_path) == snapshot
        loaded_mounts.append(mount)

    fake_winreg = SimpleNamespace(
        HKEY_LOCAL_MACHINE=hklm,
        HKEY_USERS=hku,
        KEY_QUERY_VALUE=1,
        KEY_ENUMERATE_SUB_KEYS=8,
        REG_SZ=1,
        REG_EXPAND_SZ=2,
        OpenKey=Mock(side_effect=open_key),
        EnumKey=Mock(side_effect=[sid, no_more_items()]),
        QueryValueEx=Mock(side_effect=query_value),
        LoadKey=Mock(side_effect=load_key),
        UnloadKey=Mock(),
    )
    monkeypatch.setattr(migration.sys, "platform", "win32")
    operation_order: list[str] = []
    enable_privileges = Mock(side_effect=lambda: operation_order.append("privileges"))

    def snapshot_hive(*_args, **_kwargs):
        operation_order.append("snapshot")
        return snapshot

    monkeypatch.setattr(migration, "_enable_registry_hive_privileges", enable_privileges)
    monkeypatch.setattr(migration, "_snapshot_profile_hive", Mock(side_effect=snapshot_hive))
    remove_snapshot = Mock()
    monkeypatch.setattr(migration, "_remove_profile_hive_snapshot", remove_snapshot)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert migration._profile_installation_candidates(
        snapshot_directory=snapshot_directory,
        state_reader_sid="S-1-5-21-test",
    ) == (
        profile_path
        / "AppData"
        / "Local"
        / "Programs"
        / migration.DESKTOP_INSTALL_DIRECTORY_NAME
        / migration.DESKTOP_EXECUTABLE_NAME,
    )
    fake_winreg.LoadKey.assert_called_once()
    fake_winreg.UnloadKey.assert_called_once_with(hku, loaded_mounts[0])
    remove_snapshot.assert_called_once_with(snapshot, expected_transaction_id="0" * 32)
    assert operation_order[:2] == ["privileges", "snapshot"]
    assert loaded_mounts[0].startswith("ERechnungsPrueferAudit_")


def test_profile_inventory_fails_closed_without_a_loadable_user_hive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "S-1-5-21-1-2-3-1001"
    profile_path = tmp_path / "incomplete-profile"
    profile_path.mkdir()
    profile_list = _NamedContextKey("profile-list")
    profile = _NamedContextKey("profile")
    hklm = object()
    hku = object()

    def no_more_items() -> OSError:
        error = OSError("done")
        error.winerror = migration.ERROR_NO_MORE_ITEMS  # type: ignore[attr-defined]
        return error

    def open_key(root, subkey, _reserved=0, _access=0):
        if root is hklm and subkey == migration.PROFILE_LIST_KEY:
            return profile_list
        if root is profile_list and subkey == sid:
            return profile
        if root is hku and subkey == sid:
            raise FileNotFoundError
        raise AssertionError((root, subkey))

    fake_winreg = SimpleNamespace(
        HKEY_LOCAL_MACHINE=hklm,
        HKEY_USERS=hku,
        KEY_QUERY_VALUE=1,
        KEY_ENUMERATE_SUB_KEYS=8,
        REG_SZ=1,
        REG_EXPAND_SZ=2,
        OpenKey=Mock(side_effect=open_key),
        EnumKey=Mock(side_effect=[sid, no_more_items()]),
        QueryValueEx=Mock(return_value=(str(profile_path), 2)),
    )
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_enable_registry_hive_privileges", Mock())
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    with pytest.raises(RuntimeError, match="keinen eindeutig prüfbaren NTUSER-Hive"):
        migration._profile_installation_candidates(
            snapshot_directory=tmp_path / "protected-state",
            state_reader_sid="S-1-5-21-test",
        )


def _install_fake_kernel32(
    monkeypatch: pytest.MonkeyPatch,
    *,
    open_mutex: Mock,
    last_error: Mock,
    open_event: Mock | None = None,
    set_event: Mock | None = None,
    close_handle: Mock | None = None,
) -> SimpleNamespace:
    kernel32 = SimpleNamespace(
        OpenMutexW=open_mutex,
        OpenEventW=open_event or Mock(),
        SetEvent=set_event or Mock(return_value=True),
        CloseHandle=close_handle or Mock(return_value=True),
    )
    monkeypatch.setattr(migration.ctypes, "WinDLL", Mock(return_value=kernel32), raising=False)
    monkeypatch.setattr(migration.ctypes, "get_last_error", last_error, raising=False)
    return kernel32


def test_desktop_mutex_probe_distinguishes_absence_errors_and_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_mutex = Mock(return_value=None)
    last_error = Mock(return_value=migration.ERROR_FILE_NOT_FOUND)
    kernel32 = _install_fake_kernel32(
        monkeypatch,
        open_mutex=open_mutex,
        last_error=last_error,
    )

    assert migration._desktop_backend_is_running() is False

    last_error.return_value = 5
    with pytest.raises(OSError, match="Mutex konnte nicht geprüft"):
        migration._desktop_backend_is_running()

    open_mutex.return_value = 73
    kernel32.CloseHandle.return_value = True
    assert migration._desktop_backend_is_running() is True

    kernel32.CloseHandle.return_value = False
    last_error.return_value = 6
    with pytest.raises(OSError, match="nicht freigegeben"):
        migration._desktop_backend_is_running()


def test_controlled_desktop_stop_handles_every_native_failure_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    last_error = Mock(return_value=migration.ERROR_FILE_NOT_FOUND)
    open_mutex = Mock(return_value=None)
    kernel32 = _install_fake_kernel32(
        monkeypatch,
        open_mutex=open_mutex,
        last_error=last_error,
    )

    assert migration._stop_desktop_backend() is False

    last_error.return_value = 5
    with pytest.raises(OSError, match="Mutex konnte nicht geprüft"):
        migration._stop_desktop_backend()

    open_mutex.return_value = 41
    kernel32.OpenEventW.return_value = None
    with pytest.raises(RuntimeError, match="kontrollierte Beenden"):
        migration._stop_desktop_backend()
    kernel32.CloseHandle.assert_called_with(41)

    kernel32.OpenEventW.return_value = 42
    kernel32.SetEvent.return_value = False
    last_error.return_value = 87
    with pytest.raises(OSError, match="Shutdown-Ereignis"):
        migration._stop_desktop_backend()
    assert kernel32.CloseHandle.call_args_list[-2:] == [call(42), call(41)]


def test_controlled_desktop_stop_retries_until_mutex_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    last_error = Mock(return_value=migration.ERROR_FILE_NOT_FOUND)
    open_mutex = Mock(side_effect=[51, 52, None])
    close_handle = Mock(return_value=True)
    kernel32 = _install_fake_kernel32(
        monkeypatch,
        open_mutex=open_mutex,
        last_error=last_error,
        open_event=Mock(return_value=53),
        set_event=Mock(return_value=True),
        close_handle=close_handle,
    )
    monkeypatch.setattr(migration.time, "monotonic", Mock(side_effect=[0.0, 0.0, 0.1]))
    sleep = Mock()
    monkeypatch.setattr(migration.time, "sleep", sleep)

    assert migration._stop_desktop_backend() is True

    kernel32.SetEvent.assert_called_once_with(53)
    assert close_handle.call_args_list == [call(51), call(52), call(53)]
    sleep.assert_called_once_with(0.1)


def test_controlled_desktop_stop_fails_closed_on_retry_error_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    last_error = Mock(side_effect=[5])
    open_mutex = Mock(side_effect=[61, None])
    _install_fake_kernel32(
        monkeypatch,
        open_mutex=open_mutex,
        last_error=last_error,
        open_event=Mock(return_value=62),
    )
    monkeypatch.setattr(migration.time, "monotonic", Mock(side_effect=[0.0, 0.0]))

    with pytest.raises(OSError, match="beim Beenden nicht geprüft"):
        migration._stop_desktop_backend()

    open_mutex = Mock(side_effect=[71, 72])
    _install_fake_kernel32(
        monkeypatch,
        open_mutex=open_mutex,
        last_error=Mock(return_value=0),
        open_event=Mock(return_value=73),
    )
    monkeypatch.setattr(migration.time, "monotonic", Mock(side_effect=[0.0, 0.0, 31.0]))
    monkeypatch.setattr(migration.time, "sleep", Mock())

    with pytest.raises(RuntimeError, match="nicht innerhalb von 30 Sekunden"):
        migration._stop_desktop_backend()


def test_transfer_security_attributes_bind_only_system_admin_and_current_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _FakeToken()
    token.Close = Mock()  # type: ignore[method-assign]
    win32security = SimpleNamespace(
        ACL=_FakeAcl,
        ACL_REVISION=2,
        TOKEN_QUERY=8,
        TokenUser=1,
        SE_DACL_PROTECTED=0x1000,
        SECURITY_DESCRIPTOR=_FakeDescriptor,
        OpenProcessToken=Mock(return_value=token),
        GetTokenInformation=Mock(return_value=("S-1-5-21-1000",)),
        ConvertStringSidToSid=lambda value: value,
    )
    pywintypes = SimpleNamespace(SECURITY_ATTRIBUTES=_FakeSecurityAttributes)
    win32api = SimpleNamespace(GetCurrentProcess=Mock(return_value=object()))
    win32con = SimpleNamespace()
    win32file = SimpleNamespace()
    ntsecuritycon = SimpleNamespace(FILE_ALL_ACCESS=0x1F01FF)
    for name, module in (
        ("pywintypes", pywintypes),
        ("win32api", win32api),
        ("win32con", win32con),
        ("win32file", win32file),
        ("win32security", win32security),
        ("ntsecuritycon", ntsecuritycon),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    returned_con, returned_file, attributes, descriptor = migration._transfer_security_attributes()

    assert returned_con is win32con
    assert returned_file is win32file
    assert attributes.SECURITY_DESCRIPTOR is descriptor
    assert descriptor.control == win32security.SE_DACL_PROTECTED
    assert descriptor.dacl is not None
    assert [ace[2] for ace in descriptor.dacl.aces] == [
        migration.SYSTEM_SID,
        migration.ADMINISTRATORS_SID,
        "S-1-5-21-1000",
    ]
    token.Close.assert_called_once_with()


def test_windows_private_write_flushes_and_removes_partial_file_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "transfer" / "receipt.json"
    handle = object()

    def create_file(path: str, *_args) -> object:
        Path(path).write_bytes(b"")
        return handle

    def write_file(_handle: object, payload: bytes) -> None:
        target.write_bytes(payload)

    win32file = SimpleNamespace(
        CreateFile=Mock(side_effect=create_file),
        WriteFile=Mock(side_effect=write_file),
        FlushFileBuffers=Mock(),
        CloseHandle=Mock(),
    )
    win32con = SimpleNamespace(
        GENERIC_WRITE=1,
        CREATE_NEW=2,
        FILE_ATTRIBUTE_TEMPORARY=4,
    )
    attributes = object()
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(
        migration,
        "_transfer_security_attributes",
        Mock(return_value=(win32con, win32file, attributes, object())),
    )

    migration._write_private(target, b"sealed")

    assert target.read_bytes() == b"sealed"
    win32file.FlushFileBuffers.assert_called_once_with(handle)
    win32file.CloseHandle.assert_called_once_with(handle)

    target.unlink()
    win32file.WriteFile.side_effect = OSError("disk full")
    with pytest.raises(OSError, match="disk full"):
        migration._write_private(target, b"partial")
    assert not target.exists()
    assert win32file.CloseHandle.call_count == 2


@pytest.mark.parametrize(
    "error_code",
    [migration.ERROR_FILE_EXISTS, migration.ERROR_ALREADY_EXISTS],
)
def test_windows_private_write_normalizes_existing_file_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_code: int,
) -> None:
    target = tmp_path / "token.txt"
    target.write_bytes(b"sealed")
    error = OSError(error_code, "CreateFile", "The file exists.")
    error.winerror = error_code  # type: ignore[attr-defined]
    win32file = SimpleNamespace(CreateFile=Mock(side_effect=error))
    win32con = SimpleNamespace(
        GENERIC_WRITE=1,
        CREATE_NEW=2,
        FILE_ATTRIBUTE_TEMPORARY=4,
    )
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(
        migration,
        "_transfer_security_attributes",
        Mock(return_value=(win32con, win32file, object(), object())),
    )

    with pytest.raises(FileExistsError) as exc_info:
        migration._write_private(target, b"overwrite")

    assert exc_info.value.filename == str(target)
    assert target.read_bytes() == b"sealed"


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"pid":1,"port":8000}',
        b'{"pid":true,"port":8000,"token":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
        b'{"pid":1,"port":8000,"token":12}',
        b'{"pid":1,"port":8000,"token":"short"}',
        b'{"pid":0,"port":8000,"token":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
        b'{"pid":1,"port":65536,"token":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
    ],
)
def test_desktop_runtime_identity_rejects_untrusted_or_stale_payloads(
    payload: bytes,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_bytes(payload)

    assert migration._read_desktop_runtime_identity(runtime) is None


def test_desktop_start_proof_and_health_fail_without_a_runtime_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health_is_ready = Mock(return_value=True)
    monkeypatch.setattr(
        "app.server_runtime.health_is_ready",
        health_is_ready,
    )
    assert migration._desktop_health_is_ready(8765) is True
    health_is_ready.assert_called_once_with(8765, timeout=0.5)

    monkeypatch.setattr(migration, "_desktop_runtime_path", lambda: tmp_path / "missing-runtime.json")
    monkeypatch.setattr(migration, "_read_desktop_runtime_identity", Mock(return_value=None))
    running_processes = Mock()
    monkeypatch.setattr(migration, "_running_legacy_desktop_processes", running_processes)

    assert migration._desktop_runtime_has_start_proof() is False
    running_processes.assert_not_called()


def test_restart_desktop_rejects_invalid_deadline_and_detects_exit_at_deadline(
    tmp_path: Path,
) -> None:
    executable = tmp_path / migration.DESKTOP_EXECUTABLE_NAME
    executable.write_bytes(b"legacy")

    with pytest.raises(RuntimeError, match="ungültiges Zeitlimit"):
        migration._restart_desktop(executable, timeout_seconds=0)
    with pytest.raises(RuntimeError, match="ungültiges Zeitlimit"):
        migration._restart_desktop(executable, poll_seconds=-0.1)

    process = Mock(pid=1234)
    process.poll.return_value = 9
    with pytest.raises(RuntimeError, match="vor dem Startnachweis beendet"):
        migration._restart_desktop(
            executable,
            timeout_seconds=0.5,
            _popen=Mock(return_value=process),
            _runtime_reader=Mock(return_value=None),
            _monotonic=Mock(side_effect=[0.0, 1.0]),
            _sleep=Mock(),
        )


def test_atomic_initial_publish_failure_revalidates_and_removes_only_its_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction_id = "a" * 32
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    target = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    temporary = state_directory / (f"{migration.MIGRATION_SEAL_TEMP_FILE_PREFIX}{transaction_id}-{'b' * 32}.tmp")

    def write_secure(path: Path, payload: bytes, *, reader_sid: str | None) -> None:
        assert reader_sid == "S-1-5-21-test"
        path.write_bytes(payload)

    verify = Mock()
    monkeypatch.setattr(migration.secrets, "token_hex", lambda _length: "b" * 32)
    monkeypatch.setattr(migration, "_write_secure_migration_file", write_secure)
    monkeypatch.setattr(
        migration,
        "_atomic_publish_migration_file",
        Mock(side_effect=OSError("publish failed")),
    )
    monkeypatch.setattr(
        migration,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    monkeypatch.setattr(migration, "_verify_migration_state_path", verify)

    with pytest.raises(OSError, match="publish failed"):
        migration._write_atomic_initial_migration_file(
            target,
            b"payload",
            transaction_id=transaction_id,
            temporary_prefix=migration.MIGRATION_SEAL_TEMP_FILE_PREFIX,
            reader_sid="S-1-5-21-test",
            maximum_bytes=64,
            description="Testdatei",
        )

    assert not temporary.exists()
    verify.assert_called_once_with(
        temporary,
        directory=False,
        reader_required=True,
        expected_reader_sid="S-1-5-21-test",
    )

    with pytest.raises(RuntimeError, match="Transaktionsbindung"):
        migration._write_atomic_initial_migration_file(
            target,
            b"payload",
            transaction_id="INVALID",
            temporary_prefix=migration.MIGRATION_SEAL_TEMP_FILE_PREFIX,
            reader_sid="S-1-5-21-test",
            maximum_bytes=64,
            description="Testdatei",
        )


def test_atomic_initial_publish_detects_tampering_after_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction_id = "c" * 32
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    target = state_directory / migration.MIGRATION_PHASE_FILE_NAME

    def write_secure(path: Path, payload: bytes, *, reader_sid: str | None) -> None:
        path.write_bytes(payload)

    monkeypatch.setattr(migration.secrets, "token_hex", lambda _length: "d" * 32)
    monkeypatch.setattr(migration, "_write_secure_migration_file", write_secure)
    monkeypatch.setattr(
        migration, "_atomic_publish_migration_file", lambda source, destination: source.replace(destination)
    )
    monkeypatch.setattr(migration, "_verify_migration_state_path", Mock())
    monkeypatch.setattr(migration, "_read_locked_bytes", Mock(return_value=b"tampered"))

    with pytest.raises(RuntimeError, match="nicht unverändert atomar"):
        migration._write_atomic_initial_migration_file(
            target,
            b"expected",
            transaction_id=transaction_id,
            temporary_prefix=migration.MIGRATION_PHASE_TEMP_FILE_PREFIX,
            reader_sid="S-1-5-21-test",
            maximum_bytes=64,
            description="Die Testdatei",
        )


def test_windows_initial_publish_wraps_native_move_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    move = Mock(side_effect=OSError("sharing violation"))
    modules = (object(), object(), object(), SimpleNamespace(MoveFileEx=move), object(), object())
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(migration, "_migration_windows_modules", lambda: modules)

    with pytest.raises(RuntimeError, match="nicht atomar veröffentlicht"):
        migration._atomic_publish_migration_file(source, target)
    move.assert_called_once_with(
        str(source),
        str(target),
        migration.MOVEFILE_WRITE_THROUGH,
    )


def test_atomic_phase_failure_removes_only_revalidated_transaction_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction_id = "e" * 32
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    record = migration.MigrationPhaseRecord(
        schema_version=migration.MIGRATION_PHASE_SCHEMA_VERSION,
        transaction_id=transaction_id,
        generation=migration.MIGRATION_PHASE_GENERATIONS[migration.MigrationPhase.SERVICE_TRANSITION],
        phase=migration.MigrationPhase.SERVICE_TRANSITION,
    )
    temporary = state_directory / (f"{migration.MIGRATION_PHASE_TEMP_FILE_PREFIX}{transaction_id}-{'f' * 32}.tmp")

    def write_secure(path: Path, payload: bytes, *, reader_sid: str | None) -> None:
        path.write_bytes(payload)

    verify = Mock()
    monkeypatch.setattr(
        migration,
        "_migration_state_paths",
        lambda: (state_directory, state_directory / migration.MIGRATION_SEAL_FILE_NAME),
    )
    monkeypatch.setattr(migration.secrets, "token_hex", lambda _length: "f" * 32)
    monkeypatch.setattr(migration, "_write_secure_migration_file", write_secure)
    monkeypatch.setattr(
        migration,
        "_atomic_replace_migration_file",
        Mock(side_effect=OSError("replace failed")),
    )
    monkeypatch.setattr(
        migration,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    monkeypatch.setattr(migration, "_verify_migration_state_path", verify)

    with pytest.raises(OSError, match="replace failed"):
        migration._write_atomic_migration_phase(record, reader_sid="S-1-5-21-test")

    assert not temporary.exists()
    verify.assert_called_once_with(
        temporary,
        directory=False,
        reader_required=True,
        expected_reader_sid="S-1-5-21-test",
    )


def test_atomic_phase_readback_rejects_a_different_valid_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction_id = "1" * 32
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    record = migration.MigrationPhaseRecord(
        schema_version=migration.MIGRATION_PHASE_SCHEMA_VERSION,
        transaction_id=transaction_id,
        generation=migration.MIGRATION_PHASE_GENERATIONS[migration.MigrationPhase.SERVICE_TRANSITION],
        phase=migration.MigrationPhase.SERVICE_TRANSITION,
    )
    stale = migration.MigrationPhaseRecord(
        schema_version=migration.MIGRATION_PHASE_SCHEMA_VERSION,
        transaction_id=transaction_id,
        generation=migration.MIGRATION_PHASE_GENERATIONS[migration.MigrationPhase.ROLLBACKABLE],
        phase=migration.MigrationPhase.ROLLBACKABLE,
    )

    def write_secure(path: Path, payload: bytes, *, reader_sid: str | None) -> None:
        path.write_bytes(payload)

    monkeypatch.setattr(
        migration,
        "_migration_state_paths",
        lambda: (state_directory, state_directory / migration.MIGRATION_SEAL_FILE_NAME),
    )
    monkeypatch.setattr(migration.secrets, "token_hex", lambda _length: "2" * 32)
    monkeypatch.setattr(migration, "_write_secure_migration_file", write_secure)
    monkeypatch.setattr(migration, "_atomic_replace_migration_file", lambda source, target: source.replace(target))
    monkeypatch.setattr(migration, "_verify_migration_state_path", Mock())
    monkeypatch.setattr(
        migration,
        "_read_locked_bytes",
        Mock(return_value=migration._encode_migration_phase(stale)),
    )

    with pytest.raises(RuntimeError, match="nicht unverändert atomar"):
        migration._write_atomic_migration_phase(record, reader_sid="S-1-5-21-test")


def test_profile_audit_mount_inventory_rejects_invalid_binding_and_enum_failure() -> None:
    enum_failure = OSError("registry unavailable")
    enum_failure.winerror = 5  # type: ignore[attr-defined]
    fake_winreg = SimpleNamespace(
        HKEY_USERS=object(),
        EnumKey=Mock(side_effect=enum_failure),
    )

    with pytest.raises(RuntimeError, match="Transaktionsbindung"):
        migration._profile_audit_mounts("INVALID", _winreg=fake_winreg)
    fake_winreg.EnumKey.assert_not_called()

    with pytest.raises(RuntimeError, match="nicht vollständig inventarisiert"):
        migration._profile_audit_mounts("a" * 32, _winreg=fake_winreg)


def test_orphaned_profile_recovery_validates_every_snapshot_before_any_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction_id = "3" * 32
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    snapshots = [
        state_directory / f"{migration.PROFILE_HIVE_SNAPSHOT_DIRECTORY_PREFIX}{transaction_id}-{'4' * 32}",
        state_directory / f"{migration.PROFILE_HIVE_SNAPSHOT_DIRECTORY_PREFIX}{transaction_id}-{'5' * 32}",
    ]
    for snapshot in snapshots:
        snapshot.mkdir()
    monkeypatch.setattr(
        migration,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    validate_directory = Mock(side_effect=[None, RuntimeError("invalid snapshot")])
    monkeypatch.setattr(migration, "_validate_profile_hive_recovery_directory", validate_directory)
    remove = Mock()
    monkeypatch.setattr(migration, "_remove_profile_hive_snapshot", remove)

    with pytest.raises(RuntimeError, match="invalid snapshot"):
        migration._recover_orphaned_profile_audit_state(
            state_directory,
            transaction_id=transaction_id,
            winreg=SimpleNamespace(),
        )

    remove.assert_not_called()


def test_orphaned_profile_recovery_keeps_snapshot_when_bound_mount_cannot_unload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction_id = "6" * 32
    state_directory = tmp_path / "state"
    snapshot_directory = state_directory / (
        f"{migration.PROFILE_HIVE_SNAPSHOT_DIRECTORY_PREFIX}{transaction_id}-{'7' * 32}"
    )
    snapshot_directory.mkdir(parents=True)
    mount = f"{migration.PROFILE_AUDIT_MOUNT_PREFIX}{transaction_id}_{'8' * 24}"
    fake_winreg = SimpleNamespace(
        HKEY_USERS=object(),
        UnloadKey=Mock(side_effect=OSError("busy")),
    )
    monkeypatch.setattr(
        migration,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    monkeypatch.setattr(migration, "_validate_profile_hive_recovery_directory", Mock())
    monkeypatch.setattr(migration, "_profile_audit_mounts", Mock(return_value=(mount,)))
    enable = Mock()
    monkeypatch.setattr(migration, "_enable_registry_hive_privileges", enable)
    remove = Mock()
    monkeypatch.setattr(migration, "_remove_profile_hive_snapshot", remove)

    with pytest.raises(RuntimeError, match="konnte nicht entladen"):
        migration._recover_orphaned_profile_audit_state(
            state_directory,
            transaction_id=transaction_id,
            winreg=fake_winreg,
        )

    enable.assert_called_once_with()
    fake_winreg.UnloadKey.assert_called_once_with(fake_winreg.HKEY_USERS, mount)
    remove.assert_not_called()


def test_desktop_paths_require_local_app_data_and_use_the_default_off_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(RuntimeError, match="LOCALAPPDATA"):
        migration.desktop_executable()

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(migration.sys, "platform", "darwin")
    expected_root = tmp_path / "Programs" / migration.DESKTOP_INSTALL_DIRECTORY_NAME

    assert migration.desktop_executable() == expected_root / migration.DESKTOP_EXECUTABLE_NAME
    assert migration.desktop_token_file() == tmp_path / migration.APP_DIRECTORY_NAME / migration.API_TOKEN_FILE_NAME
    assert (
        migration._desktop_runtime_path()
        == tmp_path / migration.APP_DIRECTORY_NAME / migration.DESKTOP_RUNTIME_FILE_NAME
    )


def test_windows_module_loader_is_platform_gated_and_reports_missing_pywin32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migration.sys, "platform", "darwin")
    with pytest.raises(OSError, match="ausschließlich unter Windows"):
        migration._migration_windows_modules()

    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "ntsecuritycon", None)
    with pytest.raises(RuntimeError, match="pywin32 fehlt"):
        migration._migration_windows_modules()

    modules = tuple(
        SimpleNamespace(name=name)
        for name in (
            "pywintypes",
            "win32api",
            "win32con",
            "win32file",
            "win32security",
            "ntsecuritycon",
        )
    )
    for name, module in zip(
        ("pywintypes", "win32api", "win32con", "win32file", "win32security", "ntsecuritycon"),
        modules,
        strict=True,
    ):
        monkeypatch.setitem(sys.modules, name, module)

    assert migration._migration_windows_modules() == modules


def test_native_drive_type_calls_kernel_with_the_canonical_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_drive_type = Mock(return_value=migration.DRIVE_FIXED)
    kernel32 = SimpleNamespace(GetDriveTypeW=get_drive_type)
    monkeypatch.setattr(migration.ctypes, "WinDLL", Mock(return_value=kernel32), raising=False)

    assert migration._native_drive_type("C:\\") == migration.DRIVE_FIXED
    get_drive_type.assert_called_once_with("C:\\")


def test_atomic_replace_uses_write_through_and_surfaces_native_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    move = Mock(return_value=True)
    kernel32 = SimpleNamespace(MoveFileExW=move)
    monkeypatch.setattr(migration.os, "name", "nt")
    monkeypatch.setattr(migration.ctypes, "WinDLL", Mock(return_value=kernel32), raising=False)
    monkeypatch.setattr(migration.ctypes, "get_last_error", Mock(return_value=5), raising=False)

    migration._atomic_replace_migration_file(source, target)
    move.assert_called_once_with(
        str(source),
        str(target),
        migration.MOVEFILE_REPLACE_EXISTING | migration.MOVEFILE_WRITE_THROUGH,
    )

    move.return_value = False
    with pytest.raises(OSError, match="nicht atomar ersetzt"):
        migration._atomic_replace_migration_file(source, target)


def test_posix_atomic_publish_links_without_overwrite_and_wraps_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_bytes(b"sealed")
    monkeypatch.setattr(migration.sys, "platform", "darwin")

    migration._atomic_publish_migration_file(source, target)

    assert not source.exists()
    assert target.read_bytes() == b"sealed"

    source.write_bytes(b"retry")
    with pytest.raises(FileExistsError):
        migration._atomic_publish_migration_file(source, target)
    assert source.read_bytes() == b"retry"

    target.unlink()
    monkeypatch.setattr(migration.os, "link", Mock(side_effect=OSError("link failed")))
    with pytest.raises(RuntimeError, match="nicht atomar veröffentlicht"):
        migration._atomic_publish_migration_file(source, target)


def test_posix_private_write_removes_partial_file_on_flush_and_open_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "transfer.json"
    monkeypatch.setattr(migration.sys, "platform", "darwin")
    monkeypatch.setattr(migration.os, "fsync", Mock(side_effect=OSError("flush failed")))

    with pytest.raises(OSError, match="flush failed"):
        migration._write_private(target, b"partial")
    assert not target.exists()

    monkeypatch.undo()
    monkeypatch.setattr(migration.sys, "platform", "darwin")
    real_close = os.close
    close = Mock(side_effect=real_close)
    monkeypatch.setattr(migration.os, "fdopen", Mock(side_effect=OSError("fdopen failed")))
    monkeypatch.setattr(migration.os, "close", close)

    with pytest.raises(OSError, match="fdopen failed"):
        migration._write_private(target, b"partial")
    assert not target.exists()
    close.assert_called_once()


def test_seal_failure_invokes_protected_state_cleanup_before_reraising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    seal_path = state_directory / migration.MIGRATION_SEAL_FILE_NAME
    reader_sid = "S-1-5-21-test"
    clear = Mock()
    monkeypatch.setattr(migration.sys, "platform", "win32")
    monkeypatch.setattr(
        migration,
        "_prepare_migration_state",
        Mock(return_value=(state_directory, seal_path, reader_sid)),
    )
    monkeypatch.setattr(migration, "_load_receipt", Mock(side_effect=RuntimeError("corrupt receipt")))
    monkeypatch.setattr(migration, "_clear_migration_state", clear)

    with pytest.raises(RuntimeError, match="corrupt receipt"):
        migration.seal_desktop_migration(receipt_path=tmp_path / "receipt", token_transfer_path=None)

    clear.assert_called_once_with(
        expected_reader_sid=reader_sid,
        require_current_user=False,
    )


def test_migration_state_inventory_wraps_directory_io_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migration.os, "scandir", Mock(side_effect=OSError("unavailable")))

    with pytest.raises(RuntimeError, match="nicht inventarisiert"):
        migration._migration_state_entries(tmp_path)


def test_public_migration_actions_are_all_platform_gated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migration.sys, "platform", "darwin")
    actions = (
        lambda: migration.plan_desktop_migration(
            receipt_path=tmp_path / "receipt",
            token_transfer_path=None,
        ),
        lambda: migration.prepare_desktop_migration(
            receipt_path=tmp_path / "receipt",
            token_transfer_path=None,
        ),
        lambda: migration.prepare_desktop_migration_transfer(
            tmp_path / "transfer",
            tmp_path / "client-source.exe",
            "E-Rechnungs-Pruefer-Oeffnen.exe",
        ),
        lambda: migration.validate_desktop_migration_transfer(
            tmp_path / "transfer",
            tmp_path / "transfer" / migration.DESKTOP_MIGRATION_TRANSFER_RECEIPT_NAME,
            None,
            "E-Rechnungs-Pruefer-Oeffnen.exe",
        ),
        lambda: migration.clear_desktop_migration_transfer(
            tmp_path / "transfer",
            "E-Rechnungs-Pruefer-Oeffnen.exe",
        ),
        lambda: migration.seal_desktop_migration(
            receipt_path=tmp_path / "receipt",
            token_transfer_path=None,
        ),
        migration.apply_desktop_migration,
        migration.rollback_desktop_migration,
        migration.commit_desktop_migration,
        migration.clear_desktop_migration_seal,
    )

    for action in actions:
        with pytest.raises(OSError, match="ausschließlich unter Windows"):
            action()


def test_token_path_api_distinguishes_missing_binding_and_tokenless_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migration, "load_desktop_migration_binding", Mock(return_value=None))
    with pytest.raises(RuntimeError, match="Migrationsbeleg"):
        migration.protected_desktop_migration_token_path()

    receipt = migration.MigrationReceipt(None, False, r"C:\App\E-Rechnungs-Pruefer.exe", None)
    binding = migration.DesktopMigrationBinding(
        transaction_id="a" * 32,
        reader_sid="S-1-5-21-test",
        seal_sha256="b" * 64,
        token_sha256=None,
        receipt=receipt,
        phase=migration.MigrationPhase.ROLLBACKABLE,
    )
    monkeypatch.setattr(migration, "load_desktop_migration_binding", Mock(return_value=binding))

    assert migration.protected_desktop_migration_token_path() is None


def test_decoders_and_encoders_reject_structurally_valid_but_invalid_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="Migrationsbeleg ist ungültig"):
        migration._decode_receipt(
            b'{"autostart_command":null,"was_running":0,"executable":"x","disabled_executable":null}'
        )

    bad_seal = {
        "schema_version": migration.MIGRATION_SCHEMA_VERSION,
        "transaction_id": "INVALID",
        "reader_sid": "S-1-5-21-test",
        "token_sha256": None,
        "receipt": {
            "autostart_command": None,
            "was_running": False,
            "executable": "x",
            "disabled_executable": None,
        },
    }
    with pytest.raises(RuntimeError, match="Migrationsbeleg ist ungültig"):
        migration._decode_migration_seal(json.dumps(bad_seal).encode())

    bad_phase = {
        "schema_version": migration.MIGRATION_PHASE_SCHEMA_VERSION,
        "transaction_id": "a" * 32,
        "generation": 99,
        "phase": migration.MigrationPhase.ROLLBACKABLE.value,
    }
    with pytest.raises(RuntimeError, match="Migrationsphase ist ungültig"):
        migration._decode_migration_phase(json.dumps(bad_phase).encode())

    receipt = migration.MigrationReceipt(None, False, "x", None)
    seal, phase = _transaction(receipt, phase=migration.MigrationPhase.ROLLBACKABLE)
    monkeypatch.setattr(migration, "MAXIMUM_MIGRATION_RECEIPT_BYTES", 1)
    with pytest.raises(RuntimeError, match="überschreitet"):
        migration._encode_migration_seal(seal)
    monkeypatch.setattr(migration, "MAXIMUM_MIGRATION_PHASE_BYTES", 1)
    with pytest.raises(RuntimeError, match="überschreitet"):
        migration._encode_migration_phase(phase)
