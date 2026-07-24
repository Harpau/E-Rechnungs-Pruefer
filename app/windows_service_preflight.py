from __future__ import annotations

import os
import re
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .windows_acl import WindowsServiceAcl
from .windows_service_config import (
    SERVICE_NAME,
    ServiceConfiguration,
    ServicePaths,
    TokenStore,
    load_configuration,
    validate_machine_path,
)

ERROR_SERVICE_DOES_NOT_EXIST = 1060
SERVICE_LOG_BACKUP_COUNT = 3
MAXIMUM_RUNTIME_ENTRIES = 512
MAXIMUM_RUNTIME_DEPTH = 8
_KOSIT_RUNTIME_NAME = re.compile(r"einvoice-kosit-[0-9a-f]{32}\Z")


@dataclass(frozen=True, slots=True)
class MachinePreflight:
    configuration: ServiceConfiguration
    existing_state: bool


@dataclass(frozen=True, slots=True)
class _RuntimeInventory:
    files: tuple[Path, ...]
    inherited_directories: tuple[Path, ...]
    protected_directories: tuple[Path, ...]


def _path_present(path: Path) -> bool:
    return os.path.lexists(path)


def inspect_machine_state() -> MachinePreflight:
    """Inspect existing ProgramData state without creating or changing any path."""

    if sys.platform != "win32":
        raise OSError("Die Maschinenprüfung ist ausschließlich unter Windows verfügbar.")
    paths = ServicePaths.from_environment()
    validate_machine_path(paths.data_directory, directory=True)
    directory_exists = _path_present(paths.data_directory)
    configuration_exists = _path_present(paths.configuration)
    token_exists = _path_present(paths.token)
    if not directory_exists and not configuration_exists and not token_exists:
        return MachinePreflight(ServiceConfiguration(), False)
    if not directory_exists or not configuration_exists or not token_exists:
        raise RuntimeError("Der vorhandene ProgramData-Zustand ist unvollständig und wird nicht automatisch verändert.")

    acl = WindowsServiceAcl()
    acl.verify_service_paths(paths)
    configuration = load_configuration(paths.configuration)
    TokenStore(paths.token).load()
    acl.verify_service_paths(paths)
    return MachinePreflight(configuration, True)


def _win32service() -> Any:
    if sys.platform != "win32":
        raise OSError("Der Dienststatus kann nur unter Windows geprüft werden.")
    try:
        import win32service
    except ImportError as exc:
        raise RuntimeError("pywin32 fehlt; der Dienststatus kann nicht sicher geprüft werden.") from exc
    return win32service


def require_service_stopped_or_absent() -> None:
    win32service = _win32service()
    manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    service = None
    try:
        try:
            service = win32service.OpenService(manager, SERVICE_NAME, win32service.SERVICE_QUERY_STATUS)
        except Exception as exc:
            if getattr(exc, "winerror", None) == ERROR_SERVICE_DOES_NOT_EXIST:
                return
            raise RuntimeError("Der Dienststatus konnte für den Maschinenzustand nicht sicher gelesen werden.") from exc
        status = win32service.QueryServiceStatus(service)
        if not isinstance(status, (list, tuple)) or len(status) < 2:
            raise RuntimeError("Der SCM hat einen unbekannten Dienststatus geliefert.")
        if int(status[1]) != win32service.SERVICE_STOPPED:
            raise RuntimeError("Der Windows-Dienst muss vor dieser Maschinenoperation vollständig gestoppt sein.")
    finally:
        if service is not None:
            win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(manager)


def preflight_machine() -> None:
    paths = ServicePaths.from_environment()
    if validate_machine_path(paths.data_directory, directory=True):
        WindowsServiceAcl().repair_explorer_directory_aces(paths)
    inspect_machine_state()


def _directory_entry_names(path: Path) -> tuple[str, ...]:
    try:
        with os.scandir(path) as entries:
            return tuple(entry.name for entry in entries)
    except OSError as exc:
        raise RuntimeError(f"Der Maschinenpfad {path} konnte nicht vollständig inventarisiert werden.") from exc


def _require_only_known_entries(path: Path, allowed: set[str]) -> tuple[str, ...]:
    names = _directory_entry_names(path)
    unknown = sorted(name for name in names if name not in allowed)
    if unknown:
        raise RuntimeError(f"Der Maschinenpfad {path} enthält unbekannte Einträge und wird nicht automatisch gelöscht.")
    return names


def _atomic_write_temporary_target(name: str, paths: ServicePaths) -> Path | None:
    for target in (paths.configuration, paths.token):
        prefix = f".{target.name}."
        if not name.startswith(prefix) or not name.endswith(".tmp"):
            continue
        nonce = name[len(prefix) : -len(".tmp")]
        if len(nonce) == 16 and all(character in "0123456789abcdef" for character in nonce):
            return target
    return None


def _bounded_runtime_entries(path: Path, remaining: int) -> tuple[tuple[str, bool, bool], ...]:
    observed: list[tuple[str, bool, bool]] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if len(observed) >= remaining:
                    raise RuntimeError("Der temporäre Dienstzustand enthält unerwartet viele Einträge.")
                observed.append(
                    (
                        entry.name,
                        entry.is_dir(follow_symlinks=False),
                        entry.is_file(follow_symlinks=False),
                    )
                )
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError(f"Der Maschinenpfad {path} konnte nicht vollständig inventarisiert werden.") from exc
    return tuple(observed)


def _inventory_runtime_state(paths: ServicePaths, acl: WindowsServiceAcl) -> _RuntimeInventory | None:
    runtime = paths.runtime_directory
    if not validate_machine_path(runtime, directory=True):
        return None
    acl.verify_runtime_directory(runtime)
    files: list[Path] = []
    inherited_directories: list[Path] = []
    protected_directories: list[Path] = []
    run_entries = _bounded_runtime_entries(runtime, MAXIMUM_RUNTIME_ENTRIES)
    entry_count = len(run_entries)
    for name, is_directory, _is_file in run_entries:
        run_directory = runtime / name
        if not is_directory:
            validate_machine_path(run_directory, directory=False)
            raise RuntimeError("Der temporäre Dienstzustand enthält einen unbekannten Objekttyp.")
        if _KOSIT_RUNTIME_NAME.fullmatch(name) is None:
            raise RuntimeError("Der temporäre Dienstzustand enthält einen unbekannten Eintrag.")
        if not validate_machine_path(run_directory, directory=True):
            raise RuntimeError("Ein inventarisierter KoSIT-Lauf ist unerwartet verschwunden.")
        acl.verify_runtime_directory(run_directory)
        protected_directories.append(run_directory)
        pending = [(run_directory, 0)]
        while pending:
            parent, depth = pending.pop()
            remaining = MAXIMUM_RUNTIME_ENTRIES - entry_count
            entries = _bounded_runtime_entries(parent, remaining)
            entry_count += len(entries)
            for entry_name, is_directory, is_file in entries:
                candidate = parent / entry_name
                if is_directory:
                    if depth >= MAXIMUM_RUNTIME_DEPTH:
                        raise RuntimeError("Der temporäre Dienstzustand ist unerwartet tief verschachtelt.")
                    validate_machine_path(candidate, directory=True)
                    acl.verify_runtime_entry_for_purge(candidate, directory=True)
                    inherited_directories.append(candidate)
                    pending.append((candidate, depth + 1))
                elif is_file:
                    validate_machine_path(candidate, directory=False)
                    acl.verify_runtime_entry_for_purge(candidate, directory=False)
                    files.append(candidate)
                else:
                    # validate_machine_path supplies the precise reparse-point
                    # diagnostic where applicable; other object types remain
                    # unknown and are never deleted.
                    validate_machine_path(candidate, directory=False)
                    raise RuntimeError("Der temporäre Dienstzustand enthält einen unbekannten Objekttyp.")
    return _RuntimeInventory(
        files=tuple(files),
        inherited_directories=tuple(inherited_directories),
        protected_directories=tuple(protected_directories),
    )


def _delete_runtime_state(
    paths: ServicePaths,
    acl: WindowsServiceAcl,
    inventory: _RuntimeInventory | None,
) -> None:
    if inventory is None:
        return
    try:
        for candidate in inventory.files:
            validate_machine_path(candidate, directory=False)
            acl.verify_runtime_entry_for_purge(candidate, directory=False)
            candidate.unlink()
        for candidate in sorted(
            inventory.inherited_directories,
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            validate_machine_path(candidate, directory=True)
            acl.verify_runtime_entry_for_purge(candidate, directory=True)
            candidate.rmdir()
        for candidate in inventory.protected_directories:
            validate_machine_path(candidate, directory=True)
            acl.verify_runtime_directory(candidate)
            candidate.rmdir()
        validate_machine_path(paths.runtime_directory, directory=True)
        acl.verify_runtime_directory(paths.runtime_directory)
        paths.runtime_directory.rmdir()
    except OSError as exc:
        raise RuntimeError("Der temporäre Dienstzustand konnte nicht vollständig gelöscht werden.") from exc
    if validate_machine_path(paths.runtime_directory, directory=True):
        raise RuntimeError("Der temporäre Dienstzustand wurde nicht vollständig gelöscht.")


def purge_runtime_state(
    *,
    paths: ServicePaths | None = None,
    acl: WindowsServiceAcl | None = None,
    require_stopped: bool = True,
) -> None:
    """Remove only fully inventoried transient KoSIT state."""

    if sys.platform != "win32":
        raise OSError("Der temporäre Dienstzustand kann nur unter Windows gelöscht werden.")
    if require_stopped:
        require_service_stopped_or_absent()
    resolved_paths = paths or ServicePaths.from_environment()
    if not validate_machine_path(resolved_paths.data_directory, directory=True):
        if validate_machine_path(resolved_paths.runtime_directory, directory=True):
            raise RuntimeError("Der temporäre Dienstzustand existiert ohne das geschützte Produktverzeichnis.")
        return
    if not validate_machine_path(resolved_paths.runtime_directory, directory=True):
        return
    resolved_acl = acl or WindowsServiceAcl()
    resolved_acl.repair_explorer_directory_aces(resolved_paths)
    resolved_acl.verify_data_directory(resolved_paths.data_directory)
    inventory = _inventory_runtime_state(resolved_paths, resolved_acl)
    _delete_runtime_state(resolved_paths, resolved_acl, inventory)


def purge_machine_state() -> None:
    """Delete only fully revalidated, product-owned ProgramData state."""

    if sys.platform != "win32":
        raise OSError("Der Maschinenzustand kann nur unter Windows gelöscht werden.")
    require_service_stopped_or_absent()
    paths = ServicePaths.from_environment()
    if not validate_machine_path(paths.data_directory, directory=True):
        for candidate, directory in (
            (paths.configuration, False),
            (paths.token, False),
            (paths.log.parent, True),
            (paths.log, False),
        ):
            if validate_machine_path(candidate, directory=directory):
                raise RuntimeError("Der ProgramData-Zustand ist ohne Produktverzeichnis inkonsistent.")
        return

    data_names = _directory_entry_names(paths.data_directory)
    allowed_names = {
        paths.configuration.name,
        paths.token.name,
        paths.log.parent.name,
        paths.runtime_directory.name,
    }
    temporary_paths: list[tuple[Path, Path]] = []
    observed_temporary_targets: set[Path] = set()
    unknown_names: list[str] = []
    for name in data_names:
        if name in allowed_names:
            continue
        target = _atomic_write_temporary_target(name, paths)
        if target is None or target in observed_temporary_targets:
            unknown_names.append(name)
            continue
        observed_temporary_targets.add(target)
        temporary_paths.append((paths.data_directory / name, target))
    if unknown_names:
        raise RuntimeError(
            f"Der Maschinenpfad {paths.data_directory} enthält unbekannte Einträge und wird nicht automatisch gelöscht."
        )
    acl = WindowsServiceAcl()
    acl.repair_explorer_directory_aces(paths)
    acl.verify_existing_service_paths(paths, include_log_file=False)
    acl.verify_data_directory(paths.data_directory)
    runtime_inventory = _inventory_runtime_state(paths, acl)
    for temporary, target in temporary_paths:
        if not validate_machine_path(temporary, directory=False):
            raise RuntimeError("Eine atomare Dienstdatei ist während der Inventur verschwunden.")
        if target == paths.configuration:
            acl.verify_configuration(temporary)
        else:
            acl.verify_token(temporary)

    log_files: list[Path] = []
    if validate_machine_path(paths.log.parent, directory=True):
        allowed_logs = {paths.log.name} | {
            f"{paths.log.name}.{index}" for index in range(1, SERVICE_LOG_BACKUP_COUNT + 1)
        }
        log_names = _require_only_known_entries(paths.log.parent, allowed_logs)
        for name in log_names:
            candidate = paths.log.parent / name
            if not validate_machine_path(candidate, directory=False):
                raise RuntimeError("Eine inventarisierte Protokolldatei ist unerwartet verschwunden.")
            log_files.append(candidate)

    # RotatingFileHandler may leave the newest file with the exact protected
    # parent ACL inherited. Owner/path checks inside protect_log establish the
    # trust boundary before normalizing and re-verifying every file.
    for log_file in log_files:
        acl.verify_log_for_purge(log_file)
    for log_file in log_files:
        acl.protect_log(log_file)
        acl.verify_log(log_file)

    try:
        _delete_runtime_state(paths, acl, runtime_inventory)
        for temporary, target in temporary_paths:
            if not validate_machine_path(temporary, directory=False):
                raise RuntimeError("Eine atomare Dienstdatei ist vor der Bereinigung verschwunden.")
            if target == paths.configuration:
                acl.verify_configuration(temporary)
            else:
                acl.verify_token(temporary)
            temporary.unlink()
        for log_file in log_files:
            log_file.unlink()
        if validate_machine_path(paths.log.parent, directory=True):
            paths.log.parent.rmdir()
        for candidate in (paths.token, paths.configuration):
            if validate_machine_path(candidate, directory=False):
                candidate.unlink()
        paths.data_directory.rmdir()
    except OSError as exc:
        raise RuntimeError("Der ausgewählte Maschinenzustand konnte nicht vollständig gelöscht werden.") from exc

    for candidate, directory in (
        (paths.data_directory, True),
        (paths.configuration, False),
        (paths.token, False),
        (paths.log.parent, True),
        (paths.runtime_directory, True),
    ):
        if validate_machine_path(candidate, directory=directory):
            raise RuntimeError("Der ausgewählte Maschinenzustand wurde nicht vollständig gelöscht.")


def preflight_loopback_port() -> None:
    state = inspect_machine_state()
    require_service_stopped_or_absent()
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is None:
        raise RuntimeError("Die exklusive Windows-Portreservierung wird von dieser Laufzeit nicht unterstützt.")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        listener.bind(("127.0.0.1", state.configuration.port))
        listener.listen(1)
    except OSError as exc:
        raise RuntimeError(
            f"Der ausschließlich lokale Dienstport {state.configuration.port} ist bereits belegt oder nicht reservierbar."
        ) from exc
    finally:
        listener.close()
