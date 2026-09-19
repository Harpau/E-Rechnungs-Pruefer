"""macOS watchdog in the bound job group; never accepts invoice data or arbitrary PIDs to kill.

The parent launches this direct child in the supervisor's process group and
keeps the supervisor unreaped until its last group kill. Only the parent owns
the liveness writer; descendants must not inherit it. All observed failures
after group verification terminate the watchdog's own group, including itself.
"""

from __future__ import annotations

import math
import os
import select
import signal
import stat
import sys
from time import monotonic
from typing import Any, NoReturn

from .protocol import VERSION, write_control

MAX_WATCH_SECONDS = 400.0
POLL_SECONDS = 0.1


class WatchdogError(RuntimeError):
    """A private watchdog could not establish or maintain its binding."""


def _validate_pipes(liveness_fd: int, ready_fd: int) -> None:
    if sys.platform != "darwin":
        raise WatchdogError("Native macOS-Kanäle sind erforderlich.")
    import fcntl

    if any(type(fd) is not int or fd < 0 for fd in (liveness_fd, ready_fd)) or liveness_fd == ready_fd:
        raise WatchdogError("Ungültige Wächterkanäle.")
    for fd, access in ((liveness_fd, os.O_RDONLY), (ready_fd, os.O_WRONLY)):
        if not stat.S_ISFIFO(os.fstat(fd).st_mode) or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != access:
            raise WatchdogError("Wächterkanal ist keine gebundene Pipe mit korrekter Richtung.")
    os.set_blocking(liveness_fd, False)
    os.set_blocking(ready_fd, False)


def _register(parent_pid: int, supervisor_pid: int, liveness_fd: int) -> Any:
    if sys.platform != "darwin":
        raise WatchdogError("Native macOS-Prozessüberwachung ist erforderlich.")
    queue = select.kqueue()
    try:
        events = [
            select.kevent(pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD, fflags=select.KQ_NOTE_EXIT)
            for pid in (parent_pid, supervisor_pid)
        ]
        events.append(select.kevent(liveness_fd, filter=select.KQ_FILTER_READ, flags=select.KQ_EV_ADD))
        queue.control(events, 0, 0)
        return queue
    except BaseException:
        queue.close()
        raise


def _is_process_exit(event: Any) -> bool:
    if sys.platform != "darwin":
        raise WatchdogError("Native macOS-Prozessüberwachung ist erforderlich.")
    return bool(event.filter == select.KQ_FILTER_PROC and event.fflags & select.KQ_NOTE_EXIT)


def _send_ready(fd: int, record: dict[str, object]) -> None:
    # The small frame must fit immediately; a blocked/broken ready channel is a failure.
    with os.fdopen(fd, "wb", buffering=0, closefd=False) as stream:
        write_control(stream, record)


def _check_liveness(fd: int) -> None:
    try:
        value = os.read(fd, 1)
    except BlockingIOError:
        return
    if not value:
        raise WatchdogError("Der übergeordnete Kontrollkanal wurde geschlossen.")
    raise WatchdogError("Der Livenesskanal darf keine Nutzdaten enthalten.")


def _check_parent(parent_pid: int, parent_pgid: int, supervisor_pid: int, session_id: int) -> None:
    if sys.platform != "darwin":
        raise WatchdogError("Native macOS-Prozessüberwachung ist erforderlich.")
    if (
        os.getppid() != parent_pid
        or os.getpgid(parent_pid) != parent_pgid
        or os.getsid(parent_pid) != session_id
        or os.getpgid(supervisor_pid) != supervisor_pid
        or os.getsid(supervisor_pid) != session_id
    ):
        raise WatchdogError("Die gebundenen Kontrollprozesse sind nicht mehr vorhanden.")


def _kill_own_bound_group(supervisor_pid: int, parent_pgid: int, session_id: int) -> NoReturn:
    if sys.platform != "darwin":
        raise WatchdogError("Native macOS-Prozessgruppen sind erforderlich.")
    group = os.getpgrp()
    if group != supervisor_pid or group == parent_pgid or os.getsid(0) != session_id:
        raise WatchdogError("Veränderte Prozessgruppenbindung; kein unsicherer Kill-Fallback.")
    os.killpg(group, signal.SIGKILL)
    raise WatchdogError("Die gebundene Prozessgruppe wurde nicht beendet.")  # Only reachable in mocked tests.


def run_watchdog(
    *,
    parent_pid: int,
    parent_pgid: int,
    supervisor_pid: int,
    session_id: int,
    liveness_fd: int,
    ready_fd: int,
    deadline: float,
) -> NoReturn:
    """Emit READY only after binding; any later termination kills this exact job group."""
    if sys.platform != "darwin":
        raise WatchdogError("Dieser Wächter benötigt die native macOS-kqueue-Prozessüberwachung.")
    # Darwin's initial session/process group can be 0. These are compared
    # identities only; the exclusive group we may signal remains a PID > 1.
    # https://github.com/apple-oss-distributions/xnu/blob/xnu-10063.121.3/bsd/kern/bsd_init.c
    if any(type(value) is not int or not 1 < value < 2**31 for value in (parent_pid, supervisor_pid)) or any(
        type(value) is not int or not 0 <= value < 2**31 for value in (parent_pgid, session_id)
    ):
        raise WatchdogError("Ungültige Kontrollprozessidentität.")
    now = monotonic()
    if type(deadline) not in (int, float) or not 0 < deadline <= now + MAX_WATCH_SECONDS or not math.isfinite(deadline):
        raise WatchdogError("Ungültige absolute Wächterfrist.")
    if (
        os.getpgrp() != supervisor_pid
        or supervisor_pid == parent_pgid
        or os.getpid() in (parent_pid, supervisor_pid)
        or os.getsid(0) != session_id
    ):
        raise WatchdogError("Der Wächter gehört nicht zur exklusiv gebundenen Auftragsgruppe.")
    # From here the group is pinned by this watchdog itself. Never target a
    # foreign numeric PID; startup races now also terminate our own job group.
    queue = None
    try:
        _validate_pipes(liveness_fd, ready_fd)
        _check_parent(parent_pid, parent_pgid, supervisor_pid, session_id)
        queue = _register(parent_pid, supervisor_pid, liveness_fd)
        _check_parent(parent_pid, parent_pgid, supervisor_pid, session_id)
        events = queue.control(None, 3, 0)
        if any(_is_process_exit(event) or getattr(event, "flags", 0) & select.KQ_EV_ERROR for event in events):
            raise WatchdogError("Ein Kontrollprozess wurde während des Wächterstarts beendet.")
        _check_liveness(liveness_fd)
        if monotonic() >= deadline:
            raise WatchdogError("Die Wächterfrist ist bereits abgelaufen.")
        _send_ready(
            ready_fd,
            {
                "type": "ready",
                "role": "watchdog",
                "protocol": VERSION,
                "pid": os.getpid(),
                "supervisor_pid": supervisor_pid,
                "parent_pid": parent_pid,
            },
        )
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise WatchdogError("Die absolute Auftragsfrist ist abgelaufen.")
            _check_parent(parent_pid, parent_pgid, supervisor_pid, session_id)
            events = queue.control(None, 3, min(POLL_SECONDS, remaining))
            if any(_is_process_exit(event) or getattr(event, "flags", 0) & select.KQ_EV_ERROR for event in events):
                raise WatchdogError("Ein gebundener Kontrollprozess wurde beendet.")
            _check_liveness(liveness_fd)
    finally:
        try:
            if queue is not None:
                queue.close()
        finally:
            _kill_own_bound_group(supervisor_pid, parent_pgid, session_id)
