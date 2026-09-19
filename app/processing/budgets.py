"""Finite resource profiles; independent from settings discovery and HTTP."""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, fields, replace

MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ProcessingBudgets:
    max_jobs: int = 2
    start_seconds: float = 15.0
    python_seconds: float = 30.0
    base_job_seconds: float = 60.0
    cleanup_seconds: float = 5.0
    send_seconds: float = 30.0
    python_start_memory_bytes: int = 2048 * MIB
    python_memory_bytes: int = 768 * MIB
    supervisor_memory_bytes: int = 512 * MIB
    java_memory_bytes: int = 4096 * MIB
    java_heap_bytes: int = 512 * MIB
    diagnostic_bytes: int = 64 * 1024

    @classmethod
    def for_platform(cls, platform: str | None = None) -> ProcessingBudgets:
        """Choose once in the parent; children receive the concrete IPC snapshot."""
        budgets = cls()
        if (platform or sys.platform) == "darwin":
            # Native allocator reservations require more AS headroom on macOS.
            # These are not RSS allowances or Windows committed-memory limits.
            return replace(
                budgets,
                python_start_memory_bytes=4096 * MIB,
                python_memory_bytes=1536 * MIB,
                supervisor_memory_bytes=1024 * MIB,
            )
        return budgets

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name.endswith("_seconds"):
                valid = type(value) in (int, float) and math.isfinite(value) and 0 < value <= 360
            else:
                valid = type(value) is int and 0 < value <= 4 * 1024**3
            if not valid:
                raise ValueError(f"Ungültiges Verarbeitungsbudget: {field.name}.")
        if self.max_jobs > 2 or self.java_heap_bytes >= self.java_memory_bytes:
            raise ValueError("Unzulässiges paralleles oder Java-Verarbeitungsbudget.")
        if self.start_seconds + self.python_seconds > self.base_job_seconds:
            raise ValueError("Die Verarbeitungsfristen sind widersprüchlich.")
        if self.python_memory_bytes >= self.python_start_memory_bytes:
            raise ValueError("Das Startprofil muss die vertrauenswürdigen Bibliotheksimporte abdecken.")

    def job_seconds(self, *, official: bool, kosit_seconds: int) -> float:
        if type(official) is not bool or type(kosit_seconds) is not int or not 1 <= kosit_seconds <= 300:
            raise ValueError("Ungültige KoSIT-Verarbeitungsfrist.")
        return self.base_job_seconds + (kosit_seconds if official else 0)


class ProcessingError(Exception):
    def __init__(self, status: int, error_type: str, detail: str) -> None:
        self.status = status
        self.error_type = error_type
        self.detail = detail
        self.diagnostic: dict[str, str] | None = None
        super().__init__(detail)


def unavailable() -> ProcessingError:
    return ProcessingError(
        503, "processing_unavailable_error", "Die geschützte Rechnungsverarbeitung ist derzeit nicht verfügbar."
    )


def timed_out() -> ProcessingError:
    return ProcessingError(504, "processing_timeout_error", "Die zulässige Verarbeitungszeit wurde überschritten.")


def worker_failed() -> ProcessingError:
    return ProcessingError(500, "processing_worker_error", "Die geschützte Rechnungsverarbeitung wurde unterbrochen.")
