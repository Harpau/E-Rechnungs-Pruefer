from __future__ import annotations

import hashlib
import json
import ntpath
import os
import secrets
import stat
import string
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Protocol

from . import windows_service_metadata
from .windows_service_config import validate_machine_path

TRANSACTION_SCHEMA_VERSION = 1
PHASE_SCHEMA_VERSION = 1
MAXIMUM_TRANSACTION_BYTES = 64 * 1024
PREPARED_FILE_NAME = "install-transaction.prepared.json"
PHASE_FILE_NAME = "install-transaction.phase.json"
INSTALLER_TRANSACTION_STATE_DIRECTORY_NAME = ".installer-state"
ERROR_ALREADY_EXISTS = 183
_WINDOWS_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class TransactionPhase(StrEnum):
    PREPARED = "prepared"
    SERVICE_ROLLBACK_COMPLETE = "service_rollback_complete"
    COMMIT_STARTED = "commit_started"


class ServiceState(StrEnum):
    ABSENT = "absent"
    OWNED_STOPPED = "owned_stopped"
    OWNED_RUNNING = "owned_running"
    FOREIGN = "foreign"
    UNSTABLE = "unstable"


class RecoveryDirection(StrEnum):
    ROLLBACK = "rollback"
    FORWARD = "forward"
    COMPLETE = "complete"


class RecoveryAction(StrEnum):
    STOP_SERVICE = "stop_service"
    DELETE_SERVICE = "delete_service"
    DELETE_LIVE = "delete_live"
    DELETE_NEW = "delete_new"
    MOVE_ROLLBACK_TO_LIVE = "move_rollback_to_live"
    MOVE_OBSOLETE_TO_LIVE = "move_obsolete_to_live"
    RESTORE_SERVICE_METADATA = "restore_service_metadata"
    START_SERVICE = "start_service"
    PURGE_MACHINE_STATE = "purge_machine_state"
    DELETE_OBSOLETE = "delete_obsolete"


@dataclass(frozen=True, slots=True)
class MachineBefore:
    configuration: bool
    token: bool
    logs: bool

    @property
    def any_existed(self) -> bool:
        return self.configuration or self.token or self.logs


@dataclass(frozen=True, slots=True)
class PreparedTransaction:
    transaction_id: str
    desktop_reader_sid: str
    desktop_seal_sha256: str
    expected_executable: str
    service_existed: bool
    service_running: bool
    service_metadata: Mapping[str, object] | None
    machine_before: MachineBefore
    target_service_running: bool
    token_transfer_consent: bool


@dataclass(frozen=True, slots=True)
class PartialPreparedState:
    prepared: PreparedTransaction | None


@dataclass(frozen=True, slots=True)
class TransactionState:
    prepared: PreparedTransaction
    phase: TransactionPhase


@dataclass(frozen=True, slots=True)
class OrphanedCompletionMarker:
    transaction_id: str
    phase: TransactionPhase
    prepared_sha256: str


@dataclass(frozen=True, slots=True)
class BundleTopology:
    live: bool
    new: bool
    rollback: bool
    obsolete: bool


@dataclass(frozen=True, slots=True)
class RecoveryObservation:
    bundles: BundleTopology
    service: ServiceState
    incomplete_bundles: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    transaction_id: str
    direction: RecoveryDirection
    observation: RecoveryObservation
    actions: tuple[RecoveryAction, ...]


@dataclass(frozen=True, slots=True)
class BundlePaths:
    live: Path
    new: Path
    rollback: Path
    obsolete: Path
    executable_name: str


class TransactionStore(Protocol):
    def read(self, name: str) -> bytes | None: ...

    def read_partial_prepared(self) -> PartialPreparedState | None: ...

    def create(self, name: str, payload: bytes) -> None: ...

    def delete(self, name: str) -> None: ...

    def delete_partial_prepared(self) -> None: ...

    def remove_directory_if_empty(self) -> None: ...


class RecoveryOperations(Protocol):
    def observe(self) -> RecoveryObservation: ...

    def stop_service(self) -> None: ...

    def delete_service(self) -> None: ...

    def delete_bundle(self, slot: str) -> None: ...

    def move_bundle(self, source: str, destination: str) -> None: ...

    def restore_service_metadata(self, payload: Mapping[str, object]) -> None: ...

    def start_service(self) -> None: ...

    def purge_machine_state(self) -> None: ...


def _strict_transaction_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(character not in string.hexdigits for character in value)
        or value != value.lower()
    ):
        raise RuntimeError("Die Installations-Transaktions-ID ist ungültig.")
    return value


def _strict_desktop_reader_sid(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 4 < len(value) <= 184
        or not value.startswith("S-1-")
        or value in {"S-1-5-18", "S-1-5-32-544"}
        or "\x00" in value
    ):
        raise RuntimeError("Die gebundene Desktop-Benutzeridentität ist ungültig.")
    return value


def _strict_sha256(value: object, *, description: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in string.hexdigits for character in value)
        or value != value.lower()
    ):
        raise RuntimeError(f"{description} ist ungültig.")
    return value


def _canonical_expected_executable(path: Path | str) -> str:
    value = str(path)
    pure_path = PureWindowsPath(value)
    if (
        not value
        or "\x00" in value
        or '"' in value
        or not pure_path.is_absolute()
        or value != ntpath.normpath(value)
        or pure_path.suffix.casefold() != ".exe"
        or pure_path.parent.name.casefold() != "service"
    ):
        raise RuntimeError("Der erwartete Dienstpfad ist nicht absolut, kanonisch und fest im Live-Bundle.")
    return value


def _transaction_directories(expected_executable: Path) -> tuple[Path, Path]:
    expected = _canonical_expected_executable(expected_executable)
    installation_directory = Path(ntpath.dirname(ntpath.dirname(expected)))
    return (
        installation_directory,
        installation_directory / INSTALLER_TRANSACTION_STATE_DIRECTORY_NAME,
    )


def bundle_paths(expected_executable: Path) -> BundlePaths:
    """Resolve only the four fixed sibling bundle slots for an expected live EXE."""

    expected = _canonical_expected_executable(expected_executable)
    live = Path(ntpath.dirname(expected))
    installation_directory = Path(ntpath.dirname(str(live)))
    return BundlePaths(
        live=live,
        new=installation_directory / "service.new",
        rollback=installation_directory / "service.rollback",
        obsolete=installation_directory / "service.obsolete",
        executable_name=ntpath.basename(expected),
    )


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _validate_bundle_tree(path: Path, *, executable_name: str, require_executable: bool) -> bool:
    if not validate_machine_path(path, directory=True):
        if _path_lexists(path):
            raise RuntimeError(f"Der Bundlepfad {path} ist kein sicherer Produktordner.")
        return False
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            entries = tuple(os.scandir(current))
        except OSError as exc:
            raise RuntimeError(f"Der Bundlepfad {current} konnte nicht vollständig inventarisiert werden.") from exc
        for entry in entries:
            candidate = Path(entry.path)
            try:
                candidate_stat = os.lstat(candidate)
            except OSError as exc:
                raise RuntimeError(f"Der Bundleeintrag {candidate} konnte nicht sicher geprüft werden.") from exc
            if stat.S_ISLNK(candidate_stat.st_mode) or (
                getattr(candidate_stat, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT_ATTRIBUTE
            ):
                raise RuntimeError(f"Der Bundleeintrag {candidate} darf kein Reparse-Point oder Junction sein.")
            if stat.S_ISDIR(candidate_stat.st_mode):
                pending.append(candidate)
            elif not stat.S_ISREG(candidate_stat.st_mode) or int(getattr(candidate_stat, "st_nlink", 1)) != 1:
                raise RuntimeError(f"Der Bundleeintrag {candidate} ist keine eindeutige reguläre Datei.")
    executable = path / executable_name
    if require_executable and not validate_machine_path(executable, directory=False):
        raise RuntimeError(f"Der Bundlepfad {path} enthält nicht die erwartete Dienst-EXE.")
    return True


def inspect_bundle_topology(expected_executable: Path) -> BundleTopology:
    """Inventory every fixed slot without following redirects or accepting partial backups."""

    paths = bundle_paths(expected_executable)
    return BundleTopology(
        live=_validate_bundle_tree(
            paths.live,
            executable_name=paths.executable_name,
            require_executable=True,
        ),
        new=_validate_bundle_tree(
            paths.new,
            executable_name=paths.executable_name,
            require_executable=False,
        ),
        rollback=_validate_bundle_tree(
            paths.rollback,
            executable_name=paths.executable_name,
            require_executable=True,
        ),
        obsolete=_validate_bundle_tree(
            paths.obsolete,
            executable_name=paths.executable_name,
            require_executable=True,
        ),
    )


def inspect_recovery_observation(expected_executable: Path) -> RecoveryObservation:
    """Read a stable product-owned SCM/bundle observation without mutating either."""

    first_bundles = inspect_bundle_topology(expected_executable)
    owned_service = windows_service_metadata.inspect_owned_service_metadata(expected_executable)
    service = (
        ServiceState.ABSENT
        if owned_service is None
        else ServiceState.OWNED_RUNNING
        if owned_service[1]
        else ServiceState.OWNED_STOPPED
    )
    second_bundles = inspect_bundle_topology(expected_executable)
    if first_bundles != second_bundles:
        raise RuntimeError("Die Dienst-Bundles haben sich während der Recovery-Inventur geändert.")
    return RecoveryObservation(bundles=second_bundles, service=service)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Doppeltes JSON-Feld: {key}")
        result[key] = value
    return result


def _canonical_json(payload: object) -> bytes:
    try:
        encoded = (json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Der Installations-Transaktionsbeleg ist nicht als striktes JSON darstellbar.") from exc
    if not encoded or len(encoded) > MAXIMUM_TRANSACTION_BYTES:
        raise RuntimeError("Der Installations-Transaktionsbeleg hat eine unzulässige Größe.")
    return encoded


def _decode_canonical_json(encoded: bytes) -> dict[str, object]:
    if not encoded or len(encoded) > MAXIMUM_TRANSACTION_BYTES:
        raise RuntimeError("Der Installations-Transaktionsbeleg hat eine unzulässige Größe.")
    try:
        payload = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Der Installations-Transaktionsbeleg ist kein gültiges JSON-Dokument.") from exc
    if not isinstance(payload, dict) or _canonical_json(payload) != encoded:
        raise RuntimeError("Der Installations-Transaktionsbeleg ist nicht kanonisch.")
    return payload


def _strict_boolean(value: object, *, name: str) -> bool:
    if type(value) is not bool:
        raise RuntimeError(f"Das boolesche Transaktionsfeld {name!r} ist ungültig.")
    return value


def _prepared_payload(prepared: PreparedTransaction) -> dict[str, object]:
    return {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "transaction_id": prepared.transaction_id,
        "desktop_binding": {
            "reader_sid": prepared.desktop_reader_sid,
            "seal_sha256": prepared.desktop_seal_sha256,
        },
        "expected_executable": prepared.expected_executable,
        "service_before": {
            "existed": prepared.service_existed,
            "running": prepared.service_running,
            "metadata": dict(prepared.service_metadata) if prepared.service_metadata is not None else None,
        },
        "machine_before": {
            "configuration": prepared.machine_before.configuration,
            "token": prepared.machine_before.token,
            "logs": prepared.machine_before.logs,
        },
        "target": {
            "service_running": prepared.target_service_running,
            "token_transfer_consent": prepared.token_transfer_consent,
        },
    }


def _decode_prepared(encoded: bytes, expected_executable: Path) -> PreparedTransaction:
    payload = _decode_canonical_json(encoded)
    if set(payload) != {
        "schema_version",
        "transaction_id",
        "desktop_binding",
        "expected_executable",
        "service_before",
        "machine_before",
        "target",
    }:
        raise RuntimeError("Das PREPARED-Manifest hat ein unbekanntes Format.")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != TRANSACTION_SCHEMA_VERSION:
        raise RuntimeError("Die Version des PREPARED-Manifests wird nicht unterstützt.")
    transaction_id = _strict_transaction_id(payload["transaction_id"])
    desktop_binding = payload["desktop_binding"]
    if not isinstance(desktop_binding, dict) or set(desktop_binding) != {
        "reader_sid",
        "seal_sha256",
    }:
        raise RuntimeError("Die Desktopbindung im PREPARED-Manifest ist ungültig.")
    desktop_reader_sid = _strict_desktop_reader_sid(desktop_binding["reader_sid"])
    desktop_seal_sha256 = _strict_sha256(
        desktop_binding["seal_sha256"],
        description="Der Hash des gebundenen Desktop-Seals",
    )
    expected = _canonical_expected_executable(expected_executable)
    recorded_expected = payload["expected_executable"]
    if not isinstance(recorded_expected, str) or recorded_expected.casefold() != expected.casefold():
        raise RuntimeError("Das PREPARED-Manifest gehört nicht zum erwarteten Dienstpfad.")

    service_before = payload["service_before"]
    machine_before = payload["machine_before"]
    target = payload["target"]
    if not isinstance(service_before, dict) or set(service_before) != {"existed", "running", "metadata"}:
        raise RuntimeError("Der Dienst-Baselineblock im PREPARED-Manifest ist ungültig.")
    if not isinstance(machine_before, dict) or set(machine_before) != {"configuration", "token", "logs"}:
        raise RuntimeError("Der Maschinen-Baselineblock im PREPARED-Manifest ist ungültig.")
    if not isinstance(target, dict) or set(target) != {"service_running", "token_transfer_consent"}:
        raise RuntimeError("Der Zielzustandsblock im PREPARED-Manifest ist ungültig.")

    service_existed = _strict_boolean(service_before["existed"], name="service_before.existed")
    service_running = _strict_boolean(service_before["running"], name="service_before.running")
    raw_metadata = service_before["metadata"]
    if service_existed:
        if not isinstance(raw_metadata, dict):
            raise RuntimeError("Für ein Update fehlt die vollständige SCM-Baseline.")
        service_metadata: Mapping[str, object] | None = windows_service_metadata.validate_service_metadata(
            expected_executable,
            raw_metadata,
        )
    else:
        if service_running or raw_metadata is not None:
            raise RuntimeError("Eine Erstinstallation darf keine vorhandene Dienst-Baseline behaupten.")
        service_metadata = None

    before = MachineBefore(
        configuration=_strict_boolean(machine_before["configuration"], name="machine_before.configuration"),
        token=_strict_boolean(machine_before["token"], name="machine_before.token"),
        logs=_strict_boolean(machine_before["logs"], name="machine_before.logs"),
    )
    target_running = _strict_boolean(target["service_running"], name="target.service_running")
    token_consent = _strict_boolean(target["token_transfer_consent"], name="target.token_transfer_consent")
    if service_existed and token_consent:
        raise RuntimeError("Eine Tokenübernahme ist in einer Update-Transaktion unzulässig.")
    if token_consent and before.token:
        raise RuntimeError("Eine Tokenübernahme darf kein bereits vorhandenes Maschinentoken überschreiben.")
    return PreparedTransaction(
        transaction_id=transaction_id,
        desktop_reader_sid=desktop_reader_sid,
        desktop_seal_sha256=desktop_seal_sha256,
        expected_executable=expected,
        service_existed=service_existed,
        service_running=service_running,
        service_metadata=service_metadata,
        machine_before=before,
        target_service_running=target_running,
        token_transfer_consent=token_consent,
    )


def _prepared_digest(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def _phase_payload(
    transaction_id: str,
    phase: TransactionPhase,
    prepared_encoded: bytes,
) -> dict[str, object]:
    return {
        "schema_version": PHASE_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "phase": phase.value,
        "prepared_sha256": _prepared_digest(prepared_encoded),
    }


def _decode_phase(encoded: bytes, prepared_encoded: bytes, transaction_id: str) -> TransactionPhase:
    payload = _decode_canonical_json(encoded)
    if set(payload) != {"schema_version", "transaction_id", "phase", "prepared_sha256"}:
        raise RuntimeError("Der atomare Transaktionsmarker hat ein unbekanntes Format.")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != PHASE_SCHEMA_VERSION:
        raise RuntimeError("Die Version des atomaren Transaktionsmarkers wird nicht unterstützt.")
    if _strict_transaction_id(payload["transaction_id"]) != transaction_id:
        raise RuntimeError("Der atomare Transaktionsmarker gehört zu einer anderen Transaktion.")
    recorded_digest = payload["prepared_sha256"]
    if (
        not isinstance(recorded_digest, str)
        or len(recorded_digest) != 64
        or any(character not in string.hexdigits for character in recorded_digest)
        or recorded_digest != recorded_digest.lower()
        or recorded_digest != _prepared_digest(prepared_encoded)
    ):
        raise RuntimeError("Der atomare Transaktionsmarker ist nicht an das PREPARED-Manifest gebunden.")
    raw_phase = payload["phase"]
    if not isinstance(raw_phase, str):
        raise RuntimeError("Der atomare Transaktionsmarker enthält eine unbekannte Phase.")
    try:
        phase = TransactionPhase(raw_phase)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Der atomare Transaktionsmarker enthält eine unbekannte Phase.") from exc
    if phase is TransactionPhase.PREPARED:
        raise RuntimeError("Die PREPARED-Phase darf nicht als separater Marker gespeichert werden.")
    return phase


class _ProtectedTransactionStore:
    def __init__(self, expected_executable: Path, *, create: bool) -> None:
        installation_directory, state_directory = _transaction_directories(expected_executable)
        self._expected_executable = expected_executable
        self._installation_directory_present = validate_machine_path(
            installation_directory,
            directory=True,
        )
        self._state_directory = state_directory
        if not self._installation_directory_present:
            if os.path.lexists(installation_directory):
                raise RuntimeError("Das Installationsverzeichnis für den Transaktionsbeleg ist unsicher.")
            if create:
                raise RuntimeError("Das Installationsverzeichnis für den Transaktionsbeleg fehlt.")
            return
        if create:
            self._prepare_directory()
        elif validate_machine_path(state_directory, directory=True):
            windows_service_metadata._verify_administrative_path(state_directory, directory=True)
            self._validate_inventory()
        elif os.path.lexists(state_directory):
            raise RuntimeError("Die administrative Transaktionsablage ist kein sicherer lokaler Ordner.")

    def _prepare_directory(self) -> None:
        if validate_machine_path(self._state_directory, directory=True):
            windows_service_metadata._verify_administrative_path(self._state_directory, directory=True)
            return
        _pywintypes, _win32con, win32file, _win32security, _ntsecuritycon = (
            windows_service_metadata._windows_file_modules()
        )
        try:
            win32file.CreateDirectoryW(
                str(self._state_directory),
                windows_service_metadata._administrative_security_attributes(directory=True),
            )
        except Exception as exc:
            if getattr(exc, "winerror", None) != ERROR_ALREADY_EXISTS:
                raise RuntimeError(
                    "Die administrative Transaktionsablage konnte nicht sicher erstellt werden."
                ) from exc
        windows_service_metadata._verify_administrative_path(self._state_directory, directory=True)

    def _path(self, name: str) -> Path:
        if name not in {PREPARED_FILE_NAME, PHASE_FILE_NAME}:
            raise RuntimeError("Ein unbekannter Transaktionsdateiname wurde angefordert.")
        return self._state_directory / name

    @staticmethod
    def _temporary_target(name: str) -> str | None:
        for target in (PREPARED_FILE_NAME, PHASE_FILE_NAME):
            prefix = f".{target}."
            suffix = name[len(prefix) :] if name.startswith(prefix) and name.endswith(".tmp") else ""
            nonce = suffix[:-4] if suffix.endswith(".tmp") else ""
            if len(nonce) == 32 and all(character in "0123456789abcdef" for character in nonce):
                return target
        return None

    def _validated_payload(self, path: Path) -> bytes:
        windows_service_metadata._verify_administrative_path(path, directory=False)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise RuntimeError("Ein temporärer Transaktionsbeleg konnte nicht gelesen werden.") from exc
        windows_service_metadata._verify_administrative_path(path, directory=False)
        return payload

    def _validate_inventory(self) -> tuple[Path, ...]:
        try:
            entries = tuple(self._state_directory.iterdir())
        except OSError as exc:
            raise RuntimeError("Die administrative Transaktionsablage konnte nicht inventarisiert werden.") from exc
        prepared_path = self._path(PREPARED_FILE_NAME)
        phase_path = self._path(PHASE_FILE_NAME)
        prepared_encoded = (
            self._validated_payload(prepared_path) if validate_machine_path(prepared_path, directory=False) else None
        )
        prepared = (
            _decode_prepared(prepared_encoded, self._expected_executable) if prepared_encoded is not None else None
        )
        phase_exists = validate_machine_path(phase_path, directory=False)
        temporary_entries: list[tuple[Path, str, bytes]] = []
        for entry in entries:
            if entry.name in {PREPARED_FILE_NAME, PHASE_FILE_NAME}:
                if not validate_machine_path(entry, directory=False):
                    raise RuntimeError("Ein fester Transaktionsbeleg ist kein sicherer lokaler Dateipfad.")
                continue
            target = self._temporary_target(entry.name)
            if target is None or not validate_machine_path(entry, directory=False):
                raise RuntimeError("Die administrative Transaktionsablage enthält einen unbekannten Eintrag.")
            payload = self._validated_payload(entry)
            if len(payload) > MAXIMUM_TRANSACTION_BYTES:
                raise RuntimeError("Ein temporärer Transaktionsbeleg hat eine unzulässige Größe.")
            temporary_entries.append((entry, target, payload))

        if prepared_encoded is None:
            if phase_exists:
                if temporary_entries:
                    raise RuntimeError(
                        "Ein verwaister Abschlussmarker darf keinen temporären Transaktionsbeleg begleiten."
                    )
                return ()
            if not temporary_entries:
                return ()
            if len(temporary_entries) != 1 or temporary_entries[0][1] != PREPARED_FILE_NAME:
                raise RuntimeError(
                    "Eine partielle PREPARED-Ablage muss genau ein geschütztes temporäres Manifest enthalten."
                )
            return (temporary_entries[0][0],)

        temporary_paths: list[Path] = []
        assert prepared is not None
        authoritative_phase = None
        if phase_exists:
            phase_encoded = self._validated_payload(phase_path)
            authoritative_phase = _decode_phase(
                phase_encoded,
                prepared_encoded,
                prepared.transaction_id,
            )
        for entry, target, payload in temporary_entries:
            if target == PREPARED_FILE_NAME:
                _decode_prepared(payload, self._expected_executable)
                if payload != prepared_encoded:
                    raise RuntimeError("Ein temporäres PREPARED-Manifest widerspricht dem autoritativen Manifest.")
            else:
                try:
                    temporary_phase = _decode_phase(payload, prepared_encoded, prepared.transaction_id)
                except RuntimeError:
                    try:
                        _decode_canonical_json(payload)
                    except RuntimeError:
                        temporary_phase = None
                    else:
                        raise
                if authoritative_phase is not None and temporary_phase not in {
                    None,
                    authoritative_phase,
                }:
                    raise RuntimeError("Eine temporäre Abschlussphase widerspricht der autoritativen Phase.")
            temporary_paths.append(entry)
        return tuple(temporary_paths)

    def _remove_validated_temporaries(self) -> None:
        for temporary in self._validate_inventory():
            windows_service_metadata._verify_administrative_path(temporary, directory=False)
            try:
                temporary.unlink()
            except OSError as exc:
                raise RuntimeError("Ein temporärer Transaktionsbeleg konnte nicht gelöscht werden.") from exc

    def _partial_prepared_temporary(
        self,
    ) -> tuple[Path | None, PartialPreparedState] | None:
        if not self._installation_directory_present:
            return None
        if not validate_machine_path(self._state_directory, directory=True):
            if os.path.lexists(self._state_directory):
                raise RuntimeError("Die administrative Transaktionsablage ist kein sicherer lokaler Ordner.")
            return None
        windows_service_metadata._verify_administrative_path(self._state_directory, directory=True)
        temporary_paths = self._validate_inventory()
        prepared_exists = validate_machine_path(self._path(PREPARED_FILE_NAME), directory=False)
        phase_exists = validate_machine_path(self._path(PHASE_FILE_NAME), directory=False)
        if prepared_exists:
            return None
        if phase_exists:
            if temporary_paths:
                raise RuntimeError(
                    "Ein verwaister Abschlussmarker darf nicht mit einem temporären PREPARED-Manifest "
                    "kombiniert werden."
                )
            return None
        if not temporary_paths:
            return None, PartialPreparedState(prepared=None)
        if len(temporary_paths) != 1 or self._temporary_target(temporary_paths[0].name) != PREPARED_FILE_NAME:
            raise RuntimeError(
                "Eine partielle PREPARED-Ablage muss genau ein geschütztes temporäres Manifest enthalten."
            )
        temporary = temporary_paths[0]
        encoded = self._validated_payload(temporary)
        try:
            prepared = _decode_prepared(encoded, self._expected_executable)
        except RuntimeError:
            prepared = None
        return temporary, PartialPreparedState(prepared=prepared)

    def read(self, name: str) -> bytes | None:
        path = self._path(name)
        if not self._installation_directory_present:
            return None
        if not validate_machine_path(self._state_directory, directory=True):
            if os.path.lexists(self._state_directory):
                raise RuntimeError("Die administrative Transaktionsablage ist kein sicherer lokaler Ordner.")
            return None
        windows_service_metadata._verify_administrative_path(self._state_directory, directory=True)
        self._validate_inventory()
        if not validate_machine_path(path, directory=False):
            return None
        windows_service_metadata._verify_administrative_path(path, directory=False)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise RuntimeError("Der geschützte Installations-Transaktionsbeleg konnte nicht gelesen werden.") from exc
        windows_service_metadata._verify_administrative_path(path, directory=False)
        return payload

    def read_partial_prepared(self) -> PartialPreparedState | None:
        partial = self._partial_prepared_temporary()
        return None if partial is None else partial[1]

    def create(self, name: str, payload: bytes) -> None:
        destination = self._path(name)
        windows_service_metadata._verify_administrative_path(self._state_directory, directory=True)
        if name in {PREPARED_FILE_NAME, PHASE_FILE_NAME}:
            self._remove_validated_temporaries()
        if validate_machine_path(destination, directory=False):
            raise FileExistsError(destination)
        temporary = self._state_directory / f".{name}.{secrets.token_hex(16)}.tmp"
        try:
            windows_service_metadata._write_secure_snapshot(temporary, payload)
            _atomic_publish(temporary, destination)
            windows_service_metadata._verify_administrative_path(destination, directory=False)
        finally:
            try:
                if validate_machine_path(temporary, directory=False):
                    temporary.unlink()
            except (OSError, RuntimeError):
                pass

    def delete(self, name: str) -> None:
        path = self._path(name)
        windows_service_metadata._verify_administrative_path(self._state_directory, directory=True)
        if name == PREPARED_FILE_NAME:
            self._remove_validated_temporaries()
        if not validate_machine_path(path, directory=False):
            return
        windows_service_metadata._verify_administrative_path(path, directory=False)
        try:
            path.unlink()
        except OSError as exc:
            raise RuntimeError("Der geschützte Installations-Transaktionsbeleg konnte nicht gelöscht werden.") from exc

    def delete_partial_prepared(self) -> None:
        partial = self._partial_prepared_temporary()
        if partial is None:
            return
        temporary, _state = partial
        if temporary is None:
            return
        windows_service_metadata._verify_administrative_path(temporary, directory=False)
        try:
            temporary.unlink()
        except OSError as exc:
            raise RuntimeError("Das partielle PREPARED-Manifest konnte nicht gelöscht werden.") from exc

    def remove_directory_if_empty(self) -> None:
        if not validate_machine_path(self._state_directory, directory=True):
            return
        windows_service_metadata._verify_administrative_path(self._state_directory, directory=True)
        try:
            self._state_directory.rmdir()
        except OSError:
            return


def _atomic_publish(temporary: Path, destination: Path) -> None:
    if sys.platform == "win32":
        _pywintypes, win32con, win32file, _win32security, _ntsecuritycon = (
            windows_service_metadata._windows_file_modules()
        )
        try:
            win32file.MoveFileEx(
                str(temporary),
                str(destination),
                getattr(win32con, "MOVEFILE_WRITE_THROUGH", 0x00000008),
            )
        except Exception as exc:
            if getattr(exc, "winerror", None) in {ERROR_ALREADY_EXISTS, 80}:
                raise FileExistsError(destination) from exc
            raise RuntimeError("Der Transaktionsbeleg konnte nicht atomar veröffentlicht werden.") from exc
        return
    try:
        os.link(temporary, destination)
        temporary.unlink()
    except FileExistsError:
        raise
    except OSError as exc:
        raise RuntimeError("Der Transaktionsbeleg konnte nicht atomar veröffentlicht werden.") from exc


def _store(expected_executable: Path, *, create: bool) -> TransactionStore:
    return _ProtectedTransactionStore(expected_executable, create=create)


def prepare_transaction(
    expected_executable: Path,
    *,
    transaction_id: str,
    desktop_reader_sid: str,
    desktop_seal_sha256: str,
    service_existed: bool,
    service_running: bool,
    machine_before: MachineBefore,
    target_service_running: bool,
    token_transfer_consent: bool,
    _state_store: TransactionStore | None = None,
) -> PreparedTransaction:
    """Persist an immutable, transaction-bound baseline before the first SCM mutation."""

    normalized_id = _strict_transaction_id(transaction_id)
    normalized_reader_sid = _strict_desktop_reader_sid(desktop_reader_sid)
    normalized_seal_sha256 = _strict_sha256(
        desktop_seal_sha256,
        description="Der Hash des gebundenen Desktop-Seals",
    )
    expected = _canonical_expected_executable(expected_executable)
    if type(service_existed) is not bool or type(service_running) is not bool:
        raise RuntimeError("Der vorhandene Dienstzustand muss strikt boolesch angegeben werden.")
    if type(target_service_running) is not bool or type(token_transfer_consent) is not bool:
        raise RuntimeError("Der Zielzustand muss strikt boolesch angegeben werden.")
    if not service_existed and service_running:
        raise RuntimeError("Ein nicht vorhandener Dienst kann vor der Installation nicht laufen.")
    if service_existed and token_transfer_consent:
        raise RuntimeError("Eine Tokenübernahme ist in einer Update-Transaktion unzulässig.")
    if token_transfer_consent and machine_before.token:
        raise RuntimeError("Eine Tokenübernahme darf kein bereits vorhandenes Maschinentoken überschreiben.")
    metadata = windows_service_metadata.capture_service_metadata(expected_executable) if service_existed else None
    prepared = PreparedTransaction(
        transaction_id=normalized_id,
        desktop_reader_sid=normalized_reader_sid,
        desktop_seal_sha256=normalized_seal_sha256,
        expected_executable=expected,
        service_existed=service_existed,
        service_running=service_running,
        service_metadata=metadata,
        machine_before=machine_before,
        target_service_running=target_service_running,
        token_transfer_consent=token_transfer_consent,
    )
    encoded = _canonical_json(_prepared_payload(prepared))
    # Decode before writing so every invariant applied to recovery also applies to creation.
    prepared = _decode_prepared(encoded, expected_executable)
    state_store = _state_store or _store(expected_executable, create=True)
    existing = state_store.read(PREPARED_FILE_NAME)
    if existing is not None:
        if existing != encoded:
            raise RuntimeError("Ein abweichendes PREPARED-Manifest einer früheren Transaktion ist vorhanden.")
        if state_store.read(PHASE_FILE_NAME) is not None:
            raise RuntimeError("Die angeforderte Transaktion hat bereits eine persistente Abschlussphase.")
        return prepared
    try:
        state_store.create(PREPARED_FILE_NAME, encoded)
    except FileExistsError:
        existing = state_store.read(PREPARED_FILE_NAME)
        if existing != encoded:
            raise RuntimeError("Ein konkurrierendes PREPARED-Manifest wurde veröffentlicht.") from None
    if state_store.read(PREPARED_FILE_NAME) != encoded:
        raise RuntimeError("Das PREPARED-Manifest konnte nicht unverändert zurückgelesen werden.")
    return prepared


def load_transaction(
    expected_executable: Path,
    *,
    transaction_id: str | None = None,
    _state_store: TransactionStore | None = None,
) -> TransactionState | None:
    state_store = _state_store or _store(expected_executable, create=False)
    prepared_encoded = state_store.read(PREPARED_FILE_NAME)
    phase_encoded = state_store.read(PHASE_FILE_NAME)
    if prepared_encoded is None:
        if phase_encoded is not None:
            raise RuntimeError("Ein verwaister Abschlussmarker ohne PREPARED-Manifest ist vorhanden.")
        return None
    prepared = _decode_prepared(prepared_encoded, expected_executable)
    if transaction_id is not None and prepared.transaction_id != _strict_transaction_id(transaction_id):
        raise RuntimeError("Das PREPARED-Manifest gehört zu einer anderen Installations-Transaktion.")
    phase = (
        TransactionPhase.PREPARED
        if phase_encoded is None
        else _decode_phase(phase_encoded, prepared_encoded, prepared.transaction_id)
    )
    return TransactionState(prepared=prepared, phase=phase)


def load_partial_prepared_transaction(
    expected_executable: Path,
    *,
    _state_store: TransactionStore | None = None,
) -> PartialPreparedState | None:
    """Recognize an empty or isolated PREPARED publish tail without changing it."""

    state_store = _state_store or _store(expected_executable, create=False)
    return state_store.read_partial_prepared()


def clear_partial_prepared_transaction(
    expected_executable: Path,
    *,
    _state_store: TransactionStore | None = None,
) -> None:
    """Delete only an isolated, fully validated PREPARED publish tail."""

    state_store = _state_store or _store(expected_executable, create=False)
    partial = state_store.read_partial_prepared()
    if partial is None:
        return
    state_store.delete_partial_prepared()
    state_store.remove_directory_if_empty()


def _decode_orphaned_completion_marker(encoded: bytes) -> OrphanedCompletionMarker:
    payload = _decode_canonical_json(encoded)
    if set(payload) != {"schema_version", "transaction_id", "phase", "prepared_sha256"}:
        raise RuntimeError("Der verwaiste Abschlussmarker hat ein unbekanntes Format.")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != PHASE_SCHEMA_VERSION:
        raise RuntimeError("Die Version des verwaisten Abschlussmarkers wird nicht unterstützt.")
    transaction_id = _strict_transaction_id(payload["transaction_id"])
    raw_phase = payload["phase"]
    if not isinstance(raw_phase, str):
        raise RuntimeError("Der verwaiste Abschlussmarker enthält keine terminale Phase.")
    try:
        phase = TransactionPhase(raw_phase)
    except ValueError as exc:
        raise RuntimeError("Der verwaiste Abschlussmarker enthält keine terminale Phase.") from exc
    digest = _strict_sha256(
        payload["prepared_sha256"],
        description="Der Manifest-Hash des verwaisten Abschlussmarkers",
    )
    if phase is TransactionPhase.PREPARED:
        raise RuntimeError("Der verwaiste Abschlussmarker enthält keine terminale Phase.")
    return OrphanedCompletionMarker(
        transaction_id=transaction_id,
        phase=phase,
        prepared_sha256=digest,
    )


def load_orphaned_completion_marker(
    expected_executable: Path,
    *,
    _state_store: TransactionStore | None = None,
) -> OrphanedCompletionMarker | None:
    """Return a self-describing terminal marker left by the final delete tail."""

    state_store = _state_store or _store(expected_executable, create=False)
    if state_store.read(PREPARED_FILE_NAME) is not None:
        return None
    encoded = state_store.read(PHASE_FILE_NAME)
    if encoded is None:
        return None
    return _decode_orphaned_completion_marker(encoded)


def clear_orphaned_completion_marker(
    expected_executable: Path,
    *,
    transaction_id: str | None = None,
    _state_store: TransactionStore | None = None,
) -> None:
    """Finish the harmless finalization tail left after the manifest was consumed."""

    state_store = _state_store or _store(expected_executable, create=False)
    if state_store.read(PREPARED_FILE_NAME) is not None:
        raise RuntimeError("Ein Abschlussmarker darf nicht getrennt von einem vorhandenen Manifest bereinigt werden.")
    marker = load_orphaned_completion_marker(
        expected_executable,
        _state_store=state_store,
    )
    if marker is None:
        return
    if transaction_id is not None and marker.transaction_id != _strict_transaction_id(transaction_id):
        raise RuntimeError("Der verwaiste Abschlussmarker gehört zu einer anderen Transaktion.")
    state_store.delete(PHASE_FILE_NAME)
    state_store.remove_directory_if_empty()


def _write_phase(
    expected_executable: Path,
    *,
    transaction_id: str,
    phase: TransactionPhase,
    _state_store: TransactionStore | None = None,
) -> None:
    if phase is TransactionPhase.PREPARED:
        raise RuntimeError("Für PREPARED wird kein separater Phasenmarker geschrieben.")
    state_store = _state_store or _store(expected_executable, create=False)
    prepared_encoded = state_store.read(PREPARED_FILE_NAME)
    if prepared_encoded is None:
        raise RuntimeError("Der atomare Phasenmarker darf nicht ohne PREPARED-Manifest geschrieben werden.")
    prepared = _decode_prepared(prepared_encoded, expected_executable)
    normalized_id = _strict_transaction_id(transaction_id)
    if prepared.transaction_id != normalized_id:
        raise RuntimeError("Die Transaktions-ID stimmt nicht mit dem PREPARED-Manifest überein.")
    encoded = _canonical_json(_phase_payload(normalized_id, phase, prepared_encoded))
    existing = state_store.read(PHASE_FILE_NAME)
    if existing is not None:
        if existing != encoded:
            raise RuntimeError("Die Transaktion besitzt bereits eine andere persistente Abschlussphase.")
        return
    try:
        state_store.create(PHASE_FILE_NAME, encoded)
    except FileExistsError:
        existing = state_store.read(PHASE_FILE_NAME)
        if existing != encoded:
            raise RuntimeError("Ein konkurrierender atomarer Phasenmarker wurde veröffentlicht.") from None
    if state_store.read(PHASE_FILE_NAME) != encoded:
        raise RuntimeError("Der atomare Phasenmarker konnte nicht unverändert zurückgelesen werden.")


def _require_owned_stable_service(observation: RecoveryObservation) -> None:
    if observation.service in {ServiceState.FOREIGN, ServiceState.UNSTABLE}:
        raise RuntimeError("Der Dienst ist fremd oder nicht in einem stabilen Zustand; Recovery verändert nichts.")


def _validate_incomplete_bundles(observation: RecoveryObservation) -> None:
    incomplete = observation.incomplete_bundles
    if not isinstance(incomplete, frozenset) or any(
        not isinstance(slot, str) or slot not in {"live", "rollback", "obsolete"} for slot in incomplete
    ):
        raise RuntimeError("Die unvollständigen Dienst-Bundles sind nicht eindeutig beschrieben.")
    presence = {
        "live": observation.bundles.live,
        "rollback": observation.bundles.rollback,
        "obsolete": observation.bundles.obsolete,
    }
    if any(not presence[slot] for slot in incomplete):
        raise RuntimeError("Ein als unvollständig gemeldetes Dienst-Bundle ist nicht vorhanden.")


def _target_service_state(prepared: PreparedTransaction) -> ServiceState:
    return ServiceState.OWNED_RUNNING if prepared.target_service_running else ServiceState.OWNED_STOPPED


def mark_commit_started(
    expected_executable: Path,
    *,
    transaction_id: str,
    observation: RecoveryObservation,
    _state_store: TransactionStore | None = None,
) -> None:
    """Persist the no-return marker only after an exact committed topology proof."""

    state_store = _state_store or _store(expected_executable, create=False)
    state = load_transaction(
        expected_executable,
        transaction_id=transaction_id,
        _state_store=state_store,
    )
    if state is None:
        raise RuntimeError("Ohne PREPARED-Manifest kann kein Commit bewiesen werden.")
    if state.phase is TransactionPhase.COMMIT_STARTED:
        return
    if state.phase is not TransactionPhase.PREPARED:
        raise RuntimeError("Eine bereits zurückgerollte Transaktion kann nicht mehr committed werden.")
    _validate_incomplete_bundles(observation)
    expected_topology = (
        BundleTopology(live=True, new=False, rollback=False, obsolete=True)
        if state.prepared.service_existed
        else BundleTopology(live=True, new=False, rollback=False, obsolete=False)
    )
    if (
        observation.bundles != expected_topology
        or observation.service != _target_service_state(state.prepared)
        or observation.incomplete_bundles
    ):
        raise RuntimeError("Der neue Dienststand ist nicht vollständig und stabil als Commit bewiesen.")
    _write_phase(
        expected_executable,
        transaction_id=transaction_id,
        phase=TransactionPhase.COMMIT_STARTED,
        _state_store=state_store,
    )


def _rollback_actions(prepared: PreparedTransaction, observation: RecoveryObservation) -> tuple[RecoveryAction, ...]:
    _require_owned_stable_service(observation)
    _validate_incomplete_bundles(observation)
    topology = observation.bundles
    if prepared.service_existed:
        if observation.service not in {ServiceState.OWNED_STOPPED, ServiceState.OWNED_RUNNING}:
            raise RuntimeError("Der zu aktualisierende eigene Dienst fehlt; Recovery verändert nichts.")
        if observation.incomplete_bundles & {"rollback", "obsolete"}:
            raise RuntimeError("Ein rollbackfähiges Update benötigt ein vollständiges altes Backup-Bundle.")
        if "live" in observation.incomplete_bundles and not (topology.rollback or topology.obsolete):
            raise RuntimeError("Ein unvollständiges Live-Bundle besitzt keine vollständige rollbackfähige Baseline.")
        mapping: dict[BundleTopology, tuple[RecoveryAction, ...]] = {
            BundleTopology(True, False, False, False): (),
            BundleTopology(True, True, False, False): (RecoveryAction.DELETE_NEW,),
            BundleTopology(False, True, True, False): (
                RecoveryAction.DELETE_NEW,
                RecoveryAction.MOVE_ROLLBACK_TO_LIVE,
            ),
            BundleTopology(True, False, True, False): (
                RecoveryAction.DELETE_LIVE,
                RecoveryAction.MOVE_ROLLBACK_TO_LIVE,
            ),
            BundleTopology(True, False, False, True): (
                RecoveryAction.DELETE_LIVE,
                RecoveryAction.MOVE_OBSOLETE_TO_LIVE,
            ),
            BundleTopology(False, False, True, False): (RecoveryAction.MOVE_ROLLBACK_TO_LIVE,),
            BundleTopology(False, False, False, True): (RecoveryAction.MOVE_OBSOLETE_TO_LIVE,),
        }
        bundle_actions = mapping.get(topology)
        if bundle_actions is None:
            raise RuntimeError("Die Update-Bundles bilden keinen eindeutigen rollbackfähigen Zustand.")
        actions: list[RecoveryAction] = []
        if observation.service is ServiceState.OWNED_RUNNING:
            actions.append(RecoveryAction.STOP_SERVICE)
        actions.extend(bundle_actions)
        actions.append(RecoveryAction.RESTORE_SERVICE_METADATA)
        if prepared.service_running:
            actions.append(RecoveryAction.START_SERVICE)
        return tuple(actions)

    if topology.rollback or topology.obsolete or (topology.live and topology.new):
        raise RuntimeError("Eine Erstinstallation besitzt fremde oder mehrdeutige Backup-Bundles.")
    if observation.service not in {
        ServiceState.ABSENT,
        ServiceState.OWNED_STOPPED,
        ServiceState.OWNED_RUNNING,
    }:
        raise RuntimeError("Der Dienstzustand der Erstinstallation ist nicht sicher zuordenbar.")
    actions = []
    if observation.service is ServiceState.OWNED_RUNNING:
        actions.append(RecoveryAction.STOP_SERVICE)
    if observation.service is not ServiceState.ABSENT:
        actions.append(RecoveryAction.DELETE_SERVICE)
    if topology.live:
        actions.append(RecoveryAction.DELETE_LIVE)
    if topology.new:
        actions.append(RecoveryAction.DELETE_NEW)
    if not prepared.machine_before.any_existed:
        actions.append(RecoveryAction.PURGE_MACHINE_STATE)
    return tuple(actions)


def _forward_actions(prepared: PreparedTransaction, observation: RecoveryObservation) -> tuple[RecoveryAction, ...]:
    _require_owned_stable_service(observation)
    _validate_incomplete_bundles(observation)
    if observation.service != _target_service_state(prepared):
        raise RuntimeError("Der committed Dienst besitzt nicht den persistent bewiesenen Zielzustand.")
    if "live" in observation.incomplete_bundles:
        raise RuntimeError("Das committed Live-Bundle ist nicht vollständig.")
    if prepared.service_existed:
        if observation.incomplete_bundles - {"obsolete"}:
            raise RuntimeError("Die committed Update-Bundles enthalten einen unzulässigen Teilzustand.")
        if observation.bundles == BundleTopology(True, False, False, True):
            return (RecoveryAction.DELETE_OBSOLETE,)
        if observation.bundles == BundleTopology(True, False, False, False):
            return ()
        raise RuntimeError("Die committed Update-Bundles sind nicht eindeutig vorwärts bereinigbar.")
    if observation.bundles != BundleTopology(True, False, False, False):
        raise RuntimeError("Die committed Erstinstallation besitzt einen unbekannten Bundlezustand.")
    return ()


def plan_recovery(state: TransactionState, observation: RecoveryObservation) -> RecoveryPlan:
    """Build a pure all-or-nothing recovery plan; invalid observations never produce actions."""

    if state.phase is TransactionPhase.SERVICE_ROLLBACK_COMPLETE:
        if observation != _expected_completed_observation(state.prepared, RecoveryDirection.ROLLBACK):
            raise RuntimeError("Der als abgeschlossen markierte Rollbackzustand ist nicht vollständig.")
        return RecoveryPlan(
            transaction_id=state.prepared.transaction_id,
            direction=RecoveryDirection.COMPLETE,
            observation=observation,
            actions=(),
        )
    if state.phase is TransactionPhase.COMMIT_STARTED:
        direction = RecoveryDirection.FORWARD
        actions = _forward_actions(state.prepared, observation)
    else:
        direction = RecoveryDirection.ROLLBACK
        actions = _rollback_actions(state.prepared, observation)
    return RecoveryPlan(
        transaction_id=state.prepared.transaction_id,
        direction=direction,
        observation=observation,
        actions=actions,
    )


def _expected_completed_observation(prepared: PreparedTransaction, direction: RecoveryDirection) -> RecoveryObservation:
    if direction is RecoveryDirection.FORWARD:
        return RecoveryObservation(
            bundles=BundleTopology(True, False, False, False),
            service=_target_service_state(prepared),
        )
    if prepared.service_existed:
        return RecoveryObservation(
            bundles=BundleTopology(True, False, False, False),
            service=ServiceState.OWNED_RUNNING if prepared.service_running else ServiceState.OWNED_STOPPED,
        )
    return RecoveryObservation(
        bundles=BundleTopology(False, False, False, False),
        service=ServiceState.ABSENT,
    )


def execute_recovery(
    expected_executable: Path,
    *,
    state: TransactionState,
    plan: RecoveryPlan,
    operations: RecoveryOperations,
    _state_store: TransactionStore | None = None,
) -> None:
    """Execute a prevalidated plan and durably mark a completed service rollback."""

    if plan.transaction_id != state.prepared.transaction_id:
        raise RuntimeError("Recovery-Plan und PREPARED-Manifest gehören nicht zur selben Transaktion.")
    if plan.direction is RecoveryDirection.COMPLETE:
        return
    if operations.observe() != plan.observation:
        raise RuntimeError("Der Recovery-Zustand hat sich geändert; vor einer Mutation ist eine Neuplanung nötig.")
    for action in plan.actions:
        if action is RecoveryAction.STOP_SERVICE:
            operations.stop_service()
        elif action is RecoveryAction.DELETE_SERVICE:
            operations.delete_service()
        elif action is RecoveryAction.DELETE_LIVE:
            operations.delete_bundle("live")
        elif action is RecoveryAction.DELETE_NEW:
            operations.delete_bundle("new")
        elif action is RecoveryAction.MOVE_ROLLBACK_TO_LIVE:
            operations.move_bundle("rollback", "live")
        elif action is RecoveryAction.MOVE_OBSOLETE_TO_LIVE:
            operations.move_bundle("obsolete", "live")
        elif action is RecoveryAction.RESTORE_SERVICE_METADATA:
            if state.prepared.service_metadata is None:
                raise RuntimeError("Für den Update-Rollback fehlt die persistente SCM-Baseline.")
            operations.restore_service_metadata(state.prepared.service_metadata)
        elif action is RecoveryAction.START_SERVICE:
            operations.start_service()
        elif action is RecoveryAction.PURGE_MACHINE_STATE:
            if state.prepared.machine_before.any_existed:
                raise RuntimeError("Vorhandener Maschinenzustand darf nicht durch Recovery gelöscht werden.")
            operations.purge_machine_state()
        elif action is RecoveryAction.DELETE_OBSOLETE:
            operations.delete_bundle("obsolete")
        else:
            raise AssertionError(f"Unbekannte Recovery-Aktion: {action}")
    if operations.observe() != _expected_completed_observation(state.prepared, plan.direction):
        raise RuntimeError("Der Recovery-Zielzustand konnte nicht vollständig und stabil bewiesen werden.")
    if plan.direction is RecoveryDirection.ROLLBACK:
        _write_phase(
            expected_executable,
            transaction_id=state.prepared.transaction_id,
            phase=TransactionPhase.SERVICE_ROLLBACK_COMPLETE,
            _state_store=_state_store,
        )


def finalize_transaction(
    expected_executable: Path,
    *,
    transaction_id: str,
    observation: RecoveryObservation,
    _state_store: TransactionStore | None = None,
) -> None:
    """Consume protected records only after service and caller-owned cleanup is complete."""

    state_store = _state_store or _store(expected_executable, create=False)
    state = load_transaction(
        expected_executable,
        transaction_id=transaction_id,
        _state_store=state_store,
    )
    if state is None or state.phase is TransactionPhase.PREPARED:
        raise RuntimeError("Eine nicht abgeschlossene Transaktion darf nicht finalisiert werden.")
    expected = _expected_completed_observation(
        state.prepared,
        RecoveryDirection.FORWARD if state.phase is TransactionPhase.COMMIT_STARTED else RecoveryDirection.ROLLBACK,
    )
    if observation != expected:
        raise RuntimeError("Der finale Dienstzustand stimmt nicht mit dem persistenten Abschlussbeweis überein.")
    # Both records are intentionally consumed only after every external cleanup.
    # Keeping the phase until the immutable baseline is gone makes a crash
    # between the two deletions fail closed as an orphaned completion marker.
    state_store.delete(PREPARED_FILE_NAME)
    state_store.delete(PHASE_FILE_NAME)
    state_store.remove_directory_if_empty()
