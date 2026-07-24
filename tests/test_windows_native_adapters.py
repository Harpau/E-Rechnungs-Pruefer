from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from app import windows_acl, windows_service_ipc, windows_sync
from app.windows_acl import WindowsServiceAcl
from app.windows_service_config import ServicePaths
from app.windows_service_ipc import BrowserPipeServer, IpcServerDiagnostic


class _WinError(Exception):
    def __init__(self, winerror: int) -> None:
        super().__init__(winerror)
        self.winerror = winerror


def test_ipc_server_diagnostic_discards_exception_message_and_payload() -> None:
    error = _WinError(windows_service_ipc.ERROR_PIPE_NOT_CONNECTED)
    error.args = ("token=must-never-be-logged", b'{"request":"secret"}')

    diagnostic = windows_service_ipc._safe_diagnostic("write-response", error)

    assert diagnostic == IpcServerDiagnostic(
        phase="write-response",
        exception_type="_WinError",
        winerror=windows_service_ipc.ERROR_PIPE_NOT_CONNECTED,
    )
    assert "must-never-be-logged" not in repr(diagnostic)
    assert "request" not in repr(diagnostic)


class _FakeDacl:
    def __init__(self, aces: list[tuple[tuple[int, int], int, str]] | None = None) -> None:
        self.aces = aces or []

    def AddAccessAllowedAceEx(self, revision: int, inheritance: int, mask: int, sid: str) -> None:
        self.aces.append(((revision, inheritance), mask, sid))

    def AddAccessAllowedAce(self, revision: int, mask: int, sid: str) -> None:
        self.aces.append(((revision, 0), mask, sid))

    def GetAceCount(self) -> int:
        return len(self.aces)

    def GetAce(self, index: int) -> tuple[tuple[int, int], int, str]:
        return self.aces[index]


class _FakeDescriptor:
    def __init__(
        self,
        dacl: _FakeDacl | None = None,
        *,
        protected: bool = True,
        owner: str = windows_acl.ADMINISTRATORS_SID,
    ) -> None:
        self.dacl = dacl
        self.protected = protected
        self.owner = owner
        self.set_dacl_calls: list[tuple[int, _FakeDacl, int]] = []
        self.set_control_calls: list[tuple[int, int]] = []
        self.set_owner_calls: list[tuple[str, int]] = []

    def SetSecurityDescriptorDacl(self, present: int, dacl: _FakeDacl, defaulted: int) -> None:
        self.set_dacl_calls.append((present, dacl, defaulted))
        self.dacl = dacl

    def SetSecurityDescriptorControl(self, control_bits: int, control_mask: int) -> None:
        self.set_control_calls.append((control_bits, control_mask))
        self.protected = bool(control_bits & control_mask & _FakeSecurity.SE_DACL_PROTECTED)

    def SetSecurityDescriptorOwner(self, owner: str, defaulted: int) -> None:
        self.set_owner_calls.append((owner, defaulted))
        self.owner = owner

    def GetSecurityDescriptorDacl(self) -> _FakeDacl | None:
        return self.dacl

    def GetSecurityDescriptorControl(self) -> tuple[int, int]:
        return (0x1000 if self.protected else 0, 1)

    def GetSecurityDescriptorOwner(self) -> str:
        return self.owner


class _FakeSecurity:
    ACL_REVISION = 2
    ACL_REVISION_DS = 4
    ACCESS_ALLOWED_ACE_TYPE = 0
    DACL_SECURITY_INFORMATION = 0x04
    CONTAINER_INHERIT_ACE = 0x02
    INHERITED_ACE = 0x10
    INHERIT_ONLY_ACE = 0x08
    NO_PROPAGATE_INHERIT_ACE = 0x04
    OBJECT_INHERIT_ACE = 0x01
    OWNER_SECURITY_INFORMATION = 0x01
    PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    SE_DACL_PROTECTED = 0x1000
    SE_FILE_OBJECT = 1

    def __init__(self, descriptor: _FakeDescriptor | None = None) -> None:
        self.descriptor = descriptor
        self.created_acls: list[_FakeDacl] = []
        self.created_descriptors: list[_FakeDescriptor] = []
        self.set_calls: list[tuple[object, ...]] = []

    def ACL(self) -> _FakeDacl:
        dacl = _FakeDacl()
        self.created_acls.append(dacl)
        return dacl

    def SECURITY_DESCRIPTOR(self) -> _FakeDescriptor:
        descriptor = _FakeDescriptor()
        self.created_descriptors.append(descriptor)
        return descriptor

    @staticmethod
    def ConvertStringSidToSid(sid: str) -> str:
        return sid

    @staticmethod
    def ConvertSidToStringSid(sid: str) -> str:
        return sid

    @staticmethod
    def LookupAccountName(_system: None, account: str) -> tuple[str, str, int]:
        if account == windows_acl.SERVICE_SID_ACCOUNT:
            return windows_acl.SERVICE_SID, "NT SERVICE", 5
        if account == "unknown":
            raise OSError("not found")
        return "S-1-5-21-reader", "TEST", 1

    @staticmethod
    def LookupAccountSid(_system: None, sid: str) -> tuple[str, str, int]:
        return "reader", "TEST", 1

    def SetNamedSecurityInfo(self, *args: object) -> None:
        self.set_calls.append(args)
        if self.descriptor is not None and args[3] is not None:
            self.descriptor.owner = str(args[3])

    def GetNamedSecurityInfo(self, *_args: object) -> _FakeDescriptor:
        if self.descriptor is None:
            raise OSError("missing descriptor")
        return self.descriptor


_ACL_CONSTANTS = SimpleNamespace(
    CONTAINER_INHERIT_ACE=0x02,
    FILE_ALL_ACCESS=0x0F,
    FILE_GENERIC_READ=0x01,
    GENERIC_ALL=0x10000000,
    GENERIC_READ=0x80000000,
    GENERIC_WRITE=0x40000000,
    OBJECT_INHERIT_ACE=0x01,
)


@pytest.mark.parametrize(
    "message,error",
    [
        (b"", "leer oder zu gro\u00df"),
        (b"\xff", "kein g\u00fcltiges JSON"),
        (b"{", "kein g\u00fcltiges JSON"),
        (b'{"action":"open","version":2}', "nicht zul\u00e4ssig"),
        (b'{"action":"open","version":1,"extra":true}', "nicht zul\u00e4ssig"),
    ],
)
def test_pipe_request_decoder_rejects_malformed_or_noncanonical_messages(message: bytes, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        windows_service_ipc.decode_open_request(message)


@pytest.mark.parametrize(
    "message,error",
    [
        (b"", "leer oder zu gro\u00df"),
        (b"\xff", "kein g\u00fcltiges JSON"),
        (b"[]", "unbekanntes Format"),
        (b'{"version":2,"url":"http://127.0.0.1:8080/"}', "unbekanntes Format"),
        (b'{"version":1,"url":"https://127.0.0.1:8080/"}', "keine zul\u00e4ssige Browseradresse"),
        (b'{"version":1,"url":7}', "keine zul\u00e4ssige Browseradresse"),
    ],
)
def test_pipe_response_decoder_rejects_malformed_or_non_loopback_messages(message: bytes, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        windows_service_ipc.decode_open_response(message)


def test_pipe_encoders_reject_invalid_or_oversized_urls() -> None:
    with pytest.raises(ValueError, match="Browseradresse"):
        windows_service_ipc.encode_open_response("http://localhost:8080/desktop/bootstrap?token=x")
    with pytest.raises(ValueError, match="Gr\u00f6\u00dfenbegrenzung"):
        windows_service_ipc.encode_open_response("http://127.0.0.1:8080/desktop/bootstrap?token=" + "x" * 5000)


def test_pipe_response_rejects_loopback_prefix_with_remote_userinfo_host() -> None:
    malicious = b'{"version":1,"url":"http://127.0.0.1:8080@evil.example/desktop/bootstrap?token=x"}'

    with pytest.raises(ValueError, match="keine zulässige Browseradresse"):
        windows_service_ipc.decode_open_response(malicious)


def test_pipe_client_rights_exclude_create_pipe_instance() -> None:
    assert windows_service_ipc.PIPE_CLIENT_ACCESS & 0x00000004 == 0


def test_pipe_security_descriptor_grants_full_access_only_to_service_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _FakeSecurity()

    class _Attributes:
        SECURITY_DESCRIPTOR: object | None = None

    pywintypes = SimpleNamespace(SECURITY_ATTRIBUTES=_Attributes)
    modules = (
        _ACL_CONSTANTS,
        pywintypes,
        object(),
        object(),
        object(),
        object(),
        security,
        object(),
    )
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)

    attributes = windows_service_ipc._pipe_security_attributes()

    dacl = security.created_acls[0]
    assert dacl.aces == [
        ((security.ACL_REVISION, 0), _ACL_CONSTANTS.GENERIC_ALL, "S-1-5-18"),
        ((security.ACL_REVISION, 0), _ACL_CONSTANTS.GENERIC_ALL, "S-1-5-32-544"),
        ((security.ACL_REVISION, 0), _ACL_CONSTANTS.GENERIC_ALL, windows_acl.SERVICE_SID),
        (
            (security.ACL_REVISION, 0),
            windows_service_ipc.PIPE_CLIENT_ACCESS,
            "S-1-5-4",
        ),
    ]
    assert attributes.SECURITY_DESCRIPTOR is security.created_descriptors[0]
    assert security.created_descriptors[0].set_dacl_calls == [(1, dacl, 0)]


@pytest.mark.parametrize(
    ("session_id", "group_attributes", "expected"),
    [(1, windows_service_ipc.SE_GROUP_ENABLED, True), (0, windows_service_ipc.SE_GROUP_ENABLED, False), (1, 0, False)],
)
def test_pipe_client_requires_an_enabled_interactive_group_in_a_user_session(
    session_id: int,
    group_attributes: int,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = Mock()
    pipe = SimpleNamespace()
    security = SimpleNamespace(
        ImpersonateNamedPipeClient=Mock(),
        OpenThreadToken=Mock(return_value=token),
        GetTokenInformation=Mock(side_effect=[[("S-1-5-4", group_attributes)], session_id]),
        ConvertStringSidToSid=Mock(return_value="S-1-5-4"),
        TokenGroups=1,
        TokenSessionId=2,
        RevertToSelf=Mock(),
    )
    api = SimpleNamespace(GetCurrentThread=Mock(return_value="thread"))
    con = SimpleNamespace(TOKEN_QUERY=8)
    modules = (object(), object(), api, con, object(), pipe, security, object())
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)

    assert windows_service_ipc._client_is_interactive("pipe") is expected
    security.ImpersonateNamedPipeClient.assert_called_once_with("pipe")
    security.RevertToSelf.assert_called_once_with()
    token.Close.assert_called_once_with()


def test_pipe_client_always_closes_token_and_reverts_impersonation_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = Mock()
    pipe = SimpleNamespace()
    security = SimpleNamespace(
        ImpersonateNamedPipeClient=Mock(),
        OpenThreadToken=Mock(return_value=token),
        GetTokenInformation=Mock(side_effect=OSError("token query failed")),
        TokenGroups=1,
        RevertToSelf=Mock(),
    )
    modules = (
        object(),
        object(),
        SimpleNamespace(GetCurrentThread=Mock()),
        SimpleNamespace(TOKEN_QUERY=8),
        object(),
        pipe,
        security,
        object(),
    )
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)

    with pytest.raises(OSError, match="token query failed"):
        windows_service_ipc._client_is_interactive("pipe")

    security.ImpersonateNamedPipeClient.assert_called_once_with("pipe")
    token.Close.assert_called_once_with()
    security.RevertToSelf.assert_called_once_with()


def test_pipe_read_retries_transient_empty_reads_then_returns_message() -> None:
    server = BrowserPipeServer(Mock(), 8080)
    server._stop = Mock(is_set=Mock(return_value=False), wait=Mock())
    win32file = SimpleNamespace(
        ReadFile=Mock(
            side_effect=[
                _WinError(windows_service_ipc.ERROR_NO_DATA),
                _WinError(windows_service_ipc.ERROR_PIPE_LISTENING),
                (0, bytearray(b'{"action":"open","version":1}')),
            ]
        )
    )

    message = server._read_message_with_deadline("pipe", SimpleNamespace(error=_WinError), win32file)

    assert message == b'{"action":"open","version":1}'
    assert server._stop.wait.call_count == 2


def test_pipe_read_times_out_after_bounded_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    server = BrowserPipeServer(Mock(), 8080)
    server._stop = Mock(is_set=Mock(return_value=False), wait=Mock())
    monkeypatch.setattr(windows_service_ipc.time, "monotonic", Mock(side_effect=[10.0, 10.1, 15.1]))
    win32file = SimpleNamespace(ReadFile=Mock(side_effect=_WinError(windows_service_ipc.ERROR_NO_DATA)))

    with pytest.raises(TimeoutError, match="keine vollst\u00e4ndige Anfrage"):
        server._read_message_with_deadline("pipe", SimpleNamespace(error=_WinError), win32file)

    server._stop.wait.assert_called_once_with(0.05)


def test_pipe_read_propagates_nontransient_windows_error() -> None:
    server = BrowserPipeServer(Mock(), 8080)
    win32file = SimpleNamespace(ReadFile=Mock(side_effect=_WinError(5)))

    with pytest.raises(_WinError) as error:
        server._read_message_with_deadline("pipe", SimpleNamespace(error=_WinError), win32file)

    assert error.value.winerror == 5


def test_pipe_server_processes_one_request_and_closes_the_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = Mock()
    sessions.issue_bootstrap.return_value = "short-lived"
    server = BrowserPipeServer(sessions, 18080)
    response = windows_service_ipc.encode_open_response("http://127.0.0.1:18080/desktop/bootstrap?token=short-lived")
    server._read_message_with_deadline = Mock(  # type: ignore[method-assign]
        side_effect=[
            windows_service_ipc.encode_open_request(),
            windows_service_ipc.encode_open_acknowledgement(response),
        ]
    )
    pipe_handle = object()
    events: list[str] = []
    win32file = SimpleNamespace(
        WriteFile=Mock(side_effect=lambda _handle, message: (events.append("write"), (0, len(message)))[1]),
        FlushFileBuffers=Mock(side_effect=lambda _handle: (events.append("flush"), server._stop.set())),
        CloseHandle=Mock(),
    )
    win32pipe = SimpleNamespace(
        PIPE_ACCESS_DUPLEX=1,
        PIPE_TYPE_MESSAGE=2,
        PIPE_READMODE_MESSAGE=4,
        PIPE_WAIT=8,
        PIPE_NOWAIT=16,
        CreateNamedPipe=Mock(return_value=pipe_handle),
        ConnectNamedPipe=Mock(side_effect=_WinError(535)),
        SetNamedPipeHandleState=Mock(),
        DisconnectNamedPipe=Mock(side_effect=lambda _handle: events.append("disconnect")),
    )
    modules = (object(), SimpleNamespace(error=_WinError), object(), object(), win32file, win32pipe, object(), object())
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)
    monkeypatch.setattr(windows_service_ipc, "_pipe_security_attributes", Mock(return_value="security"))
    monkeypatch.setattr(windows_service_ipc, "_client_is_interactive", Mock(return_value=True))

    server._serve()

    sessions.issue_bootstrap.assert_called_once_with()
    assert windows_service_ipc.decode_open_response(win32file.WriteFile.call_args.args[1]).endswith("token=short-lived")
    assert events == ["write", "flush", "disconnect"]
    win32file.FlushFileBuffers.assert_called_once_with(pipe_handle)
    win32pipe.DisconnectNamedPipe.assert_called_once_with(pipe_handle)
    win32file.CloseHandle.assert_called_once_with(pipe_handle)
    assert server._ready.is_set()


def test_pipe_server_keeps_first_instance_open_across_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = Mock()
    sessions.issue_bootstrap.side_effect = ["first", "second"]
    server = BrowserPipeServer(sessions, 18080)
    responses = [
        windows_service_ipc.encode_open_response(f"http://127.0.0.1:18080/desktop/bootstrap?token={token}")
        for token in ("first", "second")
    ]
    server._read_message_with_deadline = Mock(  # type: ignore[method-assign]
        side_effect=[
            windows_service_ipc.encode_open_request(),
            windows_service_ipc.encode_open_acknowledgement(responses[0]),
            windows_service_ipc.encode_open_request(),
            windows_service_ipc.encode_open_acknowledgement(responses[1]),
        ]
    )
    pipe_handle = object()

    def flush_response(_handle: object) -> None:
        if win32file.FlushFileBuffers.call_count == 2:
            server._stop.set()

    win32file = SimpleNamespace(
        WriteFile=Mock(side_effect=lambda _handle, message: (0, len(message))),
        FlushFileBuffers=Mock(side_effect=flush_response),
        CloseHandle=Mock(),
    )
    win32pipe = SimpleNamespace(
        PIPE_ACCESS_DUPLEX=1,
        PIPE_TYPE_MESSAGE=2,
        PIPE_READMODE_MESSAGE=4,
        PIPE_WAIT=8,
        PIPE_NOWAIT=16,
        CreateNamedPipe=Mock(return_value=pipe_handle),
        ConnectNamedPipe=Mock(),
        SetNamedPipeHandleState=Mock(),
        DisconnectNamedPipe=Mock(),
    )
    modules = (object(), SimpleNamespace(error=_WinError), object(), object(), win32file, win32pipe, object(), object())
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)
    monkeypatch.setattr(windows_service_ipc, "_pipe_security_attributes", Mock(return_value="security"))
    monkeypatch.setattr(windows_service_ipc, "_client_is_interactive", Mock(return_value=True))

    server._serve()

    win32pipe.CreateNamedPipe.assert_called_once()
    assert win32pipe.ConnectNamedPipe.call_count == 2
    assert win32file.FlushFileBuffers.call_count == 2
    assert win32pipe.DisconnectNamedPipe.call_count == 2
    win32file.CloseHandle.assert_called_once_with(pipe_handle)


def test_pipe_server_reports_protocol_error_without_disclosing_details(monkeypatch: pytest.MonkeyPatch) -> None:
    errors: list[IpcServerDiagnostic] = []
    server = BrowserPipeServer(Mock(), 8080, error_callback=lambda error: (errors.append(error), server._stop.set()))
    pipe_handle = object()
    server._read_message_with_deadline = Mock(return_value=b"not-json")  # type: ignore[method-assign]
    win32file = SimpleNamespace(CloseHandle=Mock())
    win32pipe = SimpleNamespace(
        PIPE_ACCESS_DUPLEX=1,
        PIPE_TYPE_MESSAGE=2,
        PIPE_READMODE_MESSAGE=4,
        PIPE_WAIT=8,
        PIPE_NOWAIT=16,
        CreateNamedPipe=Mock(return_value=pipe_handle),
        ConnectNamedPipe=Mock(),
        SetNamedPipeHandleState=Mock(),
        DisconnectNamedPipe=Mock(),
    )
    modules = (object(), SimpleNamespace(error=_WinError), object(), object(), win32file, win32pipe, object(), object())
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)
    monkeypatch.setattr(windows_service_ipc, "_pipe_security_attributes", Mock())

    server._serve()

    assert errors == [
        IpcServerDiagnostic(
            phase="decode-request",
            exception_type="ValueError",
            winerror=None,
        )
    ]
    win32file.CloseHandle.assert_called_once_with(pipe_handle)


def test_pipe_server_reports_windows_api_loading_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    errors: list[IpcServerDiagnostic] = []
    server = BrowserPipeServer(Mock(), 8080, error_callback=errors.append)
    monkeypatch.setattr(
        windows_service_ipc,
        "_windows_modules",
        Mock(side_effect=RuntimeError("sensitive import details")),
    )

    server._serve()

    assert errors == [
        IpcServerDiagnostic(
            phase="load-windows-api",
            exception_type="RuntimeError",
            winerror=None,
        )
    ]
    assert server._ready.is_set()


def test_pipe_server_requires_exact_response_acknowledgement_before_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[IpcServerDiagnostic] = []
    sessions = Mock()
    sessions.issue_bootstrap.return_value = "short-lived"
    server = BrowserPipeServer(
        sessions,
        18080,
        error_callback=lambda error: (errors.append(error), server._stop.set()),
    )
    server._read_message_with_deadline = Mock(  # type: ignore[method-assign]
        side_effect=[
            windows_service_ipc.encode_open_request(),
            b'{"response_sha256":"falsch","version":1}',
        ]
    )
    pipe_handle = object()
    win32file = SimpleNamespace(
        WriteFile=Mock(side_effect=lambda _handle, message: (0, len(message))),
        FlushFileBuffers=Mock(),
        CloseHandle=Mock(),
    )
    win32pipe = SimpleNamespace(
        PIPE_ACCESS_DUPLEX=1,
        PIPE_TYPE_MESSAGE=2,
        PIPE_READMODE_MESSAGE=4,
        PIPE_WAIT=8,
        PIPE_NOWAIT=16,
        CreateNamedPipe=Mock(return_value=pipe_handle),
        ConnectNamedPipe=Mock(),
        SetNamedPipeHandleState=Mock(),
        DisconnectNamedPipe=Mock(),
    )
    modules = (
        object(),
        SimpleNamespace(error=_WinError),
        object(),
        object(),
        win32file,
        win32pipe,
        object(),
        object(),
    )
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)
    monkeypatch.setattr(windows_service_ipc, "_pipe_security_attributes", Mock())
    monkeypatch.setattr(windows_service_ipc, "_client_is_interactive", Mock(return_value=True))

    server._serve()

    assert errors == [
        IpcServerDiagnostic(
            phase="decode-acknowledgement",
            exception_type="ValueError",
            winerror=None,
        )
    ]
    win32file.WriteFile.assert_called_once()
    win32file.FlushFileBuffers.assert_not_called()
    win32pipe.DisconnectNamedPipe.assert_called_once_with(pipe_handle)
    win32file.CloseHandle.assert_called_once_with(pipe_handle)


def test_pipe_server_start_is_single_use_and_requires_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    thread = Mock()
    monkeypatch.setattr(windows_service_ipc, "Thread", Mock(return_value=thread))
    server = BrowserPipeServer(Mock(), 8080)
    server._ready = Mock()
    server._ready.wait.return_value = True

    server.start()

    thread.start.assert_called_once_with()
    with pytest.raises(RuntimeError, match="bereits gestartet"):
        server.start()

    unready = BrowserPipeServer(Mock(), 8080)
    unready._ready = Mock()
    unready._ready.wait.return_value = False
    with pytest.raises(RuntimeError, match="nicht rechtzeitig"):
        unready.start()


def test_pipe_server_stop_clears_sessions_wakes_listener_and_joins(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = Mock()
    server = BrowserPipeServer(sessions, 8080)
    thread = Mock()
    thread.is_alive.return_value = False
    server._thread = thread
    pipe_file = SimpleNamespace(CloseHandle=Mock())
    monkeypatch.setattr(windows_service_ipc, "_connect_to_pipe", Mock(return_value="wake-handle"))
    monkeypatch.setattr(
        windows_service_ipc,
        "_windows_modules",
        lambda: (object(), object(), object(), object(), pipe_file, object(), object(), object()),
    )

    server.stop()

    sessions.clear.assert_called_once_with()
    pipe_file.CloseHandle.assert_called_once_with("wake-handle")
    thread.join.assert_called_once_with(timeout=5)


def test_pipe_server_stop_rejects_a_thread_that_does_not_terminate(monkeypatch: pytest.MonkeyPatch) -> None:
    server = BrowserPipeServer(Mock(), 8080)
    thread = Mock()
    thread.is_alive.return_value = True
    server._thread = thread
    monkeypatch.setattr(windows_service_ipc, "_connect_to_pipe", Mock(side_effect=OSError("not listening")))

    with pytest.raises(RuntimeError, match="nicht beendet"):
        server.stop()


@pytest.mark.parametrize("process_id", [731, 0])
def test_service_pid_query_closes_all_scm_handles(
    process_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(
        SC_MANAGER_CONNECT=1,
        SERVICE_QUERY_STATUS=2,
        OpenSCManager=Mock(return_value="manager"),
        OpenService=Mock(return_value="service"),
        QueryServiceStatusEx=Mock(return_value={"ProcessId": process_id}),
        CloseServiceHandle=Mock(),
    )
    modules = (object(), object(), object(), object(), object(), object(), object(), service)
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)

    if process_id:
        assert windows_service_ipc.query_service_process_id() == process_id
    else:
        with pytest.raises(RuntimeError, match="l\u00e4uft nicht"):
            windows_service_ipc.query_service_process_id()

    assert service.CloseServiceHandle.call_args_list == [call("service"), call("manager")]


def test_pipe_connection_rejects_server_pid_different_from_scm(monkeypatch: pytest.MonkeyPatch) -> None:
    win32file = SimpleNamespace(CreateFile=Mock(return_value="handle"), CloseHandle=Mock())
    win32pipe = SimpleNamespace(
        WaitNamedPipe=Mock(),
        GetNamedPipeServerProcessId=Mock(return_value=100),
    )
    win32con = SimpleNamespace(GENERIC_READ=1, GENERIC_WRITE=2, OPEN_EXISTING=3)
    modules = (object(), object(), object(), win32con, win32file, win32pipe, object(), object())
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)
    monkeypatch.setattr(windows_service_ipc, "query_service_process_id", Mock(return_value=101))

    with pytest.raises(PermissionError, match="registrierten Windows-Dienst"):
        windows_service_ipc._connect_to_pipe()

    win32pipe.WaitNamedPipe.assert_called_once_with(windows_service_ipc.SERVICE_PIPE_NAME, 5000)
    win32file.CloseHandle.assert_called_once_with("handle")


def test_pipe_connection_closes_handle_when_service_pid_query_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    win32file = SimpleNamespace(CreateFile=Mock(return_value="handle"), CloseHandle=Mock())
    win32pipe = SimpleNamespace(
        WaitNamedPipe=Mock(),
        GetNamedPipeServerProcessId=Mock(return_value=100),
    )
    win32con = SimpleNamespace(OPEN_EXISTING=3)
    modules = (object(), object(), object(), win32con, win32file, win32pipe, object(), object())
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)
    monkeypatch.setattr(
        windows_service_ipc,
        "query_service_process_id",
        Mock(side_effect=RuntimeError("SCM-Abfrage fehlgeschlagen")),
    )

    with pytest.raises(RuntimeError, match="SCM-Abfrage fehlgeschlagen"):
        windows_service_ipc._connect_to_pipe()

    win32file.CloseHandle.assert_called_once_with("handle")


def test_pipe_connection_can_skip_pid_validation_only_for_internal_wakeup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    win32file = SimpleNamespace(CreateFile=Mock(return_value="handle"), CloseHandle=Mock())
    win32pipe = SimpleNamespace(WaitNamedPipe=Mock(), GetNamedPipeServerProcessId=Mock())
    win32con = SimpleNamespace(GENERIC_READ=1, GENERIC_WRITE=2, OPEN_EXISTING=3)
    modules = (object(), object(), object(), win32con, win32file, win32pipe, object(), object())
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)

    assert windows_service_ipc._connect_to_pipe(validate_server=False) == "handle"
    win32pipe.GetNamedPipeServerProcessId.assert_not_called()


def test_browser_url_request_uses_message_mode_and_always_closes_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "http://127.0.0.1:8080/desktop/bootstrap?token=short-lived"
    request = windows_service_ipc.encode_open_request()
    response = windows_service_ipc.encode_open_response(url)
    acknowledgement = windows_service_ipc.encode_open_acknowledgement(response)
    win32file = SimpleNamespace(
        WriteFile=Mock(side_effect=lambda _handle, message: (0, len(message))),
        ReadFile=Mock(
            side_effect=[
                (0, response),
                _WinError(windows_service_ipc.ERROR_PIPE_NOT_CONNECTED),
            ]
        ),
        CloseHandle=Mock(),
    )
    win32pipe = SimpleNamespace(PIPE_READMODE_MESSAGE=4, PIPE_NOWAIT=8, SetNamedPipeHandleState=Mock())
    modules = (
        object(),
        SimpleNamespace(error=_WinError),
        object(),
        object(),
        win32file,
        win32pipe,
        object(),
        object(),
    )
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)
    monkeypatch.setattr(windows_service_ipc, "_connect_to_pipe", Mock(return_value="handle"))

    assert windows_service_ipc.request_browser_url() == url
    win32pipe.SetNamedPipeHandleState.assert_called_once_with("handle", 12, None, None)
    assert win32file.WriteFile.call_args_list == [
        call("handle", request),
        call("handle", acknowledgement),
    ]
    assert win32file.ReadFile.call_count == 2
    win32file.CloseHandle.assert_called_once_with("handle")


def test_browser_url_request_retries_nonblocking_write_and_read(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "http://127.0.0.1:8080/desktop/bootstrap?token=short-lived"
    request = windows_service_ipc.encode_open_request()
    response = windows_service_ipc.encode_open_response(url)
    acknowledgement = windows_service_ipc.encode_open_acknowledgement(response)
    win32file = SimpleNamespace(
        WriteFile=Mock(side_effect=[(0, 0), (0, len(request)), (0, len(acknowledgement))]),
        ReadFile=Mock(
            side_effect=[
                _WinError(windows_service_ipc.ERROR_PIPE_LISTENING),
                (0, response),
                _WinError(windows_service_ipc.ERROR_NO_DATA),
                _WinError(windows_service_ipc.ERROR_PIPE_NOT_CONNECTED),
            ]
        ),
        CloseHandle=Mock(),
    )
    win32pipe = SimpleNamespace(PIPE_READMODE_MESSAGE=4, PIPE_NOWAIT=8, SetNamedPipeHandleState=Mock())
    modules = (
        object(),
        SimpleNamespace(error=_WinError),
        object(),
        object(),
        win32file,
        win32pipe,
        object(),
        object(),
    )
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)
    monkeypatch.setattr(windows_service_ipc, "_connect_to_pipe", Mock(return_value="handle"))
    monkeypatch.setattr(windows_service_ipc.time, "sleep", Mock())

    assert windows_service_ipc.request_browser_url() == url

    assert win32file.WriteFile.call_args_list == [
        call("handle", request),
        call("handle", request),
        call("handle", acknowledgement),
    ]
    assert win32file.ReadFile.call_count == 4
    assert windows_service_ipc.time.sleep.call_count == 3
    win32file.CloseHandle.assert_called_once_with("handle")


def test_browser_url_request_does_not_retry_disconnect_before_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = windows_service_ipc.encode_open_request()
    win32file = SimpleNamespace(
        WriteFile=Mock(return_value=(0, len(request))),
        ReadFile=Mock(side_effect=_WinError(windows_service_ipc.ERROR_PIPE_NOT_CONNECTED)),
        CloseHandle=Mock(),
    )
    win32pipe = SimpleNamespace(PIPE_READMODE_MESSAGE=4, PIPE_NOWAIT=8, SetNamedPipeHandleState=Mock())
    modules = (
        object(),
        SimpleNamespace(error=_WinError),
        object(),
        object(),
        win32file,
        win32pipe,
        object(),
        object(),
    )
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)
    monkeypatch.setattr(windows_service_ipc, "_connect_to_pipe", Mock(return_value="handle"))

    with pytest.raises(_WinError) as error:
        windows_service_ipc.request_browser_url()

    assert error.value.winerror == windows_service_ipc.ERROR_PIPE_NOT_CONNECTED
    win32file.WriteFile.assert_called_once_with("handle", request)
    win32file.ReadFile.assert_called_once_with("handle", windows_service_ipc.MAXIMUM_IPC_MESSAGE_BYTES)
    win32file.CloseHandle.assert_called_once_with("handle")


def test_browser_url_request_uses_one_total_write_read_deadline_and_closes_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = windows_service_ipc.encode_open_request()
    win32file = SimpleNamespace(
        WriteFile=Mock(return_value=(0, len(request))),
        ReadFile=Mock(side_effect=_WinError(windows_service_ipc.ERROR_NO_DATA)),
        CloseHandle=Mock(),
    )
    win32pipe = SimpleNamespace(PIPE_READMODE_MESSAGE=4, PIPE_NOWAIT=8, SetNamedPipeHandleState=Mock())
    modules = (
        object(),
        SimpleNamespace(error=_WinError),
        object(),
        object(),
        win32file,
        win32pipe,
        object(),
        object(),
    )
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)
    monkeypatch.setattr(windows_service_ipc, "_connect_to_pipe", Mock(return_value="handle"))
    monkeypatch.setattr(
        windows_service_ipc.time,
        "monotonic",
        Mock(side_effect=[10.0, 10.1, 10.2, 10.3, 15.1]),
    )
    monkeypatch.setattr(windows_service_ipc.time, "sleep", Mock())

    with pytest.raises(TimeoutError, match="IPC-Antwort"):
        windows_service_ipc.request_browser_url()

    win32file.WriteFile.assert_called_once_with("handle", windows_service_ipc.encode_open_request())
    win32file.ReadFile.assert_called_once_with("handle", windows_service_ipc.MAXIMUM_IPC_MESSAGE_BYTES)
    win32file.CloseHandle.assert_called_once_with("handle")


def test_browser_url_request_bounds_nonblocking_write_and_closes_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    win32file = SimpleNamespace(
        WriteFile=Mock(return_value=(0, 0)),
        ReadFile=Mock(),
        CloseHandle=Mock(),
    )
    win32pipe = SimpleNamespace(PIPE_READMODE_MESSAGE=4, PIPE_NOWAIT=8, SetNamedPipeHandleState=Mock())
    modules = (
        object(),
        SimpleNamespace(error=_WinError),
        object(),
        object(),
        win32file,
        win32pipe,
        object(),
        object(),
    )
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)
    monkeypatch.setattr(windows_service_ipc, "_connect_to_pipe", Mock(return_value="handle"))
    monkeypatch.setattr(
        windows_service_ipc.time,
        "monotonic",
        Mock(side_effect=[10.0, 10.1, 10.2, 15.1]),
    )
    monkeypatch.setattr(windows_service_ipc.time, "sleep", Mock())

    with pytest.raises(TimeoutError, match="IPC-Anfrage"):
        windows_service_ipc.request_browser_url()

    win32file.WriteFile.assert_called_once_with("handle", windows_service_ipc.encode_open_request())
    win32file.ReadFile.assert_not_called()
    win32file.CloseHandle.assert_called_once_with("handle")


def test_browser_url_request_rejects_partial_message_write_and_closes_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    win32file = SimpleNamespace(
        WriteFile=Mock(return_value=(0, 1)),
        ReadFile=Mock(),
        CloseHandle=Mock(),
    )
    win32pipe = SimpleNamespace(PIPE_READMODE_MESSAGE=4, PIPE_NOWAIT=8, SetNamedPipeHandleState=Mock())
    modules = (
        object(),
        SimpleNamespace(error=_WinError),
        object(),
        object(),
        win32file,
        win32pipe,
        object(),
        object(),
    )
    monkeypatch.setattr(windows_service_ipc, "_windows_modules", lambda: modules)
    monkeypatch.setattr(windows_service_ipc, "_connect_to_pipe", Mock(return_value="handle"))

    with pytest.raises(RuntimeError, match="unvollständig"):
        windows_service_ipc.request_browser_url()

    win32file.ReadFile.assert_not_called()
    win32file.CloseHandle.assert_called_once_with("handle")


def test_acl_constructor_and_lookup_reject_broad_or_unknown_accounts() -> None:
    with pytest.raises(ValueError, match="Breite lokale Gruppen"):
        WindowsServiceAcl(token_readers=(" Authenticated Users ",))

    security = _FakeSecurity()
    assert WindowsServiceAcl._lookup("SYSTEM", security) == "S-1-5-18"
    assert WindowsServiceAcl._lookup(r"BUILTIN\Administrators", security) == "S-1-5-32-544"
    with pytest.raises(RuntimeError, match="unknown"):
        WindowsServiceAcl._lookup("unknown", security)


@pytest.mark.parametrize(
    ("sid", "sid_type", "expected"),
    [
        ("S-1-5-21-user", 1, True),
        ("S-1-5-21-computer", 9, True),
        (windows_acl.SERVICE_SID, 5, True),
        ("S-1-5-2", 5, False),
        ("S-1-5-21-group", 2, False),
    ],
)
def test_token_reader_identity_policy_is_positive_not_blocklist_only(sid: str, sid_type: int, expected: bool) -> None:
    assert WindowsServiceAcl._reader_sid_is_specific(sid, sid_type) is expected


def test_acl_set_uses_protected_inheritable_full_access_and_read_only_aces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _FakeSecurity()
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))
    acl = WindowsServiceAcl()

    acl._set(
        Path("service-data"),
        directory=True,
        readers=("reader",),
        reader_sids=("S-1-5-21-explicit",),
    )

    dacl = security.created_acls[0]
    inherited = security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE
    assert dacl.aces[:3] == [
        ((security.ACL_REVISION_DS, inherited), _ACL_CONSTANTS.FILE_ALL_ACCESS, "S-1-5-18"),
        ((security.ACL_REVISION_DS, inherited), _ACL_CONSTANTS.FILE_ALL_ACCESS, "S-1-5-32-544"),
        ((security.ACL_REVISION_DS, inherited), _ACL_CONSTANTS.FILE_ALL_ACCESS, windows_acl.SERVICE_SID),
    ]
    assert dacl.aces[3:] == [
        ((security.ACL_REVISION_DS, 0), _ACL_CONSTANTS.FILE_GENERIC_READ, "S-1-5-21-reader"),
        ((security.ACL_REVISION_DS, 0), _ACL_CONSTANTS.FILE_GENERIC_READ, "S-1-5-21-explicit"),
    ]
    assert security.set_calls[0][2] == (
        security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION
    )
    assert security.set_calls[0][3] is None


def test_missing_windows_directory_is_created_with_its_final_protected_dacl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _FakeSecurity()

    class _Attributes:
        SECURITY_DESCRIPTOR: object | None = None

    created_attributes: list[_Attributes] = []

    def create_directory(raw_path: str, attributes: _Attributes) -> None:
        path = Path(raw_path)
        assert not path.exists()
        descriptor = attributes.SECURITY_DESCRIPTOR
        assert isinstance(descriptor, _FakeDescriptor)
        assert descriptor.protected
        assert descriptor.owner == windows_acl.ADMINISTRATORS_SID
        assert descriptor.dacl is security.created_acls[0]
        created_attributes.append(attributes)
        path.mkdir()

    win32file = SimpleNamespace(CreateDirectoryW=Mock(side_effect=create_directory))
    monkeypatch.setattr(windows_acl.sys, "platform", "win32")
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))
    monkeypatch.setattr(
        windows_acl,
        "_windows_directory_modules",
        lambda: (SimpleNamespace(SECURITY_ATTRIBUTES=_Attributes), win32file),
    )
    acl = WindowsServiceAcl(administrative=True)
    verify = Mock()
    set_acl = Mock()
    monkeypatch.setattr(acl, "_verify", verify)
    monkeypatch.setattr(acl, "_set", set_acl)
    path = tmp_path / "service-data"

    acl.protect_directory(path)

    inheritance = security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE
    assert security.created_acls[0].aces == [
        ((security.ACL_REVISION_DS, inheritance), _ACL_CONSTANTS.FILE_ALL_ACCESS, "S-1-5-18"),
        ((security.ACL_REVISION_DS, inheritance), _ACL_CONSTANTS.FILE_ALL_ACCESS, "S-1-5-32-544"),
        ((security.ACL_REVISION_DS, inheritance), _ACL_CONSTANTS.FILE_ALL_ACCESS, windows_acl.SERVICE_SID),
    ]
    descriptor = created_attributes[0].SECURITY_DESCRIPTOR
    assert isinstance(descriptor, _FakeDescriptor)
    assert descriptor.set_owner_calls == [(windows_acl.ADMINISTRATORS_SID, 0)]
    assert descriptor.set_dacl_calls == [(1, security.created_acls[0], 0)]
    assert descriptor.set_control_calls == [
        (security.SE_DACL_PROTECTED, security.SE_DACL_PROTECTED),
    ]
    verify.assert_called_once_with(
        path,
        allow_readers=False,
        directory=True,
        allow_local_service_owner=False,
        require_exact_ace_count=True,
    )
    set_acl.assert_not_called()


def test_runtime_temp_directory_is_created_with_service_sid_and_owner_rights(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _FakeSecurity()

    class _Attributes:
        SECURITY_DESCRIPTOR: object | None = None

    created_descriptor: _FakeDescriptor | None = None

    def create_directory(raw_path: str, attributes: _Attributes) -> None:
        nonlocal created_descriptor
        descriptor = attributes.SECURITY_DESCRIPTOR
        assert isinstance(descriptor, _FakeDescriptor)
        created_descriptor = descriptor
        Path(raw_path).mkdir()

    win32file = SimpleNamespace(CreateDirectoryW=Mock(side_effect=create_directory))
    monkeypatch.setattr(windows_acl.sys, "platform", "win32")
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))
    monkeypatch.setattr(
        windows_acl,
        "_windows_directory_modules",
        lambda: (SimpleNamespace(SECURITY_ATTRIBUTES=_Attributes), win32file),
    )
    acl = WindowsServiceAcl()
    monkeypatch.setattr(acl, "_verify", Mock())
    path = tmp_path / "kosit-temporary"

    acl.create_protected_directory(path, allow_local_service_owner=True)

    inheritance = security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE
    assert created_descriptor is not None
    assert created_descriptor.protected
    assert security.created_acls[0].aces == [
        ((security.ACL_REVISION_DS, inheritance), _ACL_CONSTANTS.FILE_ALL_ACCESS, "S-1-5-18"),
        ((security.ACL_REVISION_DS, inheritance), _ACL_CONSTANTS.FILE_ALL_ACCESS, "S-1-5-32-544"),
        ((security.ACL_REVISION_DS, inheritance), _ACL_CONSTANTS.FILE_ALL_ACCESS, windows_acl.SERVICE_SID),
        (
            (security.ACL_REVISION_DS, inheritance),
            windows_acl.OWNER_RIGHTS_READ_CONTROL,
            windows_acl.OWNER_RIGHTS_SID,
        ),
    ]
    acl._verify.assert_called_once_with(
        path,
        allow_readers=False,
        directory=True,
        allow_local_service_owner=True,
        require_exact_ace_count=True,
    )


def test_atomic_runtime_directory_is_removed_when_post_creation_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _FakeSecurity()

    class _Attributes:
        SECURITY_DESCRIPTOR: object | None = None

    def create_directory(raw_path: str, _attributes: _Attributes) -> None:
        Path(raw_path).mkdir()

    win32file = SimpleNamespace(CreateDirectoryW=Mock(side_effect=create_directory))
    monkeypatch.setattr(windows_acl.sys, "platform", "win32")
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))
    monkeypatch.setattr(
        windows_acl,
        "_windows_directory_modules",
        lambda: (SimpleNamespace(SECURITY_ATTRIBUTES=_Attributes), win32file),
    )
    acl = WindowsServiceAcl()
    monkeypatch.setattr(acl, "_verify", Mock(side_effect=RuntimeError("native verify failed")))
    path = tmp_path / "kosit-temporary"

    with pytest.raises(RuntimeError, match="native verify failed"):
        acl.create_protected_directory(path, allow_local_service_owner=True)

    assert not path.exists()


def test_failed_atomic_windows_directory_creation_never_falls_back_to_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _FakeSecurity()

    class _Attributes:
        SECURITY_DESCRIPTOR: object | None = None

    win32file = SimpleNamespace(CreateDirectoryW=Mock(side_effect=OSError("access denied")))
    monkeypatch.setattr(windows_acl.sys, "platform", "win32")
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))
    monkeypatch.setattr(
        windows_acl,
        "_windows_directory_modules",
        lambda: (SimpleNamespace(SECURITY_ATTRIBUTES=_Attributes), win32file),
    )
    path = tmp_path / "service-data"

    with pytest.raises(RuntimeError, match="geschützte Dienstverzeichnis"):
        WindowsServiceAcl(administrative=True).protect_directory(path)

    assert not path.exists()
    win32file.CreateDirectoryW.assert_called_once()


def test_atomic_windows_directory_creation_does_not_adopt_a_racing_populated_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _FakeSecurity()

    class _Attributes:
        SECURITY_DESCRIPTOR: object | None = None

    path = tmp_path / "service-data"

    def create_directory(_raw_path: str, _attributes: _Attributes) -> None:
        path.mkdir()
        (path / "unknown.dat").write_bytes(b"untrusted")
        raise _WinError(183)

    win32file = SimpleNamespace(CreateDirectoryW=Mock(side_effect=create_directory))
    monkeypatch.setattr(windows_acl.sys, "platform", "win32")
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))
    monkeypatch.setattr(
        windows_acl,
        "_windows_directory_modules",
        lambda: (SimpleNamespace(SECURITY_ATTRIBUTES=_Attributes), win32file),
    )
    acl = WindowsServiceAcl(administrative=True)
    set_acl = Mock()
    verify = Mock()
    monkeypatch.setattr(acl, "_set", set_acl)
    monkeypatch.setattr(acl, "_verify", verify)

    with pytest.raises(RuntimeError, match="geschützte Dienstverzeichnis"):
        acl.protect_directory(path)

    assert (path / "unknown.dat").read_bytes() == b"untrusted"
    set_acl.assert_not_called()
    verify.assert_not_called()


def test_atomic_windows_directory_creation_requires_an_existing_safe_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_directory = Mock()
    monkeypatch.setattr(windows_acl.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_acl,
        "_windows_directory_modules",
        lambda: (object(), SimpleNamespace(CreateDirectoryW=create_directory)),
    )
    path = tmp_path / "missing-parent" / "service-data"

    with pytest.raises(RuntimeError, match="übergeordnete Maschinenpfad"):
        WindowsServiceAcl(administrative=True).protect_directory(path)

    assert not path.parent.exists()
    create_directory.assert_not_called()


def test_log_acl_suppresses_shared_localservice_owner_write_dac(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _FakeSecurity()
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))

    WindowsServiceAcl()._set(
        Path("logs"),
        directory=True,
        allow_local_service_owner=True,
    )

    inherited = security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE
    assert security.created_acls[0].aces[-1] == (
        (security.ACL_REVISION_DS, inherited),
        windows_acl.OWNER_RIGHTS_READ_CONTROL,
        windows_acl.OWNER_RIGHTS_SID,
    )


def test_administrative_acl_accepts_only_elevated_current_owner_then_normalizes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = _acl_descriptor(_required_acl_aces(), owner="S-1-5-21-current-admin")
    security = _FakeSecurity(descriptor)
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))
    monkeypatch.setattr(
        WindowsServiceAcl,
        "_current_elevated_administrator_sid",
        staticmethod(lambda _security: "S-1-5-21-current-admin"),
    )
    path = tmp_path / "service.json"
    path.touch()

    WindowsServiceAcl(administrative=True)._set(path, directory=False)

    assert descriptor.owner == windows_acl.ADMINISTRATORS_SID
    assert security.set_calls[0][2] & security.OWNER_SECURITY_INFORMATION
    assert security.set_calls[0][3] == windows_acl.ADMINISTRATORS_SID


def test_current_administrator_owner_requires_an_elevated_admin_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = SimpleNamespace(Close=Mock())
    security = SimpleNamespace(
        TokenElevation=1,
        TokenUser=2,
        OpenProcessToken=Mock(return_value=token),
        GetTokenInformation=Mock(side_effect=(0, ("S-1-5-21-current", 0))),
        ConvertStringSidToSid=Mock(side_effect=lambda sid: sid),
        ConvertSidToStringSid=Mock(side_effect=lambda sid: sid),
        CheckTokenMembership=Mock(return_value=True),
    )
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(GetCurrentProcess=Mock(return_value="process")))
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(TOKEN_QUERY=8))

    with pytest.raises(RuntimeError, match="aktuell erhöhte Administratoridentität"):
        WindowsServiceAcl._current_elevated_administrator_sid(security)

    token.Close.assert_called_once_with()


def test_direct_local_administrator_sids_include_only_user_members_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = Mock(
        side_effect=[
            (
                [
                    {"sid": "S-1-5-21-user-1", "sidusage": 1},
                    {"sid": "S-1-5-21-group", "sidusage": 2},
                ],
                3,
                17,
            ),
            ([{"sid": "S-1-5-21-user-2", "sidusage": 1}], 3, 0),
        ]
    )
    security = SimpleNamespace(
        ConvertStringSidToSid=Mock(side_effect=lambda sid: sid),
        ConvertSidToStringSid=Mock(side_effect=lambda sid: sid),
        LookupAccountSid=Mock(return_value=("Administrators", "BUILTIN", 4)),
    )
    monkeypatch.setattr(windows_acl.sys, "platform", "win32")
    monkeypatch.setitem(
        sys.modules,
        "win32net",
        SimpleNamespace(NetLocalGroupGetMembers=members),
    )

    assert WindowsServiceAcl._direct_local_administrator_sids(security) == frozenset(
        {"S-1-5-21-user-1", "S-1-5-21-user-2"}
    )
    assert members.call_args_list == [
        call(None, "Administrators", 1, 0, 65_536),
        call(None, "Administrators", 1, 17, 65_536),
    ]


def test_direct_local_administrator_sids_fail_closed_on_incomplete_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = SimpleNamespace(
        ConvertStringSidToSid=Mock(side_effect=lambda sid: sid),
        LookupAccountSid=Mock(return_value=("Administrators", "BUILTIN", 4)),
    )
    monkeypatch.setattr(windows_acl.sys, "platform", "win32")
    monkeypatch.setitem(
        sys.modules,
        "win32net",
        SimpleNamespace(NetLocalGroupGetMembers=Mock(return_value=([{"sid": "S-1-5-21-user"}], 1, 0))),
    )

    with pytest.raises(RuntimeError, match="nicht sicher geprüft"):
        WindowsServiceAcl._direct_local_administrator_sids(security)


def test_runtime_owner_policy_allows_local_service_only_for_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = _acl_descriptor(
        _required_acl_aces() + [((0, 0), windows_acl.OWNER_RIGHTS_READ_CONTROL, windows_acl.OWNER_RIGHTS_SID)],
        owner=windows_acl.LOCAL_SERVICE_SID,
    )
    security = _FakeSecurity(descriptor)
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))

    WindowsServiceAcl()._verify(
        Path("service.log"),
        allow_readers=False,
        allow_local_service_owner=True,
    )
    with pytest.raises(RuntimeError, match="vertrauenswürdige Windows-Identität"):
        WindowsServiceAcl()._verify(Path("api-token.txt"), allow_readers=True)

    descriptor.dacl = _FakeDacl(_required_acl_aces())
    with pytest.raises(RuntimeError, match="Besitzerrechte"):
        WindowsServiceAcl()._verify(
            Path("service.log"),
            allow_readers=False,
            allow_local_service_owner=True,
        )


def test_runtime_rejects_non_service_owner_and_never_sets_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = _acl_descriptor(_required_acl_aces(), owner="S-1-5-21-untrusted")
    security = _FakeSecurity(descriptor)
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))
    path = tmp_path / "service.json"
    path.touch()

    with pytest.raises(RuntimeError, match="vertrauenswürdige Windows-Identität"):
        WindowsServiceAcl()._set(path, directory=False)

    assert not security.set_calls


def test_acl_set_wraps_windows_security_error(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _FakeSecurity()
    security.SetNamedSecurityInfo = Mock(side_effect=OSError("access denied"))  # type: ignore[method-assign]
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))

    with pytest.raises(RuntimeError, match="restriktive DACL"):
        WindowsServiceAcl()._set(Path("token"), directory=False)


def _acl_descriptor(
    aces: list[tuple[tuple[int, int], int, str]],
    *,
    protected: bool = True,
    owner: str = windows_acl.ADMINISTRATORS_SID,
) -> _FakeDescriptor:
    return _FakeDescriptor(_FakeDacl(aces), protected=protected, owner=owner)


def _required_acl_aces(*, flags: int = 0) -> list[tuple[tuple[int, int], int, str]]:
    return [
        ((0, flags), _ACL_CONSTANTS.FILE_ALL_ACCESS, "S-1-5-18"),
        ((0, flags), _ACL_CONSTANTS.FILE_ALL_ACCESS, "S-1-5-32-544"),
        ((0, flags), _ACL_CONSTANTS.FILE_ALL_ACCESS, windows_acl.SERVICE_SID),
    ]


def _owner_rights_acl_ace(*, flags: int = 0) -> tuple[tuple[int, int], int, str]:
    return ((0, flags), windows_acl.OWNER_RIGHTS_READ_CONTROL, windows_acl.OWNER_RIGHTS_SID)


@pytest.mark.parametrize(
    ("allow_local_service_owner", "owner"),
    [
        (False, windows_acl.ADMINISTRATORS_SID),
        (True, windows_acl.LOCAL_SERVICE_SID),
    ],
)
def test_acl_verify_tolerates_one_direct_user_administrator_ace_only_on_service_directories(
    allow_local_service_owner: bool,
    owner: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inheritance = _FakeSecurity.OBJECT_INHERIT_ACE | _FakeSecurity.CONTAINER_INHERIT_ACE
    candidate = "S-1-5-21-direct-administrator"
    aces = _required_acl_aces(flags=inheritance)
    if allow_local_service_owner:
        aces.append(_owner_rights_acl_ace(flags=inheritance))
    aces.append(((0, inheritance), _ACL_CONSTANTS.FILE_ALL_ACCESS, candidate))
    descriptor = _acl_descriptor(aces, owner=owner)
    security = _FakeSecurity(descriptor)
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))
    monkeypatch.setattr(
        WindowsServiceAcl,
        "_direct_local_administrator_sids",
        staticmethod(lambda _security: frozenset({candidate})),
    )

    WindowsServiceAcl()._verify(
        Path("logs" if allow_local_service_owner else "service-data"),
        allow_readers=False,
        directory=True,
        allow_local_service_owner=allow_local_service_owner,
        allow_redundant_administrator_ace=True,
    )


def test_acl_verify_rejects_redundant_administrator_ace_on_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "S-1-5-21-direct-administrator"
    descriptor = _acl_descriptor(_required_acl_aces() + [((0, 0), _ACL_CONSTANTS.FILE_ALL_ACCESS, candidate)])
    security = _FakeSecurity(descriptor)
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))
    monkeypatch.setattr(
        WindowsServiceAcl,
        "_direct_local_administrator_sids",
        staticmethod(lambda _security: frozenset({candidate})),
    )

    with pytest.raises(RuntimeError):
        WindowsServiceAcl()._verify(
            Path("service.json"),
            allow_readers=False,
            allow_redundant_administrator_ace=True,
        )


@pytest.mark.parametrize(
    ("direct_administrators", "sid_type"),
    [
        (frozenset({"S-1-5-21-candidate"}), 2),
        (frozenset(), 1),
    ],
)
def test_acl_verify_rejects_group_or_non_administrator_directory_ace(
    direct_administrators: frozenset[str],
    sid_type: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inheritance = _FakeSecurity.OBJECT_INHERIT_ACE | _FakeSecurity.CONTAINER_INHERIT_ACE
    candidate = "S-1-5-21-candidate"
    descriptor = _acl_descriptor(
        _required_acl_aces(flags=inheritance) + [((0, inheritance), _ACL_CONSTANTS.FILE_ALL_ACCESS, candidate)]
    )
    security = _FakeSecurity(descriptor)
    security.LookupAccountSid = Mock(return_value=("candidate", "TEST", sid_type))  # type: ignore[method-assign]
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))
    monkeypatch.setattr(
        WindowsServiceAcl,
        "_direct_local_administrator_sids",
        staticmethod(lambda _security: direct_administrators),
    )

    with pytest.raises(RuntimeError):
        WindowsServiceAcl()._verify(
            Path("service-data"),
            allow_readers=False,
            directory=True,
            allow_redundant_administrator_ace=True,
        )


@pytest.mark.parametrize(
    ("mask", "flags"),
    [
        (_ACL_CONSTANTS.FILE_GENERIC_READ, _FakeSecurity.OBJECT_INHERIT_ACE | _FakeSecurity.CONTAINER_INHERIT_ACE),
        (_ACL_CONSTANTS.FILE_ALL_ACCESS, 0),
        (_ACL_CONSTANTS.FILE_ALL_ACCESS, _FakeSecurity.OBJECT_INHERIT_ACE),
    ],
)
def test_acl_verify_rejects_redundant_administrator_ace_with_noncanonical_access(
    mask: int,
    flags: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inheritance = _FakeSecurity.OBJECT_INHERIT_ACE | _FakeSecurity.CONTAINER_INHERIT_ACE
    candidate = "S-1-5-21-direct-administrator"
    descriptor = _acl_descriptor(_required_acl_aces(flags=inheritance) + [((0, flags), mask, candidate)])
    security = _FakeSecurity(descriptor)
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))
    monkeypatch.setattr(
        WindowsServiceAcl,
        "_direct_local_administrator_sids",
        staticmethod(lambda _security: frozenset({candidate})),
    )

    with pytest.raises(RuntimeError):
        WindowsServiceAcl()._verify(
            Path("service-data"),
            allow_readers=False,
            directory=True,
            allow_redundant_administrator_ace=True,
        )


def test_acl_verify_rejects_two_redundant_direct_administrator_aces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inheritance = _FakeSecurity.OBJECT_INHERIT_ACE | _FakeSecurity.CONTAINER_INHERIT_ACE
    candidates = frozenset({"S-1-5-21-direct-administrator-1", "S-1-5-21-direct-administrator-2"})
    descriptor = _acl_descriptor(
        _required_acl_aces(flags=inheritance)
        + [((0, inheritance), _ACL_CONSTANTS.FILE_ALL_ACCESS, candidate) for candidate in sorted(candidates)]
    )
    security = _FakeSecurity(descriptor)
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))
    monkeypatch.setattr(
        WindowsServiceAcl,
        "_direct_local_administrator_sids",
        staticmethod(lambda _security: candidates),
    )

    with pytest.raises(RuntimeError):
        WindowsServiceAcl()._verify(
            Path("service-data"),
            allow_readers=False,
            directory=True,
            allow_redundant_administrator_ace=True,
        )


@pytest.mark.parametrize(
    "duplicate_sid",
    ["S-1-5-18", "S-1-5-32-544", windows_acl.SERVICE_SID],
)
def test_acl_verify_rejects_duplicate_required_service_identity_aces(
    duplicate_sid: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inheritance = _FakeSecurity.OBJECT_INHERIT_ACE | _FakeSecurity.CONTAINER_INHERIT_ACE
    descriptor = _acl_descriptor(
        _required_acl_aces(flags=inheritance) + [((0, inheritance), _ACL_CONSTANTS.FILE_ALL_ACCESS, duplicate_sid)]
    )
    security = _FakeSecurity(descriptor)
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))
    monkeypatch.setattr(
        WindowsServiceAcl,
        "_direct_local_administrator_sids",
        staticmethod(lambda _security: frozenset()),
    )

    with pytest.raises(RuntimeError):
        WindowsServiceAcl()._verify(
            Path("service-data"),
            allow_readers=False,
            directory=True,
            allow_redundant_administrator_ace=True,
        )


def test_acl_verify_accepts_only_protected_required_aces_and_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    descriptor = _acl_descriptor(_required_acl_aces() + [((0, 0), _ACL_CONSTANTS.FILE_GENERIC_READ, "S-1-5-21-reader")])
    security = _FakeSecurity(descriptor)
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))

    WindowsServiceAcl()._verify(Path("token"), allow_readers=True)


def test_acl_verify_accepts_exact_directory_inheritance_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    inheritance = _FakeSecurity.OBJECT_INHERIT_ACE | _FakeSecurity.CONTAINER_INHERIT_ACE
    descriptor = _acl_descriptor(_required_acl_aces(flags=inheritance))
    security = _FakeSecurity(descriptor)
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))

    WindowsServiceAcl()._verify(Path("service-data"), allow_readers=False, directory=True)


def test_purge_acl_accepts_only_exact_inherited_log_aces(monkeypatch: pytest.MonkeyPatch) -> None:
    descriptor = _acl_descriptor(
        _required_acl_aces(flags=_FakeSecurity.INHERITED_ACE)
        + [
            (
                (0, _FakeSecurity.INHERITED_ACE),
                windows_acl.OWNER_RIGHTS_READ_CONTROL,
                windows_acl.OWNER_RIGHTS_SID,
            )
        ],
        protected=False,
    )
    security = _FakeSecurity(descriptor)
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))

    WindowsServiceAcl().verify_log_for_purge(Path("service.log.1"))

    descriptor.dacl.aces.append(  # type: ignore[union-attr]
        ((0, _FakeSecurity.INHERITED_ACE), _ACL_CONSTANTS.FILE_ALL_ACCESS, "S-1-5-18")
    )
    with pytest.raises(RuntimeError, match="nicht exakt"):
        WindowsServiceAcl().verify_log_for_purge(Path("service.log.1"))


@pytest.mark.parametrize(
    ("flags", "directory", "error"),
    [
        (_FakeSecurity.INHERIT_ONLY_ACE, False, "INHERIT_ONLY"),
        (0, True, "OI/CI-Vererbungsflags"),
        (_FakeSecurity.OBJECT_INHERIT_ACE, True, "OI/CI-Vererbungsflags"),
        (_FakeSecurity.OBJECT_INHERIT_ACE, False, "OI/CI-Vererbungsflags"),
        (
            _FakeSecurity.OBJECT_INHERIT_ACE
            | _FakeSecurity.CONTAINER_INHERIT_ACE
            | _FakeSecurity.NO_PROPAGATE_INHERIT_ACE,
            True,
            "OI/CI-Vererbungsflags",
        ),
        (
            _FakeSecurity.OBJECT_INHERIT_ACE | _FakeSecurity.CONTAINER_INHERIT_ACE | _FakeSecurity.INHERITED_ACE,
            True,
            "OI/CI-Vererbungsflags",
        ),
    ],
)
def test_acl_verify_rejects_inapplicable_or_unexpected_inheritance_flags(
    flags: int,
    directory: bool,
    error: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = _acl_descriptor(_required_acl_aces(flags=flags))
    security = _FakeSecurity(descriptor)
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))

    with pytest.raises(RuntimeError, match=error):
        WindowsServiceAcl()._verify(Path("item"), allow_readers=False, directory=directory)


@pytest.mark.parametrize(
    ("descriptor", "allow_readers", "error"),
    [
        (_FakeDescriptor(None), True, "nicht vor Vererbung gesch\u00fctzt"),
        (_acl_descriptor(_required_acl_aces(), protected=False), True, "nicht vor Vererbung gesch\u00fctzt"),
        (
            _acl_descriptor([((1, 0), _ACL_CONSTANTS.FILE_ALL_ACCESS, "S-1-5-18")] + _required_acl_aces()[1:]),
            True,
            "nicht erlaubten ACE-Typ",
        ),
        (
            _acl_descriptor(_required_acl_aces() + [((0, 0), 1, "S-1-1-0")]),
            True,
            "zu breiten lokalen Gruppe",
        ),
        (
            _acl_descriptor(_required_acl_aces() + [((0, 0), 3, "S-1-5-21-reader")]),
            True,
            "exakt provisionierte Leseberechtigung",
        ),
        (
            _acl_descriptor(_required_acl_aces() + [((0, 0), 1, "S-1-5-21-reader")]),
            False,
            "nicht provisionierte Schreibberechtigung",
        ),
        (
            _acl_descriptor(
                [((0, 0), 1, "S-1-5-18"), *_required_acl_aces()[1:]],
            ),
            True,
            "nicht den Vollzugriff",
        ),
        (
            _acl_descriptor(_required_acl_aces()[:-1]),
            True,
            "nicht alle erforderlichen",
        ),
    ],
)
def test_acl_verify_rejects_unsafe_descriptors(
    descriptor: _FakeDescriptor,
    allow_readers: bool,
    error: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _FakeSecurity(descriptor)
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))

    with pytest.raises(RuntimeError, match=error):
        WindowsServiceAcl()._verify(Path("item"), allow_readers=allow_readers)


def test_acl_verify_wraps_descriptor_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _FakeSecurity()
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))

    with pytest.raises(RuntimeError, match="DACL konnte"):
        WindowsServiceAcl()._verify(Path("missing"), allow_readers=False)


def test_acl_grant_merges_reader_sid_and_reverifies(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _FakeSecurity()
    acl = WindowsServiceAcl()
    verify = Mock()
    set_acl = Mock()
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))
    monkeypatch.setattr(acl, "_verify", verify)
    monkeypatch.setattr(acl, "_additional_reader_sids", Mock(return_value={"S-1-5-21-existing"}))
    monkeypatch.setattr(acl, "_set", set_acl)
    token = Path("token")

    acl.grant_token_reader(token, "reader")

    assert verify.call_args_list == [call(token, allow_readers=True), call(token, allow_readers=True)]
    set_acl.assert_called_once_with(
        token,
        directory=False,
        reader_sids=("S-1-5-21-existing", "S-1-5-21-reader"),
    )


def test_acl_additional_reader_sids_excludes_service_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    descriptor = _acl_descriptor(_required_acl_aces() + [((0, 0), _ACL_CONSTANTS.FILE_GENERIC_READ, "S-1-5-21-reader")])
    security = _FakeSecurity(descriptor)
    monkeypatch.setattr(windows_acl, "_windows_modules", lambda: (security, _ACL_CONSTANTS))

    assert WindowsServiceAcl()._additional_reader_sids(Path("token")) == {"S-1-5-21-reader"}


def test_acl_path_helpers_apply_and_verify_exact_service_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = ServicePaths(
        data_directory=tmp_path / "data",
        configuration=tmp_path / "data" / "service.json",
        token=tmp_path / "data" / "api-token.txt",
        log=tmp_path / "data" / "logs" / "service.log",
    )
    paths.log.parent.mkdir(parents=True)
    paths.log.touch()
    acl = WindowsServiceAcl(token_readers=("reader",))
    set_acl = Mock()
    verify = Mock()
    operations = Mock()
    operations.attach_mock(set_acl, "set_acl")
    operations.attach_mock(verify, "verify")
    monkeypatch.setattr(acl, "_set", set_acl)
    monkeypatch.setattr(acl, "_verify", verify)

    acl.protect_directory(paths.data_directory)
    acl.protect_configuration(paths.configuration)
    acl.protect_token(paths.token)
    acl.protect_log(paths.log)
    acl.verify_service_paths(paths)

    assert paths.data_directory.is_dir()
    assert set_acl.call_args_list == [
        call(paths.data_directory, directory=True, allow_local_service_owner=False),
        call(paths.configuration, directory=False),
        call(paths.token, directory=False, readers=("reader",)),
        call(paths.log, directory=False, allow_local_service_owner=True),
    ]
    assert verify.call_args_list == [
        call(
            paths.data_directory,
            allow_readers=False,
            directory=True,
            allow_local_service_owner=False,
            require_exact_ace_count=True,
        ),
        call(paths.configuration, allow_readers=False, require_exact_ace_count=True),
        call(paths.token, allow_readers=True),
        call(
            paths.log,
            allow_readers=False,
            allow_local_service_owner=True,
            require_exact_ace_count=True,
        ),
        call(
            paths.data_directory,
            allow_readers=False,
            directory=True,
        ),
        call(paths.configuration, allow_readers=False),
        call(paths.token, allow_readers=True),
        call(
            paths.log.parent,
            allow_readers=False,
            directory=True,
            allow_local_service_owner=True,
        ),
        call(paths.log, allow_readers=False, allow_local_service_owner=True),
    ]
    assert operations.mock_calls[:8] == [
        call.set_acl(paths.data_directory, directory=True, allow_local_service_owner=False),
        call.verify(
            paths.data_directory,
            allow_readers=False,
            directory=True,
            allow_local_service_owner=False,
            require_exact_ace_count=True,
        ),
        call.set_acl(paths.configuration, directory=False),
        call.verify(paths.configuration, allow_readers=False, require_exact_ace_count=True),
        call.set_acl(paths.token, directory=False, readers=("reader",)),
        call.verify(paths.token, allow_readers=True),
        call.set_acl(paths.log, directory=False, allow_local_service_owner=True),
        call.verify(
            paths.log,
            allow_readers=False,
            allow_local_service_owner=True,
            require_exact_ace_count=True,
        ),
    ]
    assert operations.mock_calls[8:] == [
        call.verify(
            paths.data_directory,
            allow_readers=False,
            directory=True,
        ),
        call.verify(paths.configuration, allow_readers=False),
        call.verify(paths.token, allow_readers=True),
        call.verify(
            paths.log.parent,
            allow_readers=False,
            directory=True,
            allow_local_service_owner=True,
        ),
        call.verify(paths.log, allow_readers=False, allow_local_service_owner=True),
    ]


def test_acl_existing_path_verification_remains_strict_for_all_existing_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = ServicePaths(
        data_directory=tmp_path / "data",
        configuration=tmp_path / "data" / "service.json",
        token=tmp_path / "data" / "api-token.txt",
        log=tmp_path / "data" / "logs" / "service.log",
    )
    paths.log.parent.mkdir(parents=True)
    paths.configuration.touch()
    paths.token.touch()
    paths.log.touch()
    acl = WindowsServiceAcl()
    verify = Mock()
    monkeypatch.setattr(acl, "_verify", verify)

    acl.verify_existing_service_paths(paths)

    assert verify.call_args_list == [
        call(
            paths.data_directory,
            allow_readers=False,
            directory=True,
            allow_local_service_owner=False,
        ),
        call(
            paths.configuration,
            allow_readers=False,
            directory=False,
            allow_local_service_owner=False,
        ),
        call(
            paths.token,
            allow_readers=True,
            directory=False,
            allow_local_service_owner=False,
        ),
        call(
            paths.log.parent,
            allow_readers=False,
            directory=True,
            allow_local_service_owner=True,
        ),
        call(
            paths.log,
            allow_readers=False,
            directory=False,
            allow_local_service_owner=True,
        ),
    ]


def test_acl_repair_validates_every_path_before_normalizing_only_affected_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = ServicePaths(
        data_directory=tmp_path / "data",
        configuration=tmp_path / "data" / "service.json",
        token=tmp_path / "data" / "api-token.txt",
        log=tmp_path / "data" / "logs" / "service.log",
    )
    paths.log.parent.mkdir(parents=True)
    paths.configuration.touch()
    paths.token.touch()
    paths.log.touch()
    acl = WindowsServiceAcl()
    verify = Mock(side_effect=(True, False, False, True, False))
    protect = Mock()
    strict_verify = Mock()
    operations = Mock()
    operations.attach_mock(verify, "verify")
    operations.attach_mock(protect, "protect")
    operations.attach_mock(strict_verify, "strict_verify")
    monkeypatch.setattr(acl, "_verify", verify)
    monkeypatch.setattr(acl, "protect_directory", protect)
    monkeypatch.setattr(acl, "verify_service_paths", strict_verify)

    acl.repair_explorer_directory_aces(paths)

    assert verify.call_args_list == [
        call(
            paths.data_directory,
            allow_readers=False,
            directory=True,
            allow_redundant_administrator_ace=True,
        ),
        call(paths.configuration, allow_readers=False),
        call(paths.token, allow_readers=True),
        call(
            paths.log.parent,
            allow_readers=False,
            directory=True,
            allow_local_service_owner=True,
            allow_redundant_administrator_ace=True,
        ),
        call(paths.log, allow_readers=False, allow_local_service_owner=True),
    ]
    assert protect.call_args_list == [
        call(paths.data_directory),
        call(paths.log.parent, allow_local_service_owner=True),
    ]
    strict_verify.assert_called_once_with(paths)
    assert operations.mock_calls[-3:] == [
        call.protect(paths.data_directory),
        call.protect(paths.log.parent, allow_local_service_owner=True),
        call.strict_verify(paths),
    ]


def test_acl_repair_does_not_rewrite_already_canonical_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = ServicePaths(
        data_directory=tmp_path / "data",
        configuration=tmp_path / "data" / "service.json",
        token=tmp_path / "data" / "api-token.txt",
        log=tmp_path / "data" / "logs" / "service.log",
    )
    paths.log.parent.mkdir(parents=True)
    paths.configuration.touch()
    paths.token.touch()
    paths.log.touch()
    acl = WindowsServiceAcl()
    monkeypatch.setattr(acl, "_verify", Mock(return_value=False))
    protect = Mock()
    strict_verify = Mock()
    monkeypatch.setattr(acl, "protect_directory", protect)
    monkeypatch.setattr(acl, "verify_service_paths", strict_verify)

    acl.repair_explorer_directory_aces(paths)

    protect.assert_not_called()
    strict_verify.assert_called_once_with(paths)


def test_native_windows_helpers_fail_closed_on_other_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_service_ipc.sys, "platform", "linux")
    monkeypatch.setattr(windows_acl.sys, "platform", "linux")
    monkeypatch.setattr(windows_sync.sys, "platform", "linux")

    with pytest.raises(OSError, match="Windows"):
        windows_service_ipc._windows_modules()
    with pytest.raises(OSError, match="Windows"):
        windows_acl._windows_modules()
    with pytest.raises(OSError, match="Windows"):
        windows_sync.create_backend_mutex()


def _install_fake_windows_dlls(
    monkeypatch: pytest.MonkeyPatch,
    *,
    convert_ok: bool = True,
    mutex_handle: int = 321,
    error: int = windows_sync.ERROR_ALREADY_EXISTS,
    close_ok: bool = True,
) -> tuple[SimpleNamespace, SimpleNamespace, dict[str, int]]:
    errors = {"value": error}
    kernel32 = SimpleNamespace(
        CreateMutexW=Mock(return_value=mutex_handle),
        LocalFree=Mock(return_value=None),
        CloseHandle=Mock(return_value=close_ok),
    )
    advapi32 = SimpleNamespace(ConvertStringSecurityDescriptorToSecurityDescriptorW=Mock(return_value=convert_ok))

    def win_dll(name: str, *, use_last_error: bool) -> SimpleNamespace:
        assert use_last_error is True
        return kernel32 if name == "kernel32" else advapi32

    monkeypatch.setattr(windows_sync.sys, "platform", "win32")
    monkeypatch.setattr(windows_sync.ctypes, "WinDLL", win_dll, raising=False)
    monkeypatch.setattr(windows_sync.ctypes, "set_last_error", lambda value: errors.update(value=value), raising=False)
    monkeypatch.setattr(windows_sync.ctypes, "get_last_error", lambda: errors["value"], raising=False)
    return kernel32, advapi32, errors


def test_backend_mutex_uses_explicit_sddl_and_reports_existing_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32, advapi32, errors = _install_fake_windows_dlls(monkeypatch)
    original_set_last_error = windows_sync.ctypes.set_last_error

    def clear_then_restore_error(value: int) -> None:
        original_set_last_error(value)
        errors["value"] = windows_sync.ERROR_ALREADY_EXISTS

    monkeypatch.setattr(windows_sync.ctypes, "set_last_error", clear_then_restore_error)

    mutex = windows_sync.create_backend_mutex()

    assert mutex == windows_sync.BackendMutex(handle=321, already_exists=True)
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    assert convert.call_args.args[0] == windows_sync.BACKEND_MUTEX_SECURITY_SDDL
    assert convert.call_args.args[1] == windows_sync.SDDL_REVISION_1
    create = kernel32.CreateMutexW
    assert create.call_args.args[1:] == (False, windows_sync.BACKEND_MUTEX_NAME)
    kernel32.LocalFree.assert_called_once()


def test_backend_mutex_creation_frees_descriptor_on_create_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel32, _advapi32, errors = _install_fake_windows_dlls(monkeypatch, mutex_handle=0, error=5)

    def clear_without_losing_failure(_value: int) -> None:
        errors["value"] = 5

    monkeypatch.setattr(windows_sync.ctypes, "set_last_error", clear_without_losing_failure)

    with pytest.raises(OSError, match="konnte nicht ge\u00f6ffnet"):
        windows_sync.create_backend_mutex()

    kernel32.LocalFree.assert_called_once()


def test_backend_mutex_creation_rejects_invalid_security_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _kernel32, advapi32, _errors = _install_fake_windows_dlls(monkeypatch, convert_ok=False, error=87)

    with pytest.raises(OSError, match="DACL"):
        windows_sync.create_backend_mutex()

    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.assert_called_once()


def test_backend_mutex_close_is_idempotent_and_clears_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel32, _advapi32, _errors = _install_fake_windows_dlls(monkeypatch)
    mutex = windows_sync.BackendMutex(handle=123, already_exists=False)

    mutex.close()
    mutex.close()

    assert mutex.handle == 0
    kernel32.CloseHandle.assert_called_once()
    assert kernel32.CloseHandle.call_args.args[0].value == 123


def test_backend_mutex_close_propagates_windows_error(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel32, _advapi32, errors = _install_fake_windows_dlls(monkeypatch, close_ok=False, error=6)
    errors["value"] = 6
    mutex = windows_sync.BackendMutex(handle=123, already_exists=False)

    with pytest.raises(OSError, match="CloseHandle"):
        mutex.close()

    assert mutex.handle == 123
    kernel32.CloseHandle.assert_called_once()
