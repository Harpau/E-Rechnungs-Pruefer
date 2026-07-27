from __future__ import annotations

import ctypes
import ntpath
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "E-Rechnungs-Pruefer"
DESKTOP_INSTALL_DIRECTORY_NAME = "E-Rechnungs-Pruefer"
DESKTOP_EXECUTABLE_NAME = "E-Rechnungs-Pruefer.exe"
DESKTOP_UNINSTALL_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{D33FD9E5-0C5E-48ED-BF0C-E9D2962A45DF}_is1"
)
PROFILE_LIST_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
SYSTEM_PROFILE_SIDS = frozenset({"S-1-5-18", "S-1-5-19", "S-1-5-20"})
WINDOWS_MUTEX_NAME = r"Local\E-Rechnungs-Pruefer-Desktop"

DRIVE_FIXED = 3
ERROR_FILE_NOT_FOUND = 2
ERROR_NO_MORE_FILES = 18
ERROR_NO_MORE_ITEMS = 259
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
SYNCHRONIZE = 0x00100000
TH32CS_SNAPPROCESS = 0x00000002
_WINDOWS_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_RESERVED_DOS_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
ctypes_windows: Any = ctypes


def _native_drive_type(root: str) -> int:
    kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
    get_drive_type = kernel32.GetDriveTypeW
    get_drive_type.argtypes = [ctypes.c_wchar_p]
    get_drive_type.restype = ctypes.c_uint
    return int(get_drive_type(root))


def _validated_local_fixed_path(value: str) -> Path:
    if not value or "\x00" in value or "/" in value:
        raise RuntimeError("Ein Desktop- oder Profilpfad ist nicht kanonisch lokal.")
    drive, tail = ntpath.splitdrive(value)
    if (
        len(drive) != 2
        or drive[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        or drive[1] != ":"
        or not tail.startswith("\\")
    ):
        raise RuntimeError("Ein Desktop- oder Profilpfad ist nicht absolut auf einem lokalen Laufwerk.")
    raw_parts = tail.split("\\")
    path_parts = raw_parts[1:]
    if path_parts and not path_parts[-1]:
        path_parts = path_parts[:-1]
    if any(
        part in {".", ".."}
        or not part
        or part.rstrip(" .") != part
        or any(character in '<>:"|?*' or ord(character) < 32 for character in part)
        or part.split(".", 1)[0].casefold() in _RESERVED_DOS_NAMES
        for part in path_parts
    ):
        raise RuntimeError("Ein Desktop- oder Profilpfad enthält unzulässige Pfadkomponenten.")
    if _native_drive_type(f"{drive}\\") != DRIVE_FIXED:
        raise RuntimeError("Ein Desktop- oder Profilpfad liegt nicht auf einem festen lokalen Laufwerk.")
    return Path(ntpath.normpath(value))


def _safe_path_exists(path: Path, *, directory: bool) -> bool:
    """Check a candidate without accepting symlinks, junctions, or hardlinks."""

    candidates = tuple(reversed((path, *path.parents)))
    final_stat: os.stat_result | None = None
    for candidate in candidates:
        try:
            candidate_stat = os.lstat(candidate)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise RuntimeError(f"Der Desktoppfad {candidate} konnte nicht sicher geprüft werden.") from exc
        if stat.S_ISLNK(candidate_stat.st_mode) or (
            getattr(candidate_stat, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT_ATTRIBUTE
        ):
            raise RuntimeError(f"Der Desktoppfad {candidate} darf kein Reparse-Point oder Junction sein.")
        if candidate == path:
            final_stat = candidate_stat
        elif not stat.S_ISDIR(candidate_stat.st_mode):
            raise RuntimeError(f"Der übergeordnete Desktoppfad {candidate} ist kein Verzeichnis.")
    if final_stat is None:
        return False
    if directory:
        if not stat.S_ISDIR(final_stat.st_mode):
            raise RuntimeError(f"Der Desktoppfad {path} ist kein Verzeichnis.")
    elif not stat.S_ISREG(final_stat.st_mode) or int(getattr(final_stat, "st_nlink", 1)) != 1:
        raise RuntimeError(f"Der Desktoppfad {path} ist keine eindeutige reguläre Datei.")
    return True


def _safe_regular_file_exists(path: Path) -> bool:
    return _safe_path_exists(path, directory=False)


def _safe_directory_exists(path: Path) -> bool:
    return _safe_path_exists(path, directory=True)


def _profile_paths() -> tuple[tuple[str, Path], ...]:
    winreg: Any = __import__("winreg")

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
            if sid in SYSTEM_PROFILE_SIDS:
                continue
            try:
                with winreg.OpenKey(profile_list, sid, 0, winreg.KEY_QUERY_VALUE) as profile_key:
                    value, value_type = winreg.QueryValueEx(profile_key, "ProfileImagePath")
            except OSError as exc:
                raise RuntimeError("Ein lokales Benutzerprofil konnte nicht sicher gelesen werden.") from exc
            allowed_types = {winreg.REG_SZ, getattr(winreg, "REG_EXPAND_SZ", winreg.REG_SZ)}
            if value_type not in allowed_types or not isinstance(value, str) or not value.strip():
                raise RuntimeError("Ein lokales Benutzerprofil besitzt einen ungültigen Profilpfad.")
            profiles.append((sid, _validated_local_fixed_path(os.path.expandvars(value))))
    return tuple(profiles)


@contextmanager
def _offline_profile_hive(path: Path) -> Iterator[Any]:
    """Open one selected unloaded NTUSER hive without mounting it."""

    if not _safe_regular_file_exists(path):
        raise RuntimeError("Der ausgewählte NTUSER-Hive ist nicht mehr sicher lesbar.")
    winreg: Any = __import__("winreg")
    advapi32 = ctypes_windows.WinDLL("advapi32", use_last_error=True)
    load_app_key = advapi32.RegLoadAppKeyW
    load_app_key.argtypes = [
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    load_app_key.restype = ctypes.c_long
    raw_handle = ctypes.c_void_p()
    result = int(load_app_key(str(path), ctypes.byref(raw_handle), winreg.KEY_READ, 0, 0))
    if result != 0 or not raw_handle.value:
        raise OSError(result, "Ein abgemeldetes Benutzerprofil konnte nicht read-only geöffnet werden.")
    handle = winreg.HKEYType(raw_handle.value)
    try:
        yield handle
    finally:
        handle.Close()


def _select_offline_profile_hive(profile_path: Path) -> Path:
    candidates = (profile_path / "NTUSER.DAT", profile_path / "NTUSER.MAN")
    available = tuple(candidate for candidate in candidates if _safe_regular_file_exists(candidate))
    if len(available) != 1:
        raise RuntimeError("Das Benutzerprofil besitzt keinen eindeutig prüfbaren NTUSER-Hive.")
    return available[0]


@contextmanager
def _profile_hive(sid: str, profile_path: Path) -> Iterator[Any]:
    winreg: Any = __import__("winreg")

    access = winreg.KEY_QUERY_VALUE | getattr(winreg, "KEY_ENUMERATE_SUB_KEYS", 0x0008)
    try:
        loaded = winreg.OpenKey(winreg.HKEY_USERS, sid, 0, access)
    except FileNotFoundError:
        with _offline_profile_hive(_select_offline_profile_hive(profile_path)) as offline:
            yield offline
    else:
        with loaded:
            yield loaded


def _registered_desktop_present(root: Any) -> bool:
    winreg: Any = __import__("winreg")

    try:
        key = winreg.OpenKey(root, DESKTOP_UNINSTALL_KEY, 0, winreg.KEY_QUERY_VALUE)
    except FileNotFoundError:
        return False
    with key:
        try:
            value, value_type = winreg.QueryValueEx(key, "InstallLocation")
        except FileNotFoundError:
            return True
    if value_type != winreg.REG_SZ or not isinstance(value, str) or not value.strip():
        raise RuntimeError("Eine registrierte Desktopinstallation besitzt einen ungültigen Installationspfad.")
    # Validate even though the registration itself already blocks service setup.
    _validated_local_fixed_path(value)
    return True


def _desktop_autostart_present(root: Any) -> bool:
    winreg: Any = __import__("winreg")

    try:
        with winreg.OpenKey(root, AUTOSTART_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            value, value_type = winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
    except FileNotFoundError:
        return False
    if value_type != winreg.REG_SZ or not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise RuntimeError("Ein registrierter Desktop-Autostart besitzt einen ungültigen Wert.")
    return True


def _running_desktop_processes() -> tuple[int, ...]:
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
        ctypes_windows.set_last_error(0)
        has_entry = process_first(snapshot, ctypes.byref(entry))
        while has_entry:
            if entry.szExeFile.casefold() == DESKTOP_EXECUTABLE_NAME.casefold():
                matches.append(int(entry.th32ProcessID))
            ctypes_windows.set_last_error(0)
            has_entry = process_next(snapshot, ctypes.byref(entry))
        error = ctypes_windows.get_last_error()
        if error not in {0, ERROR_NO_MORE_FILES}:
            raise OSError(error, "Die laufenden Prozesse konnten nicht vollständig inventarisiert werden.")
    finally:
        if not close_handle(snapshot):
            raise OSError(ctypes_windows.get_last_error(), "Die Prozessinventur konnte nicht freigegeben werden.")
    return tuple(matches)


def _desktop_mutex_exists() -> bool:
    kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
    open_mutex = kernel32.OpenMutexW
    open_mutex.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_wchar_p]
    open_mutex.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_bool
    handle = open_mutex(SYNCHRONIZE, False, WINDOWS_MUTEX_NAME)
    if not handle:
        error = ctypes_windows.get_last_error()
        if error == ERROR_FILE_NOT_FOUND:
            return False
        raise OSError(error, "Der Desktop-Mutex konnte nicht geprüft werden.")
    if not close_handle(handle):
        raise OSError(ctypes_windows.get_last_error(), "Der Desktop-Mutex konnte nicht freigegeben werden.")
    return True


def assert_no_desktop_installation() -> None:
    """Fail closed if any local profile still contains or runs the desktop mode."""

    if sys.platform != "win32":
        raise OSError("Die maschinenweite Desktopinventur ist ausschließlich unter Windows verfügbar.")
    if _running_desktop_processes() or _desktop_mutex_exists():
        raise RuntimeError(
            "Eine laufende Desktop-Version wurde gefunden. Deinstallieren Sie die Desktop-Version "
            "manuell, bevor Sie den Dienst installieren."
        )
    for sid, profile_path in _profile_paths():
        default_install_directory = profile_path / "AppData" / "Local" / "Programs" / DESKTOP_INSTALL_DIRECTORY_NAME
        if _safe_directory_exists(default_install_directory):
            raise RuntimeError(
                "Eine Desktop-Version oder Teilinstallation wurde gefunden. Deinstallieren Sie sie manuell, "
                "bevor Sie den Dienst installieren."
            )
        with _profile_hive(sid, profile_path) as hive:
            if _registered_desktop_present(hive) or _desktop_autostart_present(hive):
                raise RuntimeError(
                    "Eine Desktop-Version oder deren Autostart wurde gefunden. Deinstallieren Sie "
                    "die Desktop-Version manuell, bevor Sie den Dienst installieren."
                )
