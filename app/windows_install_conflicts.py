from __future__ import annotations

import ctypes
import importlib
import ntpath
import os
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO

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
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_SHARE_READ = 0x00000001
GENERIC_READ = 0x80000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
OFFLINE_HIVE_CONFLICT_EXIT_CODE = 10
OFFLINE_HIVE_INSPECTION_TIMEOUT_SECONDS = 30
OFFLINE_PROFILE_INVENTORY_TIMEOUT_SECONDS = 60
OFFLINE_HIVE_MAX_BYTES = 256 * 1024 * 1024
OPEN_EXISTING = 3
REGIPY_VERSION = "6.2.1"
REGISTRY_HEADER_BYTES = 4096
REGISTRY_NK_MINIMUM_CELL_BYTES = 82
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


def _safe_directory_exists(path: Path) -> bool:
    return _safe_path_exists(path, directory=True)


def _safe_regular_file_exists(path: Path) -> bool:
    return _safe_path_exists(path, directory=False)


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
def _locked_binary_reader(path: Path) -> Iterator[BinaryIO]:
    """Open a Windows file for reading while denying writes and deletion."""

    kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_bool

    handle = create_file(
        str(path),
        GENERIC_READ,
        FILE_SHARE_READ,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle in {None, INVALID_HANDLE_VALUE}:
        raise OSError(ctypes_windows.get_last_error(), "Der NTUSER-Hive konnte nicht gesperrt gelesen werden.")

    descriptor = -1
    raw_handle: int | None = int(handle)
    try:
        msvcrt: Any = __import__("msvcrt")
        descriptor = int(
            msvcrt.open_osfhandle(
                raw_handle,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        )
        raw_handle = None
        with os.fdopen(descriptor, "rb", closefd=True) as reader:
            descriptor = -1
            yield reader
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        elif raw_handle is not None and not close_handle(raw_handle):
            raise OSError(ctypes_windows.get_last_error(), "Der NTUSER-Lesehandle konnte nicht freigegeben werden.")


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        int(left.st_dev),
        int(left.st_ino),
        int(left.st_size),
        int(left.st_mtime_ns),
    ) == (
        int(right.st_dev),
        int(right.st_ino),
        int(right.st_size),
        int(right.st_mtime_ns),
    )


def _read_safe_hive_bytes(path: Path) -> bytes:
    if not _safe_regular_file_exists(path):
        raise RuntimeError("Der ausgewählte NTUSER-Hive ist nicht mehr sicher lesbar.")
    try:
        path_before = os.lstat(path)
        with _locked_binary_reader(path) as reader:
            opened_before = os.fstat(reader.fileno())
            if (
                not stat.S_ISREG(opened_before.st_mode)
                or int(getattr(opened_before, "st_nlink", 1)) != 1
                or (getattr(opened_before, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT_ATTRIBUTE)
                or not _same_file_identity(path_before, opened_before)
            ):
                raise RuntimeError("Der ausgewählte NTUSER-Hive wurde beim Öffnen ausgetauscht.")
            if opened_before.st_size < 4096 or opened_before.st_size > OFFLINE_HIVE_MAX_BYTES:
                raise RuntimeError("Der ausgewählte NTUSER-Hive besitzt eine unzulässige Größe.")
            data = reader.read(OFFLINE_HIVE_MAX_BYTES + 1)
            opened_after = os.fstat(reader.fileno())
            if not _same_file_identity(opened_before, opened_after):
                raise RuntimeError("Der ausgewählte NTUSER-Hive wurde während des Lesens verändert.")
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError("Der ausgewählte NTUSER-Hive konnte nicht sicher gelesen werden.") from exc
    if len(data) != opened_before.st_size or len(data) > OFFLINE_HIVE_MAX_BYTES:
        raise RuntimeError("Der ausgewählte NTUSER-Hive konnte nicht vollständig gelesen werden.")
    if not _safe_regular_file_exists(path):
        raise RuntimeError("Der ausgewählte NTUSER-Hive ist nach dem Lesen nicht mehr sicher.")
    try:
        path_after = os.lstat(path)
    except OSError as exc:
        raise RuntimeError("Der ausgewählte NTUSER-Hive ist nach dem Lesen nicht mehr erreichbar.") from exc
    if not _same_file_identity(opened_before, path_after):
        raise RuntimeError("Der ausgewählte NTUSER-Hive wurde nach dem Lesen ausgetauscht.")
    return data


def _registry_header_checksum(data: bytes) -> int:
    checksum = 0
    for (word,) in struct.iter_unpack("<I", data[:0x1FC]):
        checksum ^= word
    if checksum == 0:
        return 1
    if checksum == 0xFFFFFFFF:
        return 0xFFFFFFFE
    return checksum


def _validate_hive_snapshot(data: bytes) -> None:
    if len(data) < REGISTRY_HEADER_BYTES or data[:4] != b"regf":
        raise RuntimeError("Der NTUSER-Hive besitzt keinen gültigen REGF-Header.")
    (
        primary_sequence,
        secondary_sequence,
        file_type,
        file_format,
        root_key_offset,
        hive_bins_size,
        clustering_factor,
        stored_checksum,
    ) = (
        struct.unpack_from("<I", data, 0x04)[0],
        struct.unpack_from("<I", data, 0x08)[0],
        struct.unpack_from("<I", data, 0x1C)[0],
        struct.unpack_from("<I", data, 0x20)[0],
        struct.unpack_from("<I", data, 0x24)[0],
        struct.unpack_from("<I", data, 0x28)[0],
        struct.unpack_from("<I", data, 0x2C)[0],
        struct.unpack_from("<I", data, 0x1FC)[0],
    )
    if primary_sequence != secondary_sequence:
        raise RuntimeError("Der NTUSER-Hive ist nicht konsistent abgeschlossen.")
    if (
        file_type != 0
        or file_format != 1
        or clustering_factor != 1
        or hive_bins_size < REGISTRY_HEADER_BYTES
        or hive_bins_size > len(data) - REGISTRY_HEADER_BYTES
        or root_key_offset < 32
        or root_key_offset % 8 != 0
        or root_key_offset + REGISTRY_NK_MINIMUM_CELL_BYTES > hive_bins_size
        or stored_checksum != _registry_header_checksum(data)
    ):
        raise RuntimeError("Der NTUSER-Hive besitzt einen inkonsistenten REGF-Header.")


@dataclass(frozen=True)
class _OfflineProfileHive:
    hive: Any
    key_type: type[Any]


@dataclass(frozen=True)
class _OfflineProfilePath:
    path: Path


def _root_key_cell(data: bytes, registry: Any) -> tuple[Any, int]:
    root_key_offset = struct.unpack_from("<I", data, 0x24)[0]
    cell_offset = REGISTRY_HEADER_BYTES + root_key_offset
    signed_cell_size = struct.unpack_from("<i", data, cell_offset)[0]
    if signed_cell_size >= 0:
        raise RuntimeError("Der REGF-Root-Key verweist nicht auf eine belegte Registryzelle.")
    cell_size = -signed_cell_size
    cell_end = cell_offset + cell_size
    if (
        cell_size < REGISTRY_NK_MINIMUM_CELL_BYTES
        or cell_size % 8 != 0
        or not any(cell_offset >= start and cell_end <= end for start, end in _validated_hive_bin_ranges(data))
        or data[cell_offset + 4 : cell_offset + 6] != b"nk"
    ):
        raise RuntimeError("Der REGF-Root-Key verweist auf keine gültige NK-Zelle.")
    return (
        registry.Cell(
            cell_type="nk",
            offset=cell_offset + 6,
            size=cell_size - 4,
        ),
        cell_size,
    )


def _validated_hive_bin_ranges(data: bytes) -> tuple[tuple[int, int], ...]:
    hive_bins_size = struct.unpack_from("<I", data, 0x28)[0]
    relative_offset = 0
    ranges: list[tuple[int, int]] = []
    while relative_offset < hive_bins_size:
        header_offset = REGISTRY_HEADER_BYTES + relative_offset
        if header_offset + 32 > len(data) or data[header_offset : header_offset + 4] != b"hbin":
            raise RuntimeError("Der NTUSER-Hive enthält einen ungültigen HBin-Header.")
        stored_offset = struct.unpack_from("<I", data, header_offset + 4)[0]
        bin_size = struct.unpack_from("<I", data, header_offset + 8)[0]
        if (
            stored_offset != relative_offset
            or bin_size < REGISTRY_HEADER_BYTES
            or bin_size % REGISTRY_HEADER_BYTES != 0
            or relative_offset + bin_size > hive_bins_size
        ):
            raise RuntimeError("Der NTUSER-Hive enthält eine inkonsistente HBin-Kette.")
        ranges.append((header_offset + 32, header_offset + bin_size))
        relative_offset += bin_size
    if relative_offset != hive_bins_size or not ranges:
        raise RuntimeError("Der NTUSER-Hive enthält keine vollständige HBin-Kette.")
    return tuple(ranges)


def _regipy_hive_from_bytes(data: bytes) -> _OfflineProfileHive:
    """Build the pinned parser's read-only hive object from an in-memory snapshot."""

    _validate_hive_snapshot(data)
    snapshot: BytesIO | None = None
    try:
        package = importlib.import_module("regipy")
        registry = importlib.import_module("regipy.registry")
        if getattr(package, "__version__", None) != REGIPY_VERSION:
            raise RuntimeError("Die gepinnte Offline-Registry-Komponente besitzt eine unerwartete Version.")
        hive = registry.RegistryHive.__new__(registry.RegistryHive)
        hive.partial_hive_path = None
        hive.hive_type = "ntuser"
        snapshot = BytesIO(data)
        hive._stream = snapshot
        with registry.boomerang_stream(hive._stream) as stream:
            hive.header = registry.REGF_HEADER.parse_stream(stream)
            root_cell, root_cell_size = _root_key_cell(data, registry)
            hive.root = registry.NKRecord(root_cell, stream)
            root_name_size = int(hive.root.header.key_name_size)
            root_name = hive.root.name
            if (
                root_name_size < 0
                or REGISTRY_NK_MINIMUM_CELL_BYTES + root_name_size > root_cell_size
                or not bool(hive.root.header.flags.KEY_HIVE_ENTRY)
                or not isinstance(root_name, str)
                or not root_name
                or "\x00" in root_name
                or "\ufffd" in root_name
            ):
                raise RuntimeError("Der NTUSER-Hive besitzt keinen gültigen Root-Key.")
        hive.name = hive.header.file_name
        return _OfflineProfileHive(hive=hive, key_type=registry.NKRecord)
    except MemoryError:
        if snapshot is not None:
            snapshot.close()
        raise
    except RuntimeError:
        if snapshot is not None:
            snapshot.close()
        raise
    except Exception as exc:
        if snapshot is not None:
            snapshot.close()
        raise RuntimeError("Der NTUSER-Hive konnte nicht vollständig read-only ausgewertet werden.") from exc


@contextmanager
def _offline_profile_hive(path: Path) -> Iterator[_OfflineProfileHive]:
    offline = _regipy_hive_from_bytes(_read_safe_hive_bytes(path))
    try:
        yield offline
    finally:
        offline.hive._stream.close()


def _select_offline_profile_hive(profile_path: Path) -> Path:
    candidates = (profile_path / "NTUSER.DAT", profile_path / "NTUSER.MAN")
    available = tuple(candidate for candidate in candidates if _safe_regular_file_exists(candidate))
    if len(available) != 1:
        raise RuntimeError("Das Benutzerprofil besitzt keinen eindeutig prüfbaren NTUSER-Hive.")
    return available[0]


@contextmanager
def _profile_hive(sid: str, profile_path: Path) -> Iterator[Any]:
    """Prefer a loaded hive and otherwise select a path for isolated inspection."""

    winreg: Any = __import__("winreg")

    access = winreg.KEY_QUERY_VALUE | getattr(winreg, "KEY_ENUMERATE_SUB_KEYS", 0x0008)
    try:
        loaded = winreg.OpenKey(winreg.HKEY_USERS, sid, 0, access)
    except FileNotFoundError:
        yield _OfflineProfilePath(_select_offline_profile_hive(profile_path))
    except OSError as exc:
        raise RuntimeError("Ein geladenes Benutzerprofil konnte nicht sicher gelesen werden.") from exc
    else:
        with loaded:
            yield loaded


def inspect_offline_profile_hive(path: Path) -> bool:
    """Inspect one offline hive directly inside the time-limited worker process."""

    try:
        with _offline_profile_hive(path) as offline:
            return _registered_desktop_present(offline) or _desktop_autostart_present(offline)
    except MemoryError as exc:
        raise RuntimeError("Der NTUSER-Hive überschreitet die sichere Speichergrenze.") from exc


def _offline_worker_command(path: Path) -> list[str]:
    arguments = ["--inspect-offline-profile-hive", str(path)]
    if bool(getattr(sys, "frozen", False)):
        return [sys.executable, *arguments]
    return [sys.executable, "-m", "app.windows_open_client", *arguments]


def _offline_worker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
    return environment


def _inspect_offline_profile_hive_isolated(
    path: Path,
    *,
    timeout_seconds: float = OFFLINE_HIVE_INSPECTION_TIMEOUT_SECONDS,
) -> bool:
    try:
        completed = subprocess.run(
            _offline_worker_command(path),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=_offline_worker_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Der NTUSER-Hive konnte nicht innerhalb der sicheren Zeitgrenze geprüft werden.") from exc
    except OSError as exc:
        raise RuntimeError("Die isolierte NTUSER-Hive-Prüfung konnte nicht gestartet werden.") from exc
    if completed.returncode == 0:
        return False
    if completed.returncode == OFFLINE_HIVE_CONFLICT_EXIT_CODE:
        return True
    raise RuntimeError("Der NTUSER-Hive konnte im isolierten Prüfprozess nicht sicher ausgewertet werden.")


def _offline_key_values(root: _OfflineProfileHive, path: str) -> dict[str, tuple[Any, str]] | None:
    current = root.hive.root
    try:
        for part in path.split("\\"):
            children = list(current.iter_subkeys())
            if len(children) != int(current.subkey_count):
                raise RuntimeError("Der NTUSER-Hive enthält eine unvollständige Subkey-Liste.")
            indexed: dict[str, Any] = {}
            for child in children:
                if not isinstance(child, root.key_type):
                    raise RuntimeError("Der NTUSER-Hive enthält einen unerwarteten Subkey-Typ.")
                name = child.name
                if not isinstance(name, str) or not name or "\x00" in name or "\ufffd" in name:
                    raise RuntimeError("Der NTUSER-Hive enthält einen ungültigen Subkey-Namen.")
                folded = name.casefold()
                if folded in indexed:
                    raise RuntimeError("Der NTUSER-Hive enthält mehrdeutige Subkey-Namen.")
                indexed[folded] = child
            current = indexed.get(part.casefold())
            if current is None:
                return None

        values = list(current.get_values(trim_values=False))
        if len(values) != int(current.values_count):
            raise RuntimeError("Der NTUSER-Hive enthält eine unvollständige Werteliste.")
        result: dict[str, tuple[Any, str]] = {}
        for value in values:
            name = value.name
            if (
                not isinstance(name, str)
                or "\x00" in name
                or "\ufffd" in name
                or bool(value.is_corrupted)
                or not isinstance(value.value_type, str)
            ):
                raise RuntimeError("Der NTUSER-Hive enthält einen ungültigen Registrywert.")
            folded = name.casefold()
            if folded in result:
                raise RuntimeError("Der NTUSER-Hive enthält mehrdeutige Registrywerte.")
            result[folded] = (value.value, value.value_type)
        return result
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("Der NTUSER-Hive konnte nicht vollständig inventarisiert werden.") from exc


def _registered_desktop_present(root: Any) -> bool:
    if isinstance(root, _OfflineProfileHive):
        values = _offline_key_values(root, DESKTOP_UNINSTALL_KEY)
        if values is None:
            return False
        install_location = values.get("installlocation")
        if install_location is None:
            return True
        value, value_type = install_location
        if value_type != "REG_SZ" or not isinstance(value, str) or not value.strip():
            raise RuntimeError("Eine registrierte Desktopinstallation besitzt einen ungültigen Installationspfad.")
        _validated_local_fixed_path(value)
        return True

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
    if isinstance(root, _OfflineProfileHive):
        values = _offline_key_values(root, AUTOSTART_KEY)
        if values is None:
            return False
        autostart = values.get(AUTOSTART_VALUE_NAME.casefold())
        if autostart is None:
            return False
        value, value_type = autostart
        if (
            value_type != "REG_SZ"
            or not isinstance(value, str)
            or not value.strip()
            or "\x00" in value
            or "\ufffd" in value
        ):
            raise RuntimeError("Ein registrierter Desktop-Autostart besitzt einen ungültigen Wert.")
        return True

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
    offline_inventory_deadline = time.monotonic() + OFFLINE_PROFILE_INVENTORY_TIMEOUT_SECONDS
    for sid, profile_path in _profile_paths():
        default_install_directory = profile_path / "AppData" / "Local" / "Programs" / DESKTOP_INSTALL_DIRECTORY_NAME
        if _safe_directory_exists(default_install_directory):
            raise RuntimeError(
                "Eine Desktop-Version oder Teilinstallation wurde gefunden. Deinstallieren Sie sie manuell, "
                "bevor Sie den Dienst installieren."
            )
        with _profile_hive(sid, profile_path) as hive:
            if isinstance(hive, _OfflineProfilePath):
                remaining_seconds = offline_inventory_deadline - time.monotonic()
                if remaining_seconds <= 0:
                    raise RuntimeError("Die Offline-Profilinventur überschritt die sichere Gesamtzeitgrenze.")
                conflict = _inspect_offline_profile_hive_isolated(
                    hive.path,
                    timeout_seconds=min(
                        OFFLINE_HIVE_INSPECTION_TIMEOUT_SECONDS,
                        remaining_seconds,
                    ),
                )
            else:
                conflict = _registered_desktop_present(hive) or _desktop_autostart_present(hive)
            if conflict:
                raise RuntimeError(
                    "Eine Desktop-Version oder deren Autostart wurde gefunden. Deinstallieren Sie "
                    "die Desktop-Version manuell, bevor Sie den Dienst installieren."
                )
