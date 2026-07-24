from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Thread
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .desktop_security import OneTimeBrowserSessions, desktop_bootstrap_url
from .windows_service_config import SERVICE_NAME, SERVICE_SID

SERVICE_PIPE_NAME = r"\\.\pipe\E-Rechnungs-Pruefer-Service-Browser-v1"
PIPE_SECURITY_SDDL = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x00100183;;;IU)"
MAXIMUM_IPC_MESSAGE_BYTES = 4096
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_CLIENT_MESSAGE_TIMEOUT_SECONDS = 5.0
ERROR_NO_DATA = 232
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_PIPE_LISTENING = 536
SE_GROUP_ENABLED = 0x00000004
PIPE_CLIENT_ACCESS = 0x00000001 | 0x00000002 | 0x00000080 | 0x00000100 | 0x00100000


@dataclass(frozen=True)
class IpcServerDiagnostic:
    """Non-sensitive details about one failed server-side IPC phase."""

    phase: str
    exception_type: str
    winerror: int | None


IpcErrorCallback = Callable[[IpcServerDiagnostic], None]


def _safe_diagnostic(phase: str, exc: BaseException) -> IpcServerDiagnostic:
    """Extract diagnostics without retaining messages, requests, URLs, or tokens."""

    winerror = getattr(exc, "winerror", None)
    return IpcServerDiagnostic(
        phase=phase,
        exception_type=type(exc).__name__,
        winerror=winerror if isinstance(winerror, int) else None,
    )


def _json_message(payload: dict[str, object]) -> bytes:
    message = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(message) > MAXIMUM_IPC_MESSAGE_BYTES:
        raise ValueError("Die lokale IPC-Nachricht überschreitet die Größenbegrenzung.")
    return message


def encode_open_request() -> bytes:
    return _json_message({"action": "open", "version": 1})


def decode_open_request(message: bytes) -> str:
    if not message or len(message) > MAXIMUM_IPC_MESSAGE_BYTES:
        raise ValueError("Die lokale IPC-Nachricht ist leer oder zu groß.")
    try:
        payload = json.loads(message.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Die lokale IPC-Nachricht ist kein gültiges JSON.") from exc
    if payload != {"action": "open", "version": 1}:
        raise ValueError("Der lokale IPC-Befehl ist nicht zulässig.")
    return "open"


def encode_open_response(url: str) -> bytes:
    _validate_bootstrap_url(url)
    return _json_message({"url": url, "version": 1})


def decode_open_response(message: bytes) -> str:
    if not message or len(message) > MAXIMUM_IPC_MESSAGE_BYTES:
        raise ValueError("Die lokale IPC-Nachricht ist leer oder zu groß.")
    try:
        payload = json.loads(message.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Die lokale IPC-Antwort ist kein gültiges JSON.") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1 or set(payload) != {"url", "version"}:
        raise ValueError("Die lokale IPC-Antwort verwendet ein unbekanntes Format.")
    url = payload.get("url")
    if not isinstance(url, str):
        raise ValueError("Die lokale IPC-Antwort enthält keine zulässige Browseradresse.")
    try:
        _validate_bootstrap_url(url)
    except ValueError as exc:
        raise ValueError("Die lokale IPC-Antwort enthält keine zulässige Browseradresse.") from exc
    return url


def encode_open_acknowledgement(response: bytes) -> bytes:
    decode_open_response(response)
    return _json_message(
        {
            "response_sha256": hashlib.sha256(response).hexdigest(),
            "version": 1,
        }
    )


def decode_open_acknowledgement(message: bytes, response: bytes) -> None:
    if not message or len(message) > MAXIMUM_IPC_MESSAGE_BYTES:
        raise ValueError("Die lokale IPC-Bestätigung ist leer oder zu groß.")
    try:
        payload = json.loads(message.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Die lokale IPC-Bestätigung ist kein gültiges JSON.") from exc
    expected_digest = hashlib.sha256(response).hexdigest()
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or set(payload) != {"response_sha256", "version"}
        or not isinstance(payload.get("response_sha256"), str)
        or not hmac.compare_digest(payload["response_sha256"], expected_digest)
    ):
        raise ValueError("Die lokale IPC-Antwort wurde nicht gültig bestätigt.")


def _validate_bootstrap_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("Die lokale Browseradresse ist ungültig.") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65535
        or parsed.path != "/desktop/bootstrap"
        or parsed.fragment
        or set(query) != {"token"}
        or len(query["token"]) != 1
        or not query["token"][0]
    ):
        raise ValueError("Die lokale Browseradresse ist ungültig.")


def _windows_modules() -> tuple[Any, ...]:
    if sys.platform != "win32":
        raise OSError("Die lokale Dienst-IPC ist ausschließlich unter Windows verfügbar.")
    try:
        import ntsecuritycon
        import pywintypes
        import win32api
        import win32con
        import win32file
        import win32pipe
        import win32security
        import win32service
    except ImportError as exc:
        raise RuntimeError("pywin32 fehlt; die lokale Dienst-IPC ist nicht verfügbar.") from exc
    return (
        ntsecuritycon,
        pywintypes,
        win32api,
        win32con,
        win32file,
        win32pipe,
        win32security,
        win32service,
    )


def _pipe_security_attributes() -> Any:
    ntsecuritycon, pywintypes, _win32api, _win32con, _win32file, _win32pipe, win32security, _win32service = (
        _windows_modules()
    )
    dacl = win32security.ACL()
    for sid in (
        win32security.ConvertStringSidToSid("S-1-5-18"),
        win32security.ConvertStringSidToSid("S-1-5-32-544"),
        win32security.ConvertStringSidToSid(SERVICE_SID),
    ):
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.GENERIC_ALL, sid)
    interactive = win32security.ConvertStringSidToSid("S-1-5-4")
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        PIPE_CLIENT_ACCESS,
        interactive,
    )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def _client_is_interactive(pipe_handle: Any) -> bool:
    (
        _ntsecuritycon,
        _pywintypes,
        win32api,
        win32con,
        _win32file,
        _win32pipe,
        win32security,
        _win32service,
    ) = _windows_modules()
    win32security.ImpersonateNamedPipeClient(pipe_handle)
    try:
        token = win32security.OpenThreadToken(win32api.GetCurrentThread(), win32con.TOKEN_QUERY, True)
        try:
            groups = win32security.GetTokenInformation(token, win32security.TokenGroups)
            session_id = int(win32security.GetTokenInformation(token, win32security.TokenSessionId))
            interactive = win32security.ConvertStringSidToSid("S-1-5-4")
            return session_id != 0 and any(
                sid == interactive and attributes & SE_GROUP_ENABLED for sid, attributes in groups
            )
        finally:
            token.Close()
    finally:
        win32security.RevertToSelf()


class BrowserPipeServer:
    def __init__(
        self,
        sessions: OneTimeBrowserSessions,
        port: int,
        *,
        error_callback: IpcErrorCallback | None = None,
    ) -> None:
        self.sessions = sessions
        self.port = port
        self.error_callback = error_callback or (lambda _message: None)
        self._stop = Event()
        self._ready = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Der lokale IPC-Server wurde bereits gestartet.")
        self._thread = Thread(target=self._serve, name="E-Rechnungs-Pruefer-Dienst-IPC", daemon=True)
        self._thread.start()
        if not self._ready.wait(10) or not self._thread.is_alive():
            raise RuntimeError("Der lokale IPC-Server wurde nicht rechtzeitig betriebsbereit.")

    def _serve(self) -> None:
        phase = "load-windows-api"
        pipe_handle = None
        win32file = None
        try:
            (
                _ntsecuritycon,
                pywintypes,
                _win32api,
                _win32con,
                win32file,
                win32pipe,
                _win32security,
                _win32service,
            ) = _windows_modules()
            phase = "create-pipe"
            pipe_handle = win32pipe.CreateNamedPipe(
                SERVICE_PIPE_NAME,
                win32pipe.PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
                win32pipe.PIPE_TYPE_MESSAGE
                | win32pipe.PIPE_READMODE_MESSAGE
                | win32pipe.PIPE_WAIT
                | PIPE_REJECT_REMOTE_CLIENTS,
                1,
                MAXIMUM_IPC_MESSAGE_BYTES,
                MAXIMUM_IPC_MESSAGE_BYTES,
                0,
                _pipe_security_attributes(),
            )
            self._ready.set()
            while not self._stop.is_set():
                connected = False
                phase = "connect-client"
                try:
                    try:
                        win32pipe.ConnectNamedPipe(pipe_handle, None)
                    except pywintypes.error as exc:
                        if exc.winerror != 535:  # ERROR_PIPE_CONNECTED
                            raise
                    connected = True
                    if self._stop.is_set():
                        continue
                    exchange_deadline = time.monotonic() + PIPE_CLIENT_MESSAGE_TIMEOUT_SECONDS
                    phase = "set-message-mode"
                    win32pipe.SetNamedPipeHandleState(
                        pipe_handle,
                        win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_NOWAIT,
                        None,
                        None,
                    )
                    phase = "read-request"
                    message = self._read_message_with_deadline(
                        pipe_handle,
                        pywintypes,
                        win32file,
                        deadline=exchange_deadline,
                    )
                    phase = "decode-request"
                    decode_open_request(bytes(message))
                    phase = "authorize-client"
                    if not _client_is_interactive(pipe_handle):
                        raise PermissionError("Der lokale IPC-Client ist keine interaktive Windows-Sitzung.")
                    phase = "issue-bootstrap"
                    bootstrap = self.sessions.issue_bootstrap()
                    phase = "encode-response"
                    response = encode_open_response(desktop_bootstrap_url(self.port, bootstrap))
                    phase = "write-response"
                    _write_message_with_deadline(
                        pipe_handle,
                        response,
                        deadline=exchange_deadline,
                        pywintypes=pywintypes,
                        win32file=win32file,
                        description="Antwort",
                    )
                    phase = "read-acknowledgement"
                    acknowledgement = self._read_message_with_deadline(
                        pipe_handle,
                        pywintypes,
                        win32file,
                        deadline=exchange_deadline,
                        timeout_message="Der lokale IPC-Client hat die Antwort nicht rechtzeitig bestätigt.",
                    )
                    phase = "decode-acknowledgement"
                    decode_open_acknowledgement(acknowledgement, response)
                    # DisconnectNamedPipe discards unread response bytes. The
                    # acknowledgement proves that this exact response was read,
                    # so the documented flush cannot block on an idle client.
                    phase = "flush-response"
                    win32file.FlushFileBuffers(pipe_handle)
                except Exception as exc:
                    if not self._stop.is_set():
                        self.error_callback(_safe_diagnostic(phase, exc))
                        self._stop.wait(0.1)
                finally:
                    if connected:
                        try:
                            win32pipe.SetNamedPipeHandleState(
                                pipe_handle,
                                win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                                None,
                                None,
                            )
                        except Exception:
                            pass
                        try:
                            win32pipe.DisconnectNamedPipe(pipe_handle)
                        except Exception:
                            pass
        except Exception as exc:
            if not self._stop.is_set():
                self.error_callback(_safe_diagnostic(phase, exc))
        finally:
            self._ready.set()
            if pipe_handle is not None and win32file is not None:
                win32file.CloseHandle(pipe_handle)

    def _read_message_with_deadline(
        self,
        pipe_handle: Any,
        pywintypes: Any,
        win32file: Any,
        *,
        deadline: float | None = None,
        timeout_message: str = "Der lokale IPC-Client hat keine vollständige Anfrage gesendet.",
    ) -> bytes:
        deadline = deadline if deadline is not None else time.monotonic() + PIPE_CLIENT_MESSAGE_TIMEOUT_SECONDS
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                _result, message = win32file.ReadFile(pipe_handle, MAXIMUM_IPC_MESSAGE_BYTES)
                return bytes(message)
            except pywintypes.error as exc:
                if exc.winerror not in {ERROR_NO_DATA, ERROR_PIPE_LISTENING}:
                    raise
                self._stop.wait(0.05)
        raise TimeoutError(timeout_message)

    def stop(self) -> None:
        self.sessions.clear()
        self._stop.set()
        try:
            handle = _connect_to_pipe(validate_server=False)
            _windows_modules()[4].CloseHandle(handle)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("Der lokale IPC-Server konnte nicht beendet werden.")

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def query_service_process_id() -> int:
    (
        _ntsecuritycon,
        _pywintypes,
        _win32api,
        _win32con,
        _win32file,
        _win32pipe,
        _win32security,
        win32service,
    ) = _windows_modules()
    manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    service = None
    try:
        service = win32service.OpenService(manager, SERVICE_NAME, win32service.SERVICE_QUERY_STATUS)
        status = win32service.QueryServiceStatusEx(service)
        process_id = int(status.get("ProcessId", 0))
        if process_id <= 0:
            raise RuntimeError("Der E-Rechnungs-Prüfer-Dienst läuft nicht.")
        return process_id
    finally:
        if service is not None:
            win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(manager)


def _connect_to_pipe(*, validate_server: bool = True) -> Any:
    (
        _ntsecuritycon,
        _pywintypes,
        _win32api,
        win32con,
        win32file,
        win32pipe,
        _win32security,
        _win32service,
    ) = _windows_modules()
    win32pipe.WaitNamedPipe(SERVICE_PIPE_NAME, 5000)
    handle = win32file.CreateFile(
        SERVICE_PIPE_NAME,
        PIPE_CLIENT_ACCESS,
        0,
        None,
        win32con.OPEN_EXISTING,
        0,
        None,
    )
    try:
        if validate_server:
            actual_pid = int(win32pipe.GetNamedPipeServerProcessId(handle))
            expected_pid = query_service_process_id()
            if actual_pid != expected_pid:
                raise PermissionError("Der lokale Dienstkanal gehört nicht zum registrierten Windows-Dienst.")
        return handle
    except Exception:
        try:
            win32file.CloseHandle(handle)
        except Exception:
            pass
        raise


def _wait_for_client_pipe_retry(deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(min(0.05, remaining))


def _write_message_with_deadline(
    handle: Any,
    message: bytes,
    *,
    deadline: float,
    pywintypes: Any,
    win32file: Any,
    description: str,
) -> None:
    while time.monotonic() < deadline:
        try:
            _result, bytes_written = win32file.WriteFile(handle, message)
        except pywintypes.error as exc:
            if exc.winerror not in {ERROR_NO_DATA, ERROR_PIPE_LISTENING}:
                raise
        else:
            if int(bytes_written) == len(message):
                return
            if int(bytes_written) != 0:
                raise RuntimeError(f"Die lokale IPC-{description} wurde nur unvollständig gesendet.")
        _wait_for_client_pipe_retry(deadline)
    raise TimeoutError(f"Die lokale IPC-{description} konnte nicht innerhalb der Zeitgrenze gesendet werden.")


def _read_response_with_deadline(
    handle: Any,
    *,
    deadline: float,
    pywintypes: Any,
    win32file: Any,
) -> bytes:
    while time.monotonic() < deadline:
        try:
            _result, response = win32file.ReadFile(handle, MAXIMUM_IPC_MESSAGE_BYTES)
            return bytes(response)
        except pywintypes.error as exc:
            if exc.winerror not in {ERROR_NO_DATA, ERROR_PIPE_LISTENING}:
                raise
            _wait_for_client_pipe_retry(deadline)
    raise TimeoutError("Die lokale IPC-Antwort ist nicht innerhalb der Zeitgrenze eingetroffen.")


def _wait_for_server_disconnect(
    handle: Any,
    *,
    deadline: float,
    pywintypes: Any,
    win32file: Any,
) -> None:
    while time.monotonic() < deadline:
        try:
            _result, unexpected = win32file.ReadFile(handle, MAXIMUM_IPC_MESSAGE_BYTES)
        except pywintypes.error as exc:
            if exc.winerror in {ERROR_BROKEN_PIPE, ERROR_PIPE_NOT_CONNECTED}:
                return
            if exc.winerror not in {ERROR_NO_DATA, ERROR_PIPE_LISTENING}:
                raise
        else:
            raise RuntimeError(
                "Der lokale IPC-Server hat nach der Bestätigung unerwartete Daten gesendet"
                if unexpected
                else "Der lokale IPC-Server hat eine unerwartete leere Abschlussnachricht gesendet"
            )
        _wait_for_client_pipe_retry(deadline)
    raise TimeoutError("Der lokale IPC-Server hat den bestätigten Austausch nicht rechtzeitig abgeschlossen.")


def request_browser_url() -> str:
    (
        _ntsecuritycon,
        pywintypes,
        _win32api,
        _win32con,
        win32file,
        win32pipe,
        _win32security,
        _win32service,
    ) = _windows_modules()
    handle = _connect_to_pipe()
    try:
        win32pipe.SetNamedPipeHandleState(
            handle,
            win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_NOWAIT,
            None,
            None,
        )
        deadline = time.monotonic() + PIPE_CLIENT_MESSAGE_TIMEOUT_SECONDS
        _write_message_with_deadline(
            handle,
            encode_open_request(),
            deadline=deadline,
            pywintypes=pywintypes,
            win32file=win32file,
            description="Anfrage",
        )
        response = _read_response_with_deadline(
            handle,
            deadline=deadline,
            pywintypes=pywintypes,
            win32file=win32file,
        )
        url = decode_open_response(response)
        _write_message_with_deadline(
            handle,
            encode_open_acknowledgement(response),
            deadline=deadline,
            pywintypes=pywintypes,
            win32file=win32file,
            description="Bestätigung",
        )
        _wait_for_server_disconnect(
            handle,
            deadline=deadline,
            pywintypes=pywintypes,
            win32file=win32file,
        )
        return url
    finally:
        win32file.CloseHandle(handle)
