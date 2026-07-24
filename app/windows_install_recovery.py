from __future__ import annotations

import ctypes
import math
import ntpath
import os
import stat
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from . import windows_install_transaction, windows_service_metadata, windows_service_preflight
from .windows_service_config import SERVICE_ACCOUNT, SERVICE_NAME, validate_machine_path

INSTALLATION_DIRECTORY_NAME = "E-Rechnungs-Pruefer-Dienst"
SERVICE_EXECUTABLE_NAME = "E-Rechnungs-Pruefer-Dienst.exe"
BUNDLE_DIRECTORY_NAMES = {
    "live": "service",
    "new": "service.new",
    "rollback": "service.rollback",
    "obsolete": "service.obsolete",
}
SERVICE_WAIT_SECONDS = 330.0
SERVICE_POLL_SECONDS = 0.25

ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_SERVICE_MARKED_FOR_DELETE = 1072
DELETE_ACCESS = 0x00010000
_WINDOWS_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_FOLDERID_PROGRAM_FILES_X64 = uuid.UUID("6d809377-6af0-444b-8957-a3773f02200e")


class _Guid(ctypes.Structure):
    _fields_ = (
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    )


@dataclass(frozen=True, slots=True)
class InstallationLayout:
    root: Path
    live: Path
    new: Path
    rollback: Path
    obsolete: Path

    def bundle(self, slot: str) -> Path:
        bundles = {
            "live": self.live,
            "new": self.new,
            "rollback": self.rollback,
            "obsolete": self.obsolete,
        }
        try:
            return bundles[slot]
        except KeyError as exc:
            raise RuntimeError("Ein unbekannter Dienst-Bundle-Slot wurde angefordert.") from exc


@dataclass(frozen=True, order=True, slots=True)
class _TreeEntry:
    relative_name: str
    kind: str
    device: int
    inode: int
    links: int
    size: int


_TreeSnapshot = tuple[_TreeEntry, ...]


def _windows_program_files_directory() -> Path:
    """Resolve 64-bit Program Files through the Known Folder API."""

    if sys_platform() != "win32":
        raise OSError("Der kanonische Windows-Programmdateipfad ist nur unter Windows verfügbar.")
    folder = _Guid(
        _FOLDERID_PROGRAM_FILES_X64.time_low,
        _FOLDERID_PROGRAM_FILES_X64.time_mid,
        _FOLDERID_PROGRAM_FILES_X64.time_hi_version,
        (ctypes.c_ubyte * 8)(*_FOLDERID_PROGRAM_FILES_X64.bytes[8:]),
    )
    win_dll: Any = vars(ctypes)["WinDLL"]
    shell32 = win_dll("shell32", use_last_error=True)
    ole32 = win_dll("ole32", use_last_error=True)
    known_folder = shell32.SHGetKnownFolderPath
    known_folder.argtypes = [
        ctypes.POINTER(_Guid),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    known_folder.restype = ctypes.c_long
    free_memory = ole32.CoTaskMemFree
    free_memory.argtypes = [ctypes.c_void_p]
    free_memory.restype = None
    pointer = ctypes.c_void_p()
    result = known_folder(ctypes.byref(folder), 0, None, ctypes.byref(pointer))
    if result != 0 or not pointer.value:
        raise RuntimeError(f"Der 64-Bit-Programmdateipfad konnte nicht sicher bestimmt werden (HRESULT {result}).")
    try:
        value = ctypes.wstring_at(pointer.value)
    finally:
        free_memory(pointer)
    if not value:
        raise RuntimeError("Der 64-Bit-Programmdateipfad ist leer.")
    return Path(value)


def sys_platform() -> str:
    # Kept as a narrow native boundary so the Windows-only adapter is testable
    # without making environment variables part of the trust decision.
    import sys

    return sys.platform


def _canonical_windows_path(path: Path, *, description: str) -> str:
    value = str(path)
    pure_path = PureWindowsPath(value)
    if not value or "\x00" in value or '"' in value or not pure_path.is_absolute() or value != ntpath.normpath(value):
        raise RuntimeError(f"{description} ist kein absoluter kanonischer Windows-Pfad.")
    return value


def _canonical_layout(expected_executable: Path) -> InstallationLayout:
    expected = _canonical_windows_path(
        expected_executable,
        description="Der erwartete Dienstprogrammpfad",
    )
    program_files = _canonical_windows_path(
        _windows_program_files_directory(),
        description="Der 64-Bit-Programmdateipfad",
    )
    expected_pure = PureWindowsPath(expected)
    program_files_pure = PureWindowsPath(program_files)
    required_root = program_files_pure / INSTALLATION_DIRECTORY_NAME
    required_executable = required_root / BUNDLE_DIRECTORY_NAMES["live"] / SERVICE_EXECUTABLE_NAME
    if str(expected_pure).casefold() != str(required_executable).casefold():
        raise RuntimeError("Der erwartete Dienstprogrammpfad liegt nicht im festen 64-Bit-Program-Files-Bundle.")

    program_files_path = Path(program_files)
    root = Path(str(required_root))
    if not validate_machine_path(program_files_path, directory=True):
        raise RuntimeError("Der kanonische 64-Bit-Programmdateipfad fehlt.")
    if not validate_machine_path(root, directory=True):
        raise RuntimeError("Das feste Dienst-Installationsverzeichnis fehlt.")
    return InstallationLayout(
        root=root,
        live=Path(str(required_root / BUNDLE_DIRECTORY_NAMES["live"])),
        new=Path(str(required_root / BUNDLE_DIRECTORY_NAMES["new"])),
        rollback=Path(str(required_root / BUNDLE_DIRECTORY_NAMES["rollback"])),
        obsolete=Path(str(required_root / BUNDLE_DIRECTORY_NAMES["obsolete"])),
    )


def _win32service() -> Any:
    if sys_platform() != "win32":
        raise OSError("Der Windows-Dienst kann nur unter Windows verwaltet werden.")
    try:
        import win32service
    except ImportError as exc:
        raise RuntimeError("pywin32 fehlt; der Windows-Dienst kann nicht sicher verwaltet werden.") from exc
    return win32service


def _service_error(exc: BaseException, code: int) -> bool:
    return getattr(exc, "winerror", None) == code


def _classify_service_handle(
    service: Any,
    win32service: Any,
    expected_executable: Path,
) -> windows_install_transaction.ServiceState:
    try:
        configuration = win32service.QueryServiceConfig(service)
    except Exception as exc:
        raise RuntimeError("Die Dienstkonfiguration konnte nicht sicher gelesen werden.") from exc
    if not isinstance(configuration, (list, tuple)) or len(configuration) != 9:
        return windows_install_transaction.ServiceState.UNSTABLE
    image_path = configuration[3]
    account = configuration[7]
    expected_image_path = f'"{expected_executable}"'
    if (
        not isinstance(image_path, str)
        or not isinstance(account, str)
        or image_path.casefold() != expected_image_path.casefold()
        or account.casefold() != SERVICE_ACCOUNT.casefold()
    ):
        return windows_install_transaction.ServiceState.FOREIGN
    try:
        status = win32service.QueryServiceStatus(service)
    except Exception as exc:
        raise RuntimeError("Der Dienststatus konnte nicht sicher gelesen werden.") from exc
    if (
        not isinstance(status, (list, tuple))
        or len(status) < 2
        or not isinstance(status[1], int)
        or isinstance(status[1], bool)
    ):
        return windows_install_transaction.ServiceState.UNSTABLE
    if status[1] == win32service.SERVICE_STOPPED:
        return windows_install_transaction.ServiceState.OWNED_STOPPED
    if status[1] == win32service.SERVICE_RUNNING:
        return windows_install_transaction.ServiceState.OWNED_RUNNING
    return windows_install_transaction.ServiceState.UNSTABLE


def _observe_service(expected_executable: Path) -> windows_install_transaction.ServiceState:
    win32service = _win32service()
    manager = None
    service = None
    try:
        manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        try:
            service = win32service.OpenService(
                manager,
                SERVICE_NAME,
                win32service.SERVICE_QUERY_CONFIG | win32service.SERVICE_QUERY_STATUS,
            )
        except Exception as exc:
            if _service_error(exc, ERROR_SERVICE_DOES_NOT_EXIST):
                return windows_install_transaction.ServiceState.ABSENT
            if _service_error(exc, ERROR_SERVICE_MARKED_FOR_DELETE):
                return windows_install_transaction.ServiceState.UNSTABLE
            raise RuntimeError("Der erwartete Windows-Dienst konnte nicht sicher geöffnet werden.") from exc
        return _classify_service_handle(service, win32service, expected_executable)
    finally:
        if service is not None:
            win32service.CloseServiceHandle(service)
        if manager is not None:
            win32service.CloseServiceHandle(manager)


def _path_record(path: Path, root: Path) -> _TreeEntry:
    try:
        path_stat = os.lstat(path)
    except OSError as exc:
        raise RuntimeError(f"Der Bundle-Pfad {path} hat sich während der Sicherheitsprüfung geändert.") from exc
    attributes = int(getattr(path_stat, "st_file_attributes", 0))
    if stat.S_ISLNK(path_stat.st_mode) or attributes & _WINDOWS_REPARSE_POINT_ATTRIBUTE:
        raise RuntimeError(f"Der Bundle-Pfad {path} darf kein Reparse-Point, Junction oder Symlink sein.")
    if stat.S_ISDIR(path_stat.st_mode):
        kind = "directory"
    elif stat.S_ISREG(path_stat.st_mode):
        if int(path_stat.st_nlink) != 1:
            raise RuntimeError(f"Die Bundle-Datei {path} besitzt eine unerwartete Hardlink-Anzahl.")
        kind = "file"
    else:
        raise RuntimeError(f"Der Bundle-Pfad {path} besitzt einen unzulässigen Dateityp.")
    relative = "." if path == root else path.relative_to(root).as_posix()
    return _TreeEntry(
        relative_name=relative,
        kind=kind,
        device=int(path_stat.st_dev),
        inode=int(path_stat.st_ino),
        links=int(path_stat.st_nlink),
        size=int(path_stat.st_size),
    )


def _secure_tree_snapshot(root: Path) -> _TreeSnapshot:
    if not validate_machine_path(root, directory=True):
        raise RuntimeError(f"Das zu inventarisierende Dienst-Bundle fehlt: {root}")
    records: list[_TreeEntry] = []

    def inventory(path: Path) -> None:
        record = _path_record(path, root)
        records.append(record)
        if record.kind != "directory":
            validate_machine_path(path, directory=False)
            return
        validate_machine_path(path, directory=True)
        try:
            with os.scandir(path) as directory_entries:
                entries = sorted(directory_entries, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise RuntimeError(f"Das Dienst-Bundle {path} konnte nicht vollständig inventarisiert werden.") from exc
        folded_names: set[str] = set()
        for entry in entries:
            folded = entry.name.casefold()
            if folded in folded_names:
                raise RuntimeError(f"Das Dienst-Bundle {path} enthält mehrdeutige Dateinamen.")
            folded_names.add(folded)
            inventory(path / entry.name)

    inventory(root)
    return tuple(records)


def _bundle_snapshot(path: Path, *, require_executable: bool) -> _TreeSnapshot | None:
    if not os.path.lexists(path):
        return None
    snapshot = _secure_tree_snapshot(path)
    if require_executable:
        executable = path / SERVICE_EXECUTABLE_NAME
        if not validate_machine_path(executable, directory=False):
            raise RuntimeError(f"Das Dienst-Bundle {path} enthält nicht die erwartete eigene Dienstdatei.")
        if not _snapshot_has_service_executable(snapshot):
            raise RuntimeError(f"Das Dienst-Bundle {path} enthält keinen exakt benannten eigenen Dienst.")
    return snapshot


def _snapshot_has_service_executable(snapshot: _TreeSnapshot) -> bool:
    return any(entry.relative_name == SERVICE_EXECUTABLE_NAME and entry.kind == "file" for entry in snapshot)


def _remove_secure_tree(path: Path, expected: _TreeSnapshot) -> None:
    if _secure_tree_snapshot(path) != expected:
        raise RuntimeError("Das Dienst-Bundle hat sich vor dem Löschen verändert; Recovery bricht ab.")

    def remove(entry_path: Path) -> None:
        record = _path_record(entry_path, path)
        if record.kind == "file":
            validate_machine_path(entry_path, directory=False)
            try:
                entry_path.unlink()
            except OSError as exc:
                raise RuntimeError(f"Die Bundle-Datei {entry_path} konnte nicht sicher gelöscht werden.") from exc
            return
        validate_machine_path(entry_path, directory=True)
        try:
            with os.scandir(entry_path) as directory_entries:
                names = sorted((entry.name for entry in directory_entries), key=str.casefold)
        except OSError as exc:
            raise RuntimeError(f"Das Dienst-Bundle {entry_path} konnte nicht sicher gelesen werden.") from exc
        for name in names:
            remove(entry_path / name)
        try:
            entry_path.rmdir()
        except OSError as exc:
            raise RuntimeError(f"Das Bundle-Verzeichnis {entry_path} konnte nicht sicher gelöscht werden.") from exc

    remove(path)
    if os.path.lexists(path):
        raise RuntimeError("Das ausgewählte Dienst-Bundle wurde nicht vollständig gelöscht.")


class WindowsInstallRecovery:
    """Concrete fail-closed Windows adapter for durable installer recovery plans."""

    def __init__(
        self,
        expected_executable: Path,
        *,
        wait_seconds: float = SERVICE_WAIT_SECONDS,
        poll_seconds: float = SERVICE_POLL_SECONDS,
        _clock: Callable[[], float] = time.monotonic,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            not isinstance(wait_seconds, (int, float))
            or isinstance(wait_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or isinstance(poll_seconds, bool)
            or not math.isfinite(wait_seconds)
            or not math.isfinite(poll_seconds)
            or wait_seconds <= 0
            or poll_seconds <= 0
            or poll_seconds > wait_seconds
        ):
            raise RuntimeError("Die begrenzte Dienstwartezeit ist ungültig.")
        self.expected_executable = expected_executable
        self.layout = _canonical_layout(expected_executable)
        self._wait_seconds = float(wait_seconds)
        self._poll_seconds = float(poll_seconds)
        self._clock = _clock
        self._sleep = _sleep
        self._bundle_snapshots: dict[str, _TreeSnapshot | None] = {}
        self._observed_service_state: windows_install_transaction.ServiceState | None = None

    def _ensure_layout(self) -> None:
        if _canonical_layout(self.expected_executable) != self.layout:
            raise RuntimeError("Der kanonische Dienst-Installationspfad hat sich unerwartet geändert.")

    def _service_state(self) -> windows_install_transaction.ServiceState:
        return _observe_service(self.expected_executable)

    def observe(self) -> windows_install_transaction.RecoveryObservation:
        self._ensure_layout()
        snapshots = {
            slot: _bundle_snapshot(
                self.layout.bundle(slot),
                require_executable=False,
            )
            for slot in BUNDLE_DIRECTORY_NAMES
        }
        self._bundle_snapshots = snapshots
        service_state = self._service_state()
        self._observed_service_state = service_state
        incomplete_slots: set[str] = set()
        for slot in ("live", "rollback", "obsolete"):
            snapshot = snapshots[slot]
            if snapshot is not None and not _snapshot_has_service_executable(snapshot):
                incomplete_slots.add(slot)
        return windows_install_transaction.RecoveryObservation(
            bundles=windows_install_transaction.BundleTopology(
                live=snapshots["live"] is not None,
                new=snapshots["new"] is not None,
                rollback=snapshots["rollback"] is not None,
                obsolete=snapshots["obsolete"] is not None,
            ),
            service=service_state,
            incomplete_bundles=frozenset(incomplete_slots),
        )

    def _open_owned_service(self, access: int) -> tuple[Any, Any, Any]:
        win32service = _win32service()
        manager = None
        try:
            manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
            service = win32service.OpenService(manager, SERVICE_NAME, access)
        except Exception as exc:
            if manager is not None:
                win32service.CloseServiceHandle(manager)
            if _service_error(exc, ERROR_SERVICE_DOES_NOT_EXIST):
                raise RuntimeError("Der eigene Windows-Dienst fehlt unerwartet.") from exc
            raise RuntimeError("Der eigene Windows-Dienst konnte nicht sicher geöffnet werden.") from exc
        try:
            state = _classify_service_handle(service, win32service, self.expected_executable)
        except Exception:
            win32service.CloseServiceHandle(service)
            win32service.CloseServiceHandle(manager)
            raise
        if state in {
            windows_install_transaction.ServiceState.FOREIGN,
            windows_install_transaction.ServiceState.UNSTABLE,
        }:
            win32service.CloseServiceHandle(service)
            win32service.CloseServiceHandle(manager)
            raise RuntimeError("Der Windows-Dienst ist fremd oder nicht stabil; Recovery verändert ihn nicht.")
        return win32service, manager, service

    @staticmethod
    def _close_service(win32service: Any, manager: Any, service: Any) -> None:
        win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(manager)

    def _wait_for_owned_state(
        self,
        service: Any,
        win32service: Any,
        expected: windows_install_transaction.ServiceState,
    ) -> None:
        deadline = self._clock() + self._wait_seconds
        while True:
            state = _classify_service_handle(service, win32service, self.expected_executable)
            if state is expected:
                return
            if state is windows_install_transaction.ServiceState.FOREIGN:
                raise RuntimeError("Der Windows-Dienst hat während der Wartezeit seine Eigentümerschaft geändert.")
            if state is not windows_install_transaction.ServiceState.UNSTABLE:
                raise RuntimeError("Der Windows-Dienst wechselte in einen unerwarteten Zustand.")
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise RuntimeError("Der Windows-Dienst erreichte den stabilen Zielzustand nicht rechtzeitig.")
            self._sleep(min(self._poll_seconds, remaining))

    def stop_service(self) -> None:
        win32service = _win32service()
        service_module, manager, service = self._open_owned_service(
            win32service.SERVICE_QUERY_CONFIG | win32service.SERVICE_QUERY_STATUS | win32service.SERVICE_STOP
        )
        try:
            state = _classify_service_handle(service, service_module, self.expected_executable)
            if state is windows_install_transaction.ServiceState.OWNED_STOPPED:
                self._observed_service_state = state
                return
            if state is not windows_install_transaction.ServiceState.OWNED_RUNNING:
                raise RuntimeError("Nur ein stabil laufender eigener Dienst darf gestoppt werden.")
            try:
                service_module.ControlService(service, service_module.SERVICE_CONTROL_STOP)
            except Exception as exc:
                if not _service_error(exc, ERROR_SERVICE_NOT_ACTIVE):
                    raise RuntimeError("Der eigene Windows-Dienst konnte nicht kontrolliert gestoppt werden.") from exc
            self._wait_for_owned_state(
                service,
                service_module,
                windows_install_transaction.ServiceState.OWNED_STOPPED,
            )
            self._observed_service_state = windows_install_transaction.ServiceState.OWNED_STOPPED
        finally:
            self._close_service(service_module, manager, service)

    def start_service(self) -> None:
        win32service = _win32service()
        service_module, manager, service = self._open_owned_service(
            win32service.SERVICE_QUERY_CONFIG | win32service.SERVICE_QUERY_STATUS | win32service.SERVICE_START
        )
        try:
            state = _classify_service_handle(service, service_module, self.expected_executable)
            if state is windows_install_transaction.ServiceState.OWNED_RUNNING:
                self._observed_service_state = state
                return
            if state is not windows_install_transaction.ServiceState.OWNED_STOPPED:
                raise RuntimeError("Nur ein stabil gestoppter eigener Dienst darf gestartet werden.")
            try:
                service_module.StartService(service, None)
            except Exception as exc:
                if not _service_error(exc, ERROR_SERVICE_ALREADY_RUNNING):
                    raise RuntimeError("Der eigene Windows-Dienst konnte nicht kontrolliert gestartet werden.") from exc
            self._wait_for_owned_state(
                service,
                service_module,
                windows_install_transaction.ServiceState.OWNED_RUNNING,
            )
            self._observed_service_state = windows_install_transaction.ServiceState.OWNED_RUNNING
        finally:
            self._close_service(service_module, manager, service)

    def _wait_until_service_absent(self) -> None:
        deadline = self._clock() + self._wait_seconds
        while True:
            state = self._service_state()
            if state is windows_install_transaction.ServiceState.ABSENT:
                return
            if state not in {
                windows_install_transaction.ServiceState.OWNED_STOPPED,
                windows_install_transaction.ServiceState.UNSTABLE,
            }:
                raise RuntimeError("Der zu löschende Dienst wechselte in einen unsicheren Zustand.")
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise RuntimeError("Der Windows-Dienst wurde nicht innerhalb der begrenzten Wartezeit gelöscht.")
            self._sleep(min(self._poll_seconds, remaining))

    def delete_service(self) -> None:
        if self._service_state() is windows_install_transaction.ServiceState.ABSENT:
            self._observed_service_state = windows_install_transaction.ServiceState.ABSENT
            return
        win32service = _win32service()
        service_module, manager, service = self._open_owned_service(
            win32service.SERVICE_QUERY_CONFIG | win32service.SERVICE_QUERY_STATUS | DELETE_ACCESS
        )
        try:
            state = _classify_service_handle(service, service_module, self.expected_executable)
            if state is not windows_install_transaction.ServiceState.OWNED_STOPPED:
                raise RuntimeError("Nur ein stabil gestoppter eigener Dienst darf gelöscht werden.")
            try:
                service_module.DeleteService(service)
            except Exception as exc:
                if not _service_error(exc, ERROR_SERVICE_MARKED_FOR_DELETE):
                    raise RuntimeError("Der eigene Windows-Dienst konnte nicht kontrolliert gelöscht werden.") from exc
        finally:
            self._close_service(service_module, manager, service)
        self._wait_until_service_absent()
        self._observed_service_state = windows_install_transaction.ServiceState.ABSENT

    def _require_safe_service_for_bundle_mutation(self, *, live: bool) -> None:
        state = self._service_state()
        if self._observed_service_state is not None and state is not self._observed_service_state:
            raise RuntimeError("Der Dienstzustand hat sich seit der Recovery-Planung verändert.")
        if state in {
            windows_install_transaction.ServiceState.FOREIGN,
            windows_install_transaction.ServiceState.UNSTABLE,
        }:
            raise RuntimeError("Ein fremder oder instabiler Dienst blockiert die Bundle-Recovery.")
        if live and state is windows_install_transaction.ServiceState.OWNED_RUNNING:
            raise RuntimeError("Das Live-Bundle eines laufenden Dienstes darf nicht verändert werden.")

    def _unchanged_bundle_snapshot(self, slot: str) -> _TreeSnapshot | None:
        path = self.layout.bundle(slot)
        current = _bundle_snapshot(path, require_executable=False)
        if slot in self._bundle_snapshots and current != self._bundle_snapshots[slot]:
            raise RuntimeError("Das Dienst-Bundle hat sich seit der Recovery-Planung verändert.")
        return current

    def delete_bundle(self, slot: str) -> None:
        path = self.layout.bundle(slot)
        self._ensure_layout()
        self._require_safe_service_for_bundle_mutation(live=slot == "live")
        snapshot = self._unchanged_bundle_snapshot(slot)
        if snapshot is None:
            return
        _remove_secure_tree(path, snapshot)
        self._bundle_snapshots[slot] = None

    def move_bundle(self, source: str, destination: str) -> None:
        if (source, destination) not in {("rollback", "live"), ("obsolete", "live")}:
            raise RuntimeError("Die angeforderte Dienst-Bundle-Verschiebung ist nicht zulässig.")
        self._ensure_layout()
        self._require_safe_service_for_bundle_mutation(live=True)
        source_path = self.layout.bundle(source)
        destination_path = self.layout.bundle(destination)
        source_snapshot = self._unchanged_bundle_snapshot(source)
        destination_snapshot = self._unchanged_bundle_snapshot(destination)
        if source_snapshot is None:
            if destination_snapshot is not None:
                if not _snapshot_has_service_executable(destination_snapshot):
                    raise RuntimeError("Das bereits verschobene Live-Bundle ist nicht vollständig.")
                return
            raise RuntimeError("Weder das Quell- noch das Ziel-Bundle der Recovery ist vorhanden.")
        if not _snapshot_has_service_executable(source_snapshot):
            raise RuntimeError("Nur ein vollständiges altes Backup-Bundle darf in den Live-Slot verschoben werden.")
        if destination_snapshot is not None:
            raise RuntimeError("Quell- und Ziel-Bundle sind gleichzeitig vorhanden; Recovery bricht ab.")
        try:
            os.rename(source_path, destination_path)
        except OSError as exc:
            raise RuntimeError("Das Dienst-Bundle konnte nicht atomar in den Live-Slot verschoben werden.") from exc
        if os.path.lexists(source_path):
            raise RuntimeError("Das Quell-Bundle blieb nach der atomaren Verschiebung vorhanden.")
        moved_snapshot = _secure_tree_snapshot(destination_path)
        if moved_snapshot != source_snapshot:
            raise RuntimeError("Das verschobene Dienst-Bundle stimmt nicht mehr mit der geprüften Quelle überein.")
        self._bundle_snapshots[source] = None
        self._bundle_snapshots[destination] = moved_snapshot

    def restore_service_metadata(self, payload: Mapping[str, object]) -> None:
        if self._service_state() is not windows_install_transaction.ServiceState.OWNED_STOPPED:
            raise RuntimeError("SCM-Metadaten dürfen nur für den stabil gestoppten eigenen Dienst restauriert werden.")
        windows_service_metadata.restore_service_metadata_payload(self.expected_executable, payload)
        if self._service_state() is not windows_install_transaction.ServiceState.OWNED_STOPPED:
            raise RuntimeError("Der eigene Dienst ist nach der SCM-Restaurierung nicht stabil gestoppt.")
        self._observed_service_state = windows_install_transaction.ServiceState.OWNED_STOPPED

    def purge_machine_state(self) -> None:
        if self._service_state() is not windows_install_transaction.ServiceState.ABSENT:
            raise RuntimeError("Maschinenzustand darf erst nach dem sicheren Entfernen des Dienstes gelöscht werden.")
        windows_service_preflight.purge_machine_state()
        if self._service_state() is not windows_install_transaction.ServiceState.ABSENT:
            raise RuntimeError("Nach dem Löschen des Maschinenzustands ist unerwartet ein Dienst vorhanden.")
        self._observed_service_state = windows_install_transaction.ServiceState.ABSENT


def observe_installation(expected_executable: Path) -> windows_install_transaction.RecoveryObservation:
    return WindowsInstallRecovery(expected_executable).observe()


def execute_install_recovery(
    expected_executable: Path,
    *,
    transaction_id: str,
) -> windows_install_transaction.RecoveryPlan:
    operations = WindowsInstallRecovery(expected_executable)
    state = windows_install_transaction.load_transaction(
        expected_executable,
        transaction_id=transaction_id,
    )
    if state is None:
        raise RuntimeError("Für die angeforderte Installation ist keine Recovery-Transaktion vorhanden.")
    plan = windows_install_transaction.plan_recovery(state, operations.observe())
    windows_install_transaction.execute_recovery(
        expected_executable,
        state=state,
        plan=plan,
        operations=operations,
    )
    return plan


WindowsInstallRecoveryOperations = WindowsInstallRecovery
