from __future__ import annotations

import ctypes
import hashlib
import json
import os
import secrets
import stat
import struct
import sys
import uuid
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .desktop_security import (
    API_TOKEN_ENV,
    DESKTOP_PORT_ENV,
    DESKTOP_TOKEN_ENV,
    SERVICE_MODE_ENV,
    validate_api_token,
)

SERVICE_NAME = "ERechnungsPrueferService"
SERVICE_DISPLAY_NAME = "E-Rechnungs-Prüfer Dienst"
SERVICE_ACCOUNT = r"NT AUTHORITY\LocalService"
SERVICE_SID_ACCOUNT = rf"NT SERVICE\{SERVICE_NAME}"


def _derive_service_sid(service_name: str) -> str:
    digest = hashlib.sha1(service_name.upper().encode("utf-16-le"), usedforsecurity=False).digest()
    return "S-1-5-80-" + "-".join(str(part) for part in struct.unpack("<5I", digest))


SERVICE_SID = _derive_service_sid(SERVICE_NAME)
SERVICE_DATA_DIRECTORY_NAME = "E-Rechnungs-Pruefer"
CONFIG_FILE_NAME = "service.json"
TOKEN_FILE_NAME = "api-token.txt"
LOG_DIRECTORY_NAME = "logs"
LOG_FILE_NAME = "service.log"
RUNTIME_DIRECTORY_NAME = "runtime"
DEFAULT_SERVICE_PORT = 8080
MAXIMUM_KOSIT_TIMEOUT_SECONDS = 300
SHUTDOWN_GRACE_SECONDS = 15
BASE_TOKEN_PRINCIPALS = (
    "SYSTEM",
    r"BUILTIN\Administrators",
    SERVICE_SID_ACCOUNT,
)

PathProtector = Callable[[Path], None]

_WINDOWS_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_FOLDERID_PROGRAM_DATA = uuid.UUID("62ab5d82-fdc1-4dc3-a9dd-070d1d495d97")
_MOVEFILE_REPLACE_EXISTING = 0x00000001
_MOVEFILE_WRITE_THROUGH = 0x00000008


class _Guid(ctypes.Structure):
    _fields_ = (
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    )


def _windows_program_data_directory() -> Path:
    """Resolve ProgramData through the Windows Known Folder API, not an environment override."""

    if sys.platform != "win32":
        raise OSError("Der Windows-ProgramData-Pfad ist nur unter Windows verfügbar.")
    folder = _Guid(
        _FOLDERID_PROGRAM_DATA.time_low,
        _FOLDERID_PROGRAM_DATA.time_mid,
        _FOLDERID_PROGRAM_DATA.time_hi_version,
        (ctypes.c_ubyte * 8)(*_FOLDERID_PROGRAM_DATA.bytes[8:]),
    )
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    known_folder = shell32.SHGetKnownFolderPath
    known_folder.argtypes = [ctypes.POINTER(_Guid), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    known_folder.restype = ctypes.c_long
    free_memory = ole32.CoTaskMemFree
    free_memory.argtypes = [ctypes.c_void_p]
    free_memory.restype = None
    pointer = ctypes.c_void_p()
    result = known_folder(ctypes.byref(folder), 0, None, ctypes.byref(pointer))
    if result != 0 or not pointer.value:
        raise RuntimeError(f"Der Windows-ProgramData-Pfad konnte nicht sicher bestimmt werden (HRESULT {result}).")
    try:
        value = ctypes.wstring_at(pointer.value)
    finally:
        free_memory(pointer)
    if not value:
        raise RuntimeError("Der Windows-ProgramData-Pfad ist leer.")
    return Path(value)


def validate_machine_path(path: Path, *, directory: bool) -> bool:
    """Reject existing Windows machine paths that can redirect or alias I/O.

    The return value says whether the target itself exists without following it.
    Missing targets are allowed so callers can create them after every existing
    parent has been checked.
    """

    if sys.platform != "win32":
        return path.exists()

    target_stat: os.stat_result | None = None
    candidates = (path, *path.parents)
    for index, candidate in enumerate(candidates):
        try:
            candidate_stat = os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(f"Der Maschinenpfad {candidate} konnte nicht sicher geprüft werden.") from exc
        if stat.S_ISLNK(candidate_stat.st_mode) or (
            getattr(candidate_stat, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT_ATTRIBUTE
        ):
            raise RuntimeError(f"Der Maschinenpfad {candidate} darf kein Reparse-Point oder Junction sein.")
        if index:
            if not stat.S_ISDIR(candidate_stat.st_mode):
                raise RuntimeError(f"Der übergeordnete Maschinenpfad {candidate} ist kein Verzeichnis.")
            continue
        target_stat = candidate_stat

    if target_stat is None:
        return False
    if directory:
        if not stat.S_ISDIR(target_stat.st_mode):
            raise RuntimeError(f"Der Maschinenpfad {path} ist kein Verzeichnis.")
    else:
        if not stat.S_ISREG(target_stat.st_mode):
            raise RuntimeError(f"Der Maschinenpfad {path} ist keine reguläre Datei.")
        if int(getattr(target_stat, "st_nlink", 1)) != 1:
            raise RuntimeError(f"Die Dienstdatei {path} besitzt eine unerwartete Hardlink-Anzahl.")
    return True


@dataclass(frozen=True, slots=True)
class ServicePaths:
    data_directory: Path
    configuration: Path
    token: Path
    log: Path

    @property
    def runtime_directory(self) -> Path:
        return self.data_directory / RUNTIME_DIRECTORY_NAME

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ServicePaths:
        if sys.platform == "win32" and environ is None:
            program_data = _windows_program_data_directory()
        else:
            environment = os.environ if environ is None else environ
            configured = environment.get("PROGRAMDATA")
            if not configured:
                raise RuntimeError("PROGRAMDATA ist für den Windows-Dienst nicht gesetzt.")
            program_data = Path(configured)
        data_directory = program_data / SERVICE_DATA_DIRECTORY_NAME
        return cls(
            data_directory=data_directory,
            configuration=data_directory / CONFIG_FILE_NAME,
            token=data_directory / TOKEN_FILE_NAME,
            log=data_directory / LOG_DIRECTORY_NAME / LOG_FILE_NAME,
        )


@dataclass(frozen=True, slots=True)
class ServiceConfiguration:
    schema_version: int = 1
    port: int = DEFAULT_SERVICE_PORT
    kosit_enabled: bool = True
    kosit_timeout_seconds: int = 60

    @classmethod
    def from_payload(cls, payload: Any) -> ServiceConfiguration:
        if not isinstance(payload, dict):
            raise RuntimeError("Die Dienstkonfiguration muss ein JSON-Objekt sein.")
        expected = {"schema_version", "port", "kosit_enabled", "kosit_timeout_seconds"}
        unknown = set(payload) - expected
        missing = expected - set(payload)
        if unknown:
            raise RuntimeError(f"Unbekannte Konfigurationsfelder: {', '.join(sorted(unknown))}.")
        if missing:
            raise RuntimeError(f"Fehlende Konfigurationsfelder: {', '.join(sorted(missing))}.")

        schema_version = payload["schema_version"]
        port = payload["port"]
        kosit_enabled = payload["kosit_enabled"]
        kosit_timeout_seconds = payload["kosit_timeout_seconds"]
        if schema_version != 1:
            raise RuntimeError("Die Schema-Version der Dienstkonfiguration wird nicht unterstützt.")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise RuntimeError("Der Dienstport muss eine ganze Zahl zwischen 1 und 65535 sein.")
        if not isinstance(kosit_enabled, bool):
            raise RuntimeError("kosit_enabled muss ein boolescher Wert sein.")
        if (
            not isinstance(kosit_timeout_seconds, int)
            or isinstance(kosit_timeout_seconds, bool)
            or not 1 <= kosit_timeout_seconds <= MAXIMUM_KOSIT_TIMEOUT_SECONDS
        ):
            raise RuntimeError(
                f"kosit_timeout_seconds muss eine ganze Zahl zwischen 1 und {MAXIMUM_KOSIT_TIMEOUT_SECONDS} sein."
            )
        return cls(
            schema_version=schema_version,
            port=port,
            kosit_enabled=kosit_enabled,
            kosit_timeout_seconds=kosit_timeout_seconds,
        )


def _private_mode(path: Path) -> None:
    path.chmod(0o600)


def _temporary_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")


def _replace_file_durable(temporary: Path, destination: Path) -> None:
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file = kernel32.MoveFileExW
        move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move_file.restype = ctypes.c_int
        if not move_file(
            str(temporary),
            str(destination),
            _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH,
        ):
            getter = vars(ctypes).get("get_last_error")
            error = int(getter()) if getter is not None else 0
            raise OSError(error, "Die Dienstdatei konnte nicht dauerhaft atomar veröffentlicht werden.")
        return

    os.replace(temporary, destination)
    directory_descriptor = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _atomic_write(path: Path, payload: bytes, *, protect: PathProtector | None) -> None:
    validate_machine_path(path, directory=False)
    validate_machine_path(path.parent, directory=True)
    temporary = _temporary_path(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _private_mode(temporary)
        if protect is not None:
            protect(temporary)
        elif os.name == "nt":
            raise RuntimeError("Für die Dienstdatei wurde keine Windows-DACL konfiguriert.")
        validate_machine_path(temporary, directory=False)
        validate_machine_path(path, directory=False)
        _replace_file_durable(temporary, path)
        validate_machine_path(path, directory=False)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def load_configuration(path: Path) -> ServiceConfiguration:
    validate_machine_path(path, directory=False)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Die Dienstkonfiguration konnte nicht gelesen werden: {exc}") from exc
    return ServiceConfiguration.from_payload(payload)


def load_or_create_configuration(
    path: Path,
    *,
    protect: PathProtector | None = None,
) -> ServiceConfiguration:
    try:
        return load_configuration(path)
    except FileNotFoundError:
        configuration = ServiceConfiguration()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(asdict(configuration), ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()
        _atomic_write(path, payload, protect=protect)
        return configuration


class TokenStore:
    def __init__(
        self,
        path: Path,
        *,
        token_factory: Callable[[], str] | None = None,
        protect_directory: PathProtector | None = None,
        protect_file: PathProtector | None = None,
    ) -> None:
        self.path = path
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._protect_directory = protect_directory
        self._protect_file = protect_file

    def _read(self) -> str:
        validate_machine_path(self.path, directory=False)
        try:
            token = self.path.read_text(encoding="ascii").rstrip("\r\n")
        except FileNotFoundError:
            raise
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(f"Das API-Zugriffstoken konnte nicht gelesen werden: {exc}") from exc
        try:
            return validate_api_token(token, description="Das gespeicherte API-Zugriffstoken")
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    def _write(self, token: str) -> str:
        try:
            validate_api_token(token)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        validate_machine_path(self.path, directory=False)
        validate_machine_path(self.path.parent, directory=True)
        if self._protect_directory is not None:
            self._protect_directory(self.path.parent)
        elif os.name == "nt":
            raise RuntimeError("Für das Dienst-Datenverzeichnis wurde keine Windows-DACL konfiguriert.")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        validate_machine_path(self.path.parent, directory=True)
        _atomic_write(self.path, (token + "\n").encode("ascii"), protect=self._protect_file)
        return token

    def load_or_create(self) -> str:
        try:
            return self._read()
        except FileNotFoundError:
            return self._write(self._token_factory())

    def load(self) -> str:
        return self._read()

    def rotate(self) -> str:
        return self._write(self._token_factory())

    def import_value(self, token: str, *, consent: bool) -> str:
        if not consent:
            raise RuntimeError("Die Tokenübernahme benötigt eine ausdrückliche Zustimmung.")
        return self._write(token)


def activate_service_environment(
    configuration: ServiceConfiguration,
    token: str,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    try:
        validate_api_token(token)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    environment = os.environ if environ is None else environ
    environment.pop(DESKTOP_TOKEN_ENV, None)
    environment[SERVICE_MODE_ENV] = "1"
    environment["HOST"] = "127.0.0.1"
    environment["PORT"] = str(configuration.port)
    environment[DESKTOP_PORT_ENV] = str(configuration.port)
    environment[API_TOKEN_ENV] = token
    environment["KOSIT_ENABLED"] = "true" if configuration.kosit_enabled else "false"
    environment["KOSIT_TIMEOUT_SECONDS"] = str(configuration.kosit_timeout_seconds)


def service_shutdown_timeout(configuration: ServiceConfiguration) -> float:
    return float(configuration.kosit_timeout_seconds + SHUTDOWN_GRACE_SECONDS)
