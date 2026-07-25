from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from typing import Any

BACKEND_MUTEX_NAME = r"Global\E-Rechnungs-Pruefer-Backend"
BACKEND_MUTEX_SECURITY_SDDL = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;LS)(A;;GA;;;IU)"
ERROR_ALREADY_EXISTS = 183
SDDL_REVISION_1 = 1


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_ulong),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_bool),
    ]


@dataclass(slots=True)
class BackendMutex:
    handle: int
    already_exists: bool

    def close(self) -> None:
        if not self.handle:
            return
        ctypes_windows: Any = ctypes
        kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_bool
        if not close_handle(ctypes.c_void_p(self.handle)):
            raise OSError(ctypes_windows.get_last_error(), "CloseHandle ist für den Backend-Mutex fehlgeschlagen.")
        self.handle = 0


def create_backend_mutex() -> BackendMutex:
    if sys.platform != "win32":
        raise OSError("Der maschinenweite Backend-Mutex ist ausschließlich unter Windows verfügbar.")

    ctypes_windows: Any = ctypes
    kernel32 = ctypes_windows.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes_windows.WinDLL("advapi32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = ctypes.c_bool
    if not convert(BACKEND_MUTEX_SECURITY_SDDL, SDDL_REVISION_1, ctypes.byref(descriptor), None):
        raise OSError(ctypes_windows.get_last_error(), "Die DACL für den Backend-Mutex konnte nicht erzeugt werden.")

    attributes = _SecurityAttributes(
        nLength=ctypes.sizeof(_SecurityAttributes),
        lpSecurityDescriptor=descriptor,
        bInheritHandle=False,
    )
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.POINTER(_SecurityAttributes), ctypes.c_bool, ctypes.c_wchar_p]
    create_mutex.restype = ctypes.c_void_p
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    try:
        ctypes_windows.set_last_error(0)
        handle = create_mutex(ctypes.byref(attributes), False, BACKEND_MUTEX_NAME)
        error = ctypes_windows.get_last_error()
        if not handle:
            raise OSError(error, "Der maschinenweite Backend-Mutex konnte nicht geöffnet werden.")
        return BackendMutex(handle=int(handle), already_exists=error == ERROR_ALREADY_EXISTS)
    finally:
        local_free(descriptor)
