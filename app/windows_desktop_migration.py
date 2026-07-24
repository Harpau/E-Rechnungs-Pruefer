from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import ntpath
import os
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Any

from .desktop_security import validate_api_token
from .windows_service_config import ServicePaths, validate_machine_path

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "E-Rechnungs-Pruefer"
APP_DIRECTORY_NAME = "E-Rechnungs-Pruefer"
API_TOKEN_FILE_NAME = "api-token.txt"
DESKTOP_RUNTIME_FILE_NAME = "runtime.json"
DESKTOP_INSTALL_DIRECTORY_NAME = "E-Rechnungs-Pruefer"
DESKTOP_EXECUTABLE_NAME = "E-Rechnungs-Pruefer.exe"
DISABLED_SUFFIX = ".service-mode-disabled"
DESKTOP_UNINSTALL_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{D33FD9E5-0C5E-48ED-BF0C-E9D2962A45DF}_is1"
)
PROFILE_LIST_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
SYSTEM_PROFILE_SIDS = frozenset({"S-1-5-18", "S-1-5-19", "S-1-5-20"})
WINDOWS_MUTEX_NAME = r"Local\E-Rechnungs-Pruefer-Desktop"
WINDOWS_SHUTDOWN_EVENT_NAME = r"Local\E-Rechnungs-Pruefer-Desktop-Shutdown"
WAIT_OBJECT_0 = 0
SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_NO_MORE_FILES = 18
ERROR_NO_MORE_ITEMS = 259
MIGRATION_TIMEOUT_MILLISECONDS = 30_000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_WINDOWS_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
DRIVE_FIXED = 3
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_SHARE_READ = 0x1
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
MOVEFILE_REPLACE_EXISTING = 0x1
MOVEFILE_WRITE_THROUGH = 0x8
MAXIMUM_MIGRATION_RECEIPT_BYTES = 64 * 1024
MAXIMUM_MIGRATION_PHASE_BYTES = 16 * 1024
MAXIMUM_MIGRATION_TOKEN_BYTES = 4 * 1024
MAXIMUM_DESKTOP_RUNTIME_BYTES = 16 * 1024
MAXIMUM_PROFILE_HIVE_BYTES = 512 * 1024 * 1024
MIGRATION_STATE_DIRECTORY_NAME = "E-Rechnungs-Pruefer-Installer-State"
MIGRATION_SEAL_FILE_NAME = "desktop-migration-receipt.json"
MIGRATION_PHASE_FILE_NAME = "desktop-migration-phase.json"
MIGRATION_TOKEN_FILE_NAME = "desktop-api-token.txt"
MIGRATION_SEAL_TEMP_FILE_PREFIX = "desktop-migration-receipt-"
MIGRATION_PHASE_TEMP_FILE_PREFIX = "desktop-migration-phase-"
MIGRATION_TOKEN_TEMP_FILE_PREFIX = "desktop-api-token-"
DESKTOP_MIGRATION_TRANSFER_ROOT_NAME = "E-Rechnungs-Pruefer-Installer-Transfer"
DESKTOP_MIGRATION_TRANSFER_RECEIPT_NAME = "desktop-migration-receipt.json"
DESKTOP_MIGRATION_TRANSFER_TOKEN_NAME = "desktop-api-token-transfer.txt"
MIGRATION_SCHEMA_VERSION = 1
MIGRATION_PHASE_SCHEMA_VERSION = 1
PROFILE_HIVE_SNAPSHOT_DIRECTORY_PREFIX = "profile-hive-"
PROFILE_HIVE_SNAPSHOT_FILE_NAME = "NTUSER.DAT"
PROFILE_AUDIT_MOUNT_PREFIX = "ERechnungsPrueferAudit_"
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
INTERACTIVE_SID = "S-1-5-4"
RESERVED_DOS_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


class _FileTime(ctypes.Structure):
    _fields_ = (("low", ctypes.c_ulong), ("high", ctypes.c_ulong))


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = (
        ("file_attributes", ctypes.c_ulong),
        ("creation_time", _FileTime),
        ("last_access_time", _FileTime),
        ("last_write_time", _FileTime),
        ("volume_serial_number", ctypes.c_ulong),
        ("file_size_high", ctypes.c_ulong),
        ("file_size_low", ctypes.c_ulong),
        ("number_of_links", ctypes.c_ulong),
        ("file_index_high", ctypes.c_ulong),
        ("file_index_low", ctypes.c_ulong),
    )


@dataclass(frozen=True, slots=True)
class MigrationReceipt:
    autostart_command: str | None
    was_running: bool
    executable: str
    disabled_executable: str | None


class MigrationPhase(StrEnum):
    ROLLBACKABLE = "rollbackable"
    SERVICE_TRANSITION = "service_transition"
    SERVICE_ROLLBACK_COMPLETE = "service_rollback_complete"
    SERVICE_COMMITTED = "service_committed"


MIGRATION_PHASE_GENERATIONS = {
    MigrationPhase.ROLLBACKABLE: 0,
    MigrationPhase.SERVICE_TRANSITION: 1,
    MigrationPhase.SERVICE_ROLLBACK_COMPLETE: 2,
    MigrationPhase.SERVICE_COMMITTED: 2,
}


@dataclass(frozen=True, slots=True)
class MigrationSeal:
    schema_version: int
    transaction_id: str
    reader_sid: str
    token_sha256: str | None
    receipt: MigrationReceipt


@dataclass(frozen=True, slots=True)
class MigrationPhaseRecord:
    schema_version: int
    transaction_id: str
    generation: int
    phase: MigrationPhase


@dataclass(frozen=True, slots=True)
class DesktopMigrationBinding:
    transaction_id: str
    reader_sid: str
    seal_sha256: str
    token_sha256: str | None
    receipt: MigrationReceipt
    phase: MigrationPhase


@dataclass(frozen=True, slots=True)
class _PartialMigrationState:
    reader_sid: str
    paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _LockedHiveReader:
    size: int
    read: Callable[[int], bytes]


def _local_app_data() -> Path:
    value = os.getenv("LOCALAPPDATA")
    if not value:
        raise RuntimeError("LOCALAPPDATA ist für die Desktopmigration nicht verfügbar.")
    return Path(value)


def desktop_executable() -> Path:
    default = _local_app_data() / "Programs" / DESKTOP_INSTALL_DIRECTORY_NAME / DESKTOP_EXECUTABLE_NAME
    if sys.platform != "win32":
        return default
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, DESKTOP_UNINSTALL_KEY, 0, winreg.KEY_QUERY_VALUE)
    except FileNotFoundError:
        return default
    with key:
        try:
            install_location, value_type = winreg.QueryValueEx(key, "InstallLocation")
        except FileNotFoundError as exc:
            raise RuntimeError("Die registrierte Desktopinstallation besitzt keinen Installationspfad.") from exc
    if value_type != winreg.REG_SZ or not isinstance(install_location, str) or not install_location.strip():
        raise RuntimeError("Der Installationspfad der Desktopinstallation ist ungültig.")
    return _validated_local_fixed_path(install_location) / DESKTOP_EXECUTABLE_NAME


def desktop_token_file() -> Path:
    return _local_app_data() / APP_DIRECTORY_NAME / API_TOKEN_FILE_NAME


def _canonical_windows_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.expandvars(str(path))))


def _normalize_local_fixed_windows_path(value: str, *, drive_type: Callable[[str], int]) -> str:
    if not value or "\x00" in value or "/" in value:
        raise RuntimeError("Ein Desktop- oder Profilpfad ist nicht kanonisch lokal.")
    drive, tail = ntpath.splitdrive(value)
    if len(drive) != 2 or drive[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" or drive[1] != ":":
        raise RuntimeError("Ein Desktop- oder Profilpfad liegt nicht auf einem lokalen Laufwerk.")
    if not tail.startswith("\\"):
        raise RuntimeError("Ein Desktop- oder Profilpfad ist nicht absolut.")
    raw_parts = tail.split("\\")
    path_parts = raw_parts[1:]
    if path_parts and not path_parts[-1]:
        path_parts = path_parts[:-1]
    if any(
        part in {".", ".."}
        or not part
        or part.rstrip(" .") != part
        or any(character in '<>:"|?*' or ord(character) < 32 for character in part)
        or part.split(".", 1)[0].casefold() in RESERVED_DOS_NAMES
        for part in path_parts
    ):
        raise RuntimeError("Ein Desktop- oder Profilpfad enthält unzulässige Pfadkomponenten.")
    root = f"{drive}\\"
    if drive_type(root) != DRIVE_FIXED:
        raise RuntimeError("Ein Desktop- oder Profilpfad liegt nicht auf einem festen lokalen Laufwerk.")
    return ntpath.normpath(value)


def _native_drive_type(root: str) -> int:
    ctypes_windows: Any = ctypes
    kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
    get_drive_type = kernel32.GetDriveTypeW
    get_drive_type.argtypes = [ctypes.c_wchar_p]
    get_drive_type.restype = ctypes.c_uint
    return int(get_drive_type(root))


def _validated_local_fixed_path(value: str) -> Path:
    if os.name != "nt":
        return Path(value)
    return Path(_normalize_local_fixed_windows_path(value, drive_type=_native_drive_type))


def _fallback_local_path_components(path: Path, *, directory: bool) -> bool:
    current = Path(path.anchor)
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for index, part in enumerate(parts):
        current /= part
        try:
            candidate_stat = os.lstat(current)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise RuntimeError(f"Der Desktoppfad {current} konnte nicht sicher geprüft werden.") from exc
        if stat.S_ISLNK(candidate_stat.st_mode) or (
            getattr(candidate_stat, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT_ATTRIBUTE
        ):
            raise RuntimeError(f"Der Desktoppfad {current} darf kein Reparse-Point oder Junction sein.")
        final = index == len(parts) - 1
        if not final and not stat.S_ISDIR(candidate_stat.st_mode):
            raise RuntimeError(f"Der übergeordnete Desktoppfad {current} ist kein Verzeichnis.")
        if final:
            if directory and not stat.S_ISDIR(candidate_stat.st_mode):
                raise RuntimeError(f"Der Desktoppfad {current} ist kein Verzeichnis.")
            if not directory and (
                not stat.S_ISREG(candidate_stat.st_mode) or int(getattr(candidate_stat, "st_nlink", 1)) != 1
            ):
                raise RuntimeError(f"Der Desktoppfad {current} ist keine eindeutige reguläre Datei.")
    return bool(parts)


@contextmanager
def _locked_local_path(path: Path, *, directory: bool) -> Iterator[bool]:
    """Inspect a fixed-drive path component-wise while blocking replacement.

    Every existing component is opened with OPEN_REPARSE_POINT and only
    FILE_SHARE_READ. Holding all parent handles prevents a user-controlled
    directory from being written or swapped for a junction before the next
    component is inspected, a transfer file is read, or an offline hive is
    loaded.
    """

    if os.name != "nt":
        yield _fallback_local_path_components(path, directory=directory)
        return

    canonical = _validated_local_fixed_path(str(path))
    pure = PureWindowsPath(str(canonical))
    parts = pure.parts
    if len(parts) < 2:
        raise RuntimeError("Ein Desktop- oder Profilpfad darf nicht auf das Laufwerkswurzelverzeichnis zeigen.")
    ctypes_windows: Any = ctypes
    kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ByHandleFileInformation)]
    get_information.restype = ctypes.c_bool
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_bool
    handles: list[int] = []
    current = PureWindowsPath(parts[0])
    try:
        for index, part in enumerate(parts[1:]):
            current /= part
            handle = create_file(
                str(current),
                0,
                FILE_SHARE_READ,
                None,
                OPEN_EXISTING,
                FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
            if handle in {None, INVALID_HANDLE_VALUE}:
                error = ctypes_windows.get_last_error()
                if error in {ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND}:
                    yield False
                    return
                raise OSError(error, f"Der Desktoppfad {current} konnte nicht no-follow geöffnet werden.")
            handles.append(int(handle))
            information = _ByHandleFileInformation()
            if not get_information(handle, ctypes.byref(information)):
                raise OSError(
                    ctypes_windows.get_last_error(),
                    f"Der Desktoppfad {current} konnte nicht no-follow geprüft werden.",
                )
            if information.file_attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                raise RuntimeError(f"Der Desktoppfad {current} darf kein Reparse-Point oder Junction sein.")
            final = index == len(parts) - 2
            is_directory = bool(information.file_attributes & FILE_ATTRIBUTE_DIRECTORY)
            if not final and not is_directory:
                raise RuntimeError(f"Der übergeordnete Desktoppfad {current} ist kein Verzeichnis.")
            if final:
                if directory and not is_directory:
                    raise RuntimeError(f"Der Desktoppfad {current} ist kein Verzeichnis.")
                if not directory and (is_directory or int(information.number_of_links) != 1):
                    raise RuntimeError(f"Der Desktoppfad {current} ist keine eindeutige reguläre Datei.")
        yield True
    finally:
        active_error = sys.exc_info()[0] is not None
        close_error = 0
        for handle in reversed(handles):
            if not close_handle(handle) and not close_error:
                close_error = ctypes_windows.get_last_error()
        if close_error and not active_error:
            raise OSError(close_error, "Ein gesperrter Desktoppfad konnte nicht sicher freigegeben werden.")


def _read_locked_bytes(path: Path, *, maximum_bytes: int, description: str) -> bytes:
    """Read a unique local file while every path component remains immutable."""

    try:
        with _locked_local_path(path, directory=False) as exists:
            if not exists:
                raise FileNotFoundError(path)
            with path.open("rb") as handle:
                payload = handle.read(maximum_bytes + 1)
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError(f"{description} konnte nicht sicher gelesen werden.") from exc
    if len(payload) > maximum_bytes:
        raise RuntimeError(f"{description} überschreitet die zulässige Größe.")
    return payload


def read_desktop_migration_token(path: Path) -> str:
    """Read the explicitly selected desktop token through the no-follow lock boundary."""

    payload = _read_locked_bytes(
        path,
        maximum_bytes=MAXIMUM_MIGRATION_TOKEN_BYTES,
        description="Das Desktop-API-Token für die Migration",
    )
    try:
        token = payload.decode("ascii").rstrip("\r\n")
    except UnicodeError as exc:
        raise RuntimeError("Das Desktop-API-Token für die Migration ist ungültig.") from exc
    try:
        return validate_api_token(token, description="Das Desktop-API-Token für die Migration")
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def _migration_windows_modules() -> tuple[Any, Any, Any, Any, Any, Any]:
    if sys.platform != "win32":
        raise OSError("Der geschützte Migrationszustand ist ausschließlich unter Windows verfügbar.")
    try:
        import ntsecuritycon
        import pywintypes
        import win32api
        import win32con
        import win32file
        import win32security
    except ImportError as exc:
        raise RuntimeError("pywin32 fehlt für den geschützten Migrationszustand.") from exc
    return pywintypes, win32api, win32con, win32file, win32security, ntsecuritycon


def _migration_state_paths() -> tuple[Path, Path]:
    program_data = ServicePaths.from_environment().data_directory.parent
    state_directory = program_data / MIGRATION_STATE_DIRECTORY_NAME
    return state_directory, state_directory / MIGRATION_SEAL_FILE_NAME


def _migration_phase_path(state_directory: Path) -> Path:
    return state_directory / MIGRATION_PHASE_FILE_NAME


def _migration_token_path(state_directory: Path) -> Path:
    return state_directory / MIGRATION_TOKEN_FILE_NAME


def _specific_user_sid(sid: Any, *, win32security: Any) -> str:
    sid_text = win32security.ConvertSidToStringSid(sid)
    if sid_text in {SYSTEM_SID, ADMINISTRATORS_SID}:
        raise RuntimeError("Der Desktop-Migrationsbeleg gehört keiner konkreten Benutzeridentität.")
    try:
        _name, _domain, sid_type = win32security.LookupAccountSid(None, sid)
    except Exception as exc:
        raise RuntimeError("Die Benutzeridentität des Desktop-Migrationsbelegs ist nicht auflösbar.") from exc
    if int(sid_type) != 1:  # SidTypeUser
        raise RuntimeError("Der Desktop-Migrationsbeleg gehört keiner konkreten Benutzeridentität.")
    return sid_text


def _receipt_owner_sid(receipt_path: Path) -> str:
    """Validate the exact DACL emitted by the unelevated transfer writer."""

    _pywintypes, _win32api, _win32con, _win32file, win32security, ntsecuritycon = _migration_windows_modules()
    with _locked_local_path(receipt_path, directory=False) as exists:
        if not exists:
            raise RuntimeError("Der Desktop-Migrationsbeleg fehlt vor der erhöhten Prüfung.")
        try:
            descriptor = win32security.GetNamedSecurityInfo(
                str(receipt_path),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION
                | getattr(win32security, "OWNER_SECURITY_INFORMATION", 0x00000001),
            )
            owner_sid = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
            dacl = descriptor.GetSecurityDescriptorDacl()
            control, _revision = descriptor.GetSecurityDescriptorControl()
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("Der Desktop-Migrationsbeleg besitzt keine prüfbare Windows-DACL.") from exc
        if dacl is None or not control & win32security.SE_DACL_PROTECTED:
            raise RuntimeError("Die DACL des Desktop-Migrationsbelegs ist nicht vor Vererbung geschützt.")
        fixed = {SYSTEM_SID, ADMINISTRATORS_SID}
        observed_fixed: set[str] = set()
        reader_sid: str | None = None
        for index in range(dacl.GetAceCount()):
            header, mask, sid = dacl.GetAce(index)
            sid_text = win32security.ConvertSidToStringSid(sid)
            if (
                int(header[0]) != win32security.ACCESS_ALLOWED_ACE_TYPE
                or int(header[1]) != 0
                or int(mask) != int(ntsecuritycon.FILE_ALL_ACCESS)
            ):
                raise RuntimeError("Der Desktop-Migrationsbeleg besitzt eine unerwartete Windows-Berechtigung.")
            if sid_text in fixed:
                if sid_text in observed_fixed:
                    raise RuntimeError("Der Desktop-Migrationsbeleg besitzt eine doppelte Windows-Berechtigung.")
                observed_fixed.add(sid_text)
            else:
                if reader_sid is not None:
                    raise RuntimeError("Der Desktop-Migrationsbeleg besitzt mehrere Benutzeridentitäten.")
                reader_sid = _specific_user_sid(sid, win32security=win32security)
        if (
            observed_fixed != fixed
            or reader_sid is None
            or owner_sid not in {reader_sid, ADMINISTRATORS_SID}
            or dacl.GetAceCount() != 3
        ):
            raise RuntimeError("Der Desktop-Migrationsbeleg besitzt nicht die exakt erforderliche Windows-DACL.")
    return reader_sid


def _migration_security_attributes(*, directory: bool, reader_sid: str | None) -> Any:
    pywintypes, _win32api, _win32con, _win32file, win32security, ntsecuritycon = _migration_windows_modules()
    dacl = win32security.ACL()
    inheritance = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE if directory else 0
    for sid_text in (SYSTEM_SID, ADMINISTRATORS_SID):
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS,
            inheritance,
            ntsecuritycon.FILE_ALL_ACCESS,
            win32security.ConvertStringSidToSid(sid_text),
        )
    if reader_sid is not None:
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS,
            0,
            ntsecuritycon.FILE_GENERIC_READ,
            win32security.ConvertStringSidToSid(reader_sid),
        )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(win32security.ConvertStringSidToSid(ADMINISTRATORS_SID), 0)
    descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
    descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED,
        win32security.SE_DACL_PROTECTED,
    )
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def _verify_migration_state_path(
    path: Path,
    *,
    directory: bool,
    reader_required: bool,
    expected_reader_sid: str | None = None,
) -> str | None:
    if not validate_machine_path(path, directory=directory):
        raise RuntimeError(f"Der geschützte Migrationspfad {path} fehlt.")
    _pywintypes, _win32api, _win32con, _win32file, win32security, ntsecuritycon = _migration_windows_modules()
    try:
        descriptor = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | getattr(win32security, "OWNER_SECURITY_INFORMATION", 0x00000001),
        )
        owner_sid = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
        dacl = descriptor.GetSecurityDescriptorDacl()
        control, _revision = descriptor.GetSecurityDescriptorControl()
    except Exception as exc:
        raise RuntimeError(f"Der geschützte Migrationspfad {path} besitzt keine prüfbare Windows-DACL.") from exc
    if owner_sid != ADMINISTRATORS_SID:
        raise RuntimeError("Der geschützte Migrationszustand gehört nicht der Administratorengruppe.")
    if dacl is None or not control & win32security.SE_DACL_PROTECTED:
        raise RuntimeError("Die DACL des geschützten Migrationszustands ist nicht vor Vererbung geschützt.")

    inheritance = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE if directory else 0
    fixed = {SYSTEM_SID, ADMINISTRATORS_SID}
    observed_fixed: set[str] = set()
    observed_reader: str | None = None
    for index in range(dacl.GetAceCount()):
        header, mask, sid = dacl.GetAce(index)
        sid_text = win32security.ConvertSidToStringSid(sid)
        if int(header[0]) != win32security.ACCESS_ALLOWED_ACE_TYPE:
            raise RuntimeError("Der geschützte Migrationszustand enthält einen unerwarteten ACE-Typ.")
        if sid_text in fixed:
            if (
                int(header[1]) != inheritance
                or int(mask) != int(ntsecuritycon.FILE_ALL_ACCESS)
                or sid_text in observed_fixed
            ):
                raise RuntimeError("Der geschützte Migrationszustand enthält eine ungültige Administrator-DACL.")
            observed_fixed.add(sid_text)
            continue
        reader_sid = _specific_user_sid(sid, win32security=win32security)
        if observed_reader is not None or int(header[1]) != 0 or int(mask) != int(ntsecuritycon.FILE_GENERIC_READ):
            raise RuntimeError("Der geschützte Migrationszustand enthält eine ungültige Leser-DACL.")
        observed_reader = reader_sid
    if observed_fixed != fixed or len(observed_fixed) != 2:
        raise RuntimeError("Der geschützte Migrationszustand enthält nicht alle administrativen Identitäten.")
    if reader_required != (observed_reader is not None):
        raise RuntimeError("Der geschützte Migrationszustand enthält eine unerwartete Leseridentität.")
    if expected_reader_sid is not None and observed_reader != expected_reader_sid:
        raise RuntimeError("Der geschützte Migrationszustand gehört einer anderen Benutzeridentität.")
    expected_ace_count = 2 + int(reader_required)
    if dacl.GetAceCount() != expected_ace_count:
        raise RuntimeError("Der geschützte Migrationszustand besitzt nicht die exakt erforderliche DACL.")
    return observed_reader


def _current_process_user_sid() -> str:
    _pywintypes, win32api, win32con, _win32file, win32security, _ntsecuritycon = _migration_windows_modules()
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        return win32security.ConvertSidToStringSid(user_sid)
    finally:
        token.Close()


def _prepare_migration_state(receipt_path: Path) -> tuple[Path, Path, str]:
    reader_sid = _receipt_owner_sid(receipt_path)
    state_directory, seal_path = _migration_state_paths()
    validate_machine_path(state_directory.parent, directory=True)
    if validate_machine_path(state_directory, directory=True):
        _verify_migration_state_path(
            state_directory,
            directory=True,
            reader_required=True,
            expected_reader_sid=reader_sid,
        )
        raise RuntimeError("Ein geschützter Migrationszustand einer früheren Transaktion ist vorhanden.")
    _pywintypes, _win32api, _win32con, win32file, _win32security, _ntsecuritycon = _migration_windows_modules()
    try:
        win32file.CreateDirectoryW(
            str(state_directory),
            _migration_security_attributes(directory=True, reader_sid=reader_sid),
        )
    except Exception as exc:
        raise RuntimeError("Der geschützte Desktop-Migrationszustand konnte nicht erstellt werden.") from exc
    _verify_migration_state_path(
        state_directory,
        directory=True,
        reader_required=True,
        expected_reader_sid=reader_sid,
    )
    if validate_machine_path(seal_path, directory=False):
        raise RuntimeError("Ein geschützter Desktop-Migrationsbeleg ist unerwartet vorhanden.")
    return state_directory, seal_path, reader_sid


def _write_secure_migration_file(path: Path, payload: bytes, *, reader_sid: str | None) -> None:
    if not payload:
        raise RuntimeError("Eine leere Migrationsdatei wird nicht gespeichert.")
    _verify_migration_state_path(
        path.parent,
        directory=True,
        reader_required=True,
        expected_reader_sid=reader_sid if reader_sid is not None else None,
    )
    if validate_machine_path(path, directory=False):
        raise RuntimeError("Eine geschützte Migrationsdatei ist bereits vorhanden.")
    _pywintypes, _win32api, win32con, win32file, _win32security, _ntsecuritycon = _migration_windows_modules()
    handle = None
    created = False
    failure: Exception | None = None
    try:
        handle = win32file.CreateFile(
            str(path),
            win32con.GENERIC_WRITE,
            0,
            _migration_security_attributes(directory=False, reader_sid=reader_sid),
            win32con.CREATE_NEW,
            win32con.FILE_ATTRIBUTE_TEMPORARY,
            None,
        )
        created = True
        win32file.WriteFile(handle, payload)
        win32file.FlushFileBuffers(handle)
    except Exception as exc:
        failure = exc
    finally:
        if handle is not None:
            try:
                win32file.CloseHandle(handle)
            except Exception as exc:
                if failure is None:
                    failure = exc
        if created and failure is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    if failure is not None:
        raise RuntimeError("Die geschützte Migrationsdatei konnte nicht geschrieben werden.") from failure
    _verify_migration_state_path(
        path,
        directory=False,
        reader_required=reader_sid is not None,
        expected_reader_sid=reader_sid,
    )


def _registered_install_location(root: Any, *, winreg: Any) -> Path | None:
    try:
        key = winreg.OpenKey(root, DESKTOP_UNINSTALL_KEY, 0, winreg.KEY_QUERY_VALUE)
    except FileNotFoundError:
        return None
    with key:
        try:
            value, value_type = winreg.QueryValueEx(key, "InstallLocation")
        except FileNotFoundError as exc:
            raise RuntimeError("Eine registrierte Desktopinstallation besitzt keinen Installationspfad.") from exc
    if value_type != winreg.REG_SZ or not isinstance(value, str) or not value.strip():
        raise RuntimeError("Eine registrierte Desktopinstallation besitzt einen ungültigen Installationspfad.")
    return _validated_local_fixed_path(value) / DESKTOP_EXECUTABLE_NAME


def _registered_autostart(root: Any, *, winreg: Any) -> str | None:
    try:
        with winreg.OpenKey(root, AUTOSTART_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            value, value_type = winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
    except FileNotFoundError:
        return None
    if value_type != winreg.REG_SZ or not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise RuntimeError("Ein registrierter Desktop-Autostart besitzt einen ungültigen Wert.")
    return value


def _profile_sid_is_in_scope(sid: str) -> bool:
    return sid not in SYSTEM_PROFILE_SIDS


def _enable_registry_hive_privileges() -> None:
    try:
        import ntsecuritycon
        import win32api
        import win32security
    except ImportError as exc:
        raise RuntimeError("pywin32 fehlt für die maschinenweite Desktopinventur.") from exc
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        ntsecuritycon.TOKEN_ADJUST_PRIVILEGES | ntsecuritycon.TOKEN_QUERY,
    )
    try:
        privileges = []
        for name in (win32security.SE_BACKUP_NAME, win32security.SE_RESTORE_NAME):
            privileges.append((win32security.LookupPrivilegeValue(None, name), win32security.SE_PRIVILEGE_ENABLED))
        win32security.AdjustTokenPrivileges(token, False, privileges)
    finally:
        token.Close()


@contextmanager
def _open_locked_profile_hive_reader(source: Path) -> Iterator[_LockedHiveReader]:
    """Open the selected no-follow hive handle for backup reads while its path locks remain held."""

    if os.name != "nt":
        try:
            with source.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                yield _LockedHiveReader(size=size, read=handle.read)
        except OSError as exc:
            raise RuntimeError("Der ausgewählte NTUSER-Hive konnte nicht sicher gelesen werden.") from exc
        return

    ctypes_windows: Any = ctypes
    kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ByHandleFileInformation)]
    get_information.restype = ctypes.c_bool
    read_file = kernel32.ReadFile
    read_file.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
    ]
    read_file.restype = ctypes.c_bool
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_bool
    handle = create_file(
        str(source),
        GENERIC_READ,
        FILE_SHARE_READ,
        None,
        OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle in {None, INVALID_HANDLE_VALUE}:
        raise OSError(
            ctypes_windows.get_last_error(),
            "Der ausgewählte NTUSER-Hive konnte nicht als geschützter Backup-Handle geöffnet werden.",
        )
    active_error = False
    try:
        information = _ByHandleFileInformation()
        if not get_information(handle, ctypes.byref(information)):
            raise OSError(
                ctypes_windows.get_last_error(),
                "Der ausgewählte NTUSER-Hive konnte nicht über seinen Handle geprüft werden.",
            )
        if (
            information.file_attributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)
            or int(information.number_of_links) != 1
        ):
            raise RuntimeError("Der ausgewählte NTUSER-Hive ist keine eindeutige reguläre Datei.")
        size = (int(information.file_size_high) << 32) | int(information.file_size_low)

        def read_chunk(maximum_bytes: int) -> bytes:
            if not 0 < maximum_bytes <= 1024 * 1024:
                raise ValueError("Ungültige Blockgröße für die geschützte Hive-Kopie.")
            buffer = ctypes.create_string_buffer(maximum_bytes)
            bytes_read = ctypes.c_ulong()
            if not read_file(handle, buffer, maximum_bytes, ctypes.byref(bytes_read), None):
                raise OSError(
                    ctypes_windows.get_last_error(),
                    "Der ausgewählte NTUSER-Hive konnte nicht über seinen Handle gelesen werden.",
                )
            return buffer.raw[: bytes_read.value]

        yield _LockedHiveReader(size=size, read=read_chunk)
    except BaseException:
        active_error = True
        raise
    finally:
        if not close_handle(handle) and not active_error:
            raise OSError(
                ctypes_windows.get_last_error(),
                "Der geschützte Backup-Handle des NTUSER-Hives konnte nicht freigegeben werden.",
            )


def _is_profile_snapshot_name(name: str, *, transaction_id: str) -> bool:
    prefix = f"{PROFILE_HIVE_SNAPSHOT_DIRECTORY_PREFIX}{transaction_id}-"
    suffix = name[len(prefix) :] if name.startswith(prefix) else ""
    return len(suffix) == 32 and all(character in "0123456789abcdef" for character in suffix)


def _is_profile_audit_mount_name(name: str, *, transaction_id: str) -> bool:
    prefix = f"{PROFILE_AUDIT_MOUNT_PREFIX}{transaction_id}_"
    nonce = name[len(prefix) :] if name.startswith(prefix) else ""
    return len(nonce) == 24 and all(character in "0123456789abcdef" for character in nonce)


def _profile_audit_mounts(
    transaction_id: str,
    *,
    _winreg: Any | None = None,
) -> tuple[str, ...]:
    if not _valid_transaction_id(transaction_id):
        raise RuntimeError("Die Registry-Audit-Mounts besitzen keine gültige Transaktionsbindung.")
    winreg: Any
    if _winreg is None:
        if os.name != "nt":
            return ()
        import winreg as native_winreg

        winreg = native_winreg
    else:
        winreg = _winreg
    mounts: list[str] = []
    index = 0
    while True:
        try:
            name = winreg.EnumKey(winreg.HKEY_USERS, index)
        except OSError as exc:
            if getattr(exc, "winerror", None) != ERROR_NO_MORE_ITEMS:
                raise RuntimeError(
                    "Die geladenen Benutzer-Hives konnten nicht vollständig inventarisiert werden."
                ) from exc
            break
        index += 1
        if not name.casefold().startswith(PROFILE_AUDIT_MOUNT_PREFIX.casefold()):
            continue
        if not _is_profile_audit_mount_name(name, transaction_id=transaction_id):
            raise RuntimeError("Ein fremder oder ungültiger Registry-Audit-Mount ist vorhanden.")
        mounts.append(name)
    return tuple(mounts)


def _temporary_transaction_id(name: str, *, prefix: str) -> str | None:
    suffix = name[len(prefix) :] if name.startswith(prefix) and name.endswith(".tmp") else ""
    body = suffix[:-4] if suffix.endswith(".tmp") else ""
    if len(body) != 65 or body[32] != "-":
        return None
    transaction_id, nonce = body[:32], body[33:]
    if not _valid_transaction_id(transaction_id) or len(nonce) != 32:
        return None
    if any(character not in "0123456789abcdef" for character in nonce):
        return None
    return transaction_id


def _is_seal_temporary_name(name: str, *, transaction_id: str) -> bool:
    return _temporary_transaction_id(name, prefix=MIGRATION_SEAL_TEMP_FILE_PREFIX) == transaction_id


def _is_phase_temporary_name(name: str, *, transaction_id: str) -> bool:
    return _temporary_transaction_id(name, prefix=MIGRATION_PHASE_TEMP_FILE_PREFIX) == transaction_id


def _is_token_temporary_name(name: str, *, transaction_id: str) -> bool:
    return _temporary_transaction_id(name, prefix=MIGRATION_TOKEN_TEMP_FILE_PREFIX) == transaction_id


def _create_profile_hive_snapshot_directory(
    state_directory: Path,
    *,
    state_reader_sid: str,
    transaction_id: str,
) -> Path:
    _verify_migration_state_path(
        state_directory,
        directory=True,
        reader_required=True,
        expected_reader_sid=state_reader_sid,
    )
    if not _valid_transaction_id(transaction_id):
        raise RuntimeError("Die NTUSER-Hive-Kopie besitzt keine gültige Transaktionsbindung.")
    snapshot_directory = state_directory / (
        f"{PROFILE_HIVE_SNAPSHOT_DIRECTORY_PREFIX}{transaction_id}-{secrets.token_hex(16)}"
    )
    if validate_machine_path(snapshot_directory, directory=True):
        raise RuntimeError("Ein temporäres NTUSER-Hive-Verzeichnis ist bereits vorhanden.")
    _pywintypes, _win32api, _win32con, win32file, _win32security, _ntsecuritycon = _migration_windows_modules()
    try:
        win32file.CreateDirectoryW(
            str(snapshot_directory),
            _migration_security_attributes(directory=True, reader_sid=None),
        )
    except Exception as exc:
        raise RuntimeError("Das geschützte NTUSER-Hive-Verzeichnis konnte nicht erstellt werden.") from exc
    _verify_migration_state_path(
        snapshot_directory,
        directory=True,
        reader_required=False,
    )
    return snapshot_directory


def _copy_locked_profile_hive(reader: _LockedHiveReader, target: Path) -> None:
    """Copy an immutable offline hive from its held no-follow handle."""

    _verify_migration_state_path(
        target.parent,
        directory=True,
        reader_required=False,
    )
    source_size = reader.size
    if source_size <= 0 or source_size > MAXIMUM_PROFILE_HIVE_BYTES:
        raise RuntimeError("Der ausgewählte NTUSER-Hive besitzt eine unzulässige Größe.")
    if validate_machine_path(target, directory=False):
        raise RuntimeError("Eine temporäre NTUSER-Hive-Kopie ist bereits vorhanden.")
    _pywintypes, _win32api, win32con, win32file, _win32security, _ntsecuritycon = _migration_windows_modules()
    output_handle = None
    created = False
    copied = 0
    failure: Exception | None = None
    try:
        output_handle = win32file.CreateFile(
            str(target),
            win32con.GENERIC_WRITE,
            0,
            _migration_security_attributes(directory=False, reader_sid=None),
            win32con.CREATE_NEW,
            win32con.FILE_ATTRIBUTE_TEMPORARY,
            None,
        )
        created = True
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if copied > source_size or copied > MAXIMUM_PROFILE_HIVE_BYTES:
                raise RuntimeError("Der NTUSER-Hive änderte sich während der geschützten Kopie.")
            win32file.WriteFile(output_handle, chunk)
        if copied != source_size:
            raise RuntimeError("Der NTUSER-Hive wurde nicht vollständig in den geschützten Zustand kopiert.")
        win32file.FlushFileBuffers(output_handle)
    except Exception as exc:
        failure = exc
    finally:
        if output_handle is not None:
            try:
                win32file.CloseHandle(output_handle)
            except Exception as exc:
                if failure is None:
                    failure = exc
        if created and failure is not None:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
    if failure is not None:
        if isinstance(failure, RuntimeError):
            raise failure
        raise RuntimeError("Der NTUSER-Hive konnte nicht geschützt kopiert werden.") from failure
    _verify_migration_state_path(
        target,
        directory=False,
        reader_required=False,
    )
    try:
        target_size = target.stat().st_size
    except OSError as exc:
        raise RuntimeError("Die geschützte NTUSER-Hive-Kopie konnte nicht vermessen werden.") from exc
    if target_size != source_size:
        raise RuntimeError("Die geschützte NTUSER-Hive-Kopie besitzt eine unerwartete Größe.")


def _snapshot_profile_hive(
    profile_path: Path,
    state_directory: Path,
    *,
    state_reader_sid: str,
    transaction_id: str = "0" * 32,
) -> Path:
    """Select DAT or MAN atomically and copy it into an isolated protected directory."""

    dat_path = profile_path / "NTUSER.DAT"
    man_path = profile_path / "NTUSER.MAN"
    with _locked_local_path(profile_path, directory=True) as profile_exists:
        if not profile_exists:
            raise RuntimeError(f"Das Benutzerprofil {profile_path} ist unerwartet verschwunden.")
        with _locked_local_path(dat_path, directory=False) as dat_exists:
            with _locked_local_path(man_path, directory=False) as man_exists:
                if int(dat_exists) + int(man_exists) != 1:
                    raise RuntimeError("Das Benutzerprofil besitzt keinen eindeutig prüfbaren NTUSER-Hive.")
                source = dat_path if dat_exists else man_path
                snapshot_directory = _create_profile_hive_snapshot_directory(
                    state_directory,
                    state_reader_sid=state_reader_sid,
                    transaction_id=transaction_id,
                )
                snapshot = snapshot_directory / PROFILE_HIVE_SNAPSHOT_FILE_NAME
                try:
                    with _open_locked_profile_hive_reader(source) as reader:
                        _copy_locked_profile_hive(reader, snapshot)
                except Exception:
                    _remove_profile_hive_snapshot(
                        snapshot,
                        expected_transaction_id=transaction_id,
                    )
                    raise
    return snapshot


def _verify_profile_hive_support_file(path: Path) -> None:
    """Accept only unique regular files confined to the verified admin-only snapshot directory."""

    with _locked_local_path(path, directory=False) as exists:
        if not exists:
            raise RuntimeError("Eine Registry-Supportdatei ist während der Bereinigung verschwunden.")
        _pywintypes, _win32api, _win32con, _win32file, win32security, ntsecuritycon = _migration_windows_modules()
        try:
            descriptor = win32security.GetNamedSecurityInfo(
                str(path),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION
                | getattr(win32security, "OWNER_SECURITY_INFORMATION", 0x00000001),
            )
            owner_sid = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
            dacl = descriptor.GetSecurityDescriptorDacl()
            control, _revision = descriptor.GetSecurityDescriptorControl()
        except Exception as exc:
            raise RuntimeError("Eine Registry-Supportdatei besitzt keine prüfbare Windows-DACL.") from exc
        if owner_sid not in {SYSTEM_SID, ADMINISTRATORS_SID} or dacl is None:
            raise RuntimeError("Eine Registry-Supportdatei besitzt keinen administrativen Eigentümer.")
        protected = bool(control & win32security.SE_DACL_PROTECTED)
        inherited_ace = getattr(win32security, "INHERITED_ACE", 0x10)
        expected_flags = 0 if protected else inherited_ace
        observed: set[str] = set()
        for index in range(dacl.GetAceCount()):
            header, mask, sid = dacl.GetAce(index)
            sid_text = win32security.ConvertSidToStringSid(sid)
            if (
                int(header[0]) != win32security.ACCESS_ALLOWED_ACE_TYPE
                or int(header[1]) != expected_flags
                or int(mask) != int(ntsecuritycon.FILE_ALL_ACCESS)
                or sid_text not in {SYSTEM_SID, ADMINISTRATORS_SID}
                or sid_text in observed
            ):
                raise RuntimeError("Eine Registry-Supportdatei besitzt eine unerwartete Windows-DACL.")
            observed.add(sid_text)
        if observed != {SYSTEM_SID, ADMINISTRATORS_SID} or dacl.GetAceCount() != 2:
            raise RuntimeError("Eine Registry-Supportdatei besitzt nicht die exakt erforderliche Windows-DACL.")


def _validate_profile_hive_snapshot(
    snapshot: Path,
    *,
    expected_transaction_id: str | None = None,
) -> tuple[Path, ...]:
    snapshot_directory = snapshot.parent
    _verify_migration_state_path(
        snapshot_directory,
        directory=True,
        reader_required=False,
    )
    valid_directory_name = snapshot_directory.name.startswith(PROFILE_HIVE_SNAPSHOT_DIRECTORY_PREFIX)
    if expected_transaction_id is not None:
        valid_directory_name = _is_profile_snapshot_name(
            snapshot_directory.name,
            transaction_id=expected_transaction_id,
        )
    if snapshot.name.casefold() != PROFILE_HIVE_SNAPSHOT_FILE_NAME.casefold() or not valid_directory_name:
        raise RuntimeError("Der geschützte NTUSER-Hive-Pfad ist nicht eindeutig.")
    try:
        entries = tuple(snapshot_directory.iterdir())
    except OSError as exc:
        raise RuntimeError("Das geschützte NTUSER-Hive-Verzeichnis konnte nicht inventarisiert werden.") from exc
    for entry in entries:
        _verify_profile_hive_support_file(entry)
    return entries


def _validate_profile_hive_recovery_directory(
    snapshot_directory: Path,
    *,
    expected_transaction_id: str,
) -> None:
    _verify_migration_state_path(
        snapshot_directory,
        directory=True,
        reader_required=False,
    )
    if not _is_profile_snapshot_name(
        snapshot_directory.name,
        transaction_id=expected_transaction_id,
    ):
        raise RuntimeError("Das geschützte NTUSER-Hive-Verzeichnis gehört zu einer anderen Transaktion.")


def _validate_profile_hive_recovery_tail(
    snapshot_directory: Path,
    *,
    expected_transaction_id: str,
) -> tuple[Path, ...]:
    """Validate a tx-bound snapshot plus Registry-created support-file tails."""

    _validate_profile_hive_recovery_directory(
        snapshot_directory,
        expected_transaction_id=expected_transaction_id,
    )
    try:
        entries = tuple(snapshot_directory.iterdir())
    except OSError as exc:
        raise RuntimeError("Das geschützte NTUSER-Hive-Verzeichnis konnte nicht inventarisiert werden.") from exc
    for entry in entries:
        _verify_profile_hive_support_file(entry)
    return entries


def _remove_profile_hive_snapshot(
    snapshot: Path,
    *,
    expected_transaction_id: str | None = None,
) -> None:
    snapshot_directory = snapshot.parent
    entries = _validate_profile_hive_snapshot(
        snapshot,
        expected_transaction_id=expected_transaction_id,
    )
    for entry in entries:
        try:
            entry.unlink()
        except OSError as exc:
            raise RuntimeError("Eine geschützte Registry-Supportdatei konnte nicht gelöscht werden.") from exc
    try:
        if any(snapshot_directory.iterdir()):
            raise RuntimeError("Das geschützte NTUSER-Hive-Verzeichnis ist nach der Bereinigung nicht leer.")
        snapshot_directory.rmdir()
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError("Das geschützte NTUSER-Hive-Verzeichnis konnte nicht gelöscht werden.") from exc
    if validate_machine_path(snapshot_directory, directory=True):
        raise RuntimeError("Das geschützte NTUSER-Hive-Verzeichnis wurde nicht vollständig entfernt.")


def _recover_orphaned_profile_audit_state(
    state_directory: Path,
    *,
    transaction_id: str,
    winreg: Any,
) -> None:
    """Unload only tx-bound orphan mounts, then remove their validated snapshots."""

    if not validate_machine_path(state_directory, directory=True):
        return
    entry_names = _migration_state_entries(state_directory)
    snapshot_directories: list[Path] = []
    for name in entry_names:
        if not _is_profile_snapshot_name(name, transaction_id=transaction_id):
            continue
        snapshot_directory = state_directory / name
        if not validate_machine_path(snapshot_directory, directory=True):
            raise RuntimeError("Ein geschütztes NTUSER-Hive-Verzeichnis ist kein sicheres Verzeichnis.")
        _validate_profile_hive_recovery_directory(
            snapshot_directory,
            expected_transaction_id=transaction_id,
        )
        snapshot_directories.append(snapshot_directory)
    if not snapshot_directories:
        return
    allowed_fixed = {
        MIGRATION_SEAL_FILE_NAME,
        MIGRATION_PHASE_FILE_NAME,
        MIGRATION_TOKEN_FILE_NAME,
    }
    for name in entry_names:
        if (
            name in allowed_fixed
            or _is_phase_temporary_name(name, transaction_id=transaction_id)
            or _is_profile_snapshot_name(name, transaction_id=transaction_id)
        ):
            continue
        raise RuntimeError("Der geschützte Desktop-Migrationszustand enthält unerwartete Einträge.")
    mounts = _profile_audit_mounts(transaction_id, _winreg=winreg)

    if mounts:
        _enable_registry_hive_privileges()
        for mounted_name in mounts:
            try:
                winreg.UnloadKey(winreg.HKEY_USERS, mounted_name)
            except OSError as exc:
                raise RuntimeError(
                    "Ein verwaister, transaktionsgebundener Registry-Audit-Mount konnte nicht entladen werden."
                ) from exc
    for snapshot_directory in snapshot_directories:
        _validate_profile_hive_recovery_tail(
            snapshot_directory,
            expected_transaction_id=transaction_id,
        )
    for snapshot_directory in snapshot_directories:
        _remove_profile_hive_snapshot(
            snapshot_directory / PROFILE_HIVE_SNAPSHOT_FILE_NAME,
            expected_transaction_id=transaction_id,
        )


def _profile_installation_candidates(
    *,
    snapshot_directory: Path,
    state_reader_sid: str,
    transaction_id: str = "0" * 32,
) -> tuple[Path, ...]:
    """Inventory default and registered v1.3 desktop paths for every local user profile."""

    if sys.platform != "win32":
        raise OSError("Die maschinenweite Desktopinventur ist ausschließlich unter Windows verfügbar.")
    import winreg

    _recover_orphaned_profile_audit_state(
        snapshot_directory,
        transaction_id=transaction_id,
        winreg=winreg,
    )

    profiles: list[tuple[str, Path]] = []
    access = winreg.KEY_QUERY_VALUE | getattr(winreg, "KEY_ENUMERATE_SUB_KEYS", 0x0008)
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, PROFILE_LIST_KEY, 0, access) as profile_list:
        index = 0
        while True:
            try:
                sid = winreg.EnumKey(profile_list, index)
            except OSError as exc:
                if getattr(exc, "winerror", None) != ERROR_NO_MORE_ITEMS:
                    raise RuntimeError(
                        "Die lokalen Benutzerprofile konnten nicht vollständig inventarisiert werden."
                    ) from exc
                break
            index += 1
            if not _profile_sid_is_in_scope(sid):
                continue
            with winreg.OpenKey(profile_list, sid, 0, winreg.KEY_QUERY_VALUE) as profile_key:
                value, value_type = winreg.QueryValueEx(profile_key, "ProfileImagePath")
            allowed_types = {winreg.REG_SZ, getattr(winreg, "REG_EXPAND_SZ", winreg.REG_SZ)}
            if value_type not in allowed_types or not isinstance(value, str) or not value.strip():
                raise RuntimeError("Ein lokales Benutzerprofil besitzt einen ungültigen Profilpfad.")
            profiles.append((sid, _validated_local_fixed_path(os.path.expandvars(value))))

    candidates: list[Path] = []
    privileges_enabled = False
    hive_access = winreg.KEY_QUERY_VALUE | getattr(winreg, "KEY_ENUMERATE_SUB_KEYS", 0x0008)
    for sid, profile_path in profiles:
        candidates.append(
            profile_path / "AppData" / "Local" / "Programs" / DESKTOP_INSTALL_DIRECTORY_NAME / DESKTOP_EXECUTABLE_NAME
        )
        mounted_name: str | None = None
        loaded_by_us = False
        hive_snapshot: Path | None = None
        try:
            try:
                hive: Any = winreg.OpenKey(winreg.HKEY_USERS, sid, 0, hive_access)
            except FileNotFoundError:
                if not privileges_enabled:
                    _enable_registry_hive_privileges()
                    privileges_enabled = True
                hive_snapshot = _snapshot_profile_hive(
                    profile_path,
                    snapshot_directory,
                    state_reader_sid=state_reader_sid,
                    transaction_id=transaction_id,
                )
                candidate_mount = f"ERechnungsPrueferAudit_{transaction_id}_{secrets.token_hex(12)}"
                try:
                    winreg.LoadKey(winreg.HKEY_USERS, candidate_mount, str(hive_snapshot))
                except OSError:
                    try:
                        hive = winreg.OpenKey(winreg.HKEY_USERS, sid, 0, hive_access)
                    except FileNotFoundError as exc:
                        raise RuntimeError(
                            f"Das Benutzerprofil {sid} konnte nicht sicher auf eine Desktopinstallation geprüft werden."
                        ) from exc
                else:
                    mounted_name = candidate_mount
                    loaded_by_us = True
                    hive = winreg.OpenKey(winreg.HKEY_USERS, mounted_name, 0, hive_access)
            with hive:
                registered = _registered_install_location(hive, winreg=winreg)
                autostart = _registered_autostart(hive, winreg=winreg)
            if registered is not None:
                candidates.append(registered)
            if autostart is not None:
                raise RuntimeError(
                    f"Das Benutzerprofil {sid} enthält weiterhin einen Desktop-Autostart und blockiert den Dienstmodus."
                )
        finally:
            cleanup_error: RuntimeError | None = None
            cleanup_cause: OSError | None = None
            if loaded_by_us and mounted_name is not None:
                try:
                    winreg.UnloadKey(winreg.HKEY_USERS, mounted_name)
                except OSError as exc:
                    cleanup_error = RuntimeError(
                        f"Das temporär geladene Benutzerprofil {sid} konnte nicht sicher freigegeben werden."
                    )
                    cleanup_cause = exc
            if hive_snapshot is not None:
                try:
                    _remove_profile_hive_snapshot(
                        hive_snapshot,
                        expected_transaction_id=transaction_id,
                    )
                except RuntimeError as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
            if cleanup_error is not None:
                if cleanup_cause is not None:
                    raise cleanup_error from cleanup_cause
                raise cleanup_error
    unique = {_canonical_windows_path(candidate): candidate for candidate in candidates}
    return tuple(unique.values())


def _running_legacy_desktop_processes() -> tuple[int, ...]:
    if sys.platform != "win32":
        raise OSError("Die maschinenweite Prozessinventur ist ausschließlich unter Windows verfügbar.")

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    ctypes_windows: Any = ctypes
    kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    create_snapshot.restype = ctypes.c_void_p
    process_first = kernel32.Process32FirstW
    process_first.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry32W)]
    process_first.restype = ctypes.c_bool
    process_next = kernel32.Process32NextW
    process_next.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry32W)]
    process_next.restype = ctypes.c_bool
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_bool
    snapshot = create_snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise OSError(ctypes_windows.get_last_error(), "Die Prozessinventur konnte nicht gestartet werden.")
    entry = ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(entry)
    matches: list[int] = []
    try:
        has_entry = process_first(snapshot, ctypes.byref(entry))
        while has_entry:
            if entry.szExeFile.casefold() == DESKTOP_EXECUTABLE_NAME.casefold():
                matches.append(int(entry.th32ProcessID))
            has_entry = process_next(snapshot, ctypes.byref(entry))
        if ctypes_windows.get_last_error() != ERROR_NO_MORE_FILES:
            raise OSError(
                ctypes_windows.get_last_error(),
                "Die laufenden Prozesse konnten nicht vollständig inventarisiert werden.",
            )
    finally:
        close_handle(snapshot)
    return tuple(matches)


def _inventory_allowed_executable(receipt: MigrationReceipt) -> Path:
    executable = _validated_local_fixed_path(receipt.executable)
    if (
        not executable.is_absolute()
        or executable.name.casefold() != DESKTOP_EXECUTABLE_NAME.casefold()
        or "\x00" in receipt.executable
    ):
        raise RuntimeError("Der erlaubte Desktop-Pfad im Migrationsbeleg ist ungültig.")
    expected_disabled = executable.with_name(executable.name + DISABLED_SUFFIX)
    if receipt.disabled_executable is not None and Path(receipt.disabled_executable) != expected_disabled:
        raise RuntimeError("Der Quarantänepfad im Migrationsbeleg ist ungültig.")
    with _locked_local_path(expected_disabled, directory=False) as disabled_exists:
        pass
    if receipt.disabled_executable is None:
        if disabled_exists:
            raise RuntimeError("Der Migrationsbeleg verschweigt eine vorhandene Desktop-Quarantänekopie.")
    else:
        if not disabled_exists:
            raise RuntimeError("Die im Migrationsbeleg erwartete Desktop-Quarantänekopie fehlt.")
    return executable


def verify_no_legacy_desktop_conflicts(receipt_path: Path | None = None) -> None:
    """Fail closed unless the receipt-bound v1.3 binary is the sole quarantined copy."""

    if sys.platform != "win32":
        raise OSError("Die maschinenweite Desktopinventur ist ausschließlich unter Windows verfügbar.")
    del receipt_path  # Mutable transfer plans are never authoritative after sealing.
    seal, phase = _load_migration_transaction(require_current_user=False)
    if phase.phase is not MigrationPhase.ROLLBACKABLE:
        raise RuntimeError("Die Desktopinventur ist in der aktuellen Migrationsphase nicht zulässig.")
    running = _running_legacy_desktop_processes()
    if running:
        process_list = ", ".join(str(process_id) for process_id in running)
        raise RuntimeError(f"Laufende Desktop-Altprozesse wurden gefunden (PID {process_list}).")

    receipt = seal.receipt
    allowed = _canonical_windows_path(_inventory_allowed_executable(receipt))
    state_directory, _seal_path = _migration_state_paths()
    for candidate in _profile_installation_candidates(
        snapshot_directory=state_directory,
        state_reader_sid=seal.reader_sid,
        transaction_id=seal.transaction_id,
    ):
        canonical = _canonical_windows_path(candidate)
        disabled = candidate.with_name(candidate.name + DISABLED_SUFFIX)
        if canonical == allowed:
            with _locked_local_path(candidate, directory=False) as active_exists:
                if active_exists:
                    raise RuntimeError("Die aktuelle Desktop-EXE wurde nicht sicher quarantänisiert.")
            continue
        with _locked_local_path(candidate, directory=False) as active_exists:
            pass
        with _locked_local_path(disabled, directory=False) as disabled_exists:
            pass
        if active_exists or disabled_exists:
            raise RuntimeError(
                "In einem weiteren Benutzerprofil wurde eine Desktopinstallation gefunden. "
                "Deinstallieren Sie alle weiteren Desktopinstallationen vor dem Dienstmodus."
            )


def expected_autostart_command() -> str:
    return f'"{desktop_executable()}" --background'


def validate_autostart_command(value: Any) -> str:
    if not isinstance(value, str) or value != expected_autostart_command():
        raise RuntimeError("Der vorhandene HKCU-Autostart gehört nicht eindeutig zur Desktopinstallation.")
    return value


def _desktop_backend_is_running() -> bool:
    ctypes_windows: Any = ctypes
    kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
    open_mutex = kernel32.OpenMutexW
    open_mutex.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_wchar_p]
    open_mutex.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_bool
    mutex = open_mutex(SYNCHRONIZE, False, WINDOWS_MUTEX_NAME)
    if not mutex:
        error = ctypes_windows.get_last_error()
        if error == ERROR_FILE_NOT_FOUND:
            return False
        raise OSError(error, "Der Desktop-Mutex konnte nicht geprüft werden.")
    if not close_handle(mutex):
        raise OSError(
            ctypes_windows.get_last_error(),
            "Der Desktop-Mutex konnte nach der Prüfung nicht freigegeben werden.",
        )
    return True


def _stop_desktop_backend() -> bool:
    ctypes_windows: Any = ctypes
    kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
    open_mutex = kernel32.OpenMutexW
    open_mutex.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_wchar_p]
    open_mutex.restype = ctypes.c_void_p
    mutex = open_mutex(SYNCHRONIZE, False, WINDOWS_MUTEX_NAME)
    if not mutex:
        error = ctypes_windows.get_last_error()
        if error == ERROR_FILE_NOT_FOUND:
            return False
        raise OSError(error, "Der Desktop-Mutex konnte nicht geprüft werden.")

    open_event = kernel32.OpenEventW
    open_event.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_wchar_p]
    open_event.restype = ctypes.c_void_p
    set_event = kernel32.SetEvent
    set_event.argtypes = [ctypes.c_void_p]
    set_event.restype = ctypes.c_bool
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_bool
    event = open_event(EVENT_MODIFY_STATE, False, WINDOWS_SHUTDOWN_EVENT_NAME)
    try:
        if not event:
            raise RuntimeError(
                "Die laufende Desktop-App unterstützt das kontrollierte Beenden nicht; "
                "sie muss vor der Migration manuell beendet werden."
            )
        if not set_event(event):
            raise OSError(
                ctypes_windows.get_last_error(),
                "Das Desktop-Shutdown-Ereignis konnte nicht signalisiert werden.",
            )
        close_handle(mutex)
        mutex = None
        deadline = time.monotonic() + MIGRATION_TIMEOUT_MILLISECONDS / 1000
        while time.monotonic() < deadline:
            candidate = open_mutex(SYNCHRONIZE, False, WINDOWS_MUTEX_NAME)
            if not candidate:
                error = ctypes_windows.get_last_error()
                if error == ERROR_FILE_NOT_FOUND:
                    return True
                raise OSError(error, "Der Desktop-Mutex konnte beim Beenden nicht geprüft werden.")
            close_handle(candidate)
            time.sleep(0.1)
        raise RuntimeError("Die Desktop-App wurde nicht innerhalb von 30 Sekunden beendet.")
    finally:
        if event:
            close_handle(event)
        if mutex:
            close_handle(mutex)


def _desktop_migration_transfer_root() -> Path:
    program_data = ServicePaths.from_environment().data_directory.parent
    return program_data / DESKTOP_MIGRATION_TRANSFER_ROOT_NAME


def _transfer_path_key(path: Path) -> str:
    return ntpath.normcase(ntpath.normpath(str(path)))


def _validate_transfer_component(name: str, *, description: str) -> None:
    if (
        not name
        or len(name) > 128
        or name in {".", ".."}
        or name.rstrip(" .") != name
        or any(not (character.isascii() and (character.isalnum() or character in "._-")) for character in name)
        or name.split(".", 1)[0].casefold() in RESERVED_DOS_NAMES
    ):
        raise RuntimeError(f"{description} besitzt keinen sicheren Dateinamen.")


def _validate_transfer_layout(transfer_directory: Path, client_name: str) -> Path:
    _validate_transfer_component(transfer_directory.name, description="Das Desktop-Migrationstransferverzeichnis")
    _validate_transfer_component(client_name, description="Der Desktop-Migrationsclient")
    reserved_names = {
        DESKTOP_MIGRATION_TRANSFER_RECEIPT_NAME.casefold(),
        DESKTOP_MIGRATION_TRANSFER_TOKEN_NAME.casefold(),
    }
    if client_name.casefold() in reserved_names:
        raise RuntimeError("Der Desktop-Migrationsclient kollidiert mit einer Transferdatei.")
    root = _desktop_migration_transfer_root()
    if (
        not transfer_directory.is_absolute()
        or any(part in {".", ".."} for part in transfer_directory.parts)
        or _transfer_path_key(transfer_directory.parent) != _transfer_path_key(root)
    ):
        raise RuntimeError(
            "Das Desktop-Migrationstransferverzeichnis liegt nicht direkt unter dem geschützten ProgramData-Pfad."
        )
    return root


def _transfer_staging_mask(kind: str, *, ntsecuritycon: Any) -> int | None:
    if kind == "private-directory":
        return None
    if kind == "root":
        return int(getattr(ntsecuritycon, "FILE_TRAVERSE", 0x20))
    if kind == "leaf":
        return (
            int(getattr(ntsecuritycon, "FILE_TRAVERSE", 0x20))
            | int(getattr(ntsecuritycon, "FILE_READ_ATTRIBUTES", 0x80))
            | int(getattr(ntsecuritycon, "FILE_ADD_FILE", 0x02))
        )
    if kind == "client":
        return int(ntsecuritycon.FILE_GENERIC_READ) | int(getattr(ntsecuritycon, "FILE_GENERIC_EXECUTE", 0x1200A0))
    raise ValueError(f"Unbekannte Transfer-DACL-Art: {kind}")


def _transfer_staging_security_attributes(kind: str) -> Any:
    pywintypes, _win32api, _win32con, _win32file, win32security, ntsecuritycon = _migration_windows_modules()
    dacl = win32security.ACL()
    for sid_text in (SYSTEM_SID, ADMINISTRATORS_SID):
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS,
            0,
            ntsecuritycon.FILE_ALL_ACCESS,
            win32security.ConvertStringSidToSid(sid_text),
        )
    interactive_mask = _transfer_staging_mask(kind, ntsecuritycon=ntsecuritycon)
    if interactive_mask is not None:
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS,
            0,
            interactive_mask,
            win32security.ConvertStringSidToSid(INTERACTIVE_SID),
        )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(win32security.ConvertStringSidToSid(ADMINISTRATORS_SID), 0)
    descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
    descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED,
        win32security.SE_DACL_PROTECTED,
    )
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def _verify_transfer_staging_path(path: Path, *, directory: bool, kind: str) -> None:
    if not validate_machine_path(path, directory=directory):
        raise RuntimeError(f"Der geschützte Desktop-Transferpfad {path} fehlt.")
    _pywintypes, _win32api, _win32con, _win32file, win32security, ntsecuritycon = _migration_windows_modules()
    try:
        descriptor = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | getattr(win32security, "OWNER_SECURITY_INFORMATION", 0x00000001),
        )
        owner_sid = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
        dacl = descriptor.GetSecurityDescriptorDacl()
        control, _revision = descriptor.GetSecurityDescriptorControl()
    except Exception as exc:
        raise RuntimeError(f"Der geschützte Desktop-Transferpfad {path} besitzt keine prüfbare DACL.") from exc
    if owner_sid != ADMINISTRATORS_SID:
        raise RuntimeError("Ein Desktop-Transferpfad gehört nicht der Administratorengruppe.")
    if dacl is None or not control & win32security.SE_DACL_PROTECTED:
        raise RuntimeError("Die DACL eines Desktop-Transferpfads ist nicht vor Vererbung geschützt.")

    expected_masks = {
        SYSTEM_SID: int(ntsecuritycon.FILE_ALL_ACCESS),
        ADMINISTRATORS_SID: int(ntsecuritycon.FILE_ALL_ACCESS),
    }
    interactive_mask = _transfer_staging_mask(kind, ntsecuritycon=ntsecuritycon)
    if interactive_mask is not None:
        expected_masks[INTERACTIVE_SID] = interactive_mask
    observed: set[str] = set()
    for index in range(dacl.GetAceCount()):
        header, mask, sid = dacl.GetAce(index)
        sid_text = win32security.ConvertSidToStringSid(sid)
        if (
            int(header[0]) != win32security.ACCESS_ALLOWED_ACE_TYPE
            or int(header[1]) != 0
            or sid_text not in expected_masks
            or sid_text in observed
            or int(mask) != expected_masks[sid_text]
        ):
            raise RuntimeError("Ein Desktop-Transferpfad besitzt eine unerwartete Windows-Berechtigung.")
        observed.add(sid_text)
    if observed != set(expected_masks) or dacl.GetAceCount() != len(expected_masks):
        raise RuntimeError("Ein Desktop-Transferpfad besitzt nicht die exakt erforderliche Windows-DACL.")


def _set_transfer_staging_acl(path: Path, *, kind: str) -> None:
    attributes = _transfer_staging_security_attributes(kind)
    descriptor = attributes.SECURITY_DESCRIPTOR
    _pywintypes, _win32api, _win32con, _win32file, win32security, _ntsecuritycon = _migration_windows_modules()
    information = win32security.DACL_SECURITY_INFORMATION | getattr(
        win32security,
        "PROTECTED_DACL_SECURITY_INFORMATION",
        0x80000000,
    )
    try:
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            information,
            None,
            None,
            descriptor.GetSecurityDescriptorDacl(),
            None,
        )
    except Exception as exc:
        raise RuntimeError(
            "Die DACL des Desktop-Migrationstransferverzeichnisses konnte nicht gesetzt werden."
        ) from exc
    _verify_transfer_staging_path(path, directory=True, kind=kind)


def _create_transfer_staging_directory(path: Path, *, kind: str) -> None:
    _pywintypes, _win32api, _win32con, win32file, _win32security, _ntsecuritycon = _migration_windows_modules()
    try:
        win32file.CreateDirectoryW(str(path), _transfer_staging_security_attributes(kind))
    except Exception as exc:
        raise RuntimeError(f"Der geschützte Desktop-Transferpfad {path} konnte nicht erstellt werden.") from exc
    _verify_transfer_staging_path(path, directory=True, kind=kind)


def _ensure_transfer_staging_root(root: Path) -> None:
    if not validate_machine_path(root.parent, directory=True):
        raise RuntimeError("Der Windows-ProgramData-Pfad für die Desktopmigration fehlt.")
    if validate_machine_path(root, directory=True):
        _verify_transfer_staging_path(root, directory=True, kind="root")
        return
    try:
        _create_transfer_staging_directory(root, kind="root")
    except RuntimeError:
        if not validate_machine_path(root, directory=True):
            raise
        _verify_transfer_staging_path(root, directory=True, kind="root")


def _transfer_directory_entries(directory: Path) -> tuple[str, ...]:
    try:
        with os.scandir(directory) as iterator:
            names = tuple(entry.name for entry in iterator)
    except OSError as exc:
        raise RuntimeError("Das Desktop-Migrationstransferverzeichnis konnte nicht inventarisiert werden.") from exc
    folded = {name.casefold() for name in names}
    if len(folded) != len(names):
        raise RuntimeError("Das Desktop-Migrationstransferverzeichnis enthält mehrdeutige Dateinamen.")
    return names


def _hash_locked_transfer_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with _locked_local_path(path, directory=False) as exists:
            if not exists:
                raise FileNotFoundError(path)
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError(f"Die Desktop-Transferdatei {path} konnte nicht sicher gelesen werden.") from exc
    return size, digest.hexdigest()


def _copy_transfer_client_atomic(source: Path, target: Path, temporary: Path) -> Path:
    if validate_machine_path(target, directory=False):
        raise RuntimeError("Der Desktop-Migrationsclient ist bereits im Transferverzeichnis vorhanden.")
    _pywintypes, _win32api, win32con, win32file, _win32security, _ntsecuritycon = _migration_windows_modules()
    output_handle = None
    created = False
    source_size = 0
    source_digest = hashlib.sha256()
    failure: Exception | None = None
    try:
        with _locked_local_path(source, directory=False) as source_exists:
            if not source_exists:
                raise RuntimeError("Der zu übertragende Desktop-Migrationsclient fehlt.")
            with source.open("rb") as source_handle:
                output_handle = win32file.CreateFile(
                    str(temporary),
                    win32con.GENERIC_WRITE,
                    0,
                    _transfer_staging_security_attributes("client"),
                    win32con.CREATE_NEW,
                    getattr(win32con, "FILE_ATTRIBUTE_NORMAL", 0x80),
                    None,
                )
                created = True
                while chunk := source_handle.read(1024 * 1024):
                    source_size += len(chunk)
                    source_digest.update(chunk)
                    win32file.WriteFile(output_handle, chunk)
                if source_size == 0:
                    raise RuntimeError("Der zu übertragende Desktop-Migrationsclient ist leer.")
                win32file.FlushFileBuffers(output_handle)
    except Exception as exc:
        failure = exc
    finally:
        if output_handle is not None:
            try:
                win32file.CloseHandle(output_handle)
            except Exception as exc:
                if failure is None:
                    failure = exc
    if failure is not None:
        if created:
            try:
                if validate_machine_path(temporary, directory=False):
                    _verify_transfer_staging_path(temporary, directory=False, kind="client")
                    temporary.unlink()
            except (OSError, RuntimeError):
                pass
        if isinstance(failure, RuntimeError):
            raise failure
        raise RuntimeError("Der Desktop-Migrationsclient konnte nicht geschützt kopiert werden.") from failure

    _verify_transfer_staging_path(temporary, directory=False, kind="client")
    try:
        win32file.MoveFileEx(str(temporary), str(target), MOVEFILE_WRITE_THROUGH)
    except Exception as exc:
        raise RuntimeError("Der Desktop-Migrationsclient konnte nicht atomar veröffentlicht werden.") from exc
    _verify_transfer_staging_path(target, directory=False, kind="client")
    target_size, target_digest = _hash_locked_transfer_file(target)
    if target_size != source_size or target_digest != source_digest.hexdigest():
        raise RuntimeError("Der geschützte Desktop-Migrationsclient stimmt nicht mit der Quelldatei überein.")
    return target


def _remove_empty_transfer_directory(path: Path, *, allow_nonempty: bool) -> bool:
    try:
        path.rmdir()
    except OSError as exc:
        is_nonempty = exc.errno in {errno.ENOTEMPTY, errno.EEXIST} or getattr(exc, "winerror", None) == 145
        if allow_nonempty and is_nonempty:
            return False
        raise RuntimeError(f"Das leere Desktop-Transferverzeichnis {path} konnte nicht entfernt werden.") from exc
    return True


def _clear_failed_transfer_prepare(
    root: Path,
    transfer_directory: Path,
    *,
    client_path: Path,
    temporary_path: Path,
    leaf_kind: str,
) -> None:
    if not validate_machine_path(transfer_directory, directory=True):
        if validate_machine_path(root, directory=True):
            _verify_transfer_staging_path(root, directory=True, kind="root")
            _remove_empty_transfer_directory(root, allow_nonempty=True)
        return
    _verify_transfer_staging_path(transfer_directory, directory=True, kind=leaf_kind)
    names = set(_transfer_directory_entries(transfer_directory))
    known_paths = {client_path.name: client_path, temporary_path.name: temporary_path}
    if not names <= set(known_paths):
        raise RuntimeError("Ein fehlgeschlagener Desktop-Transfer enthält unerwartete Einträge.")
    for name in names:
        _verify_transfer_staging_path(known_paths[name], directory=False, kind="client")
    for name in names:
        known_paths[name].unlink()
    _remove_empty_transfer_directory(transfer_directory, allow_nonempty=False)
    if validate_machine_path(root, directory=True):
        _verify_transfer_staging_path(root, directory=True, kind="root")
        _remove_empty_transfer_directory(root, allow_nonempty=True)


def prepare_desktop_migration_transfer(
    transfer_directory: Path,
    client_source: Path,
    client_name: str,
) -> Path:
    """Publish the unelevated migration helper in a least-privilege ProgramData leaf."""

    if sys.platform != "win32":
        raise OSError("Der Desktop-Migrationstransfer ist ausschließlich unter Windows verfügbar.")
    root = _validate_transfer_layout(transfer_directory, client_name)
    _ensure_transfer_staging_root(root)
    if validate_machine_path(transfer_directory, directory=True):
        _verify_transfer_staging_path(transfer_directory, directory=True, kind="leaf")
        if set(_transfer_directory_entries(transfer_directory)) != {client_name}:
            raise RuntimeError("Ein vorhandener Desktop-Transfer enthält nicht ausschließlich den Migrationsclient.")
        existing_client = transfer_directory / client_name
        _verify_transfer_staging_path(existing_client, directory=False, kind="client")
        if _hash_locked_transfer_file(existing_client) != _hash_locked_transfer_file(client_source):
            raise RuntimeError("Ein vorhandener Desktop-Migrationsclient stimmt nicht mit der Quelldatei überein.")
        return existing_client
    client_path = transfer_directory / client_name
    temporary_path = transfer_directory / f".{client_name}.{secrets.token_hex(16)}.tmp"
    leaf_kind = "private-directory"
    try:
        _create_transfer_staging_directory(transfer_directory, kind=leaf_kind)
        _copy_transfer_client_atomic(client_source, client_path, temporary_path)
        _set_transfer_staging_acl(transfer_directory, kind="leaf")
        leaf_kind = "leaf"
        if set(_transfer_directory_entries(transfer_directory)) != {client_name}:
            raise RuntimeError("Das Desktop-Migrationstransferverzeichnis enthält unerwartete Einträge.")
        return client_path
    except Exception:
        try:
            _clear_failed_transfer_prepare(
                root,
                transfer_directory,
                client_path=client_path,
                temporary_path=temporary_path,
                leaf_kind=leaf_kind,
            )
        except (OSError, RuntimeError):
            pass
        raise


def _require_transfer_file_path(path: Path, expected: Path, *, description: str) -> None:
    if (
        any(part in {".", ".."} for part in path.parts)
        or path.name != expected.name
        or _transfer_path_key(path.parent) != _transfer_path_key(expected.parent)
    ):
        raise RuntimeError(f"{description} liegt nicht am erwarteten Desktop-Transferpfad.")


def _validate_transfer_inventory(
    transfer_directory: Path,
    *,
    receipt_path: Path,
    token_transfer_path: Path | None,
    client_name: str,
    exact: bool,
) -> str | None:
    client_path = transfer_directory / client_name
    expected_names = {client_name, DESKTOP_MIGRATION_TRANSFER_RECEIPT_NAME}
    if token_transfer_path is not None:
        expected_names.add(DESKTOP_MIGRATION_TRANSFER_TOKEN_NAME)
    names = set(_transfer_directory_entries(transfer_directory))
    if (exact and names != expected_names) or (not exact and not names <= expected_names):
        raise RuntimeError("Das Desktop-Migrationstransferverzeichnis enthält unerwartete Einträge.")

    if client_name in names:
        _verify_transfer_staging_path(client_path, directory=False, kind="client")
    reader_sid: str | None = None
    if DESKTOP_MIGRATION_TRANSFER_RECEIPT_NAME in names:
        reader_sid = _receipt_owner_sid(receipt_path)
    if DESKTOP_MIGRATION_TRANSFER_TOKEN_NAME in names:
        if token_transfer_path is None:
            raise RuntimeError("Eine unerwartete Desktop-Token-Transferdatei ist vorhanden.")
        token_reader_sid = _receipt_owner_sid(token_transfer_path)
        if reader_sid is not None and token_reader_sid != reader_sid:
            raise RuntimeError("Desktop-Migrationsbeleg und Token gehören unterschiedlichen Benutzeridentitäten.")
        reader_sid = token_reader_sid if reader_sid is None else reader_sid
    return reader_sid


def validate_desktop_migration_transfer(
    transfer_directory: Path,
    receipt_path: Path,
    token_transfer_path: Path | None,
    client_name: str,
) -> None:
    """Validate exact post-planning inventory before sealing the user-owned files."""

    if sys.platform != "win32":
        raise OSError("Der Desktop-Migrationstransfer ist ausschließlich unter Windows verfügbar.")
    root = _validate_transfer_layout(transfer_directory, client_name)
    expected_receipt = transfer_directory / DESKTOP_MIGRATION_TRANSFER_RECEIPT_NAME
    _require_transfer_file_path(receipt_path, expected_receipt, description="Der Desktop-Migrationsbeleg")
    if token_transfer_path is not None:
        expected_token = transfer_directory / DESKTOP_MIGRATION_TRANSFER_TOKEN_NAME
        _require_transfer_file_path(
            token_transfer_path,
            expected_token,
            description="Das Desktop-API-Token",
        )
    _verify_transfer_staging_path(root, directory=True, kind="root")
    _verify_transfer_staging_path(transfer_directory, directory=True, kind="leaf")
    reader_sid = _validate_transfer_inventory(
        transfer_directory,
        receipt_path=receipt_path,
        token_transfer_path=token_transfer_path,
        client_name=client_name,
        exact=True,
    )
    if reader_sid is None:
        raise RuntimeError("Der Desktop-Migrationsbeleg besitzt keine eindeutige Benutzeridentität.")
    _verify_transfer_staging_path(root, directory=True, kind="root")
    _verify_transfer_staging_path(transfer_directory, directory=True, kind="leaf")
    repeated_reader_sid = _validate_transfer_inventory(
        transfer_directory,
        receipt_path=receipt_path,
        token_transfer_path=token_transfer_path,
        client_name=client_name,
        exact=True,
    )
    if repeated_reader_sid != reader_sid:
        raise RuntimeError("Die Benutzeridentität des Desktop-Migrationstransfers änderte sich während der Prüfung.")


def clear_desktop_migration_transfer(
    transfer_directory: Path,
    client_name: str,
) -> None:
    """Delete only a fully inventoried transfer leaf and empty parents, never recursively."""

    if sys.platform != "win32":
        raise OSError("Der Desktop-Migrationstransfer ist ausschließlich unter Windows verfügbar.")
    root = _validate_transfer_layout(transfer_directory, client_name)
    if not validate_machine_path(root, directory=True):
        return
    _verify_transfer_staging_path(root, directory=True, kind="root")
    if not validate_machine_path(transfer_directory, directory=True):
        _remove_empty_transfer_directory(root, allow_nonempty=True)
        return
    _verify_transfer_staging_path(transfer_directory, directory=True, kind="leaf")
    receipt_path = transfer_directory / DESKTOP_MIGRATION_TRANSFER_RECEIPT_NAME
    token_path = transfer_directory / DESKTOP_MIGRATION_TRANSFER_TOKEN_NAME
    names = set(_transfer_directory_entries(transfer_directory))
    token_transfer_path = token_path if DESKTOP_MIGRATION_TRANSFER_TOKEN_NAME in names else None
    _validate_transfer_inventory(
        transfer_directory,
        receipt_path=receipt_path,
        token_transfer_path=token_transfer_path,
        client_name=client_name,
        exact=False,
    )
    repeated_names = set(_transfer_directory_entries(transfer_directory))
    if repeated_names != names:
        raise RuntimeError("Das Desktop-Migrationstransferverzeichnis änderte sich während der Bereinigung.")
    _validate_transfer_inventory(
        transfer_directory,
        receipt_path=receipt_path,
        token_transfer_path=token_transfer_path,
        client_name=client_name,
        exact=False,
    )
    known_paths = {
        client_name: transfer_directory / client_name,
        DESKTOP_MIGRATION_TRANSFER_RECEIPT_NAME: receipt_path,
        DESKTOP_MIGRATION_TRANSFER_TOKEN_NAME: token_path,
    }
    for name in sorted(names):
        known_paths[name].unlink()
    _remove_empty_transfer_directory(transfer_directory, allow_nonempty=False)
    _verify_transfer_staging_path(root, directory=True, kind="root")
    _remove_empty_transfer_directory(root, allow_nonempty=True)


def _transfer_security_attributes() -> tuple[Any, Any, Any, Any]:
    try:
        import ntsecuritycon
        import pywintypes
        import win32api
        import win32con
        import win32file
        import win32security
    except ImportError as exc:
        raise RuntimeError("pywin32 fehlt; die Tokenmigration kann nicht geschützt werden.") from exc
    dacl = win32security.ACL()
    process_token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        current_user = win32security.GetTokenInformation(process_token, win32security.TokenUser)[0]
    finally:
        process_token.Close()
    accounts = (
        win32security.ConvertStringSidToSid("S-1-5-18"),
        win32security.ConvertStringSidToSid("S-1-5-32-544"),
        current_user,
    )
    for sid in accounts:
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
    security_descriptor = win32security.SECURITY_DESCRIPTOR()
    security_descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
    security_descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED,
        win32security.SE_DACL_PROTECTED,
    )
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = security_descriptor
    return win32con, win32file, attributes, security_descriptor


def _write_private(path: Path, payload: bytes) -> None:
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        win32con, win32file, attributes, _descriptor = _transfer_security_attributes()
        handle = win32file.CreateFile(
            str(path),
            win32con.GENERIC_WRITE,
            0,
            attributes,
            win32con.CREATE_NEW,
            win32con.FILE_ATTRIBUTE_TEMPORARY,
            None,
        )
        try:
            try:
                win32file.WriteFile(handle, payload)
                win32file.FlushFileBuffers(handle)
            finally:
                win32file.CloseHandle(handle)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def plan_desktop_migration(*, receipt_path: Path, token_transfer_path: Path | None) -> None:
    """Capture a private rollback plan without changing HKCU, processes, or binaries."""

    if sys.platform != "win32":
        raise OSError("Die Desktopmigration ist ausschließlich unter Windows verfügbar.")
    import winreg

    executable = desktop_executable()
    autostart_command: str | None = None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_KEY,
            0,
            winreg.KEY_QUERY_VALUE,
        ) as key:
            try:
                value, value_type = winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
            except FileNotFoundError:
                value = None
                value_type = None
            if value is not None:
                if value_type != winreg.REG_SZ:
                    raise RuntimeError("Der vorhandene HKCU-Autostart hat einen unerwarteten Registrytyp.")
                autostart_command = validate_autostart_command(value)
    except FileNotFoundError:
        pass

    token_payload: bytes | None = None
    if token_transfer_path is not None:
        try:
            token = read_desktop_migration_token(desktop_token_file())
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("Das Desktop-API-Token konnte nicht sicher für die Migration gelesen werden.") from exc
        token_payload = (token + "\n").encode("ascii")

    disabled_executable: Path | None = None
    expected_disabled = executable.with_name(executable.name + DISABLED_SUFFIX)
    with _locked_local_path(executable, directory=False) as executable_exists:
        pass
    with _locked_local_path(expected_disabled, directory=False) as disabled_exists:
        pass
    if disabled_exists:
        raise RuntimeError("Eine unvollständige frühere Desktopmigration wurde gefunden.")
    if executable_exists:
        disabled_executable = executable.with_name(executable.name + DISABLED_SUFFIX)

    was_running = _desktop_backend_is_running()
    if was_running and disabled_executable is None:
        raise RuntimeError("Eine laufende Desktop-App besitzt keine sicher wiederherstellbare Programmdatei.")
    if autostart_command is not None and disabled_executable is None:
        raise RuntimeError("Der Desktop-Autostart verweist auf eine fehlende Programmdatei.")
    receipt = MigrationReceipt(
        autostart_command,
        was_running,
        str(executable),
        str(disabled_executable) if disabled_executable is not None else None,
    )
    try:
        _write_private(
            receipt_path,
            (json.dumps(asdict(receipt), ensure_ascii=True) + "\n").encode("utf-8"),
        )
        if token_transfer_path is not None and token_payload is not None:
            _write_private(token_transfer_path, token_payload)
    except Exception:
        if token_transfer_path is not None:
            token_transfer_path.unlink(missing_ok=True)
        receipt_path.unlink(missing_ok=True)
        raise


def prepare_desktop_migration(*, receipt_path: Path, token_transfer_path: Path | None) -> None:
    """Compatibility alias: preparation is now the read-only planning step."""

    plan_desktop_migration(
        receipt_path=receipt_path,
        token_transfer_path=token_transfer_path,
    )


def _desktop_runtime_path() -> Path:
    return _local_app_data() / APP_DIRECTORY_NAME / DESKTOP_RUNTIME_FILE_NAME


def _read_desktop_runtime_identity(path: Path) -> tuple[int, int] | None:
    try:
        payload = _read_locked_bytes(
            path,
            maximum_bytes=MAXIMUM_DESKTOP_RUNTIME_BYTES,
            description="Der Desktop-Laufzeitbeleg",
        )
        decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_json_object)
        if not isinstance(decoded, dict) or set(decoded) != {"pid", "port", "token"}:
            return None
        if type(decoded["pid"]) is not int or type(decoded["port"]) is not int:
            return None
        if not isinstance(decoded["token"], str):
            return None
        validate_api_token(decoded["token"], description="Das Desktop-Laufzeittoken")
    except (RuntimeError, UnicodeError, ValueError):
        return None
    pid = decoded["pid"]
    port = decoded["port"]
    if pid <= 0 or not 1 <= port <= 65535:
        return None
    return pid, port


def _desktop_health_is_ready(port: int) -> bool:
    from .server_runtime import health_is_ready

    return health_is_ready(port, timeout=0.5)


def _desktop_runtime_has_start_proof() -> bool:
    """Bind an existing Desktop mutex to its runtime PID, port, and healthy API."""

    runtime = _read_desktop_runtime_identity(_desktop_runtime_path())
    if runtime is None:
        return False
    runtime_pid, port = runtime
    if runtime_pid not in _running_legacy_desktop_processes():
        return False
    return _desktop_health_is_ready(port)


def _restart_desktop(
    executable: Path,
    *,
    timeout_seconds: float = MIGRATION_TIMEOUT_MILLISECONDS / 1000,
    poll_seconds: float = 0.1,
    _runtime_reader: Callable[[Path], tuple[int, int] | None] | None = None,
    _health_probe: Callable[[int], bool] | None = None,
    _mutex_probe: Callable[[], bool] | None = None,
    _popen: Callable[..., Any] | None = None,
    _monotonic: Callable[[], float] | None = None,
    _sleep: Callable[[float], None] | None = None,
) -> None:
    if not executable.is_file():
        raise RuntimeError("Die zuvor laufende Desktop-App ist für die Rücknahme nicht mehr vorhanden.")
    if timeout_seconds <= 0 or poll_seconds < 0:
        raise RuntimeError("Der Desktop-Startnachweis besitzt ein ungültiges Zeitlimit.")
    process = (_popen or subprocess.Popen)(
        [str(executable), "--background"],
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    runtime_reader = _runtime_reader or _read_desktop_runtime_identity
    health_probe = _health_probe or _desktop_health_is_ready
    mutex_probe = _mutex_probe or _desktop_backend_is_running
    monotonic = _monotonic or time.monotonic
    sleep = _sleep or time.sleep
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Die wiederhergestellte Desktop-App wurde vor dem Startnachweis beendet.")
        runtime = runtime_reader(_desktop_runtime_path())
        if runtime is not None:
            runtime_pid, port = runtime
            if runtime_pid == process.pid and mutex_probe() and health_probe(port) and process.poll() is None:
                return
        sleep(poll_seconds)
    if process.poll() is not None:
        raise RuntimeError("Die wiederhergestellte Desktop-App wurde vor dem Startnachweis beendet.")
    raise RuntimeError("Die wiederhergestellte Desktop-App wurde nicht rechtzeitig betriebsbereit.")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"Doppeltes JSON-Feld: {key}")
        payload[key] = value
    return payload


def _decode_receipt(serialized: bytes) -> MigrationReceipt:
    try:
        payload = json.loads(serialized.decode("utf-8"), object_pairs_hook=_unique_json_object)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"autostart_command", "was_running", "executable", "disabled_executable"}
            or type(payload["was_running"]) is not bool
            or not isinstance(payload["executable"], str)
            or payload["autostart_command"] is not None
            and not isinstance(payload["autostart_command"], str)
            or payload["disabled_executable"] is not None
            and not isinstance(payload["disabled_executable"], str)
        ):
            raise TypeError
        return MigrationReceipt(
            autostart_command=payload["autostart_command"],
            was_running=payload["was_running"],
            executable=payload["executable"],
            disabled_executable=payload["disabled_executable"],
        )
    except (UnicodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Der Desktop-Migrationsbeleg ist ungültig.") from exc


def _load_receipt(receipt_path: Path) -> MigrationReceipt:
    serialized = _read_locked_bytes(
        receipt_path,
        maximum_bytes=MAXIMUM_MIGRATION_RECEIPT_BYTES,
        description="Der Desktop-Migrationsbeleg",
    )
    return _decode_receipt(serialized)


def seal_desktop_migration(*, receipt_path: Path, token_transfer_path: Path | None) -> MigrationSeal:
    """Copy a private read-only plan into the fixed protected transaction state."""

    if sys.platform != "win32":
        raise OSError("Die Desktopmigration ist ausschließlich unter Windows verfügbar.")

    state_directory: Path | None = None
    reader_sid: str | None = None
    try:
        state_directory, seal_path, reader_sid = _prepare_migration_state(receipt_path)
        receipt = _load_receipt(receipt_path)
        if _receipt_owner_sid(receipt_path) != reader_sid:
            raise RuntimeError("Der Desktop-Migrationsplan wechselte während der geschützten Übernahme den Eigentümer.")
        _validate_receipt_paths(receipt, bind_to_current_registration=False)
        _validate_sealed_receipt_semantics(receipt)

        token_payload: bytes | None = None
        token_sha256: str | None = None
        if token_transfer_path is not None:
            if _receipt_owner_sid(token_transfer_path) != reader_sid:
                raise RuntimeError("Der Token-Transfer gehört einer anderen Benutzeridentität als der Migrationsplan.")
            token = read_desktop_migration_token(token_transfer_path)
            if _receipt_owner_sid(token_transfer_path) != reader_sid:
                raise RuntimeError("Der Token-Transfer wechselte während der geschützten Übernahme den Eigentümer.")
            token_payload = (token + "\n").encode("ascii")
            token_sha256 = hashlib.sha256(token_payload).hexdigest()

        seal = _store_migration_seal(
            seal_path,
            receipt,
            reader_sid=reader_sid,
            token_sha256=token_sha256,
        )
        if token_payload is not None:
            _store_migration_token(
                state_directory,
                token_payload,
                transaction_id=seal.transaction_id,
            )
        _store_initial_migration_phase(
            state_directory,
            transaction_id=seal.transaction_id,
            reader_sid=reader_sid,
        )
        stored_seal, stored_phase = _load_migration_transaction(require_current_user=False)
        if stored_seal != seal or stored_phase.phase is not MigrationPhase.ROLLBACKABLE:
            raise RuntimeError("Der geschützte Desktop-Migrationszustand wurde nicht vollständig gespeichert.")
        return seal
    except Exception:
        if state_directory is not None and reader_sid is not None:
            _clear_migration_state(
                expected_reader_sid=reader_sid,
                require_current_user=False,
            )
        raise


def _valid_transaction_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and value == value.casefold()
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_reader_sid(value: object) -> bool:
    return (
        isinstance(value, str)
        and 4 < len(value) <= 184
        and value.startswith("S-1-")
        and value not in {SYSTEM_SID, ADMINISTRATORS_SID}
        and "\x00" not in value
    )


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.casefold()
        and all(character in "0123456789abcdef" for character in value)
    )


def _decode_migration_seal(serialized: bytes) -> MigrationSeal:
    try:
        payload = json.loads(serialized.decode("utf-8"), object_pairs_hook=_unique_json_object)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "transaction_id", "reader_sid", "token_sha256", "receipt"}
            or type(payload["schema_version"]) is not int
            or payload["schema_version"] != MIGRATION_SCHEMA_VERSION
            or not _valid_transaction_id(payload["transaction_id"])
            or not _valid_reader_sid(payload["reader_sid"])
            or payload["token_sha256"] is not None
            and not _valid_sha256(payload["token_sha256"])
            or not isinstance(payload["receipt"], dict)
        ):
            raise TypeError
        receipt_bytes = json.dumps(
            payload["receipt"],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        receipt = _decode_receipt(receipt_bytes)
        return MigrationSeal(
            schema_version=payload["schema_version"],
            transaction_id=payload["transaction_id"],
            reader_sid=payload["reader_sid"],
            token_sha256=payload["token_sha256"],
            receipt=receipt,
        )
    except (UnicodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Der geschützte Desktop-Migrationsbeleg ist ungültig.") from exc


def _encode_migration_seal(seal: MigrationSeal) -> bytes:
    serialized = (
        json.dumps(
            asdict(seal),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(serialized) > MAXIMUM_MIGRATION_RECEIPT_BYTES or _decode_migration_seal(serialized) != seal:
        raise RuntimeError("Der geschützte Desktop-Migrationsbeleg überschreitet das zulässige Format.")
    return serialized


def _decode_migration_phase(serialized: bytes) -> MigrationPhaseRecord:
    try:
        payload = json.loads(serialized.decode("utf-8"), object_pairs_hook=_unique_json_object)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "transaction_id", "generation", "phase"}
            or type(payload["schema_version"]) is not int
            or payload["schema_version"] != MIGRATION_PHASE_SCHEMA_VERSION
            or not _valid_transaction_id(payload["transaction_id"])
            or type(payload["generation"]) is not int
            or not isinstance(payload["phase"], str)
        ):
            raise TypeError
        phase = MigrationPhase(payload["phase"])
        if payload["generation"] != MIGRATION_PHASE_GENERATIONS[phase]:
            raise ValueError
        return MigrationPhaseRecord(
            schema_version=payload["schema_version"],
            transaction_id=payload["transaction_id"],
            generation=payload["generation"],
            phase=phase,
        )
    except (UnicodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Die geschützte Desktop-Migrationsphase ist ungültig.") from exc


def _encode_migration_phase(record: MigrationPhaseRecord) -> bytes:
    serialized = (
        json.dumps(
            {
                "schema_version": record.schema_version,
                "transaction_id": record.transaction_id,
                "generation": record.generation,
                "phase": record.phase.value,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(serialized) > MAXIMUM_MIGRATION_PHASE_BYTES or _decode_migration_phase(serialized) != record:
        raise RuntimeError("Die geschützte Desktop-Migrationsphase überschreitet das zulässige Format.")
    return serialized


def _atomic_publish_migration_file(source: Path, target: Path) -> None:
    if sys.platform == "win32":
        _pywintypes, _win32api, _win32con, win32file, _win32security, _ntsecuritycon = _migration_windows_modules()
        try:
            win32file.MoveFileEx(
                str(source),
                str(target),
                MOVEFILE_WRITE_THROUGH,
            )
        except Exception as exc:
            raise RuntimeError("Eine initiale Migrationsdatei konnte nicht atomar veröffentlicht werden.") from exc
        return
    try:
        os.link(source, target)
        source.unlink()
    except FileExistsError:
        raise
    except OSError as exc:
        raise RuntimeError("Eine initiale Migrationsdatei konnte nicht atomar veröffentlicht werden.") from exc


def _write_atomic_initial_migration_file(
    target: Path,
    payload: bytes,
    *,
    transaction_id: str,
    temporary_prefix: str,
    reader_sid: str | None,
    maximum_bytes: int,
    description: str,
) -> bytes:
    if not _valid_transaction_id(transaction_id):
        raise RuntimeError("Eine initiale Migrationsdatei besitzt keine gültige Transaktionsbindung.")
    temporary = target.parent / f"{temporary_prefix}{transaction_id}-{secrets.token_hex(16)}.tmp"
    _write_secure_migration_file(temporary, payload, reader_sid=reader_sid)
    try:
        _atomic_publish_migration_file(temporary, target)
    except Exception:
        if validate_machine_path(temporary, directory=False):
            _verify_migration_state_path(
                temporary,
                directory=False,
                reader_required=reader_sid is not None,
                expected_reader_sid=reader_sid,
            )
            temporary.unlink()
        raise
    _verify_migration_state_path(
        target,
        directory=False,
        reader_required=reader_sid is not None,
        expected_reader_sid=reader_sid,
    )
    stored = _read_locked_bytes(
        target,
        maximum_bytes=maximum_bytes,
        description=description,
    )
    _verify_migration_state_path(
        target,
        directory=False,
        reader_required=reader_sid is not None,
        expected_reader_sid=reader_sid,
    )
    if stored != payload:
        raise RuntimeError(f"{description} wurde nicht unverändert atomar gespeichert.")
    return stored


def _store_migration_seal(
    seal_path: Path,
    receipt: MigrationReceipt,
    *,
    reader_sid: str,
    transaction_id: str | None = None,
    token_sha256: str | None = None,
) -> MigrationSeal:
    seal = MigrationSeal(
        schema_version=MIGRATION_SCHEMA_VERSION,
        transaction_id=transaction_id or secrets.token_hex(16),
        reader_sid=reader_sid,
        token_sha256=token_sha256,
        receipt=receipt,
    )
    serialized = _encode_migration_seal(seal)
    stored = _write_atomic_initial_migration_file(
        seal_path,
        serialized,
        transaction_id=seal.transaction_id,
        temporary_prefix=MIGRATION_SEAL_TEMP_FILE_PREFIX,
        reader_sid=reader_sid,
        maximum_bytes=MAXIMUM_MIGRATION_RECEIPT_BYTES,
        description="Der geschützte Desktop-Migrationsbeleg",
    )
    if _decode_migration_seal(stored) != seal:
        raise RuntimeError("Der geschützte Desktop-Migrationsbeleg wurde nicht unverändert gespeichert.")
    return seal


def _store_initial_migration_phase(
    state_directory: Path,
    *,
    transaction_id: str,
    reader_sid: str,
) -> MigrationPhaseRecord:
    phase = MigrationPhaseRecord(
        schema_version=MIGRATION_PHASE_SCHEMA_VERSION,
        transaction_id=transaction_id,
        generation=MIGRATION_PHASE_GENERATIONS[MigrationPhase.ROLLBACKABLE],
        phase=MigrationPhase.ROLLBACKABLE,
    )
    phase_path = _migration_phase_path(state_directory)
    stored = _write_atomic_initial_migration_file(
        phase_path,
        _encode_migration_phase(phase),
        transaction_id=transaction_id,
        temporary_prefix=MIGRATION_PHASE_TEMP_FILE_PREFIX,
        reader_sid=reader_sid,
        maximum_bytes=MAXIMUM_MIGRATION_PHASE_BYTES,
        description="Die initiale geschützte Desktop-Migrationsphase",
    )
    if _decode_migration_phase(stored) != phase:
        raise RuntimeError("Die initiale Desktop-Migrationsphase wurde nicht unverändert gespeichert.")
    return phase


def _store_migration_token(
    state_directory: Path,
    payload: bytes,
    *,
    transaction_id: str,
) -> None:
    stored = _write_atomic_initial_migration_file(
        _migration_token_path(state_directory),
        payload,
        transaction_id=transaction_id,
        temporary_prefix=MIGRATION_TOKEN_TEMP_FILE_PREFIX,
        reader_sid=None,
        maximum_bytes=MAXIMUM_MIGRATION_TOKEN_BYTES,
        description="Das geschützte Desktop-API-Token",
    )
    if stored != payload:
        raise RuntimeError("Das geschützte Desktop-API-Token wurde nicht unverändert gespeichert.")


def _migration_state_entries(state_directory: Path) -> tuple[str, ...]:
    try:
        with os.scandir(state_directory) as entries:
            return tuple(entry.name for entry in entries)
    except OSError as exc:
        raise RuntimeError("Der geschützte Desktop-Migrationszustand konnte nicht inventarisiert werden.") from exc


def _load_migration_seal_envelope(*, require_current_user: bool) -> MigrationSeal:
    state_directory, seal_path = _migration_state_paths()
    reader_sid = _verify_migration_state_path(
        state_directory,
        directory=True,
        reader_required=True,
    )
    assert reader_sid is not None
    if require_current_user and _current_process_user_sid() != reader_sid:
        raise RuntimeError("Der geschützte Desktop-Migrationsbeleg gehört einer anderen Benutzeridentität.")
    _verify_migration_state_path(
        seal_path,
        directory=False,
        reader_required=True,
        expected_reader_sid=reader_sid,
    )
    serialized = _read_locked_bytes(
        seal_path,
        maximum_bytes=MAXIMUM_MIGRATION_RECEIPT_BYTES,
        description="Der geschützte Desktop-Migrationsbeleg",
    )
    _verify_migration_state_path(
        seal_path,
        directory=False,
        reader_required=True,
        expected_reader_sid=reader_sid,
    )
    seal = _decode_migration_seal(serialized)
    if seal.reader_sid != reader_sid:
        raise RuntimeError("Der geschützte Desktop-Migrationsbeleg bindet eine andere Benutzeridentität.")
    return seal


def _validate_sealed_migration_token(state_directory: Path, seal: MigrationSeal) -> None:
    token_path = _migration_token_path(state_directory)
    if seal.token_sha256 is None:
        return
    _verify_migration_state_path(
        token_path,
        directory=False,
        reader_required=False,
    )
    token_payload = _read_locked_bytes(
        token_path,
        maximum_bytes=MAXIMUM_MIGRATION_TOKEN_BYTES,
        description="Das geschützte Desktop-API-Token",
    )
    _verify_migration_state_path(
        token_path,
        directory=False,
        reader_required=False,
    )
    if hashlib.sha256(token_payload).hexdigest() != seal.token_sha256:
        raise RuntimeError("Das geschützte Desktop-API-Token gehört nicht zum Migrationsbeleg.")
    try:
        validate_api_token(
            token_payload.decode("ascii").rstrip("\r\n"),
            description="Das geschützte Desktop-API-Token",
        )
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("Das geschützte Desktop-API-Token ist ungültig.") from exc


def _allowed_temporary_migration_phases(
    current: MigrationPhase,
) -> set[MigrationPhase]:
    return {
        MigrationPhase.ROLLBACKABLE: {
            MigrationPhase.ROLLBACKABLE,
            MigrationPhase.SERVICE_TRANSITION,
        },
        MigrationPhase.SERVICE_TRANSITION: {
            MigrationPhase.SERVICE_TRANSITION,
            MigrationPhase.SERVICE_ROLLBACK_COMPLETE,
            MigrationPhase.SERVICE_COMMITTED,
        },
        MigrationPhase.SERVICE_ROLLBACK_COMPLETE: {
            MigrationPhase.SERVICE_ROLLBACK_COMPLETE,
        },
        MigrationPhase.SERVICE_COMMITTED: {
            MigrationPhase.SERVICE_COMMITTED,
        },
    }[current]


def _decode_temporary_migration_phase(
    payload: bytes,
    *,
    transaction_id: str,
    current: MigrationPhase,
) -> MigrationPhaseRecord | None:
    try:
        temporary_phase = _decode_migration_phase(payload)
    except RuntimeError:
        try:
            json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_json_object)
        except (UnicodeError, ValueError):
            return None
        raise
    if (
        temporary_phase.transaction_id != transaction_id
        or temporary_phase.phase not in _allowed_temporary_migration_phases(current)
    ):
        raise RuntimeError("Eine temporäre Desktop-Migrationsphase widerspricht dem autoritativen Zustand.")
    return temporary_phase


def _load_migration_transaction(
    *,
    require_current_user: bool,
) -> tuple[MigrationSeal, MigrationPhaseRecord]:
    state_directory, _seal_path = _migration_state_paths()
    seal = _load_migration_seal_envelope(require_current_user=require_current_user)
    expected_entries = {MIGRATION_SEAL_FILE_NAME, MIGRATION_PHASE_FILE_NAME}
    entry_names = set(_migration_state_entries(state_directory))
    if not expected_entries <= entry_names:
        raise RuntimeError("Der geschützte Desktop-Migrationszustand ist unvollständig.")

    phase_path = _migration_phase_path(state_directory)
    _verify_migration_state_path(
        phase_path,
        directory=False,
        reader_required=True,
        expected_reader_sid=seal.reader_sid,
    )
    phase_payload = _read_locked_bytes(
        phase_path,
        maximum_bytes=MAXIMUM_MIGRATION_PHASE_BYTES,
        description="Die geschützte Desktop-Migrationsphase",
    )
    _verify_migration_state_path(
        phase_path,
        directory=False,
        reader_required=True,
        expected_reader_sid=seal.reader_sid,
    )
    phase = _decode_migration_phase(phase_payload)
    if phase.transaction_id != seal.transaction_id:
        raise RuntimeError("Desktop-Migrationsbeleg und -phase gehören zu verschiedenen Transaktionen.")

    token_present = MIGRATION_TOKEN_FILE_NAME in entry_names
    if seal.token_sha256 is not None:
        if token_present:
            _validate_sealed_migration_token(state_directory, seal)
            expected_entries.add(MIGRATION_TOKEN_FILE_NAME)
        elif phase.phase not in {
            MigrationPhase.SERVICE_ROLLBACK_COMPLETE,
            MigrationPhase.SERVICE_COMMITTED,
        }:
            raise RuntimeError("Der geschützte Desktop-Migrationszustand enthält den versiegelten Token nicht.")

    orphan_mounts = _profile_audit_mounts(seal.transaction_id)
    snapshot_paths: list[Path] = []
    for name in entry_names - expected_entries:
        path = state_directory / name
        if _is_phase_temporary_name(name, transaction_id=seal.transaction_id):
            _verify_migration_state_path(
                path,
                directory=False,
                reader_required=True,
                expected_reader_sid=seal.reader_sid,
            )
            temporary_payload = _read_locked_bytes(
                path,
                maximum_bytes=MAXIMUM_MIGRATION_PHASE_BYTES,
                description="Eine temporäre geschützte Desktop-Migrationsphase",
            )
            _verify_migration_state_path(
                path,
                directory=False,
                reader_required=True,
                expected_reader_sid=seal.reader_sid,
            )
            _decode_temporary_migration_phase(
                temporary_payload,
                transaction_id=seal.transaction_id,
                current=phase.phase,
            )
            continue
        if _is_profile_snapshot_name(name, transaction_id=seal.transaction_id):
            if not validate_machine_path(path, directory=True):
                raise RuntimeError("Ein geschütztes NTUSER-Hive-Verzeichnis ist kein sicheres Verzeichnis.")
            _validate_profile_hive_recovery_directory(
                path,
                expected_transaction_id=seal.transaction_id,
            )
            snapshot_paths.append(path)
            continue
        raise RuntimeError("Der geschützte Desktop-Migrationszustand enthält unerwartete Einträge.")
    if orphan_mounts and not snapshot_paths:
        raise RuntimeError("Ein Registry-Audit-Mount besitzt keinen geschützten Snapshot-Tail.")
    if not orphan_mounts:
        for snapshot_path in snapshot_paths:
            _validate_profile_hive_recovery_tail(
                snapshot_path,
                expected_transaction_id=seal.transaction_id,
            )
    return seal, phase


def _load_migration_seal() -> tuple[MigrationReceipt, str]:
    """Compatibility reader for callers that need only the sealed desktop plan."""

    seal, _phase = _load_migration_transaction(require_current_user=True)
    return seal.receipt, seal.reader_sid


def verify_desktop_migration_owner() -> None:
    """Verify that the caller is the exact user SID bound to the protected transaction."""

    _load_migration_transaction(require_current_user=True)


def load_desktop_migration_binding(
    *,
    require_current_user: bool = False,
) -> DesktopMigrationBinding | None:
    """Return the canonical protected Desktop binding, or ``None`` when absent."""

    state_directory, _seal_path = _migration_state_paths()
    if not validate_machine_path(state_directory, directory=True):
        if os.path.lexists(state_directory):
            raise RuntimeError("Der Desktop-Migrationszustand ist kein sicherer lokaler Ordner.")
        return None
    seal, phase = _load_migration_transaction(require_current_user=require_current_user)
    return DesktopMigrationBinding(
        transaction_id=seal.transaction_id,
        reader_sid=seal.reader_sid,
        seal_sha256=hashlib.sha256(_encode_migration_seal(seal)).hexdigest(),
        token_sha256=seal.token_sha256,
        receipt=seal.receipt,
        phase=phase.phase,
    )


def _validate_partial_migration_scratch(
    path: Path,
    *,
    reader_sid: str,
    reader_required: bool,
    maximum_bytes: int,
) -> None:
    _verify_migration_state_path(
        path,
        directory=False,
        reader_required=reader_required,
        expected_reader_sid=reader_sid if reader_required else None,
    )
    _read_locked_bytes(
        path,
        maximum_bytes=maximum_bytes,
        description="Eine partielle geschützte Desktop-Migrationsdatei",
    )
    _verify_migration_state_path(
        path,
        directory=False,
        reader_required=reader_required,
        expected_reader_sid=reader_sid if reader_required else None,
    )


def _partial_migration_state_inventory(
    *,
    expected_reader_sid: str | None = None,
) -> _PartialMigrationState | None:
    state_directory, seal_path = _migration_state_paths()
    if not validate_machine_path(state_directory, directory=True):
        if os.path.lexists(state_directory):
            raise RuntimeError("Der Desktop-Migrationszustand ist kein sicherer lokaler Ordner.")
        return None
    reader_sid = _verify_migration_state_path(
        state_directory,
        directory=True,
        reader_required=True,
        expected_reader_sid=expected_reader_sid,
    )
    assert reader_sid is not None
    names = set(_migration_state_entries(state_directory))
    if MIGRATION_PHASE_FILE_NAME in names:
        return None
    if not names:
        return _PartialMigrationState(reader_sid=reader_sid, paths=())

    if MIGRATION_SEAL_FILE_NAME not in names:
        if len(names) != 1:
            raise RuntimeError("Ein partieller Desktop-Migrationszustand enthält unerwartete Einträge.")
        name = next(iter(names))
        transaction_id = _temporary_transaction_id(
            name,
            prefix=MIGRATION_SEAL_TEMP_FILE_PREFIX,
        )
        if transaction_id is None:
            raise RuntimeError("Ein partieller Desktop-Migrationszustand enthält Dateien ohne Seal.")
        scratch = state_directory / name
        _validate_partial_migration_scratch(
            scratch,
            reader_sid=reader_sid,
            reader_required=True,
            maximum_bytes=MAXIMUM_MIGRATION_RECEIPT_BYTES,
        )
        return _PartialMigrationState(reader_sid=reader_sid, paths=(scratch,))

    seal = _load_migration_seal_envelope(require_current_user=False)
    if seal.reader_sid != reader_sid:
        raise RuntimeError("Der partielle Desktop-Migrationsbeleg gehört einer anderen Benutzeridentität.")
    token_path = _migration_token_path(state_directory)
    fixed_token_present = MIGRATION_TOKEN_FILE_NAME in names
    token_temporaries = [name for name in names if _is_token_temporary_name(name, transaction_id=seal.transaction_id)]
    phase_temporaries = [name for name in names if _is_phase_temporary_name(name, transaction_id=seal.transaction_id)]
    recognized = {
        MIGRATION_SEAL_FILE_NAME,
        *(token_temporaries),
        *(phase_temporaries),
    }
    if fixed_token_present:
        recognized.add(MIGRATION_TOKEN_FILE_NAME)
    if names != recognized:
        raise RuntimeError("Der partielle Desktop-Migrationszustand enthält unerwartete Einträge.")
    if len(token_temporaries) > 1 or len(phase_temporaries) > 1:
        raise RuntimeError("Der partielle Desktop-Migrationszustand enthält mehrdeutige Scratchdateien.")
    if seal.token_sha256 is None and (fixed_token_present or token_temporaries):
        raise RuntimeError("Ein unerwarteter Tokenzustand begleitet den partiellen Desktop-Migrationsbeleg.")
    if fixed_token_present and token_temporaries:
        raise RuntimeError("Fester und temporärer Tokenzustand dürfen nicht gleichzeitig vorhanden sein.")
    if token_temporaries and phase_temporaries:
        raise RuntimeError("Die initiale Phase darf nicht vor atomarem Publish des Tokens geschrieben werden.")
    if seal.token_sha256 is not None:
        if fixed_token_present:
            _validate_sealed_migration_token(state_directory, seal)
        elif phase_temporaries:
            raise RuntimeError("Eine initiale Phase darf nicht ohne den versiegelten Token geschrieben werden.")

    paths: list[Path] = [seal_path]
    if fixed_token_present:
        paths.append(token_path)
    if token_temporaries:
        token_temporary = state_directory / token_temporaries[0]
        _validate_partial_migration_scratch(
            token_temporary,
            reader_sid=reader_sid,
            reader_required=False,
            maximum_bytes=MAXIMUM_MIGRATION_TOKEN_BYTES,
        )
        paths.append(token_temporary)
    if phase_temporaries:
        phase_temporary = state_directory / phase_temporaries[0]
        _validate_partial_migration_scratch(
            phase_temporary,
            reader_sid=reader_sid,
            reader_required=True,
            maximum_bytes=MAXIMUM_MIGRATION_PHASE_BYTES,
        )
        paths.append(phase_temporary)
    return _PartialMigrationState(reader_sid=reader_sid, paths=tuple(paths))


def desktop_migration_state_is_partial() -> bool:
    """Recognize only rollback-neutral hard-kill tails created before initial phase publish."""

    return _partial_migration_state_inventory() is not None


def protected_desktop_migration_token_path() -> Path | None:
    """Return only the hash-verified protected token copy bound to the active seal."""

    binding = load_desktop_migration_binding(require_current_user=False)
    if binding is None:
        raise RuntimeError("Für die Tokenübernahme fehlt ein geschützter Desktop-Migrationsbeleg.")
    if binding.token_sha256 is None:
        return None
    state_directory, _seal_path = _migration_state_paths()
    token_path = _migration_token_path(state_directory)
    if not validate_machine_path(token_path, directory=False):
        raise RuntimeError("Der versiegelte Desktop-Token ist nicht mehr verfügbar.")
    # load_desktop_migration_binding() has already revalidated the fixed token
    # path, exact DACL, bounded payload, token syntax, and seal hash.
    return token_path


def verify_applied_desktop_migration() -> None:
    """Verify the sealed quarantine state and all-profile Desktop inventory."""

    verify_no_legacy_desktop_conflicts()


def _atomic_replace_migration_file(source: Path, target: Path) -> None:
    if os.name != "nt":
        os.replace(source, target)
        return
    ctypes_windows: Any = ctypes
    kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileExW
    move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    move_file.restype = ctypes.c_bool
    if not move_file(
        str(source),
        str(target),
        MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
    ):
        raise OSError(
            ctypes_windows.get_last_error(),
            "Die Desktop-Migrationsphase konnte nicht atomar ersetzt werden.",
        )


def _write_atomic_migration_phase(record: MigrationPhaseRecord, *, reader_sid: str) -> None:
    state_directory, _seal_path = _migration_state_paths()
    phase_path = _migration_phase_path(state_directory)
    temporary = state_directory / (
        f"{MIGRATION_PHASE_TEMP_FILE_PREFIX}{record.transaction_id}-{secrets.token_hex(16)}.tmp"
    )
    _write_secure_migration_file(
        temporary,
        _encode_migration_phase(record),
        reader_sid=reader_sid,
    )
    try:
        _atomic_replace_migration_file(temporary, phase_path)
    except Exception:
        if validate_machine_path(temporary, directory=False):
            _verify_migration_state_path(
                temporary,
                directory=False,
                reader_required=True,
                expected_reader_sid=reader_sid,
            )
            temporary.unlink()
        raise
    _verify_migration_state_path(
        phase_path,
        directory=False,
        reader_required=True,
        expected_reader_sid=reader_sid,
    )
    stored = _read_locked_bytes(
        phase_path,
        maximum_bytes=MAXIMUM_MIGRATION_PHASE_BYTES,
        description="Die atomar gespeicherte Desktop-Migrationsphase",
    )
    if _decode_migration_phase(stored) != record:
        raise RuntimeError("Die Desktop-Migrationsphase wurde nicht unverändert atomar gespeichert.")


def _remove_abandoned_migration_phase_temporaries(
    seal: MigrationSeal,
    current: MigrationPhaseRecord,
) -> None:
    reloaded_seal, reloaded_phase = _load_migration_transaction(require_current_user=False)
    if reloaded_seal != seal or reloaded_phase != current:
        raise RuntimeError("Der Desktop-Migrationszustand änderte sich vor dem atomaren Phasenübergang.")
    state_directory, _seal_path = _migration_state_paths()
    temporary_paths = [
        state_directory / name
        for name in _migration_state_entries(state_directory)
        if _is_phase_temporary_name(name, transaction_id=seal.transaction_id)
    ]
    for path in temporary_paths:
        _verify_migration_state_path(
            path,
            directory=False,
            reader_required=True,
            expected_reader_sid=seal.reader_sid,
        )
        _decode_temporary_migration_phase(
            _read_locked_bytes(
                path,
                maximum_bytes=MAXIMUM_MIGRATION_PHASE_BYTES,
                description="Eine temporäre geschützte Desktop-Migrationsphase",
            ),
            transaction_id=seal.transaction_id,
            current=current.phase,
        )
        _verify_migration_state_path(
            path,
            directory=False,
            reader_required=True,
            expected_reader_sid=seal.reader_sid,
        )
    for path in temporary_paths:
        try:
            path.unlink()
        except OSError as exc:
            raise RuntimeError(
                "Eine veraltete temporäre Desktop-Migrationsphase konnte nicht gelöscht werden."
            ) from exc


def advance_desktop_migration_phase(target: MigrationPhase) -> MigrationPhaseRecord:
    """Advance the protected transaction along one explicitly allowed edge."""

    seal, current = _load_migration_transaction(require_current_user=False)
    allowed = {
        MigrationPhase.ROLLBACKABLE: {MigrationPhase.SERVICE_TRANSITION},
        MigrationPhase.SERVICE_TRANSITION: {
            MigrationPhase.SERVICE_ROLLBACK_COMPLETE,
            MigrationPhase.SERVICE_COMMITTED,
        },
        MigrationPhase.SERVICE_ROLLBACK_COMPLETE: set(),
        MigrationPhase.SERVICE_COMMITTED: set(),
    }
    if target not in allowed[current.phase]:
        raise RuntimeError(
            f"Der Desktop-Phasenübergang {current.phase.value!r} -> {target.value!r} ist nicht zulässig."
        )
    advanced = MigrationPhaseRecord(
        schema_version=MIGRATION_PHASE_SCHEMA_VERSION,
        transaction_id=seal.transaction_id,
        generation=MIGRATION_PHASE_GENERATIONS[target],
        phase=target,
    )
    _remove_abandoned_migration_phase_temporaries(seal, current)
    _write_atomic_migration_phase(advanced, reader_sid=seal.reader_sid)
    return advanced


def _clear_migration_state(*, expected_reader_sid: str, require_current_user: bool) -> None:
    state_directory, _seal_path = _migration_state_paths()
    if not validate_machine_path(state_directory, directory=True):
        return
    reader_sid = _verify_migration_state_path(
        state_directory,
        directory=True,
        reader_required=True,
        expected_reader_sid=expected_reader_sid,
    )
    assert reader_sid is not None
    if require_current_user and _current_process_user_sid() != reader_sid:
        raise RuntimeError("Der geschützte Desktop-Migrationszustand gehört einer anderen Benutzeridentität.")

    partial = _partial_migration_state_inventory(expected_reader_sid=reader_sid)
    if partial is not None:
        for path in sorted(
            partial.paths,
            key=lambda candidate: candidate.name == MIGRATION_SEAL_FILE_NAME,
        ):
            try:
                path.unlink()
            except OSError as exc:
                raise RuntimeError(
                    "Eine partielle geschützte Desktop-Migrationsdatei konnte nicht gelöscht werden."
                ) from exc
        if _migration_state_entries(state_directory):
            raise RuntimeError("Der partielle Desktop-Migrationszustand ist nach der Bereinigung nicht leer.")
        try:
            state_directory.rmdir()
        except OSError as exc:
            raise RuntimeError(
                "Der partielle geschützte Desktop-Migrationszustand konnte nicht entfernt werden."
            ) from exc
        return

    entry_names = _migration_state_entries(state_directory)
    entry_paths = {name: state_directory / name for name in entry_names}
    seal: MigrationSeal | None = None
    if MIGRATION_SEAL_FILE_NAME in entry_paths:
        seal = _load_migration_seal_envelope(require_current_user=False)
        if seal.reader_sid != expected_reader_sid:
            raise RuntimeError("Der geschützte Desktop-Migrationszustand gehört einer anderen Benutzeridentität.")

    regular_names: set[str] = set()
    snapshot_paths: list[Path] = []
    transaction_id = seal.transaction_id if seal is not None else None
    expected_regular = {MIGRATION_SEAL_FILE_NAME, MIGRATION_PHASE_FILE_NAME}
    if seal is not None and seal.token_sha256 is not None:
        expected_regular.add(MIGRATION_TOKEN_FILE_NAME)
    for name, path in entry_paths.items():
        if name in expected_regular:
            regular_names.add(name)
            continue
        if transaction_id is not None and _is_phase_temporary_name(name, transaction_id=transaction_id):
            regular_names.add(name)
            continue
        if transaction_id is not None and _is_profile_snapshot_name(name, transaction_id=transaction_id):
            if not validate_machine_path(path, directory=True):
                raise RuntimeError("Ein geschütztes NTUSER-Hive-Verzeichnis ist kein sicheres Verzeichnis.")
            snapshot_paths.append(path)
            continue
        raise RuntimeError("Der geschützte Desktop-Migrationszustand enthält unerwartete Einträge.")

    if seal is None and regular_names:
        raise RuntimeError("Der geschützte Desktop-Migrationszustand enthält Dateien ohne autoritativen Beleg.")

    stored_phase: MigrationPhaseRecord | None = None
    if MIGRATION_PHASE_FILE_NAME in regular_names:
        phase_path = entry_paths[MIGRATION_PHASE_FILE_NAME]
        _verify_migration_state_path(
            phase_path,
            directory=False,
            reader_required=True,
            expected_reader_sid=expected_reader_sid,
        )
        stored_phase = _decode_migration_phase(
            _read_locked_bytes(
                phase_path,
                maximum_bytes=MAXIMUM_MIGRATION_PHASE_BYTES,
                description="Die geschützte Desktop-Migrationsphase",
            )
        )
        _verify_migration_state_path(
            phase_path,
            directory=False,
            reader_required=True,
            expected_reader_sid=expected_reader_sid,
        )
        if seal is None or stored_phase.transaction_id != seal.transaction_id:
            raise RuntimeError("Eine Desktop-Migrationsphase gehört zu einer anderen Transaktion.")

    for name in regular_names:
        path = entry_paths[name]
        reader_required = name != MIGRATION_TOKEN_FILE_NAME
        _verify_migration_state_path(
            path,
            directory=False,
            reader_required=reader_required,
            expected_reader_sid=expected_reader_sid if reader_required else None,
        )
        if name.startswith(MIGRATION_PHASE_TEMP_FILE_PREFIX):
            if seal is None or stored_phase is None:
                raise RuntimeError("Eine temporäre Desktop-Migrationsphase besitzt keine autoritative Phase.")
            _decode_temporary_migration_phase(
                _read_locked_bytes(
                    path,
                    maximum_bytes=MAXIMUM_MIGRATION_PHASE_BYTES,
                    description="Eine temporäre geschützte Desktop-Migrationsphase",
                ),
                transaction_id=seal.transaction_id,
                current=stored_phase.phase,
            )
        if name == MIGRATION_TOKEN_FILE_NAME:
            assert seal is not None and seal.token_sha256 is not None
            payload = _read_locked_bytes(
                path,
                maximum_bytes=MAXIMUM_MIGRATION_TOKEN_BYTES,
                description="Das geschützte Desktop-API-Token",
            )
            if hashlib.sha256(payload).hexdigest() != seal.token_sha256:
                raise RuntimeError("Das geschützte Desktop-API-Token gehört nicht zum Migrationsbeleg.")

    if (
        seal is not None
        and seal.token_sha256 is not None
        and MIGRATION_TOKEN_FILE_NAME not in regular_names
        and (
            stored_phase is None
            or stored_phase.phase
            not in {
                MigrationPhase.SERVICE_ROLLBACK_COMPLETE,
                MigrationPhase.SERVICE_COMMITTED,
            }
        )
    ):
        raise RuntimeError("Der geschützte Desktop-Migrationszustand enthält den versiegelten Token nicht.")

    assert transaction_id is not None
    for snapshot_directory in snapshot_paths:
        _validate_profile_hive_recovery_directory(
            snapshot_directory,
            expected_transaction_id=transaction_id,
        )

    orphan_mounts = _profile_audit_mounts(transaction_id)
    if orphan_mounts and not snapshot_paths:
        raise RuntimeError("Ein Registry-Audit-Mount besitzt keinen geschützten Snapshot-Tail.")
    if orphan_mounts:
        import winreg as native_winreg

        winreg: Any = native_winreg
        _enable_registry_hive_privileges()
        for mounted_name in orphan_mounts:
            try:
                winreg.UnloadKey(winreg.HKEY_USERS, mounted_name)
            except OSError as exc:
                raise RuntimeError(
                    "Ein verwaister, transaktionsgebundener Registry-Audit-Mount konnte nicht entladen werden."
                ) from exc

    for snapshot_directory in snapshot_paths:
        _validate_profile_hive_snapshot(
            snapshot_directory / PROFILE_HIVE_SNAPSHOT_FILE_NAME,
            expected_transaction_id=transaction_id,
        )

    for snapshot_directory in snapshot_paths:
        _remove_profile_hive_snapshot(
            snapshot_directory / PROFILE_HIVE_SNAPSHOT_FILE_NAME,
            expected_transaction_id=transaction_id,
        )
    for name in sorted(regular_names, key=lambda candidate: candidate == MIGRATION_SEAL_FILE_NAME):
        path = entry_paths[name]
        _verify_migration_state_path(
            path,
            directory=False,
            reader_required=name != MIGRATION_TOKEN_FILE_NAME,
            expected_reader_sid=reader_sid if name != MIGRATION_TOKEN_FILE_NAME else None,
        )
        try:
            path.unlink()
        except OSError as exc:
            raise RuntimeError("Eine geschützte Desktop-Migrationsdatei konnte nicht gelöscht werden.") from exc
    if _migration_state_entries(state_directory):
        raise RuntimeError("Der geschützte Desktop-Migrationszustand ist nach der Bereinigung nicht leer.")
    try:
        state_directory.rmdir()
    except OSError as exc:
        raise RuntimeError("Der geschützte Desktop-Migrationszustand konnte nicht entfernt werden.") from exc
    if validate_machine_path(state_directory, directory=True):
        raise RuntimeError("Der geschützte Desktop-Migrationszustand wurde nicht vollständig entfernt.")


def _validate_receipt_paths(
    receipt: MigrationReceipt,
    *,
    bind_to_current_registration: bool,
) -> tuple[Path, Path | None]:
    executable = _validated_local_fixed_path(receipt.executable)
    if (
        "\x00" in receipt.executable
        or executable.name.casefold() != DESKTOP_EXECUTABLE_NAME.casefold()
        or not executable.is_absolute()
    ):
        raise RuntimeError("Der Desktop-Pfad im Migrationsbeleg ist ungültig.")
    if bind_to_current_registration and executable != desktop_executable():
        raise RuntimeError("Der Desktop-Pfad im Migrationsbeleg ist nicht mehr eindeutig.")
    expected_disabled = executable.with_name(executable.name + DISABLED_SUFFIX)
    disabled = Path(receipt.disabled_executable) if receipt.disabled_executable is not None else None
    if disabled is not None and disabled != expected_disabled:
        raise RuntimeError("Der Quarantänepfad im Desktop-Migrationsbeleg ist ungültig.")
    return executable, disabled


def _validate_sealed_receipt_semantics(receipt: MigrationReceipt) -> None:
    if receipt.autostart_command is not None:
        expected_command = f'"{receipt.executable}" --background'
        if receipt.autostart_command != expected_command:
            raise RuntimeError("Der Autostart im geschützten Migrationsbeleg ist ungültig.")
    if receipt.was_running and receipt.disabled_executable is None:
        raise RuntimeError("Der geschützte Migrationsbeleg kann den laufenden Desktop nicht wiederherstellen.")


def _desktop_binary_state(executable: Path) -> tuple[bool, bool]:
    disabled = executable.with_name(executable.name + DISABLED_SUFFIX)
    with _locked_local_path(executable, directory=False) as active_exists:
        pass
    with _locked_local_path(disabled, directory=False) as disabled_exists:
        pass
    return active_exists, disabled_exists


def _current_autostart(winreg: Any) -> tuple[object, int] | None:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_KEY,
            0,
            winreg.KEY_QUERY_VALUE,
        )
    except FileNotFoundError:
        return None
    with key:
        try:
            value, value_type = winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
        except FileNotFoundError:
            return None
    return value, int(value_type)


def _validate_current_autostart(
    receipt: MigrationReceipt,
    current: tuple[object, int] | None,
    *,
    allow_absent: bool,
    winreg: Any,
) -> bool:
    if current is None:
        if allow_absent:
            return False
        raise RuntimeError("Der erwartete HKCU-Autostart fehlt.")
    value, value_type = current
    if receipt.autostart_command is None or value_type != winreg.REG_SZ or value != receipt.autostart_command:
        raise RuntimeError("Ein fremder HKCU-Autostart verhindert die sichere Desktopmigration.")
    return True


def apply_desktop_migration() -> None:
    """Apply the sealed desktop plan idempotently as its exact original user."""

    if sys.platform != "win32":
        raise OSError("Die Desktopmigration ist ausschließlich unter Windows verfügbar.")

    seal, phase = _load_migration_transaction(require_current_user=True)
    if phase.phase is not MigrationPhase.ROLLBACKABLE:
        raise RuntimeError("Die Desktopmigration kann in der aktuellen Phase nicht angewendet werden.")
    import winreg

    receipt = seal.receipt
    _validate_sealed_receipt_semantics(receipt)
    executable, disabled = _validate_receipt_paths(receipt, bind_to_current_registration=False)
    active_exists, disabled_exists = _desktop_binary_state(executable)
    if disabled is None:
        if active_exists or disabled_exists:
            raise RuntimeError("Der Migrationsbeleg passt nicht zum aktuellen Desktopzustand.")
    elif active_exists == disabled_exists:
        raise RuntimeError("Desktop-EXE und Quarantänekopie bilden keinen eindeutigen Migrationszustand.")

    current_autostart = _current_autostart(winreg)
    autostart_present = _validate_current_autostart(
        receipt,
        current_autostart,
        allow_absent=True,
        winreg=winreg,
    )

    if disabled is not None:
        _stop_desktop_backend()
    if autostart_present:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_KEY,
            0,
            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
        ) as key:
            current, value_type = winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
            if value_type != winreg.REG_SZ or current != receipt.autostart_command:
                raise RuntimeError("Der HKCU-Autostart wurde während der Desktopmigration verändert.")
            winreg.DeleteValue(key, AUTOSTART_VALUE_NAME)
    if disabled is not None and active_exists:
        os.replace(executable, disabled)


def rollback_desktop_migration(
    receipt_path: Path | None = None,
    *,
    require_seal: bool = True,
) -> None:
    if sys.platform != "win32":
        raise OSError("Die Desktopmigration ist ausschließlich unter Windows verfügbar.")
    del receipt_path
    if not require_seal:
        raise RuntimeError("Die Desktopmigration darf nur aus dem geschützten Migrationsbeleg zurückgenommen werden.")
    import winreg

    seal, phase = _load_migration_transaction(require_current_user=True)
    if phase.phase not in {
        MigrationPhase.ROLLBACKABLE,
        MigrationPhase.SERVICE_ROLLBACK_COMPLETE,
    }:
        raise RuntimeError("Die Desktopmigration darf in der aktuellen Phase nicht zurückgenommen werden.")
    receipt = seal.receipt
    _validate_sealed_receipt_semantics(receipt)
    executable, disabled = _validate_receipt_paths(
        receipt,
        bind_to_current_registration=False,
    )
    active_exists, disabled_exists = _desktop_binary_state(executable)
    if disabled is not None:
        if active_exists == disabled_exists:
            raise RuntimeError("Desktop-EXE und Quarantänekopie bilden keinen eindeutigen Rollbackzustand.")
    elif active_exists or disabled_exists:
        raise RuntimeError("Der Migrationsbeleg passt nicht zum aktuellen Desktopzustand.")

    current_autostart = _current_autostart(winreg)
    autostart_present = _validate_current_autostart(
        receipt,
        current_autostart,
        allow_absent=True,
        winreg=winreg,
    )
    if disabled is not None and disabled_exists:
        os.replace(disabled, executable)
    if receipt.autostart_command is not None and not autostart_present:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_KEY,
            0,
            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
        ) as key:
            try:
                current, current_type = winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
            except FileNotFoundError:
                current = None
                current_type = None
            if current is not None and (current != receipt.autostart_command or current_type != winreg.REG_SZ):
                raise RuntimeError("Ein fremder HKCU-Autostart verhindert die sichere Rücknahme der Migration.")
            winreg.SetValueEx(key, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, receipt.autostart_command)
    if receipt.was_running:
        if _desktop_backend_is_running():
            if not _desktop_runtime_has_start_proof():
                raise RuntimeError("Die bereits laufende Desktop-App besitzt keinen belastbaren Startnachweis.")
        else:
            _restart_desktop(executable)


def commit_desktop_migration(
    receipt_path: Path | None = None,
    *,
    require_seal: bool = True,
) -> None:
    """Irreversibly remove the quarantined legacy backend after service commit."""

    if sys.platform != "win32":
        raise OSError("Die Desktopmigration ist ausschließlich unter Windows verfügbar.")
    del receipt_path
    if not require_seal:
        raise RuntimeError("Die Desktopmigration darf nur aus dem geschützten Migrationsbeleg abgeschlossen werden.")
    import winreg

    seal, phase = _load_migration_transaction(require_current_user=True)
    if phase.phase is not MigrationPhase.SERVICE_COMMITTED:
        raise RuntimeError("Die Desktopmigration darf vor dem bestätigten Dienst-Commit nicht abgeschlossen werden.")
    receipt = seal.receipt
    _validate_sealed_receipt_semantics(receipt)
    executable, disabled = _validate_receipt_paths(receipt, bind_to_current_registration=False)
    if _current_autostart(winreg) is not None:
        raise RuntimeError("Ein HKCU-Autostart verhindert den sicheren Abschluss der Desktopmigration.")
    active_exists, disabled_exists = _desktop_binary_state(executable)
    if _desktop_backend_is_running():
        raise RuntimeError("Die Desktop-App läuft beim Abschluss der Migration noch.")
    if disabled is None:
        if active_exists or disabled_exists:
            raise RuntimeError("Der Migrationsbeleg passt nicht zum aktuellen Desktopzustand.")
        return
    if active_exists:
        raise RuntimeError("Die Desktop-EXE ist vor Abschluss der Migration wieder aktiv geworden.")
    if disabled_exists:
        disabled.unlink()


def clear_desktop_migration_seal() -> None:
    """Remove the fixed protected seal from an elevated administrative process."""

    if sys.platform != "win32":
        raise OSError("Der Desktop-Migrationszustand ist ausschließlich unter Windows verfügbar.")
    state_directory, _seal_path = _migration_state_paths()
    if not validate_machine_path(state_directory, directory=True):
        return
    reader_sid = _verify_migration_state_path(
        state_directory,
        directory=True,
        reader_required=True,
    )
    assert reader_sid is not None
    _clear_migration_state(expected_reader_sid=reader_sid, require_current_user=False)
