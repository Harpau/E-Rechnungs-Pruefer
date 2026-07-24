import ctypes
import os
import sys
from collections.abc import Sequence
from ctypes import wintypes

from app.windows_service import DIRECT_START_EXIT_CODE, main

_DIRECT_START_MESSAGE = (
    "Der E-Rechnungs-Prüfer-Dienst wird von der Windows-Dienstverwaltung gestartet "
    "und kann nicht direkt ausgeführt werden.\n\n"
    'Zum Öffnen der Anwendung verwenden Sie bitte "E-Rechnungs-Pruefer-Oeffnen.exe".'
)
_DIRECT_START_TITLE = "E-Rechnungs-Prüfer Dienst"
_MB_OK = 0x00000000
_MB_ICONINFORMATION = 0x00000040
_MB_SETFOREGROUND = 0x00010000


def _current_process_session_id() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        # The service package is x64-only, where Windows uses one calling
        # convention for these APIs; CDLL also keeps cross-platform type
        # checking independent of ctypes.WinDLL availability.
        kernel32 = ctypes.CDLL("kernel32", use_last_error=True)
        process_id_to_session_id = kernel32.ProcessIdToSessionId
        process_id_to_session_id.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        process_id_to_session_id.restype = wintypes.BOOL
        session_id = wintypes.DWORD()
        if not process_id_to_session_id(os.getpid(), ctypes.byref(session_id)):
            return None
        return int(session_id.value)
    except (AttributeError, OSError):
        return None


def _display_direct_start_message() -> None:
    try:
        user32 = ctypes.CDLL("user32", use_last_error=True)
        message_box = user32.MessageBoxW
        message_box.argtypes = [
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.UINT,
        ]
        message_box.restype = ctypes.c_int
        message_box(
            None,
            _DIRECT_START_MESSAGE,
            _DIRECT_START_TITLE,
            _MB_OK | _MB_ICONINFORMATION | _MB_SETFOREGROUND,
        )
    except (AttributeError, OSError):
        # The notice is optional; a missing UI API must not turn a controlled
        # direct launch into another bootloader exception.
        return


def _show_direct_start_notice() -> None:
    # Services run in isolated session 0. Never attempt interactive UI there.
    session_id = _current_process_session_id()
    if session_id is None or session_id == 0:
        return
    _display_direct_start_message()


def _run(argv: Sequence[str]) -> int:
    exit_code = main(argv)
    if exit_code == DIRECT_START_EXIT_CODE:
        _show_direct_start_notice()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(_run(sys.argv[1:]))
