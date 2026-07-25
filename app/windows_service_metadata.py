from __future__ import annotations

import json
import ntpath
import os
import re
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path, PureWindowsPath
from typing import Any

from .windows_service_config import SERVICE_ACCOUNT, SERVICE_NAME, validate_machine_path

SNAPSHOT_SCHEMA_VERSION = 1
UNINSTALL_RECORD_SCHEMA_VERSION = 1
MAXIMUM_SNAPSHOT_BYTES = 64 * 1024
ALLOWED_SERVICE_START_TYPES = frozenset({2, 3, 4})  # auto, demand, disabled
ALLOWED_SERVICE_SID_TYPES = frozenset({0, 1, 3})  # none, unrestricted, restricted
ALLOWED_FAILURE_ACTION_TYPES = frozenset({0, 1, 3})  # none, restart, run-command
UNINSTALLER_STATE_DIRECTORY_NAME = ".uninstaller-state"
SERVICE_METADATA_FILE_NAME = "service-metadata.json"
SERVICE_METADATA_TEMP_PATTERN = re.compile(r"\.service-metadata\.json\.[0-9a-f]{32}\.tmp\Z")
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
ERROR_ALREADY_EXISTS = 183
ERROR_SERVICE_DOES_NOT_EXIST = 1060
MOVEFILE_WRITE_THROUGH = 0x00000008
SERVICE_START_TIMEOUT_SECONDS = 30.0


def _win32service() -> Any:
    if sys.platform != "win32":
        raise OSError("SCM-Metadaten können nur unter Windows verwaltet werden.")
    try:
        import win32service
    except ImportError as exc:
        raise RuntimeError("pywin32 fehlt; die SCM-Metadaten können nicht sicher verwaltet werden.") from exc
    return win32service


def _windows_file_modules() -> tuple[Any, Any, Any, Any, Any]:
    if sys.platform != "win32":
        raise OSError("Die geschützte SCM-Sicherung ist ausschließlich unter Windows verfügbar.")
    try:
        import ntsecuritycon
        import pywintypes
        import win32con
        import win32file
        import win32security
    except ImportError as exc:
        raise RuntimeError("pywin32 fehlt; die SCM-Sicherung kann nicht geschützt werden.") from exc
    return pywintypes, win32con, win32file, win32security, ntsecuritycon


def _strict_integer(value: object, *, name: str, minimum: int = 0, maximum: int = 0xFFFFFFFF) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise RuntimeError(f"Das Feld {name!r} im SCM-Sicherungsbeleg ist ungültig.")
    return value


def _normalized_scm_text(value: object, *, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RuntimeError(f"Der SCM hat einen ungültigen Textwert für {name} geliefert.")
    return value


def _canonical_expected_executable(path: Path) -> str:
    value = str(path)
    pure_path = PureWindowsPath(value)
    if (
        not value
        or "\x00" in value
        or '"' in value
        or not pure_path.is_absolute()
        or value != ntpath.normpath(value)
        or pure_path.suffix.casefold() != ".exe"
    ):
        raise RuntimeError("Der erwartete Dienstpfad ist nicht absolut und kanonisch.")
    return value


def _metadata_paths(expected_executable: Path) -> tuple[Path, Path, Path]:
    expected = _canonical_expected_executable(expected_executable)
    service_directory = ntpath.dirname(expected)
    if ntpath.basename(service_directory).casefold() != "service":
        raise RuntimeError("Der erwartete Dienstpfad liegt nicht im festen Dienst-Binärverzeichnis.")
    installation_directory = Path(ntpath.dirname(service_directory))
    state_directory = installation_directory / UNINSTALLER_STATE_DIRECTORY_NAME
    return installation_directory, state_directory, state_directory / SERVICE_METADATA_FILE_NAME


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Doppeltes JSON-Feld: {key}")
        result[key] = value
    return result


def _validate_service_provenance(configuration: Sequence[object], expected_executable: Path) -> str:
    if len(configuration) != 9:
        raise RuntimeError("Der SCM hat eine unerwartete Dienstkonfiguration geliefert.")
    expected = _canonical_expected_executable(expected_executable)
    image_path = configuration[3]
    account = configuration[7]
    if not isinstance(image_path, str) or image_path.casefold() != f'"{expected}"'.casefold():
        raise RuntimeError("Der gleichnamige Windows-Dienst verwendet nicht den exakt erwarteten Programmdateipfad.")
    if not isinstance(account, str) or account.casefold() != SERVICE_ACCOUNT.casefold():
        raise RuntimeError("Der gleichnamige Windows-Dienst verwendet nicht das erwartete LocalService-Konto.")
    return expected


def _normalize_failure_actions(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"ResetPeriod", "RebootMsg", "Command", "Actions"}:
        raise RuntimeError("Der SCM hat ungültige Dienstfehleraktionen geliefert.")
    reset_period = _strict_integer(value["ResetPeriod"], name="failure_actions.ResetPeriod")
    reboot_message = _normalized_scm_text(value["RebootMsg"], name="failure_actions.RebootMsg")
    command = _normalized_scm_text(value["Command"], name="failure_actions.Command")
    raw_actions = value["Actions"]
    if not isinstance(raw_actions, (list, tuple)):
        raise RuntimeError("Der SCM hat eine ungültige Fehleraktionsliste geliefert.")
    actions: list[list[int]] = []
    for index, action in enumerate(raw_actions):
        if not isinstance(action, (list, tuple)) or len(action) != 2:
            raise RuntimeError("Der SCM hat eine ungültige Fehleraktion geliefert.")
        action_type = _strict_integer(action[0], name=f"failure_actions.Actions[{index}].type", maximum=3)
        if action_type not in ALLOWED_FAILURE_ACTION_TYPES:
            raise RuntimeError(
                "Eine SCM-Neustartaktion des Betriebssystems kann ohne Shutdown-Privileg nicht sicher restauriert werden."
            )
        delay = _strict_integer(action[1], name=f"failure_actions.Actions[{index}].delay")
        actions.append([action_type, delay])
    return {
        "ResetPeriod": reset_period,
        "RebootMsg": reboot_message,
        "Command": command,
        "Actions": actions,
    }


def _query_snapshot(service: Any, win32service: Any, expected_executable: Path) -> dict[str, object]:
    configuration = win32service.QueryServiceConfig(service)
    expected = _validate_service_provenance(configuration, expected_executable)
    start_type = _strict_integer(configuration[1], name="start_type")
    if start_type not in ALLOWED_SERVICE_START_TYPES:
        raise RuntimeError("Der SCM hat einen für diesen Windows-Dienst unzulässigen Starttyp geliefert.")
    description = _normalized_scm_text(
        win32service.QueryServiceConfig2(service, win32service.SERVICE_CONFIG_DESCRIPTION),
        name="description",
    )
    delayed_start = win32service.QueryServiceConfig2(
        service,
        win32service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO,
    )
    failure_actions_flag = win32service.QueryServiceConfig2(
        service,
        win32service.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG,
    )
    if type(delayed_start) is not bool or type(failure_actions_flag) is not bool:
        raise RuntimeError("Der SCM hat ungültige boolesche Dienstmetadaten geliefert.")
    service_sid_type = _strict_integer(
        win32service.QueryServiceConfig2(service, win32service.SERVICE_CONFIG_SERVICE_SID_INFO),
        name="service_sid_type",
    )
    if service_sid_type not in ALLOWED_SERVICE_SID_TYPES:
        raise RuntimeError("Der SCM hat einen unbekannten dienstspezifischen SID-Typ geliefert.")
    failure_actions = _normalize_failure_actions(
        win32service.QueryServiceConfig2(service, win32service.SERVICE_CONFIG_FAILURE_ACTIONS)
    )
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "service_name": SERVICE_NAME,
        "expected_executable": expected,
        "service_account": SERVICE_ACCOUNT,
        "start_type": start_type,
        "description": description,
        "delayed_start": delayed_start,
        "service_sid_type": service_sid_type,
        "failure_actions": failure_actions,
        "failure_actions_flag": failure_actions_flag,
    }


def _with_service(*, access: int, operation: Any) -> Any:
    win32service = _win32service()
    manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    service = None
    try:
        service = win32service.OpenService(manager, SERVICE_NAME, access)
        return operation(service, win32service)
    finally:
        if service is not None:
            win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(manager)


def _administrative_security_attributes(*, directory: bool) -> Any:
    pywintypes, _win32con, _win32file, win32security, ntsecuritycon = _windows_file_modules()
    dacl = win32security.ACL()
    inheritance = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE if directory else 0
    for sid_text in (SYSTEM_SID, ADMINISTRATORS_SID):
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS,
            inheritance,
            ntsecuritycon.FILE_ALL_ACCESS,
            win32security.ConvertStringSidToSid(sid_text),
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


def _verify_administrative_path(path: Path, *, directory: bool) -> None:
    if not validate_machine_path(path, directory=directory):
        raise RuntimeError(f"Die geschützte SCM-Ablage {path} fehlt.")
    _pywintypes, _win32con, _win32file, win32security, ntsecuritycon = _windows_file_modules()
    information = win32security.DACL_SECURITY_INFORMATION | getattr(
        win32security,
        "OWNER_SECURITY_INFORMATION",
        0x00000001,
    )
    try:
        descriptor = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            information,
        )
        owner = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
        dacl = descriptor.GetSecurityDescriptorDacl()
        control, _revision = descriptor.GetSecurityDescriptorControl()
    except Exception as exc:
        raise RuntimeError(f"Die Sicherheitsattribute der SCM-Ablage {path} konnten nicht geprüft werden.") from exc
    if owner != ADMINISTRATORS_SID:
        raise RuntimeError("Die SCM-Ablage besitzt nicht die Administratorengruppe als Eigentümer.")
    if dacl is None or not control & win32security.SE_DACL_PROTECTED:
        raise RuntimeError("Die DACL der SCM-Ablage ist nicht vor Vererbung geschützt.")
    expected_inheritance = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE if directory else 0
    observed: set[str] = set()
    for index in range(dacl.GetAceCount()):
        header, mask, sid = dacl.GetAce(index)
        sid_text = win32security.ConvertSidToStringSid(sid)
        if (
            int(header[0]) != win32security.ACCESS_ALLOWED_ACE_TYPE
            or int(header[1]) != expected_inheritance
            or int(mask) != ntsecuritycon.FILE_ALL_ACCESS
            or sid_text not in {SYSTEM_SID, ADMINISTRATORS_SID}
        ):
            raise RuntimeError("Die SCM-Ablage enthält eine nicht erlaubte Windows-Berechtigung.")
        observed.add(sid_text)
    if observed != {SYSTEM_SID, ADMINISTRATORS_SID} or dacl.GetAceCount() != 2:
        raise RuntimeError("Die SCM-Ablage besitzt nicht die exakt erforderliche administrative DACL.")


def _validate_metadata_base(expected_executable: Path) -> tuple[Path, Path]:
    installation_directory, state_directory, snapshot_path = _metadata_paths(expected_executable)
    if not validate_machine_path(installation_directory, directory=True):
        raise RuntimeError("Das Installationsverzeichnis für die SCM-Sicherung fehlt.")
    if not validate_machine_path(expected_executable, directory=False):
        raise RuntimeError("Die erwartete Dienst-EXE für die SCM-Sicherung fehlt.")
    return state_directory, snapshot_path


def _prepare_metadata_directory(expected_executable: Path) -> tuple[Path, Path]:
    state_directory, snapshot_path = _validate_metadata_base(expected_executable)
    if validate_machine_path(state_directory, directory=True):
        _verify_administrative_path(state_directory, directory=True)
        return state_directory, snapshot_path

    _pywintypes, _win32con, win32file, _win32security, _ntsecuritycon = _windows_file_modules()
    try:
        win32file.CreateDirectoryW(
            str(state_directory),
            _administrative_security_attributes(directory=True),
        )
    except Exception as exc:
        if getattr(exc, "winerror", None) != ERROR_ALREADY_EXISTS:
            raise RuntimeError("Die administrative SCM-Ablage konnte nicht sicher erstellt werden.") from exc
    _verify_administrative_path(state_directory, directory=True)
    return state_directory, snapshot_path


def _require_metadata_directory(expected_executable: Path) -> tuple[Path, Path]:
    state_directory, snapshot_path = _validate_metadata_base(expected_executable)
    _verify_administrative_path(state_directory, directory=True)
    return state_directory, snapshot_path


def _secure_snapshot_exists(snapshot_path: Path) -> bool:
    if not validate_machine_path(snapshot_path, directory=False):
        return False
    _verify_administrative_path(snapshot_path, directory=False)
    return True


def _inventory_metadata_directory(state_directory: Path) -> tuple[Path | None, tuple[Path, ...]]:
    _verify_administrative_path(state_directory, directory=True)
    snapshot: Path | None = None
    temporary: list[Path] = []
    try:
        entries = tuple(state_directory.iterdir())
    except OSError as exc:
        raise RuntimeError("Die administrative SCM-Ablage konnte nicht inventarisiert werden.") from exc
    for entry in entries:
        if entry.name == SERVICE_METADATA_FILE_NAME:
            if snapshot is not None:
                raise RuntimeError("Die administrative SCM-Ablage enthält einen doppelten Sicherungsbeleg.")
            _verify_administrative_path(entry, directory=False)
            snapshot = entry
        elif SERVICE_METADATA_TEMP_PATTERN.fullmatch(entry.name):
            _verify_administrative_path(entry, directory=False)
            temporary.append(entry)
        else:
            raise RuntimeError("Die administrative SCM-Ablage enthält einen unbekannten Eintrag.")
    if len(temporary) > 1:
        raise RuntimeError("Die administrative SCM-Ablage enthält mehrere temporäre Sicherungsbelege.")
    return snapshot, tuple(temporary)


def _delete_verified_metadata_file(path: Path) -> None:
    _verify_administrative_path(path.parent, directory=True)
    _verify_administrative_path(path, directory=False)
    try:
        path.unlink()
    except OSError as exc:
        raise RuntimeError("Ein geschützter SCM-Sicherungsbeleg konnte nicht gelöscht werden.") from exc


def _write_secure_snapshot(snapshot_path: Path, payload: bytes) -> None:
    if not payload or len(payload) > MAXIMUM_SNAPSHOT_BYTES:
        raise RuntimeError("Der SCM-Sicherungsbeleg hat eine unzulässige Größe.")
    _verify_administrative_path(snapshot_path.parent, directory=True)
    if validate_machine_path(snapshot_path, directory=False):
        raise RuntimeError("Ein geschützter SCM-Sicherungsbeleg ist bereits vorhanden.")
    _pywintypes, win32con, win32file, _win32security, _ntsecuritycon = _windows_file_modules()
    handle = None
    created = False
    failure: Exception | None = None
    try:
        handle = win32file.CreateFile(
            str(snapshot_path),
            win32con.GENERIC_WRITE,
            0,
            _administrative_security_attributes(directory=False),
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
                snapshot_path.unlink(missing_ok=True)
            except OSError:
                pass
    if failure is not None:
        raise RuntimeError("Der geschützte SCM-Sicherungsbeleg konnte nicht geschrieben werden.") from failure
    _verify_administrative_path(snapshot_path, directory=False)


def _publish_secure_snapshot(temporary_path: Path, snapshot_path: Path) -> None:
    if temporary_path.parent != snapshot_path.parent:
        raise RuntimeError("Der temporäre SCM-Sicherungsbeleg liegt nicht im festen Zustandsverzeichnis.")
    _verify_administrative_path(temporary_path.parent, directory=True)
    _verify_administrative_path(temporary_path, directory=False)
    if validate_machine_path(snapshot_path, directory=False):
        raise RuntimeError("Ein geschützter SCM-Sicherungsbeleg ist bereits vorhanden.")
    _pywintypes, _win32con, win32file, _win32security, _ntsecuritycon = _windows_file_modules()
    try:
        win32file.MoveFileEx(
            str(temporary_path),
            str(snapshot_path),
            MOVEFILE_WRITE_THROUGH,
        )
    except Exception as exc:
        raise RuntimeError("Der SCM-Sicherungsbeleg konnte nicht atomar veröffentlicht werden.") from exc
    _verify_administrative_path(snapshot_path, directory=False)


def _read_secure_snapshot(snapshot_path: Path) -> bytes:
    _verify_administrative_path(snapshot_path.parent, directory=True)
    _verify_administrative_path(snapshot_path, directory=False)
    try:
        payload = snapshot_path.read_bytes()
    except OSError as exc:
        raise RuntimeError("Der SCM-Sicherungsbeleg konnte nicht gelesen werden.") from exc
    _verify_administrative_path(snapshot_path, directory=False)
    return payload


def _decode_snapshot(encoded: bytes, expected_executable: Path) -> dict[str, object]:
    if not encoded or len(encoded) > MAXIMUM_SNAPSHOT_BYTES:
        raise RuntimeError("Der SCM-Sicherungsbeleg hat eine unzulässige Größe.")
    try:
        payload = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Der SCM-Sicherungsbeleg ist kein gültiges JSON-Dokument.") from exc
    expected_keys = {
        "schema_version",
        "service_name",
        "expected_executable",
        "service_account",
        "start_type",
        "description",
        "delayed_start",
        "service_sid_type",
        "failure_actions",
        "failure_actions_flag",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise RuntimeError("Der SCM-Sicherungsbeleg hat ein unbekanntes Format.")
    expected = _canonical_expected_executable(expected_executable)
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != SNAPSHOT_SCHEMA_VERSION
        or payload["service_name"] != SERVICE_NAME
        or payload["service_account"] != SERVICE_ACCOUNT
        or not isinstance(payload["expected_executable"], str)
        or payload["expected_executable"].casefold() != expected.casefold()
    ):
        raise RuntimeError("Der SCM-Sicherungsbeleg gehört nicht zum erwarteten Windows-Dienst.")
    description = payload["description"]
    if not isinstance(description, str):
        raise RuntimeError("Die Dienstbeschreibung im SCM-Sicherungsbeleg ist ungültig.")
    if type(payload["delayed_start"]) is not bool or type(payload["failure_actions_flag"]) is not bool:
        raise RuntimeError("Der SCM-Sicherungsbeleg enthält ungültige boolesche Metadaten.")
    payload["start_type"] = _strict_integer(payload["start_type"], name="start_type")
    if payload["start_type"] not in ALLOWED_SERVICE_START_TYPES:
        raise RuntimeError("Der SCM-Sicherungsbeleg enthält einen unzulässigen Dienststarttyp.")
    payload["service_sid_type"] = _strict_integer(payload["service_sid_type"], name="service_sid_type")
    if payload["service_sid_type"] not in ALLOWED_SERVICE_SID_TYPES:
        raise RuntimeError("Der SCM-Sicherungsbeleg enthält einen unbekannten dienstspezifischen SID-Typ.")
    normalized_failure_actions = _normalize_failure_actions(payload["failure_actions"])
    if payload["failure_actions"] != normalized_failure_actions:
        raise RuntimeError("Der SCM-Sicherungsbeleg enthält nicht kanonische Dienstfehleraktionen.")
    payload["failure_actions"] = normalized_failure_actions
    return payload


def _load_snapshot(snapshot_path: Path, expected_executable: Path) -> dict[str, object]:
    return _decode_snapshot(_read_secure_snapshot(snapshot_path), expected_executable)


def validate_service_metadata(
    expected_executable: Path,
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Return a strict, normalized copy of an SCM baseline supplied by trusted state."""

    try:
        encoded = (json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Die SCM-Baseline kann nicht als striktes JSON dargestellt werden.") from exc
    return _decode_snapshot(encoded, expected_executable)


def _encode_uninstall_record(
    expected_executable: Path,
    service_metadata: Mapping[str, object],
    *,
    service_was_running: bool,
) -> bytes:
    normalized_metadata = validate_service_metadata(expected_executable, service_metadata)
    payload = {
        "schema_version": UNINSTALL_RECORD_SCHEMA_VERSION,
        "service_metadata": normalized_metadata,
        "service_was_running": service_was_running,
    }
    return (json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _decode_uninstall_record(encoded: bytes, expected_executable: Path) -> tuple[dict[str, object], bool]:
    if not encoded or len(encoded) > MAXIMUM_SNAPSHOT_BYTES:
        raise RuntimeError("Der Deinstallationsbeleg hat eine unzulässige Größe.")
    try:
        payload = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Der Deinstallationsbeleg ist kein gültiges JSON-Dokument.") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "service_metadata",
        "service_was_running",
    }:
        raise RuntimeError("Der Deinstallationsbeleg hat ein unbekanntes Format.")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != UNINSTALL_RECORD_SCHEMA_VERSION
        or type(payload["service_was_running"]) is not bool
        or not isinstance(payload["service_metadata"], dict)
    ):
        raise RuntimeError("Der Deinstallationsbeleg enthält ungültige Zustandsdaten.")
    metadata = validate_service_metadata(expected_executable, payload["service_metadata"])
    return metadata, payload["service_was_running"]


def _load_uninstall_record(snapshot_path: Path, expected_executable: Path) -> tuple[dict[str, object], bool]:
    return _decode_uninstall_record(_read_secure_snapshot(snapshot_path), expected_executable)


def capture_service_metadata(expected_executable: Path) -> dict[str, object]:
    """Capture and normalize the complete owned-service SCM configuration."""

    def capture(service: Any, win32service: Any) -> dict[str, object]:
        return _query_snapshot(service, win32service, expected_executable)

    win32service = _win32service()
    payload = _with_service(access=win32service.SERVICE_QUERY_CONFIG, operation=capture)
    return validate_service_metadata(expected_executable, payload)


def inspect_owned_service_metadata(expected_executable: Path) -> tuple[dict[str, object], bool] | None:
    """Inspect an owned service and require a stable RUNNING or STOPPED state."""

    def inspect(service: Any, win32service: Any) -> tuple[dict[str, object], bool]:
        payload = _query_snapshot(service, win32service, expected_executable)
        status = win32service.QueryServiceStatus(service)
        if not isinstance(status, (list, tuple)) or len(status) < 2:
            raise RuntimeError("Der SCM hat einen unbekannten Dienststatus geliefert.")
        state = int(status[1])
        if state == win32service.SERVICE_RUNNING:
            return payload, True
        if state == win32service.SERVICE_STOPPED:
            return payload, False
        raise RuntimeError("Der eigene Windows-Dienst befindet sich nicht stabil in RUNNING oder STOPPED.")

    win32service = _win32service()
    access = win32service.SERVICE_QUERY_CONFIG | win32service.SERVICE_QUERY_STATUS
    try:
        payload, running = _with_service(access=access, operation=inspect)
    except Exception as exc:
        if getattr(exc, "winerror", None) == ERROR_SERVICE_DOES_NOT_EXIST:
            return None
        raise
    return validate_service_metadata(expected_executable, payload), running


def snapshot_service_metadata(expected_executable: Path) -> None:
    """Persist the exact pre-uninstall SCM state before the first mutation."""

    state_directory, snapshot_path = _prepare_metadata_directory(expected_executable)
    existing_snapshot, temporary_paths = _inventory_metadata_directory(state_directory)
    if existing_snapshot is not None:
        _load_uninstall_record(existing_snapshot, expected_executable)
        for temporary_path in temporary_paths:
            _delete_verified_metadata_file(temporary_path)
        return
    for temporary_path in temporary_paths:
        # The snapshot call cannot return before publication, so a temp-only
        # record proves that no uninstall mutation was authorized yet.
        _delete_verified_metadata_file(temporary_path)

    observed = inspect_owned_service_metadata(expected_executable)
    if observed is None:
        raise RuntimeError("Der eigene Windows-Dienst fehlt vor Beginn der Deinstallation.")
    payload, service_was_running = observed
    if service_was_running and payload["start_type"] == 4:
        raise RuntimeError(
            "Ein laufender Dienst mit deaktiviertem Starttyp ist keine sicher restaurierbare Deinstallationsbaseline."
        )
    if service_was_running and payload["service_sid_type"] == 0:
        raise RuntimeError(
            "Ein laufender Dienst ohne dienstspezifischen SID ist keine sicher restaurierbare Deinstallationsbaseline."
        )
    encoded = _encode_uninstall_record(
        expected_executable,
        payload,
        service_was_running=service_was_running,
    )
    temporary_path = state_directory / f".{SERVICE_METADATA_FILE_NAME}.{uuid.uuid4().hex}.tmp"
    _write_secure_snapshot(temporary_path, encoded)
    if _decode_uninstall_record(_read_secure_snapshot(temporary_path), expected_executable) != (
        payload,
        service_was_running,
    ):
        raise RuntimeError("Der SCM-Sicherungsbeleg konnte nicht unverändert zurückgelesen werden.")
    _publish_secure_snapshot(temporary_path, snapshot_path)
    if _load_uninstall_record(snapshot_path, expected_executable) != (payload, service_was_running):
        raise RuntimeError("Der veröffentlichte SCM-Sicherungsbeleg ist nicht unverändert lesbar.")


def restore_service_metadata_payload(
    expected_executable: Path,
    payload: Mapping[str, object],
) -> None:
    """Restore a validated SCM baseline without consuming its transaction record."""

    normalized_payload = validate_service_metadata(expected_executable, payload)

    def restore(service: Any, win32service: Any) -> None:
        _validate_service_provenance(win32service.QueryServiceConfig(service), expected_executable)
        win32service.ChangeServiceConfig(
            service,
            win32service.SERVICE_NO_CHANGE,
            normalized_payload["start_type"],
            win32service.SERVICE_NO_CHANGE,
            None,
            None,
            0,
            None,
            None,
            None,
            None,
        )
        win32service.ChangeServiceConfig2(
            service,
            win32service.SERVICE_CONFIG_DESCRIPTION,
            normalized_payload["description"],
        )
        win32service.ChangeServiceConfig2(
            service,
            win32service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO,
            normalized_payload["delayed_start"],
        )
        win32service.ChangeServiceConfig2(
            service,
            win32service.SERVICE_CONFIG_SERVICE_SID_INFO,
            normalized_payload["service_sid_type"],
        )
        failure_actions = normalized_payload["failure_actions"]
        assert isinstance(failure_actions, dict)
        win32service.ChangeServiceConfig2(
            service,
            win32service.SERVICE_CONFIG_FAILURE_ACTIONS,
            {
                "ResetPeriod": failure_actions["ResetPeriod"],
                "RebootMsg": failure_actions["RebootMsg"],
                "Command": failure_actions["Command"],
                "Actions": [tuple(action) for action in failure_actions["Actions"]],
            },
        )
        win32service.ChangeServiceConfig2(
            service,
            win32service.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG,
            normalized_payload["failure_actions_flag"],
        )
        if _query_snapshot(service, win32service, expected_executable) != normalized_payload:
            raise RuntimeError("Die SCM-Metadaten konnten nicht vollständig und exakt wiederhergestellt werden.")

    win32service = _win32service()
    access = win32service.SERVICE_QUERY_CONFIG | win32service.SERVICE_CHANGE_CONFIG | win32service.SERVICE_START
    _with_service(access=access, operation=restore)


def restore_service_metadata(expected_executable: Path) -> None:
    """Restore protected metadata through SCM APIs after revalidating ownership."""

    state_directory, snapshot_path = _require_metadata_directory(expected_executable)
    observed_snapshot, temporary_paths = _inventory_metadata_directory(state_directory)
    if observed_snapshot != snapshot_path or temporary_paths:
        raise RuntimeError("Die administrative SCM-Ablage ist nicht in einem restaurierbaren Zustand.")
    payload, _service_was_running = _load_uninstall_record(snapshot_path, expected_executable)
    restore_service_metadata_payload(expected_executable, payload)
    clear_service_metadata(expected_executable)


def _service_metadata_matches_uninstall_progress(
    current: Mapping[str, object],
    baseline: Mapping[str, object],
) -> bool:
    normalized_current = dict(current)
    normalized_baseline = dict(baseline)
    if normalized_current == normalized_baseline:
        return True
    normalized_baseline["start_type"] = 4
    return normalized_current == normalized_baseline


def _start_owned_service_and_wait(expected_executable: Path) -> None:
    def start(service: Any, win32service: Any) -> None:
        _validate_service_provenance(win32service.QueryServiceConfig(service), expected_executable)
        status = win32service.QueryServiceStatus(service)
        if not isinstance(status, (list, tuple)) or len(status) < 2:
            raise RuntimeError("Der SCM hat beim Wiederanlauf einen unbekannten Dienststatus geliefert.")
        if int(status[1]) == win32service.SERVICE_RUNNING:
            return
        if int(status[1]) != win32service.SERVICE_STOPPED:
            raise RuntimeError("Der eigene Windows-Dienst ist vor dem Wiederanlauf nicht stabil gestoppt.")
        win32service.StartService(service, None)
        deadline = time.monotonic() + SERVICE_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            status = win32service.QueryServiceStatus(service)
            if not isinstance(status, (list, tuple)) or len(status) < 2:
                raise RuntimeError("Der SCM hat beim Wiederanlauf einen unbekannten Dienststatus geliefert.")
            if int(status[1]) == win32service.SERVICE_RUNNING:
                return
            time.sleep(0.1)
        raise RuntimeError("Der eigene Windows-Dienst erreichte nach dem Rollback nicht rechtzeitig RUNNING.")

    win32service = _win32service()
    access = win32service.SERVICE_QUERY_CONFIG | win32service.SERVICE_QUERY_STATUS | win32service.SERVICE_START
    _with_service(access=access, operation=start)


def reconcile_service_uninstall(expected_executable: Path) -> None:
    """Recover an interrupted uninstall before a new uninstall inspection."""

    _installation_directory, state_directory, snapshot_path = _metadata_paths(expected_executable)
    if not os.path.lexists(state_directory):
        return
    if not validate_machine_path(state_directory, directory=True):
        raise RuntimeError("Der Deinstallationszustand ist kein vertrauenswürdiges lokales Verzeichnis.")
    observed_snapshot, temporary_paths = _inventory_metadata_directory(state_directory)
    if observed_snapshot is None:
        for temporary_path in temporary_paths:
            _delete_verified_metadata_file(temporary_path)
        try:
            state_directory.rmdir()
        except OSError as exc:
            raise RuntimeError("Die leere administrative Deinstallationsablage konnte nicht entfernt werden.") from exc
        return
    if observed_snapshot != snapshot_path:
        raise RuntimeError("Der Deinstallationsbeleg liegt nicht am festen erwarteten Pfad.")
    for temporary_path in temporary_paths:
        _delete_verified_metadata_file(temporary_path)
    baseline, service_was_running = _load_uninstall_record(snapshot_path, expected_executable)
    current = inspect_owned_service_metadata(expected_executable)
    if current is None:
        clear_service_metadata(expected_executable)
        return
    current_metadata, currently_running = current
    if not _service_metadata_matches_uninstall_progress(current_metadata, baseline):
        raise RuntimeError("Der Windows-Dienst weicht von der belegten Deinstallationsbaseline ab.")
    if currently_running and not service_was_running:
        raise RuntimeError("Der ursprünglich gestoppte Windows-Dienst wurde außerhalb der Deinstallation gestartet.")
    restore_service_metadata_payload(expected_executable, baseline)
    if service_was_running and not currently_running:
        _start_owned_service_and_wait(expected_executable)
    if inspect_owned_service_metadata(expected_executable) != (baseline, service_was_running):
        raise RuntimeError("Die Deinstallationsbaseline konnte nicht vollständig wiederhergestellt werden.")
    clear_service_metadata(expected_executable)


def assert_no_pending_service_uninstall(expected_executable: Path) -> None:
    """Block installation while any protected uninstall transaction exists."""

    _installation_directory, state_directory, _snapshot_path = _metadata_paths(expected_executable)
    if not os.path.lexists(state_directory):
        return
    if not validate_machine_path(state_directory, directory=True):
        raise RuntimeError("Ein nicht vertrauenswürdiger Deinstallationszustand blockiert die Installation.")
    _inventory_metadata_directory(state_directory)
    raise RuntimeError(
        "Eine frühere Deinstallation ist noch nicht abgeschlossen; führen Sie denselben Deinstaller erneut aus."
    )


def clear_service_metadata(expected_executable: Path) -> None:
    """Remove only strictly inventoried protected uninstall records."""

    state_directory, _snapshot_path = _validate_metadata_base(expected_executable)
    if not validate_machine_path(state_directory, directory=True):
        return
    snapshot_path, temporary_paths = _inventory_metadata_directory(state_directory)
    if snapshot_path is not None:
        _delete_verified_metadata_file(snapshot_path)
    for temporary_path in temporary_paths:
        _delete_verified_metadata_file(temporary_path)
    try:
        state_directory.rmdir()
    except OSError as exc:
        raise RuntimeError("Die administrative SCM-Ablage konnte nicht leer entfernt werden.") from exc


def disable_service_delayed_start(expected_executable: Path) -> None:
    """Clear DelayedAutoStart through the SCM, never by direct registry mutation."""

    def disable(service: Any, win32service: Any) -> None:
        _validate_service_provenance(win32service.QueryServiceConfig(service), expected_executable)
        win32service.ChangeServiceConfig2(
            service,
            win32service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO,
            False,
        )
        delayed = win32service.QueryServiceConfig2(
            service,
            win32service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO,
        )
        if delayed is not False:
            raise RuntimeError("Der verzögerte Dienststart konnte nicht verifiziert deaktiviert werden.")

    win32service = _win32service()
    access = win32service.SERVICE_QUERY_CONFIG | win32service.SERVICE_CHANGE_CONFIG
    _with_service(access=access, operation=disable)
