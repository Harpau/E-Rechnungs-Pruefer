"""POSIX mechanisms called in a fresh trusted bootstrap before invoice input."""

from __future__ import annotations

import ctypes
import os
import signal
import sys
from pathlib import Path


def virtual_memory_bytes() -> int:
    if sys.platform == "darwin":

        class BasicInfo(ctypes.Structure):
            _fields_ = [
                ("virtual_size", ctypes.c_uint64),
                ("resident_size", ctypes.c_uint64),
                ("resident_size_max", ctypes.c_uint64),
                ("user_seconds", ctypes.c_int32),
                ("user_microseconds", ctypes.c_int32),
                ("system_seconds", ctypes.c_int32),
                ("system_microseconds", ctypes.c_int32),
                ("policy", ctypes.c_int32),
                ("suspend_count", ctypes.c_int32),
            ]

        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        library.mach_task_self.argtypes = []
        library.mach_task_self.restype = ctypes.c_uint32
        library.task_info.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        library.task_info.restype = ctypes.c_int
        info = BasicInfo()
        count = ctypes.c_uint32(ctypes.sizeof(info) // ctypes.sizeof(ctypes.c_uint32))
        status = library.task_info(library.mach_task_self(), 20, ctypes.byref(info), ctypes.byref(count))
        if status != 0 or count.value != 12:
            raise OSError("Der Prozessadressraum kann nicht sicher bestimmt werden.")
        return int(info.virtual_size)
    if sys.platform.startswith("linux"):
        return int(Path("/proc/self/statm").read_text(encoding="ascii").split()[0]) * os.sysconf("SC_PAGE_SIZE")
    raise OSError("Die POSIX-Prozessbegrenzung ist auf dieser Plattform nicht verfügbar.")


def bind_parent(expected_parent: int) -> None:
    if expected_parent <= 1 or os.getppid() != expected_parent:
        raise OSError("Der gebundene Elternprozess ist nicht mehr vorhanden.")
    if sys.platform.startswith("linux"):
        library = ctypes.CDLL(None, use_errno=True)
        prctl = library.prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        if prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Parent-Tod-Bindung konnte nicht hergestellt werden.")
        if os.getppid() != expected_parent:
            raise OSError("Der gebundene Elternprozess wurde während des Starts beendet.")


def apply_limits(*, memory_headroom: int, cpu_seconds: int) -> dict[str, int]:
    if sys.platform == "win32":
        raise OSError("POSIX-Ressourcenlimits sind unter Windows nicht verfügbar.")
    import resource

    if type(memory_headroom) is not int or not 1024**2 <= memory_headroom <= 8 * 1024**3:
        raise ValueError("Ungültiges Prozessspeicherbudget.")
    if type(cpu_seconds) is not int or not 1 <= cpu_seconds <= 360:
        raise ValueError("Ungültiges CPU-Budget.")
    baseline = virtual_memory_bytes()
    # Shared mappings on macOS occupy tens of GiB of virtual address space.
    # Charge the fixed workload headroom on top, never infer it from invoices.
    baseline_ceiling = 64 * 1024**3 if sys.platform == "darwin" else 1024**3
    if not 0 < baseline <= baseline_ceiling:
        raise OSError("Unzulässiger Prozessgrundbedarf.")
    address_limit = baseline + memory_headroom
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (address_limit, address_limit))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
    signal.signal(signal.SIGXCPU, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGXCPU})
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    if resource.getrlimit(resource.RLIMIT_AS) != (address_limit, address_limit):
        raise OSError("Das Prozessspeicherlimit ist nicht wirksam gesetzt.")
    return {"baseline_as_bytes": baseline, "address_space_bytes": address_limit, "cpu_seconds": cpu_seconds}


def seal_runtime_memory(*, memory_headroom: int) -> dict[str, int]:
    """Lower the fixed start limit after trusted imports, still before input."""
    if sys.platform == "win32":
        raise OSError("POSIX-Ressourcenlimits sind unter Windows nicht verfügbar.")
    import resource

    if type(memory_headroom) is not int or not 1024**2 <= memory_headroom <= 4 * 1024**3:
        raise ValueError("Ungültiges Prozessspeicherbudget.")
    baseline = virtual_memory_bytes()
    maximum_baseline = 64 * 1024**3 if sys.platform == "darwin" else 1024**3
    limit = baseline + memory_headroom
    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if not 0 < baseline <= maximum_baseline or hard == resource.RLIM_INFINITY or limit > hard:
        raise OSError("Die Bibliotheksimporte überschreiten das geprüfte Startprofil.")
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    if resource.getrlimit(resource.RLIMIT_AS) != (limit, limit):
        raise OSError("Das Laufzeitspeicherlimit konnte nicht abgesenkt werden.")
    return {"baseline_as_bytes": baseline, "address_space_bytes": limit}
