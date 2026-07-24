from __future__ import annotations

import ctypes
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from app import windows_open_client, windows_service_config, windows_service_metadata, windows_service_preflight
from app.windows_service_config import SERVICE_ACCOUNT, SERVICE_NAME, ServiceConfiguration, ServicePaths

EXPECTED_EXECUTABLE = Path(r"C:\Program Files\E-Rechnungs-Pruefer-Dienst\service\E-Rechnungs-Pruefer-Dienst.exe")


class _FakeServiceApi:
    SC_MANAGER_CONNECT = 1
    SERVICE_QUERY_CONFIG = 2
    SERVICE_CHANGE_CONFIG = 4
    SERVICE_QUERY_STATUS = 8
    SERVICE_START = 16
    SERVICE_STOPPED = 1
    SERVICE_RUNNING = 4
    SERVICE_NO_CHANGE = 0xFFFFFFFF
    SERVICE_CONFIG_DESCRIPTION = 1
    SERVICE_CONFIG_FAILURE_ACTIONS = 2
    SERVICE_CONFIG_DELAYED_AUTO_START_INFO = 3
    SERVICE_CONFIG_FAILURE_ACTIONS_FLAG = 4
    SERVICE_CONFIG_SERVICE_SID_INFO = 5

    def __init__(self) -> None:
        self.manager = object()
        self.service = object()
        self.configuration: tuple[object, ...] = (
            0x10,
            2,
            1,
            f'"{EXPECTED_EXECUTABLE}"',
            "",
            0,
            (),
            SERVICE_ACCOUNT,
            "E-Rechnungs-Prüfer Dienst",
        )
        self.values: dict[int, object] = {
            self.SERVICE_CONFIG_DESCRIPTION: "Eigene Testbeschreibung",
            self.SERVICE_CONFIG_FAILURE_ACTIONS: {
                "ResetPeriod": 86400,
                "RebootMsg": "",
                "Command": "",
                "Actions": [(1, 1000), (0, 0)],
            },
            self.SERVICE_CONFIG_DELAYED_AUTO_START_INFO: True,
            self.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG: True,
            self.SERVICE_CONFIG_SERVICE_SID_INFO: 1,
        }
        self.OpenSCManager = Mock(return_value=self.manager)
        self.OpenService = Mock(return_value=self.service)
        self.CloseServiceHandle = Mock()
        self.QueryServiceConfig = Mock(side_effect=lambda _service: self.configuration)
        self.QueryServiceConfig2 = Mock(side_effect=lambda _service, level: self.values[level])
        self.ChangeServiceConfig = Mock()
        self.ChangeServiceConfig2 = Mock()
        self.QueryServiceStatus = Mock(return_value=(0, self.SERVICE_STOPPED, 0, 0, 0, 0, 0))


def _snapshot_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "service_name": SERVICE_NAME,
        "expected_executable": str(EXPECTED_EXECUTABLE),
        "service_account": SERVICE_ACCOUNT,
        "start_type": 2,
        "description": "Eigene Testbeschreibung",
        "delayed_start": True,
        "service_sid_type": 1,
        "failure_actions": {
            "ResetPeriod": 86400,
            "RebootMsg": "",
            "Command": "",
            "Actions": [[1, 1000], [0, 0]],
        },
        "failure_actions_flag": True,
    }


def _uninstall_record(*, service_was_running: bool = False) -> dict[str, object]:
    return {
        "schema_version": windows_service_metadata.UNINSTALL_RECORD_SCHEMA_VERSION,
        "service_metadata": _snapshot_payload(),
        "service_was_running": service_was_running,
    }


def test_snapshot_reads_all_metadata_through_scm_and_writes_private_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeServiceApi()
    written: dict[Path, bytes] = {}
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)
    state_directory = tmp_path / ".uninstaller-state"
    snapshot = tmp_path / "service-state.json"
    monkeypatch.setattr(
        windows_service_metadata,
        "_prepare_metadata_directory",
        lambda _expected: (state_directory, snapshot),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(None, ())),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_write_secure_snapshot",
        lambda path, data: written.setdefault(path, data),
    )
    monkeypatch.setattr(windows_service_metadata, "_read_secure_snapshot", lambda path: written[path])

    def publish(source: Path, destination: Path) -> None:
        written[destination] = written.pop(source)

    monkeypatch.setattr(windows_service_metadata, "_publish_secure_snapshot", publish)

    windows_service_metadata.snapshot_service_metadata(EXPECTED_EXECUTABLE)

    assert json.loads(written[snapshot]) == _uninstall_record()
    api.OpenService.assert_called_once_with(
        api.manager,
        SERVICE_NAME,
        api.SERVICE_QUERY_CONFIG | api.SERVICE_QUERY_STATUS,
    )
    assert api.QueryServiceConfig2.call_args_list == [
        call(api.service, api.SERVICE_CONFIG_DESCRIPTION),
        call(api.service, api.SERVICE_CONFIG_DELAYED_AUTO_START_INFO),
        call(api.service, api.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG),
        call(api.service, api.SERVICE_CONFIG_SERVICE_SID_INFO),
        call(api.service, api.SERVICE_CONFIG_FAILURE_ACTIONS),
    ]
    assert api.CloseServiceHandle.call_args_list == [call(api.service), call(api.manager)]


@pytest.mark.parametrize(
    ("image_path", "account", "message"),
    [
        (str(EXPECTED_EXECUTABLE), SERVICE_ACCOUNT, "Programmdateipfad"),
        (f'"{EXPECTED_EXECUTABLE}" --fremd', SERVICE_ACCOUNT, "Programmdateipfad"),
        (f'"{EXPECTED_EXECUTABLE}"', "LocalSystem", "LocalService"),
    ],
)
def test_snapshot_rejects_nonexact_service_provenance_before_writing(
    tmp_path: Path,
    image_path: str,
    account: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeServiceApi()
    configuration = list(api.configuration)
    configuration[3] = image_path
    configuration[7] = account
    api.configuration = tuple(configuration)
    writer = Mock()
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)
    monkeypatch.setattr(windows_service_metadata, "_write_secure_snapshot", writer)
    monkeypatch.setattr(
        windows_service_metadata,
        "_prepare_metadata_directory",
        Mock(return_value=(tmp_path, tmp_path / "service-metadata.json")),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(None, ())),
    )

    with pytest.raises(RuntimeError, match=message):
        windows_service_metadata.snapshot_service_metadata(EXPECTED_EXECUTABLE)

    writer.assert_not_called()


def test_restore_revalidates_owned_service_and_uses_only_scm_change_apis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(_uninstall_record()), encoding="utf-8")
    api = _FakeServiceApi()
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)
    monkeypatch.setattr(
        windows_service_metadata,
        "_require_metadata_directory",
        lambda _expected: (tmp_path, snapshot),
    )
    monkeypatch.setattr(windows_service_metadata, "_read_secure_snapshot", lambda _path: snapshot.read_bytes())
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(snapshot, ())),
    )
    clear = Mock()
    monkeypatch.setattr(windows_service_metadata, "clear_service_metadata", clear)

    windows_service_metadata.restore_service_metadata(EXPECTED_EXECUTABLE)

    api.OpenService.assert_called_once_with(
        api.manager,
        SERVICE_NAME,
        api.SERVICE_QUERY_CONFIG | api.SERVICE_CHANGE_CONFIG | api.SERVICE_START,
    )
    api.ChangeServiceConfig.assert_called_once_with(
        api.service,
        api.SERVICE_NO_CHANGE,
        2,
        api.SERVICE_NO_CHANGE,
        None,
        None,
        0,
        None,
        None,
        None,
        None,
    )
    assert api.ChangeServiceConfig2.call_args_list == [
        call(api.service, api.SERVICE_CONFIG_DESCRIPTION, "Eigene Testbeschreibung"),
        call(api.service, api.SERVICE_CONFIG_DELAYED_AUTO_START_INFO, True),
        call(api.service, api.SERVICE_CONFIG_SERVICE_SID_INFO, 1),
        call(
            api.service,
            api.SERVICE_CONFIG_FAILURE_ACTIONS,
            {
                "ResetPeriod": 86400,
                "RebootMsg": "",
                "Command": "",
                "Actions": [(1, 1000), (0, 0)],
            },
        ),
        call(api.service, api.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG, True),
    ]
    clear.assert_called_once_with(EXPECTED_EXECUTABLE)


def test_payload_restore_is_retryable_and_never_consumes_transaction_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeServiceApi()
    clear = Mock()
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)
    monkeypatch.setattr(windows_service_metadata, "clear_service_metadata", clear)

    windows_service_metadata.restore_service_metadata_payload(
        EXPECTED_EXECUTABLE,
        _snapshot_payload(),
    )
    windows_service_metadata.restore_service_metadata_payload(
        EXPECTED_EXECUTABLE,
        _snapshot_payload(),
    )

    assert api.ChangeServiceConfig.call_count == 2
    assert api.ChangeServiceConfig2.call_count == 10
    clear.assert_not_called()


def test_public_capture_returns_a_strict_detached_scm_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _FakeServiceApi()
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)

    baseline = windows_service_metadata.capture_service_metadata(EXPECTED_EXECUTABLE)

    assert baseline == _snapshot_payload()
    api.values[api.SERVICE_CONFIG_DESCRIPTION] = "Nachträgliche Änderung"
    assert baseline["description"] == "Eigene Testbeschreibung"


def test_restore_preserves_run_command_action_with_required_service_start_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _snapshot_payload()
    payload["failure_actions"] = {
        "ResetPeriod": 60,
        "RebootMsg": "",
        "Command": r"C:\Program Files\Admin Tool\recover.exe",
        "Actions": [[3, 250]],
    }
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": windows_service_metadata.UNINSTALL_RECORD_SCHEMA_VERSION,
                "service_metadata": payload,
                "service_was_running": False,
            }
        ),
        encoding="utf-8",
    )
    api = _FakeServiceApi()
    api.values[api.SERVICE_CONFIG_FAILURE_ACTIONS] = {
        "ResetPeriod": 60,
        "RebootMsg": "",
        "Command": r"C:\Program Files\Admin Tool\recover.exe",
        "Actions": [(3, 250)],
    }
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)
    monkeypatch.setattr(
        windows_service_metadata,
        "_require_metadata_directory",
        lambda _expected: (tmp_path, snapshot),
    )
    monkeypatch.setattr(windows_service_metadata, "_read_secure_snapshot", lambda _path: snapshot.read_bytes())
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(snapshot, ())),
    )
    monkeypatch.setattr(windows_service_metadata, "clear_service_metadata", Mock())

    windows_service_metadata.restore_service_metadata(EXPECTED_EXECUTABLE)

    api.OpenService.assert_called_once_with(
        api.manager,
        SERVICE_NAME,
        api.SERVICE_QUERY_CONFIG | api.SERVICE_CHANGE_CONFIG | api.SERVICE_START,
    )
    api.ChangeServiceConfig2.assert_any_call(
        api.service,
        api.SERVICE_CONFIG_FAILURE_ACTIONS,
        {
            "ResetPeriod": 60,
            "RebootMsg": "",
            "Command": r"C:\Program Files\Admin Tool\recover.exe",
            "Actions": [(3, 250)],
        },
    )


def test_restore_rejects_tampered_snapshot_before_opening_scm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _uninstall_record()
    assert isinstance(payload["service_metadata"], dict)
    payload["service_metadata"]["service_name"] = "ForeignService"
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    api = _FakeServiceApi()
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)
    monkeypatch.setattr(
        windows_service_metadata,
        "_require_metadata_directory",
        lambda _expected: (tmp_path, snapshot),
    )
    monkeypatch.setattr(windows_service_metadata, "_read_secure_snapshot", lambda _path: snapshot.read_bytes())
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(snapshot, ())),
    )

    with pytest.raises(RuntimeError, match="gehört nicht"):
        windows_service_metadata.restore_service_metadata(EXPECTED_EXECUTABLE)

    api.OpenSCManager.assert_not_called()


def test_restore_rejects_changed_service_provenance_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(_uninstall_record()), encoding="utf-8")
    api = _FakeServiceApi()
    configuration = list(api.configuration)
    configuration[7] = "LocalSystem"
    api.configuration = tuple(configuration)
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)
    monkeypatch.setattr(
        windows_service_metadata,
        "_require_metadata_directory",
        lambda _expected: (tmp_path, snapshot),
    )
    monkeypatch.setattr(windows_service_metadata, "_read_secure_snapshot", lambda _path: snapshot.read_bytes())
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(snapshot, ())),
    )

    with pytest.raises(RuntimeError, match="LocalService"):
        windows_service_metadata.restore_service_metadata(EXPECTED_EXECUTABLE)

    api.ChangeServiceConfig.assert_not_called()
    api.ChangeServiceConfig2.assert_not_called()


def test_service_snapshot_location_is_fixed_below_machine_installation() -> None:
    installation, state_directory, snapshot = windows_service_metadata._metadata_paths(EXPECTED_EXECUTABLE)

    assert str(installation) == r"C:\Program Files\E-Rechnungs-Pruefer-Dienst"
    assert state_directory.name == windows_service_metadata.UNINSTALLER_STATE_DIRECTORY_NAME
    assert snapshot.parent == state_directory
    assert snapshot.name == windows_service_metadata.SERVICE_METADATA_FILE_NAME


def test_snapshot_reuses_existing_protected_uninstall_record_without_comparing_mutated_scm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeServiceApi()
    snapshot = tmp_path / "service-metadata.json"
    stale = _snapshot_payload()
    stale["description"] = "Ursprünglicher Zustand vor der Deinstallation"
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)
    monkeypatch.setattr(
        windows_service_metadata,
        "_prepare_metadata_directory",
        lambda _expected: (tmp_path, snapshot),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(snapshot, ())),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_load_uninstall_record",
        Mock(return_value=(stale, True)),
    )
    writer = Mock()
    monkeypatch.setattr(windows_service_metadata, "_write_secure_snapshot", writer)

    windows_service_metadata.snapshot_service_metadata(EXPECTED_EXECUTABLE)

    writer.assert_not_called()


def test_administrative_snapshot_dacl_contains_only_system_and_administrators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAcl:
        def __init__(self) -> None:
            self.aces: list[tuple[int, int, int, str]] = []

        def AddAccessAllowedAceEx(self, revision: int, inheritance: int, mask: int, sid: str) -> None:
            self.aces.append((revision, inheritance, mask, sid))

    class FakeDescriptor:
        def __init__(self) -> None:
            self.owner: tuple[str, int] | None = None
            self.dacl: tuple[int, FakeAcl, int] | None = None
            self.control: tuple[int, int] | None = None

        def SetSecurityDescriptorOwner(self, owner: str, defaulted: int) -> None:
            self.owner = (owner, defaulted)

        def SetSecurityDescriptorDacl(self, present: int, dacl: FakeAcl, defaulted: int) -> None:
            self.dacl = (present, dacl, defaulted)

        def SetSecurityDescriptorControl(self, control: int, mask: int) -> None:
            self.control = (control, mask)

    class Attributes:
        SECURITY_DESCRIPTOR: object | None = None

    acl = FakeAcl()
    descriptor = FakeDescriptor()
    security = SimpleNamespace(
        ACL=Mock(return_value=acl),
        ACL_REVISION_DS=4,
        OBJECT_INHERIT_ACE=1,
        CONTAINER_INHERIT_ACE=2,
        SE_DACL_PROTECTED=0x1000,
        ConvertStringSidToSid=Mock(side_effect=lambda sid: sid),
        SECURITY_DESCRIPTOR=Mock(return_value=descriptor),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_windows_file_modules",
        lambda: (
            SimpleNamespace(SECURITY_ATTRIBUTES=Attributes),
            object(),
            object(),
            security,
            SimpleNamespace(FILE_ALL_ACCESS=0x0F),
        ),
    )

    attributes = windows_service_metadata._administrative_security_attributes(directory=False)

    assert [ace[3] for ace in acl.aces] == [
        windows_service_metadata.SYSTEM_SID,
        windows_service_metadata.ADMINISTRATORS_SID,
    ]
    assert all(ace[1] == 0 and ace[2] == 0x0F for ace in acl.aces)
    assert descriptor.owner == (windows_service_metadata.ADMINISTRATORS_SID, 0)
    assert descriptor.dacl == (1, acl, 0)
    assert attributes.SECURITY_DESCRIPTOR is descriptor


def test_snapshot_acl_verification_rejects_any_broad_user_ace(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDacl:
        @staticmethod
        def GetAceCount() -> int:
            return 3

        @staticmethod
        def GetAce(index: int) -> tuple[tuple[int, int], int, str]:
            return (
                ((0, 0), 0x0F, windows_service_metadata.SYSTEM_SID),
                ((0, 0), 0x0F, windows_service_metadata.ADMINISTRATORS_SID),
                ((0, 0), 0x0F, "S-1-5-32-545"),
            )[index]

    descriptor = SimpleNamespace(
        GetSecurityDescriptorOwner=Mock(return_value=windows_service_metadata.ADMINISTRATORS_SID),
        GetSecurityDescriptorDacl=Mock(return_value=FakeDacl()),
        GetSecurityDescriptorControl=Mock(return_value=(0x1000, 1)),
    )
    security = SimpleNamespace(
        DACL_SECURITY_INFORMATION=4,
        OWNER_SECURITY_INFORMATION=1,
        SE_FILE_OBJECT=1,
        SE_DACL_PROTECTED=0x1000,
        ACCESS_ALLOWED_ACE_TYPE=0,
        ConvertSidToStringSid=Mock(side_effect=lambda sid: sid),
        GetNamedSecurityInfo=Mock(return_value=descriptor),
        OBJECT_INHERIT_ACE=1,
        CONTAINER_INHERIT_ACE=2,
    )
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=True))
    monkeypatch.setattr(
        windows_service_metadata,
        "_windows_file_modules",
        lambda: (object(), object(), object(), security, SimpleNamespace(FILE_ALL_ACCESS=0x0F)),
    )

    with pytest.raises(RuntimeError, match="nicht erlaubte Windows-Berechtigung"):
        windows_service_metadata._verify_administrative_path(Path("snapshot.json"), directory=False)


def test_snapshot_file_is_created_exclusively_with_administrative_security(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / ".uninstaller-state" / "service-metadata.json"
    handle = object()
    file_api = SimpleNamespace(
        CreateFile=Mock(return_value=handle),
        WriteFile=Mock(),
        FlushFileBuffers=Mock(),
        CloseHandle=Mock(),
    )
    constants = SimpleNamespace(GENERIC_WRITE=1, CREATE_NEW=2, FILE_ATTRIBUTE_TEMPORARY=4)
    verify = Mock()
    monkeypatch.setattr(windows_service_metadata, "_verify_administrative_path", verify)
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=False))
    monkeypatch.setattr(
        windows_service_metadata,
        "_administrative_security_attributes",
        Mock(return_value="admins-and-system-only"),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_windows_file_modules",
        lambda: (object(), constants, file_api, object(), object()),
    )

    windows_service_metadata._write_secure_snapshot(snapshot, b"protected")

    file_api.CreateFile.assert_called_once_with(
        str(snapshot),
        constants.GENERIC_WRITE,
        0,
        "admins-and-system-only",
        constants.CREATE_NEW,
        constants.FILE_ATTRIBUTE_TEMPORARY,
        None,
    )
    file_api.WriteFile.assert_called_once_with(handle, b"protected")
    file_api.FlushFileBuffers.assert_called_once_with(handle)
    file_api.CloseHandle.assert_called_once_with(handle)
    assert verify.call_args_list == [
        call(snapshot.parent, directory=True),
        call(snapshot, directory=False),
    ]


def test_snapshot_is_published_with_write_through_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".uninstaller-state"
    temporary = state_directory / ".service-metadata.json.0123456789abcdef0123456789abcdef.tmp"
    snapshot = state_directory / "service-metadata.json"
    move = Mock()
    verify = Mock()
    monkeypatch.setattr(windows_service_metadata, "_verify_administrative_path", verify)
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=False))
    monkeypatch.setattr(
        windows_service_metadata,
        "_windows_file_modules",
        lambda: (object(), object(), SimpleNamespace(MoveFileEx=move), object(), object()),
    )

    windows_service_metadata._publish_secure_snapshot(temporary, snapshot)

    move.assert_called_once_with(
        str(temporary),
        str(snapshot),
        windows_service_metadata.MOVEFILE_WRITE_THROUGH,
    )
    assert verify.call_args_list == [
        call(state_directory, directory=True),
        call(temporary, directory=False),
        call(snapshot, directory=False),
    ]


def test_snapshot_rejects_running_service_without_restorable_sid_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _snapshot_payload()
    payload["service_sid_type"] = 0
    writer = Mock()
    monkeypatch.setattr(
        windows_service_metadata,
        "_prepare_metadata_directory",
        Mock(return_value=(tmp_path, tmp_path / "service-metadata.json")),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(None, ())),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "inspect_owned_service_metadata",
        Mock(return_value=(payload, True)),
    )
    monkeypatch.setattr(windows_service_metadata, "_write_secure_snapshot", writer)

    with pytest.raises(RuntimeError, match="dienstspezifischen SID"):
        windows_service_metadata.snapshot_service_metadata(EXPECTED_EXECUTABLE)

    writer.assert_not_called()


def test_service_metadata_rejects_reboot_failure_action_without_shutdown_privilege() -> None:
    payload = _snapshot_payload()
    payload["failure_actions"] = {
        "ResetPeriod": 60,
        "RebootMsg": "Neustart",
        "Command": "",
        "Actions": [[2, 1000]],
    }

    with pytest.raises(RuntimeError, match="Shutdown-Privileg"):
        windows_service_metadata.validate_service_metadata(EXPECTED_EXECUTABLE, payload)


def test_uninstall_reconcile_restores_metadata_and_original_running_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".uninstaller-state"
    state_directory.mkdir()
    snapshot = state_directory / "service-metadata.json"
    snapshot.write_bytes(b"record")
    baseline = _snapshot_payload()
    disabled = dict(baseline)
    disabled["start_type"] = 4
    inspect = Mock(side_effect=[(disabled, False), (baseline, True)])
    restore = Mock()
    start = Mock()
    clear = Mock()
    monkeypatch.setattr(
        windows_service_metadata,
        "_metadata_paths",
        Mock(return_value=(tmp_path, state_directory, snapshot)),
    )
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=True))
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(snapshot, ())),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_load_uninstall_record",
        Mock(return_value=(baseline, True)),
    )
    monkeypatch.setattr(windows_service_metadata, "inspect_owned_service_metadata", inspect)
    monkeypatch.setattr(windows_service_metadata, "restore_service_metadata_payload", restore)
    monkeypatch.setattr(windows_service_metadata, "_start_owned_service_and_wait", start)
    monkeypatch.setattr(windows_service_metadata, "clear_service_metadata", clear)

    windows_service_metadata.reconcile_service_uninstall(EXPECTED_EXECUTABLE)

    restore.assert_called_once_with(EXPECTED_EXECUTABLE, baseline)
    start.assert_called_once_with(EXPECTED_EXECUTABLE)
    clear.assert_called_once_with(EXPECTED_EXECUTABLE)


def test_uninstall_reconcile_accepts_completed_service_deletion_and_clears_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".uninstaller-state"
    state_directory.mkdir()
    snapshot = state_directory / "service-metadata.json"
    snapshot.write_bytes(b"record")
    clear = Mock()
    monkeypatch.setattr(
        windows_service_metadata,
        "_metadata_paths",
        Mock(return_value=(tmp_path, state_directory, snapshot)),
    )
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=True))
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(snapshot, ())),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_load_uninstall_record",
        Mock(return_value=(_snapshot_payload(), True)),
    )
    monkeypatch.setattr(windows_service_metadata, "inspect_owned_service_metadata", Mock(return_value=None))
    monkeypatch.setattr(windows_service_metadata, "clear_service_metadata", clear)

    windows_service_metadata.reconcile_service_uninstall(EXPECTED_EXECUTABLE)

    clear.assert_called_once_with(EXPECTED_EXECUTABLE)


def test_uninstall_reconcile_rejects_unrelated_scm_drift_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".uninstaller-state"
    state_directory.mkdir()
    snapshot = state_directory / "service-metadata.json"
    snapshot.write_bytes(b"record")
    baseline = _snapshot_payload()
    drifted = dict(baseline)
    drifted["description"] = "Extern geändert"
    restore = Mock()
    clear = Mock()
    monkeypatch.setattr(
        windows_service_metadata,
        "_metadata_paths",
        Mock(return_value=(tmp_path, state_directory, snapshot)),
    )
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=True))
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(snapshot, ())),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_load_uninstall_record",
        Mock(return_value=(baseline, True)),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "inspect_owned_service_metadata",
        Mock(return_value=(drifted, False)),
    )
    monkeypatch.setattr(windows_service_metadata, "restore_service_metadata_payload", restore)
    monkeypatch.setattr(windows_service_metadata, "clear_service_metadata", clear)

    with pytest.raises(RuntimeError, match="Deinstallationsbaseline"):
        windows_service_metadata.reconcile_service_uninstall(EXPECTED_EXECUTABLE)

    restore.assert_not_called()
    clear.assert_not_called()


def test_uninstall_reconcile_removes_only_a_temp_only_prepublication_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".uninstaller-state"
    state_directory.mkdir()
    temporary = state_directory / ".service-metadata.json.0123456789abcdef0123456789abcdef.tmp"
    temporary.write_bytes(b"partial")
    monkeypatch.setattr(
        windows_service_metadata,
        "_metadata_paths",
        Mock(return_value=(tmp_path, state_directory, state_directory / "service-metadata.json")),
    )
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=True))
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(None, (temporary,))),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_delete_verified_metadata_file",
        lambda path: path.unlink(),
    )

    windows_service_metadata.reconcile_service_uninstall(EXPECTED_EXECUTABLE)

    assert not state_directory.exists()


def test_installation_is_blocked_by_any_validated_uninstall_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".uninstaller-state"
    state_directory.mkdir()
    inventory = Mock(return_value=(state_directory / "service-metadata.json", ()))
    monkeypatch.setattr(
        windows_service_metadata,
        "_metadata_paths",
        Mock(return_value=(tmp_path, state_directory, state_directory / "service-metadata.json")),
    )
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=True))
    monkeypatch.setattr(windows_service_metadata, "_inventory_metadata_directory", inventory)

    with pytest.raises(RuntimeError, match="Deinstallation.*nicht abgeschlossen"):
        windows_service_metadata.assert_no_pending_service_uninstall(EXPECTED_EXECUTABLE)

    inventory.assert_called_once_with(state_directory)


def test_disable_delayed_start_uses_scm_and_verifies_result(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _FakeServiceApi()
    api.QueryServiceConfig2 = Mock(return_value=False)
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)

    windows_service_metadata.disable_service_delayed_start(EXPECTED_EXECUTABLE)

    api.OpenService.assert_called_once_with(
        api.manager,
        SERVICE_NAME,
        api.SERVICE_QUERY_CONFIG | api.SERVICE_CHANGE_CONFIG,
    )
    api.ChangeServiceConfig2.assert_called_once_with(
        api.service,
        api.SERVICE_CONFIG_DELAYED_AUTO_START_INFO,
        False,
    )
    api.QueryServiceConfig2.assert_called_once_with(
        api.service,
        api.SERVICE_CONFIG_DELAYED_AUTO_START_INFO,
    )


def test_machine_preflight_accepts_clean_state_without_creating_programdata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program_data = tmp_path / "ProgramData"
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    monkeypatch.setattr(windows_service_config, "_windows_program_data_directory", lambda: program_data)
    acl = Mock()
    monkeypatch.setattr(windows_service_preflight, "WindowsServiceAcl", Mock(return_value=acl))
    validate = Mock(wraps=windows_service_preflight.validate_machine_path)
    monkeypatch.setattr(windows_service_preflight, "validate_machine_path", validate)

    state = windows_service_preflight.inspect_machine_state()

    assert state == windows_service_preflight.MachinePreflight(ServiceConfiguration(), False)
    assert not program_data.exists()
    validate.assert_called_once_with(
        program_data / windows_service_config.SERVICE_DATA_DIRECTORY_NAME,
        directory=True,
    )
    acl.verify_service_paths.assert_not_called()


def test_machine_preflight_rejects_redirected_programdata_parent_without_product_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program_data = tmp_path / "ProgramData"
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setattr(windows_service_config, "_windows_program_data_directory", lambda: program_data)
    validate = Mock(side_effect=RuntimeError("Reparse-Point oder Junction"))
    monkeypatch.setattr(windows_service_preflight, "validate_machine_path", validate)
    acl_factory = Mock()
    monkeypatch.setattr(windows_service_preflight, "WindowsServiceAcl", acl_factory)

    with pytest.raises(RuntimeError, match="Reparse-Point oder Junction"):
        windows_service_preflight.inspect_machine_state()

    validate.assert_called_once_with(
        program_data / windows_service_config.SERVICE_DATA_DIRECTORY_NAME,
        directory=True,
    )
    assert not program_data.exists()
    acl_factory.assert_not_called()


def test_machine_inspection_reads_canonical_retained_state_without_content_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "ProgramData" / "E-Rechnungs-Pruefer"
    logs = data / "logs"
    logs.mkdir(parents=True)
    configuration = ServiceConfiguration(port=18080, kosit_enabled=False, kosit_timeout_seconds=42)
    configuration_path = data / "service.json"
    token_path = data / "api-token.txt"
    log_path = logs / "service.log"
    rotated_log_path = logs / "service.log.1"
    configuration_path.write_text(
        json.dumps(
            {
                "schema_version": configuration.schema_version,
                "port": configuration.port,
                "kosit_enabled": configuration.kosit_enabled,
                "kosit_timeout_seconds": configuration.kosit_timeout_seconds,
            }
        ),
        encoding="utf-8",
    )
    token_path.write_text("t" * 43 + "\n", encoding="ascii")
    log_path.write_bytes(b"retained active log\n")
    rotated_log_path.write_bytes(b"retained rotated log\n")
    paths = ServicePaths(
        data_directory=data,
        configuration=configuration_path,
        token=token_path,
        log=log_path,
    )
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (configuration_path, token_path, log_path, rotated_log_path)
    }
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_service_preflight.ServicePaths,
        "from_environment",
        classmethod(lambda _cls: paths),
    )
    acl = Mock(name="acl_accepting_canonical_retained_state")
    acl_factory = Mock(return_value=acl)
    monkeypatch.setattr(windows_service_preflight, "WindowsServiceAcl", acl_factory)

    state = windows_service_preflight.inspect_machine_state()

    assert state == windows_service_preflight.MachinePreflight(configuration, True)
    acl_factory.assert_called_once_with()
    assert acl.mock_calls == [
        call.verify_service_paths(paths),
        call.verify_service_paths(paths),
    ]
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (configuration_path, token_path, log_path, rotated_log_path)
    } == before
    assert sorted(path.name for path in logs.iterdir()) == ["service.log", "service.log.1"]


def test_machine_preflight_repairs_direct_admin_directory_aces_before_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = ServicePaths(
        data_directory=tmp_path / "ProgramData" / "E-Rechnungs-Pruefer",
        configuration=tmp_path / "ProgramData" / "E-Rechnungs-Pruefer" / "service.json",
        token=tmp_path / "ProgramData" / "E-Rechnungs-Pruefer" / "api-token.txt",
        log=tmp_path / "ProgramData" / "E-Rechnungs-Pruefer" / "logs" / "service.log",
    )
    paths.data_directory.mkdir(parents=True)
    acl = Mock()
    inspection = Mock(return_value=windows_service_preflight.MachinePreflight(ServiceConfiguration(), True))
    operations = Mock()
    operations.attach_mock(acl.repair_explorer_directory_aces, "repair")
    operations.attach_mock(inspection, "inspect")
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_service_preflight.ServicePaths,
        "from_environment",
        classmethod(lambda _cls: paths),
    )
    monkeypatch.setattr(windows_service_preflight, "WindowsServiceAcl", Mock(return_value=acl))
    monkeypatch.setattr(windows_service_preflight, "inspect_machine_state", inspection)

    windows_service_preflight.preflight_machine()

    assert operations.mock_calls == [call.repair(paths), call.inspect()]


def test_machine_preflight_rejects_partial_state_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "ProgramData" / "E-Rechnungs-Pruefer"
    data.mkdir(parents=True)
    configuration = data / "service.json"
    configuration.write_text("{}", encoding="utf-8")
    before = configuration.read_bytes()
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    monkeypatch.setattr(
        windows_service_config,
        "_windows_program_data_directory",
        lambda: tmp_path / "ProgramData",
    )

    with pytest.raises(RuntimeError, match="unvollständig"):
        windows_service_preflight.inspect_machine_state()

    assert configuration.read_bytes() == before


def _purge_test_paths(tmp_path: Path) -> ServicePaths:
    data = tmp_path / "ProgramData" / "E-Rechnungs-Pruefer"
    return ServicePaths(
        data_directory=data,
        configuration=data / "service.json",
        token=data / "api-token.txt",
        log=data / "logs" / "service.log",
    )


def test_machine_purge_revalidates_and_removes_only_known_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _purge_test_paths(tmp_path)
    paths.log.parent.mkdir(parents=True)
    paths.configuration.write_text("{}", encoding="utf-8")
    paths.token.write_text("t" * 43, encoding="ascii")
    paths.log.write_text("log", encoding="utf-8")
    rotated = paths.log.with_name("service.log.1")
    rotated.write_text("old", encoding="utf-8")
    runtime_run = paths.runtime_directory / f"einvoice-kosit-{'a' * 32}"
    report_directory = runtime_run / "reports"
    report_directory.mkdir(parents=True)
    runtime_invoice = runtime_run / "invoice.xml"
    runtime_report = report_directory / "invoice-report.xml"
    runtime_invoice.write_bytes(b"<Invoice/>")
    runtime_report.write_bytes(b"<report/>")
    acl = Mock()
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_service_preflight.ServicePaths,
        "from_environment",
        classmethod(lambda _cls: paths),
    )
    stopped = Mock()
    monkeypatch.setattr(windows_service_preflight, "require_service_stopped_or_absent", stopped)
    monkeypatch.setattr(windows_service_preflight, "WindowsServiceAcl", Mock(return_value=acl))

    windows_service_preflight.purge_machine_state()

    assert not paths.data_directory.exists()
    stopped.assert_called_once_with()
    acl.verify_existing_service_paths.assert_called_once_with(paths, include_log_file=False)
    for operation in (acl.verify_log_for_purge, acl.protect_log, acl.verify_log):
        assert operation.call_count == 2
        operation.assert_has_calls([call(paths.log), call(rotated)], any_order=True)
    assert acl.verify_runtime_directory.call_count == 4
    assert acl.verify_runtime_entry_for_purge.call_count == 6


def test_runtime_purge_removes_only_fully_inventoried_kosit_crash_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _purge_test_paths(tmp_path)
    run = paths.runtime_directory / f"einvoice-kosit-{'b' * 32}"
    reports = run / "reports"
    reports.mkdir(parents=True)
    invoice = run / "rechnung.xml"
    report = reports / "rechnung-report.xml"
    invoice.write_bytes(b"<Invoice/>")
    report.write_bytes(b"<report/>")
    acl = Mock()
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_service_preflight.ServicePaths,
        "from_environment",
        classmethod(lambda _cls: paths),
    )
    stopped = Mock()
    monkeypatch.setattr(windows_service_preflight, "require_service_stopped_or_absent", stopped)
    monkeypatch.setattr(windows_service_preflight, "WindowsServiceAcl", Mock(return_value=acl))

    windows_service_preflight.purge_runtime_state()

    assert paths.data_directory.is_dir()
    assert not paths.runtime_directory.exists()
    stopped.assert_called_once_with()
    acl.repair_explorer_directory_aces.assert_called_once_with(paths)
    acl.verify_data_directory.assert_called_once_with(paths.data_directory)
    assert acl.verify_runtime_directory.call_count == 4
    acl.verify_runtime_entry_for_purge.assert_has_calls(
        [
            call(reports, directory=True),
            call(report, directory=False),
            call(invoice, directory=False),
            call(invoice, directory=False),
            call(report, directory=False),
            call(reports, directory=True),
        ],
        any_order=True,
    )


def test_runtime_purge_does_not_revalidate_retained_state_when_runtime_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _purge_test_paths(tmp_path)
    paths.data_directory.mkdir(parents=True)
    paths.configuration.write_text("{}", encoding="utf-8")
    paths.token.write_text("t" * 43, encoding="ascii")
    acl_factory = Mock()
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_service_preflight.ServicePaths,
        "from_environment",
        classmethod(lambda _cls: paths),
    )
    stopped = Mock()
    monkeypatch.setattr(windows_service_preflight, "require_service_stopped_or_absent", stopped)
    monkeypatch.setattr(windows_service_preflight, "WindowsServiceAcl", acl_factory)

    windows_service_preflight.purge_runtime_state()

    stopped.assert_called_once_with()
    acl_factory.assert_not_called()
    assert paths.configuration.exists()
    assert paths.token.exists()


def test_runtime_purge_rejects_unknown_run_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _purge_test_paths(tmp_path)
    unknown = paths.runtime_directory / "foreign"
    unknown.mkdir(parents=True)
    marker = unknown / "keep.xml"
    marker.write_bytes(b"keep")
    acl = Mock()
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_service_preflight.ServicePaths,
        "from_environment",
        classmethod(lambda _cls: paths),
    )
    monkeypatch.setattr(windows_service_preflight, "require_service_stopped_or_absent", Mock())
    monkeypatch.setattr(windows_service_preflight, "WindowsServiceAcl", Mock(return_value=acl))

    with pytest.raises(RuntimeError, match="unbekannten Eintrag"):
        windows_service_preflight.purge_runtime_state()

    assert marker.read_bytes() == b"keep"
    acl.verify_runtime_entry_for_purge.assert_not_called()


def test_runtime_purge_rejects_reparse_entry_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _purge_test_paths(tmp_path)
    run = paths.runtime_directory / f"einvoice-kosit-{'c' * 32}"
    run.mkdir(parents=True)
    outside = tmp_path / "outside.xml"
    outside.write_bytes(b"keep")
    redirected = run / "invoice.xml"
    redirected.symlink_to(outside)
    acl = Mock()
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_service_preflight.ServicePaths,
        "from_environment",
        classmethod(lambda _cls: paths),
    )
    monkeypatch.setattr(windows_service_preflight, "require_service_stopped_or_absent", Mock())
    monkeypatch.setattr(windows_service_preflight, "WindowsServiceAcl", Mock(return_value=acl))

    with pytest.raises(RuntimeError, match="Reparse-Point oder Junction"):
        windows_service_preflight.purge_runtime_state()

    assert redirected.is_symlink()
    assert outside.read_bytes() == b"keep"


def test_machine_purge_resumes_exact_atomic_configuration_and_token_write_tails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _purge_test_paths(tmp_path)
    paths.data_directory.mkdir(parents=True)
    configuration_tail = paths.data_directory / ".service.json.0123456789abcdef.tmp"
    token_tail = paths.data_directory / ".api-token.txt.fedcba9876543210.tmp"
    configuration_tail.write_bytes(b'{"schema_version":')
    token_tail.write_bytes(b"partial-token")
    acl = Mock()
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_service_preflight.ServicePaths,
        "from_environment",
        classmethod(lambda _cls: paths),
    )
    monkeypatch.setattr(windows_service_preflight, "require_service_stopped_or_absent", Mock())
    monkeypatch.setattr(windows_service_preflight, "WindowsServiceAcl", Mock(return_value=acl))

    windows_service_preflight.purge_machine_state()

    assert not paths.data_directory.exists()
    assert acl.verify_configuration.call_count == 2
    assert acl.verify_token.call_count == 2
    acl.verify_configuration.assert_has_calls([call(configuration_tail), call(configuration_tail)])
    acl.verify_token.assert_has_calls([call(token_tail), call(token_tail)])


def test_machine_purge_repairs_supported_explorer_ace_when_runtime_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _purge_test_paths(tmp_path)
    paths.data_directory.mkdir(parents=True)
    paths.configuration.write_text("{}", encoding="utf-8")
    paths.token.write_text("t" * 43, encoding="ascii")
    acl = Mock()
    operations = Mock()
    operations.attach_mock(acl.repair_explorer_directory_aces, "repair")
    operations.attach_mock(acl.verify_existing_service_paths, "verify")
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_service_preflight.ServicePaths,
        "from_environment",
        classmethod(lambda _cls: paths),
    )
    monkeypatch.setattr(windows_service_preflight, "require_service_stopped_or_absent", Mock())
    monkeypatch.setattr(windows_service_preflight, "WindowsServiceAcl", Mock(return_value=acl))

    windows_service_preflight.purge_machine_state()

    assert not paths.data_directory.exists()
    assert operations.mock_calls == [
        call.repair(paths),
        call.verify(paths, include_log_file=False),
    ]


@pytest.mark.parametrize(
    "name",
    [
        ".service.json.short.tmp",
        ".service.json.0123456789ABCDEf.tmp",
        ".service.json.0123456789abcdef.tmp.extra",
        ".api-token.txt.0123456789abcdeg.tmp",
        ".foreign.0123456789abcdef.tmp",
    ],
)
def test_machine_purge_rejects_near_match_atomic_write_tails_without_deletion(
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _purge_test_paths(tmp_path)
    paths.data_directory.mkdir(parents=True)
    candidate = paths.data_directory / name
    candidate.write_bytes(b"keep")
    acl_factory = Mock()
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_service_preflight.ServicePaths,
        "from_environment",
        classmethod(lambda _cls: paths),
    )
    monkeypatch.setattr(windows_service_preflight, "require_service_stopped_or_absent", Mock())
    monkeypatch.setattr(windows_service_preflight, "WindowsServiceAcl", acl_factory)

    with pytest.raises(RuntimeError, match="unbekannte Einträge"):
        windows_service_preflight.purge_machine_state()

    assert candidate.read_bytes() == b"keep"
    acl_factory.assert_not_called()


def test_machine_purge_rejects_unknown_entry_before_any_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _purge_test_paths(tmp_path)
    paths.data_directory.mkdir(parents=True)
    paths.configuration.write_text("{}", encoding="utf-8")
    unknown = paths.data_directory / "foreign.bin"
    unknown.write_bytes(b"keep")
    acl_factory = Mock()
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_service_preflight.ServicePaths,
        "from_environment",
        classmethod(lambda _cls: paths),
    )
    monkeypatch.setattr(windows_service_preflight, "require_service_stopped_or_absent", Mock())
    monkeypatch.setattr(windows_service_preflight, "WindowsServiceAcl", acl_factory)

    with pytest.raises(RuntimeError, match="unbekannte Einträge"):
        windows_service_preflight.purge_machine_state()

    assert paths.configuration.exists()
    assert unknown.read_bytes() == b"keep"
    acl_factory.assert_not_called()


def test_machine_purge_rejects_redirected_log_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _purge_test_paths(tmp_path)
    paths.log.parent.mkdir(parents=True)
    paths.configuration.write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.log"
    outside.write_text("keep", encoding="utf-8")
    paths.log.symlink_to(outside)
    acl = Mock()
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_service_preflight.ServicePaths,
        "from_environment",
        classmethod(lambda _cls: paths),
    )
    monkeypatch.setattr(windows_service_preflight, "require_service_stopped_or_absent", Mock())
    monkeypatch.setattr(windows_service_preflight, "WindowsServiceAcl", Mock(return_value=acl))

    with pytest.raises(RuntimeError, match="Reparse-Point oder Junction"):
        windows_service_preflight.purge_machine_state()

    assert paths.configuration.exists()
    assert outside.read_text(encoding="utf-8") == "keep"
    acl.protect_log.assert_not_called()
    assert not paths.token.exists()


def test_machine_purge_checks_service_before_resolving_or_touching_programdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_factory = Mock()
    monkeypatch.setattr(windows_service_preflight.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_service_preflight.ServicePaths,
        "from_environment",
        classmethod(lambda _cls: path_factory()),
    )
    monkeypatch.setattr(
        windows_service_preflight,
        "require_service_stopped_or_absent",
        Mock(side_effect=RuntimeError("Dienst läuft")),
    )

    with pytest.raises(RuntimeError, match="Dienst läuft"):
        windows_service_preflight.purge_machine_state()

    path_factory.assert_not_called()


def test_loopback_preflight_uses_exclusive_stdlib_socket_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = Mock()
    monkeypatch.setattr(
        windows_service_preflight,
        "inspect_machine_state",
        Mock(return_value=windows_service_preflight.MachinePreflight(ServiceConfiguration(port=18080), True)),
    )
    stopped = Mock()
    monkeypatch.setattr(windows_service_preflight, "require_service_stopped_or_absent", stopped)
    monkeypatch.setattr(windows_service_preflight.socket, "SO_EXCLUSIVEADDRUSE", 0x04, raising=False)
    monkeypatch.setattr(windows_service_preflight.socket, "socket", Mock(return_value=listener))

    windows_service_preflight.preflight_loopback_port()

    stopped.assert_called_once_with()
    listener.setsockopt.assert_called_once_with(socket.SOL_SOCKET, 0x04, 1)
    listener.bind.assert_called_once_with(("127.0.0.1", 18080))
    listener.listen.assert_called_once_with(1)
    listener.close.assert_called_once_with()


def test_loopback_preflight_reports_busy_port_and_still_closes_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = Mock()
    listener.bind.side_effect = OSError("busy")
    monkeypatch.setattr(
        windows_service_preflight,
        "inspect_machine_state",
        Mock(return_value=windows_service_preflight.MachinePreflight(ServiceConfiguration(), False)),
    )
    monkeypatch.setattr(windows_service_preflight, "require_service_stopped_or_absent", Mock())
    monkeypatch.setattr(windows_service_preflight.socket, "SO_EXCLUSIVEADDRUSE", 0x04, raising=False)
    monkeypatch.setattr(windows_service_preflight.socket, "socket", Mock(return_value=listener))

    with pytest.raises(RuntimeError, match="bereits belegt"):
        windows_service_preflight.preflight_loopback_port()

    listener.close.assert_called_once_with()


def test_port_preflight_requires_service_to_be_stopped_or_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _FakeServiceApi()
    monkeypatch.setattr(windows_service_preflight, "_win32service", lambda: api)

    windows_service_preflight.require_service_stopped_or_absent()

    api.OpenService.assert_called_once_with(api.manager, SERVICE_NAME, api.SERVICE_QUERY_STATUS)
    api.CloseServiceHandle.assert_has_calls([call(api.service), call(api.manager)])

    api.QueryServiceStatus.return_value = (0, 4, 0, 0, 0, 0, 0)
    with pytest.raises(RuntimeError, match="vollständig gestoppt"):
        windows_service_preflight.require_service_stopped_or_absent()


def test_open_client_routes_hidden_administrative_commands_without_message_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    elevated = Mock(return_value=True)
    monkeypatch.setattr(windows_open_client, "is_process_elevated", elevated)
    preflight = Mock()
    monkeypatch.setattr(windows_open_client, "preflight_machine", preflight)
    message = Mock()
    monkeypatch.setattr(windows_open_client, "_show_message", message)

    assert windows_open_client.main(["--preflight-machine"]) == 0
    elevated.assert_called_once_with()
    preflight.assert_called_once_with()
    message.assert_not_called()

    preflight.side_effect = RuntimeError("simulated")
    assert windows_open_client.main(["--preflight-machine"]) == 1
    message.assert_not_called()


def test_open_client_rejects_machine_preflight_without_elevation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(windows_open_client, "is_process_elevated", Mock(return_value=False))
    preflight = Mock()
    monkeypatch.setattr(windows_open_client, "preflight_machine", preflight)
    message = Mock()
    monkeypatch.setattr(windows_open_client, "_show_message", message)

    assert windows_open_client.main(["--preflight-machine"]) == 1
    preflight.assert_not_called()
    message.assert_not_called()


def test_open_client_routes_machine_purge_without_message_box(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(windows_open_client, "is_process_elevated", Mock(return_value=True))
    purge = Mock()
    monkeypatch.setattr(windows_open_client, "purge_machine_state", purge)
    message = Mock()
    monkeypatch.setattr(windows_open_client, "_show_message", message)

    assert windows_open_client.main(["--purge-machine-state"]) == 0
    purge.assert_called_once_with()
    message.assert_not_called()


def test_open_client_routes_runtime_purge_without_message_box(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(windows_open_client, "is_process_elevated", Mock(return_value=True))
    purge = Mock()
    monkeypatch.setattr(windows_open_client, "purge_runtime_state", purge)
    message = Mock()
    monkeypatch.setattr(windows_open_client, "_show_message", message)

    assert windows_open_client.main(["--purge-runtime-state"]) == 0
    purge.assert_called_once_with()
    message.assert_not_called()


def test_open_client_metadata_arguments_are_strict() -> None:
    with pytest.raises(SystemExit):
        windows_open_client._parse_arguments(["--snapshot-service-metadata"])
    with pytest.raises(SystemExit):
        windows_open_client._parse_arguments(["--preflight-machine", "--service-snapshot", "state.json"])


def test_open_client_routes_uninstall_reconcile_and_install_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(windows_open_client, "is_process_elevated", Mock(return_value=True))
    reconcile = Mock()
    guard = Mock()
    monkeypatch.setattr(windows_open_client, "reconcile_service_uninstall", reconcile)
    monkeypatch.setattr(windows_open_client, "assert_no_pending_service_uninstall", guard)

    assert (
        windows_open_client.main(
            [
                "--reconcile-service-uninstall",
                "--expected-service-exe",
                str(EXPECTED_EXECUTABLE),
            ]
        )
        == 0
    )
    assert (
        windows_open_client.main(
            [
                "--assert-no-pending-service-uninstall",
                "--expected-service-exe",
                str(EXPECTED_EXECUTABLE),
            ]
        )
        == 0
    )

    reconcile.assert_called_once_with(EXPECTED_EXECUTABLE)
    guard.assert_called_once_with(EXPECTED_EXECUTABLE)


def test_migration_context_rejects_elevated_original_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_open_client, "is_process_elevated", Mock(return_value=True))

    with pytest.raises(RuntimeError, match="Setup normal"):
        windows_open_client.verify_migration_context()

    monkeypatch.setattr(windows_open_client, "is_process_elevated", Mock(return_value=False))
    windows_open_client.verify_migration_context()


def test_process_elevation_uses_current_windows_token_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    check = Mock(return_value=1)
    shell32 = SimpleNamespace(IsUserAnAdmin=check)
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=shell32), raising=False)

    assert windows_open_client.is_process_elevated() is True
    assert check.argtypes == []
    assert check.restype is ctypes.c_bool


def test_early_preflight_does_not_import_full_server_runtime() -> None:
    source = Path(windows_service_preflight.__file__).read_text(encoding="utf-8")
    assert "server_runtime" not in source


@pytest.mark.parametrize(
    "value",
    [
        True,
        -1,
        0x1_0000_0000,
        "1",
    ],
)
def test_service_metadata_rejects_non_unsigned_integer_fields(value: object) -> None:
    payload = _snapshot_payload()
    payload["failure_actions"] = {
        "ResetPeriod": value,
        "RebootMsg": "",
        "Command": "",
        "Actions": [],
    }

    with pytest.raises(RuntimeError, match="ResetPeriod"):
        windows_service_metadata.validate_service_metadata(EXPECTED_EXECUTABLE, payload)


@pytest.mark.parametrize(
    ("failure_actions", "message"),
    [
        ({}, "ungültige Dienstfehleraktionen"),
        (
            {
                "ResetPeriod": 0,
                "RebootMsg": 1,
                "Command": "",
                "Actions": [],
            },
            "Textwert",
        ),
        (
            {
                "ResetPeriod": 0,
                "RebootMsg": "",
                "Command": "",
                "Actions": "restart",
            },
            "Fehleraktionsliste",
        ),
        (
            {
                "ResetPeriod": 0,
                "RebootMsg": "",
                "Command": "",
                "Actions": [[1]],
            },
            "ungültige Fehleraktion",
        ),
        (
            {
                "ResetPeriod": 0,
                "RebootMsg": "",
                "Command": "",
                "Actions": [[1, True]],
            },
            r"Actions\[0\]\.delay",
        ),
        (
            {
                "ResetPeriod": 0,
                "RebootMsg": None,
                "Command": "",
                "Actions": [],
            },
            "nicht kanonische",
        ),
    ],
)
def test_service_metadata_rejects_malformed_or_noncanonical_failure_actions(
    failure_actions: object,
    message: str,
) -> None:
    payload = _snapshot_payload()
    payload["failure_actions"] = failure_actions

    with pytest.raises(RuntimeError, match=message):
        windows_service_metadata.validate_service_metadata(EXPECTED_EXECUTABLE, payload)


@pytest.mark.parametrize(
    ("encoded", "message"),
    [
        (b"", "unzulässige Größe"),
        (b"\xff", "kein gültiges JSON"),
        (b"[]", "unbekanntes Format"),
        (b'{"schema_version":1,"schema_version":1}', "kein gültiges JSON"),
    ],
)
def test_snapshot_decoder_rejects_truncated_duplicate_or_nonobject_json(
    encoded: bytes,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        windows_service_metadata._decode_snapshot(encoded, EXPECTED_EXECUTABLE)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("description", 7, "Dienstbeschreibung"),
        ("delayed_start", 1, "boolesche"),
        ("failure_actions_flag", 0, "boolesche"),
        ("start_type", 1, "unzulässigen Dienststarttyp"),
        ("service_sid_type", 2, "unbekannten dienstspezifischen SID-Typ"),
    ],
)
def test_snapshot_decoder_rejects_invalid_typed_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _snapshot_payload()
    payload[field] = value
    encoded = json.dumps(payload).encode()

    with pytest.raises(RuntimeError, match=message):
        windows_service_metadata._decode_snapshot(encoded, EXPECTED_EXECUTABLE)


@pytest.mark.parametrize(
    ("encoded", "message"),
    [
        (b"", "unzulässige Größe"),
        (b"{", "kein gültiges JSON"),
        (b"[]", "unbekanntes Format"),
        (
            json.dumps(
                {
                    "schema_version": True,
                    "service_metadata": {},
                    "service_was_running": False,
                }
            ).encode(),
            "ungültige Zustandsdaten",
        ),
        (
            json.dumps(
                {
                    "schema_version": windows_service_metadata.UNINSTALL_RECORD_SCHEMA_VERSION,
                    "service_metadata": [],
                    "service_was_running": False,
                }
            ).encode(),
            "ungültige Zustandsdaten",
        ),
        (
            json.dumps(
                {
                    "schema_version": windows_service_metadata.UNINSTALL_RECORD_SCHEMA_VERSION,
                    "service_metadata": {},
                    "service_was_running": 0,
                }
            ).encode(),
            "ungültige Zustandsdaten",
        ),
    ],
)
def test_uninstall_record_decoder_rejects_invalid_envelopes(
    encoded: bytes,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        windows_service_metadata._decode_uninstall_record(encoded, EXPECTED_EXECUTABLE)


def test_service_metadata_json_encoding_failure_is_reported_as_transaction_error() -> None:
    payload = _snapshot_payload()
    payload["description"] = {"not", "json"}

    with pytest.raises(RuntimeError, match="striktes JSON"):
        windows_service_metadata.validate_service_metadata(EXPECTED_EXECUTABLE, payload)


def test_service_handle_wrapper_closes_manager_when_opening_service_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeServiceApi()
    api.OpenService.side_effect = OSError("SCM failure")
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)

    with pytest.raises(OSError, match="SCM failure"):
        windows_service_metadata._with_service(access=api.SERVICE_QUERY_CONFIG, operation=Mock())

    api.CloseServiceHandle.assert_called_once_with(api.manager)


def test_metadata_inventory_accepts_only_one_final_and_one_atomic_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".uninstaller-state"
    state_directory.mkdir()
    snapshot = state_directory / windows_service_metadata.SERVICE_METADATA_FILE_NAME
    temporary = state_directory / ".service-metadata.json.0123456789abcdef0123456789abcdef.tmp"
    snapshot.write_bytes(b"final")
    temporary.write_bytes(b"tail")
    verify = Mock()
    monkeypatch.setattr(windows_service_metadata, "_verify_administrative_path", verify)

    assert windows_service_metadata._inventory_metadata_directory(state_directory) == (
        snapshot,
        (temporary,),
    )
    assert verify.call_args_list[0] == call(state_directory, directory=True)
    verify.assert_any_call(snapshot, directory=False)
    verify.assert_any_call(temporary, directory=False)
    assert verify.call_count == 3


@pytest.mark.parametrize(
    "names",
    [
        ("foreign.bin",),
        (
            ".service-metadata.json.0123456789abcdef0123456789abcdef.tmp",
            ".service-metadata.json.fedcba9876543210fedcba9876543210.tmp",
        ),
    ],
)
def test_metadata_inventory_rejects_unknown_or_multiple_atomic_tails(
    tmp_path: Path,
    names: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".uninstaller-state"
    state_directory.mkdir()
    for name in names:
        (state_directory / name).write_bytes(b"state")
    monkeypatch.setattr(windows_service_metadata, "_verify_administrative_path", Mock())

    with pytest.raises(RuntimeError, match="unbekannten Eintrag|mehrere temporäre"):
        windows_service_metadata._inventory_metadata_directory(state_directory)


def test_metadata_directory_is_created_with_final_security_attributes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".uninstaller-state"
    snapshot = state_directory / windows_service_metadata.SERVICE_METADATA_FILE_NAME
    create = Mock()
    verify = Mock()
    monkeypatch.setattr(
        windows_service_metadata,
        "_validate_metadata_base",
        Mock(return_value=(state_directory, snapshot)),
    )
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=False))
    monkeypatch.setattr(windows_service_metadata, "_verify_administrative_path", verify)
    monkeypatch.setattr(
        windows_service_metadata,
        "_administrative_security_attributes",
        Mock(return_value="protected"),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_windows_file_modules",
        Mock(return_value=(object(), object(), SimpleNamespace(CreateDirectoryW=create), object(), object())),
    )

    assert windows_service_metadata._prepare_metadata_directory(EXPECTED_EXECUTABLE) == (
        state_directory,
        snapshot,
    )
    create.assert_called_once_with(str(state_directory), "protected")
    verify.assert_called_once_with(state_directory, directory=True)


def test_metadata_directory_creation_failure_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".uninstaller-state"
    snapshot = state_directory / windows_service_metadata.SERVICE_METADATA_FILE_NAME
    create = Mock(side_effect=OSError("denied"))
    monkeypatch.setattr(
        windows_service_metadata,
        "_validate_metadata_base",
        Mock(return_value=(state_directory, snapshot)),
    )
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=False))
    monkeypatch.setattr(windows_service_metadata, "_administrative_security_attributes", Mock(return_value=object()))
    monkeypatch.setattr(
        windows_service_metadata,
        "_windows_file_modules",
        Mock(return_value=(object(), object(), SimpleNamespace(CreateDirectoryW=create), object(), object())),
    )

    with pytest.raises(RuntimeError, match="nicht sicher erstellt"):
        windows_service_metadata._prepare_metadata_directory(EXPECTED_EXECUTABLE)


@pytest.mark.parametrize("close_fails", [False, True])
def test_secure_snapshot_write_failure_closes_handle_and_remains_retryable(
    tmp_path: Path,
    close_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / ".service-metadata.json.0123456789abcdef0123456789abcdef.tmp"
    snapshot.write_bytes(b"partial")
    file_api = SimpleNamespace(
        CreateFile=Mock(return_value=object()),
        WriteFile=Mock(side_effect=None if close_fails else OSError("disk full")),
        FlushFileBuffers=Mock(),
        CloseHandle=Mock(side_effect=OSError("close failed") if close_fails else None),
    )
    constants = SimpleNamespace(GENERIC_WRITE=1, CREATE_NEW=2, FILE_ATTRIBUTE_TEMPORARY=4)
    monkeypatch.setattr(windows_service_metadata, "_verify_administrative_path", Mock())
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=False))
    monkeypatch.setattr(
        windows_service_metadata,
        "_administrative_security_attributes",
        Mock(return_value=object()),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_windows_file_modules",
        Mock(return_value=(object(), constants, file_api, object(), object())),
    )

    with pytest.raises(RuntimeError, match="konnte nicht geschrieben"):
        windows_service_metadata._write_secure_snapshot(snapshot, b"state")

    file_api.CloseHandle.assert_called_once()
    assert not snapshot.exists()


def test_secure_snapshot_publish_rejects_wrong_parent_and_move_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "one" / ".service-metadata.json.0123456789abcdef0123456789abcdef.tmp"
    destination = tmp_path / "two" / windows_service_metadata.SERVICE_METADATA_FILE_NAME

    with pytest.raises(RuntimeError, match="festen Zustandsverzeichnis"):
        windows_service_metadata._publish_secure_snapshot(source, destination)

    source = destination.parent / source.name
    monkeypatch.setattr(windows_service_metadata, "_verify_administrative_path", Mock())
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=False))
    monkeypatch.setattr(
        windows_service_metadata,
        "_windows_file_modules",
        Mock(
            return_value=(
                object(),
                object(),
                SimpleNamespace(MoveFileEx=Mock(side_effect=OSError("rename failed"))),
                object(),
                object(),
            )
        ),
    )

    with pytest.raises(RuntimeError, match="atomar veröffentlicht"):
        windows_service_metadata._publish_secure_snapshot(source, destination)


def test_secure_snapshot_read_failure_is_reported_after_acl_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "service-metadata.json"
    snapshot.mkdir()
    verify = Mock()
    monkeypatch.setattr(windows_service_metadata, "_verify_administrative_path", verify)

    with pytest.raises(RuntimeError, match="konnte nicht gelesen"):
        windows_service_metadata._read_secure_snapshot(snapshot)

    assert verify.call_args_list == [
        call(snapshot.parent, directory=True),
        call(snapshot, directory=False),
    ]


@pytest.mark.parametrize(
    ("status", "expected", "message"),
    [
        ((0, _FakeServiceApi.SERVICE_RUNNING), True, None),
        ((0, _FakeServiceApi.SERVICE_STOPPED), False, None),
        ((0,), None, "unbekannten Dienststatus"),
        ((0, 2), None, "nicht stabil"),
    ],
)
def test_owned_service_inspection_requires_stable_status(
    status: tuple[int, ...],
    expected: bool | None,
    message: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeServiceApi()
    api.QueryServiceStatus.return_value = status
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)

    if message is not None:
        with pytest.raises(RuntimeError, match=message):
            windows_service_metadata.inspect_owned_service_metadata(EXPECTED_EXECUTABLE)
    else:
        result = windows_service_metadata.inspect_owned_service_metadata(EXPECTED_EXECUTABLE)
        assert result == (_snapshot_payload(), expected)


def test_owned_service_inspection_treats_only_scm_missing_error_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ServiceMissingError(OSError):
        winerror = windows_service_metadata.ERROR_SERVICE_DOES_NOT_EXIST

    api = _FakeServiceApi()
    api.OpenService.side_effect = ServiceMissingError("missing")
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)

    assert windows_service_metadata.inspect_owned_service_metadata(EXPECTED_EXECUTABLE) is None


@pytest.mark.parametrize(
    ("observed", "message"),
    [
        (None, "fehlt vor Beginn"),
        (({**_snapshot_payload(), "start_type": 4}, True), "deaktiviertem Starttyp"),
    ],
)
def test_snapshot_rejects_missing_or_nonrollbackable_running_service(
    tmp_path: Path,
    observed: tuple[dict[str, object], bool] | None,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = Mock()
    monkeypatch.setattr(
        windows_service_metadata,
        "_prepare_metadata_directory",
        Mock(return_value=(tmp_path, tmp_path / windows_service_metadata.SERVICE_METADATA_FILE_NAME)),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(None, ())),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "inspect_owned_service_metadata",
        Mock(return_value=observed),
    )
    monkeypatch.setattr(windows_service_metadata, "_write_secure_snapshot", writer)

    with pytest.raises(RuntimeError, match=message):
        windows_service_metadata.snapshot_service_metadata(EXPECTED_EXECUTABLE)

    writer.assert_not_called()


def test_snapshot_rejects_changed_atomic_readback_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / windows_service_metadata.SERVICE_METADATA_FILE_NAME
    payload = _snapshot_payload()
    publish = Mock()
    monkeypatch.setattr(
        windows_service_metadata,
        "_prepare_metadata_directory",
        Mock(return_value=(tmp_path, snapshot)),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(None, ())),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "inspect_owned_service_metadata",
        Mock(return_value=(payload, False)),
    )
    monkeypatch.setattr(windows_service_metadata, "_write_secure_snapshot", Mock())
    monkeypatch.setattr(
        windows_service_metadata,
        "_read_secure_snapshot",
        Mock(
            return_value=windows_service_metadata._encode_uninstall_record(
                EXPECTED_EXECUTABLE, payload, service_was_running=True
            )
        ),
    )
    monkeypatch.setattr(windows_service_metadata, "_publish_secure_snapshot", publish)

    with pytest.raises(RuntimeError, match="unverändert zurückgelesen"):
        windows_service_metadata.snapshot_service_metadata(EXPECTED_EXECUTABLE)

    publish.assert_not_called()


@pytest.mark.parametrize(
    ("statuses", "message"),
    [
        ([(0, 2)], "nicht stabil gestoppt"),
        ([(0, _FakeServiceApi.SERVICE_STOPPED), (0,)], "unbekannten Dienststatus"),
    ],
)
def test_service_restart_rejects_unstable_or_malformed_status(
    statuses: list[tuple[int, ...]],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeServiceApi()
    api.StartService = Mock()
    api.QueryServiceStatus.side_effect = statuses
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)
    monkeypatch.setattr(windows_service_metadata.time, "monotonic", Mock(side_effect=[0.0, 0.1]))

    with pytest.raises(RuntimeError, match=message):
        windows_service_metadata._start_owned_service_and_wait(EXPECTED_EXECUTABLE)


def test_service_restart_starts_stopped_owned_service_and_waits_for_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeServiceApi()
    api.StartService = Mock()
    api.QueryServiceStatus.side_effect = [
        (0, api.SERVICE_STOPPED),
        (0, 2),
        (0, api.SERVICE_RUNNING),
    ]
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)
    monkeypatch.setattr(windows_service_metadata.time, "monotonic", Mock(side_effect=[0.0, 0.1, 0.2]))
    sleep = Mock()
    monkeypatch.setattr(windows_service_metadata.time, "sleep", sleep)

    windows_service_metadata._start_owned_service_and_wait(EXPECTED_EXECUTABLE)

    api.StartService.assert_called_once_with(api.service, None)
    sleep.assert_called_once_with(0.1)


def test_uninstall_reconcile_rejects_untrusted_state_before_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".uninstaller-state"
    state_directory.mkdir()
    inventory = Mock()
    monkeypatch.setattr(
        windows_service_metadata,
        "_metadata_paths",
        Mock(return_value=(tmp_path, state_directory, state_directory / "service-metadata.json")),
    )
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=False))
    monkeypatch.setattr(windows_service_metadata, "_inventory_metadata_directory", inventory)

    with pytest.raises(RuntimeError, match="kein vertrauenswürdiges"):
        windows_service_metadata.reconcile_service_uninstall(EXPECTED_EXECUTABLE)

    inventory.assert_not_called()


def test_uninstall_reconcile_rejects_externally_started_originally_stopped_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".uninstaller-state"
    state_directory.mkdir()
    snapshot = state_directory / windows_service_metadata.SERVICE_METADATA_FILE_NAME
    snapshot.write_bytes(b"record")
    baseline = _snapshot_payload()
    restore = Mock()
    monkeypatch.setattr(
        windows_service_metadata,
        "_metadata_paths",
        Mock(return_value=(tmp_path, state_directory, snapshot)),
    )
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=True))
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(snapshot, ())),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "_load_uninstall_record",
        Mock(return_value=(baseline, False)),
    )
    monkeypatch.setattr(
        windows_service_metadata,
        "inspect_owned_service_metadata",
        Mock(return_value=(baseline, True)),
    )
    monkeypatch.setattr(windows_service_metadata, "restore_service_metadata_payload", restore)

    with pytest.raises(RuntimeError, match="außerhalb der Deinstallation gestartet"):
        windows_service_metadata.reconcile_service_uninstall(EXPECTED_EXECUTABLE)

    restore.assert_not_called()


def test_metadata_clear_is_idempotent_and_reports_nonempty_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".uninstaller-state"
    snapshot = state_directory / windows_service_metadata.SERVICE_METADATA_FILE_NAME
    monkeypatch.setattr(
        windows_service_metadata,
        "_validate_metadata_base",
        Mock(return_value=(state_directory, snapshot)),
    )
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=False))

    windows_service_metadata.clear_service_metadata(EXPECTED_EXECUTABLE)

    state_directory.mkdir()
    (state_directory / "unexpected").write_bytes(b"keep")
    monkeypatch.setattr(windows_service_metadata, "validate_machine_path", Mock(return_value=True))
    monkeypatch.setattr(
        windows_service_metadata,
        "_inventory_metadata_directory",
        Mock(return_value=(None, ())),
    )
    with pytest.raises(RuntimeError, match="nicht leer entfernt"):
        windows_service_metadata.clear_service_metadata(EXPECTED_EXECUTABLE)


def test_disable_delayed_start_fails_when_scm_readback_does_not_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeServiceApi()
    api.QueryServiceConfig2 = Mock(return_value=True)
    monkeypatch.setattr(windows_service_metadata, "_win32service", lambda: api)

    with pytest.raises(RuntimeError, match="nicht verifiziert deaktiviert"):
        windows_service_metadata.disable_service_delayed_start(EXPECTED_EXECUTABLE)
