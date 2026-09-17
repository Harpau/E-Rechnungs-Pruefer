"""Trusted process and IPC creation. No invoice or application imports."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, BinaryIO, cast

ROLE_FLAG = "--einvoice-processing"
_select_api: Any = select


def child_environment() -> dict[str, str]:
    result = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONUTF8": "1"}
    if sys.platform == "win32":
        for name in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE"):
            if value := os.environ.get(name):
                result[name] = value
    if getattr(sys, "frozen", False):
        result["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return result


def role_command(role: str, arguments: Sequence[str]) -> list[str]:
    if role not in {"supervisor", "worker", "java", "watchdog"}:
        raise ValueError("Unbekannte interne Prozessrolle.")
    if getattr(sys, "frozen", False):
        prefix = [sys.executable]
    else:
        prefix = [sys.executable, "-I", str(Path(__file__).with_name("bootstrap.py"))]
    return [*prefix, ROLE_FLAG, role, *arguments]


def _native_handle(fd: int) -> int:
    if sys.platform != "win32":
        return fd
    import msvcrt

    os.set_inheritable(fd, True)
    return int(msvcrt.get_osfhandle(fd))


def inherited_file(value: int, mode: str) -> BinaryIO:
    if value < 0 or mode not in {"rb", "wb"}:
        raise ValueError("Ungültiger interner Prozesskanal.")
    if sys.platform == "win32":
        import msvcrt

        flags = os.O_RDONLY if mode == "rb" else os.O_WRONLY
        value = msvcrt.open_osfhandle(value, flags | os.O_BINARY)
    try:
        os.set_inheritable(value, False)
        return cast(BinaryIO, os.fdopen(value, mode, buffering=0))
    except BaseException:
        os.close(value)
        raise


class Child:
    def __init__(self, process: Any, incoming: BinaryIO, outgoing: BinaryIO, *, job: Any = None) -> None:
        self.process = process
        self.incoming = incoming
        self.outgoing = outgoing
        self.job = job
        self.pid: int = process.pid
        self._finish_result: bool | None = None
        self._channels_closed = False
        self._channel_close_failed = False

    def close_channels(self) -> bool:
        if not self._channels_closed:
            self._channels_closed = True
            for stream in (self.incoming, self.outgoing):
                try:
                    stream.close()
                except Exception:
                    self._channel_close_failed = True
        return not self._channel_close_failed

    def kill(self) -> None:
        try:
            if self.job is not None:
                self.job.terminate()
            elif self.process.poll() is None:
                self.process.kill()
        except ProcessLookupError:
            pass

    def finish(self, deadline: float) -> bool:
        if self._finish_result is not None:
            return self._finish_result
        clean = True
        try:
            self.kill()
        except Exception:
            clean = False
        try:
            self.process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if self.job is not None:
                while self.job.active_process_count():
                    if time.monotonic() >= deadline:
                        clean = False
                        break
                    time.sleep(0.01)
        except Exception:
            clean = False
        finally:
            if not self.close_channels():
                clean = False
            if self.job is not None:
                for close in (self.job.close, self.process.close):
                    try:
                        close()
                    except Exception:
                        clean = False
        self._finish_result = clean
        return clean


class PreparedRole:
    """A parent's two pipe ends may be inherited by the trusted broker only."""

    def __init__(self) -> None:
        descriptors: list[int] = []
        try:
            self.input_read, input_write = os.pipe()
            descriptors.extend((self.input_read, input_write))
            output_read, self.output_write = os.pipe()
            descriptors.extend((output_read, self.output_write))
            self.incoming = os.fdopen(output_read, "rb", buffering=0)
            descriptors.remove(output_read)
            self.outgoing = os.fdopen(input_write, "wb", buffering=0)
            descriptors.remove(input_write)
        except BaseException:
            while descriptors:
                descriptor = descriptors.pop()
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            for name in ("incoming", "outgoing"):
                stream = getattr(self, name, None)
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            raise
        self.child_closed = False
        self._child_descriptors = [self.input_read, self.output_write]
        self._broker_closed = False
        self._close_failed = False

    def broker_descriptors(self) -> tuple[int, int]:
        return self.incoming.fileno(), self.outgoing.fileno()

    def close_child(self) -> None:
        while self._child_descriptors:
            # A failed close may already have released the numeric descriptor;
            # consume it before the syscall and never close a reused number.
            descriptor = self._child_descriptors.pop()
            try:
                os.close(descriptor)
            except OSError:
                self._close_failed = True
        self.child_closed = True
        if self._close_failed:
            raise OSError("Das Schließen der internen Prozesskanäle ist unbestätigt.")

    def close(self) -> None:
        try:
            self.close_child()
        except OSError:
            self._close_failed = True
        if not self._broker_closed:
            self._broker_closed = True
            for stream in (self.incoming, self.outgoing):
                try:
                    stream.close()
                except OSError:
                    self._close_failed = True
        if self._close_failed:
            raise OSError("Das Schließen der internen Prozesskanäle ist unbestätigt.")


def spawn_role(
    role: str,
    *,
    group: int | None = None,
    job: Any = None,
    arguments: Sequence[str] = (),
    extra_fds: Sequence[int] = (),
    prepared: PreparedRole | None = None,
    job_handles: Sequence[int] = (),
) -> Child:
    descriptors: set[int] = set()
    opened: list[BinaryIO] = []
    process: Any = None
    try:
        if prepared is not None:
            input_read, output_write = prepared.input_read, prepared.output_write
        else:
            input_read, input_write = os.pipe()
            descriptors.update((input_read, input_write))
            output_read, output_write = os.pipe()
            descriptors.update((output_read, output_write))
        inherited = (_native_handle(input_read), _native_handle(output_write))
        extras = tuple(_native_handle(fd) for fd in extra_fds)
        command = role_command(
            role, [str(os.getpid()), *(str(h) for h in inherited), *arguments, *(str(h) for h in extras)]
        )
        if sys.platform == "win32":
            if job is None:
                raise OSError("Der geschützte Prozess benötigt ein Windows-Job-Objekt.")
            process = job.spawn(
                command, child_environment(), inherited_handles=(*inherited, *extras), job_handles=job_handles
            )
        else:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(input_read, output_write, *extra_fds),
                env=child_environment(),
                process_group=group,
            )
        if prepared is not None:
            prepared.close_child()
            return Child(process, prepared.incoming, prepared.outgoing, job=job)
        descriptors.remove(input_read)
        os.close(input_read)
        descriptors.remove(output_write)
        os.close(output_write)
        incoming = os.fdopen(output_read, "rb", buffering=0)
        opened.append(incoming)
        descriptors.remove(output_read)
        outgoing = os.fdopen(input_write, "wb", buffering=0)
        opened.append(outgoing)
        descriptors.remove(input_write)
        return Child(process, incoming, outgoing, job=job)
    except BaseException:
        if process is not None:
            if job is not None:
                job.terminate()
            else:
                process.kill()
            process.wait(timeout=5)
            if job is not None:
                process.close()
        for stream in opened:
            stream.close()
        raise
    finally:
        close_error: OSError | None = None
        while descriptors:
            # The descriptor may have been released even if close raises.
            # Consume ownership first and attempt all other owned ends.
            descriptor = descriptors.pop()
            try:
                os.close(descriptor)
            except OSError as exc:
                if close_error is None:
                    close_error = exc
        if close_error is not None:
            raise close_error


class ProcessTree:
    """Parent-only ownership. Keep the group leader unreaped until the last group kill."""

    def __init__(self) -> None:
        self.supervisor: Child | None = None
        self.watchdog: Child | None = None
        self.cancelled = threading.Event()
        self.cancelled_at: float | None = None
        self._lock = threading.Lock()
        self._killed = False
        self._kill_error: OSError | None = None
        self.cleaned = False
        self.exits: ExitBindings | None = None
        self.roles: dict[str, Child] = {}
        self.outer_job: Any = None

    def install_watchdog(self, watchdog: Child) -> None:
        with self._lock:
            self.watchdog = watchdog
            if self.cancelled.is_set():
                watchdog.kill()

    def install_role(self, role: str, child: Child) -> None:
        with self._lock:
            if role in self.roles:
                raise ValueError("Prozessrolle ist bereits gebunden.")
            self.roles[role] = child
            if self.cancelled.is_set():
                child.kill()

    def install(self, supervisor: Child) -> None:
        with self._lock:
            self.supervisor = supervisor
            if sys.platform != "win32":
                self.exits = ExitBindings([supervisor.pid])
            if self.cancelled.is_set():
                self._kill_locked()

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            if self.cancelled_at is None:
                self.cancelled_at = time.monotonic()
            try:
                self._kill_locked()
            except OSError as exc:
                self._kill_error = exc

    def _kill_locked(self) -> None:
        child = self.supervisor
        if child is None or self._killed:
            return
        # Mark the last numeric group operation before wait()/poll() may reap.
        self._killed = True
        if sys.platform == "win32":
            if self.outer_job is not None:
                self.outer_job.terminate()
            else:
                child.kill()
        else:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def healthy(self) -> bool:
        watcher = self.watchdog
        if self._killed:
            return True
        supervisor = self.supervisor
        supervisor_alive = supervisor is None or (
            not self.exits.exited() if self.exits is not None else supervisor.process.poll() is None
        )
        return supervisor_alive and (watcher is None or watcher.process.poll() is None)

    def cleanup(self, seconds: float) -> bool:
        self.cancel()
        deadline = min(time.monotonic(), self.cancelled_at or time.monotonic()) + seconds
        children = [*self.roles.values(), *(c for c in (self.watchdog, self.supervisor) if c is not None)]
        ok = True
        for child in children:
            if child._finish_result is not None:
                ok = child._finish_result and ok
                continue
            try:
                if self._kill_error is not None:
                    # All starts have finished. The last group signal already
                    # happened; owned process handles/PIDs are safe to reap now.
                    child.kill()
            except Exception:
                ok = False
            try:
                child.process.wait(timeout=max(0.001, deadline - time.monotonic()))
                if child.job is not None:
                    while child.job.active_process_count():
                        if time.monotonic() >= deadline:
                            ok = False
                            break
                        time.sleep(0.01)
            except Exception:
                ok = False
            finally:
                try:
                    if not child.close_channels():
                        ok = False
                except Exception:
                    ok = False
                if child.job is not None:
                    for close in (child.job.close, child.process.close):
                        try:
                            close()
                        except Exception:
                            ok = False
        if self.exits is not None:
            try:
                ok = self.exits.wait(deadline) and ok
            except Exception:
                ok = False
            finally:
                try:
                    self.exits.close()
                except Exception:
                    ok = False
        if self.outer_job is not None:
            try:
                while self.outer_job.active_process_count():
                    if time.monotonic() >= deadline:
                        ok = False
                        break
                    time.sleep(0.01)
            except Exception:
                ok = False
            finally:
                try:
                    self.outer_job.close()
                except Exception:
                    ok = False
        self.cleaned = ok
        return ok


class ExitBindings:
    """Kernel identities stay bound even when a numeric child PID is reused."""

    def __init__(self, pids: Sequence[int]) -> None:
        self.descriptors: list[int] = []
        self.queue: Any = None
        self.pending = set(pids)
        try:
            if sys.platform == "darwin":
                self.queue = select.kqueue()
                changes = [
                    select.kevent(
                        pid,
                        filter=select.KQ_FILTER_PROC,
                        flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
                        fflags=select.KQ_NOTE_EXIT,
                    )
                    for pid in pids
                ]
                self._record(self.queue.control(changes, len(changes), 0))
            elif sys.platform.startswith("linux"):
                for pid in pids:
                    self.descriptors.append(os.pidfd_open(pid, 0))
            else:
                raise OSError("Native Kindprozessidentitäten sind nicht verfügbar.")
        except BaseException:
            self.close()
            raise

    def _record(self, events: Sequence[Any]) -> None:
        for event in events:
            if event.flags & _select_api.KQ_EV_ERROR:
                raise OSError("Kindprozessüberwachung konnte nicht gebunden werden.")
            if event.filter == _select_api.KQ_FILTER_PROC and event.fflags & _select_api.KQ_NOTE_EXIT:
                self.pending.discard(int(event.ident))

    def exited(self) -> bool:
        if self.queue is not None:
            self._record(self.queue.control(None, max(1, len(self.pending)), 0))
            return not self.pending
        return any(select.select([fd], [], [], 0)[0] for fd in self.descriptors)

    def wait(self, deadline: float) -> bool:
        if self.queue is not None:
            while self.pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._record(self.queue.control(None, len(self.pending), remaining))
            return True
        poller = _select_api.poll()
        for fd in self.descriptors:
            poller.register(fd, _select_api.POLLIN)
        pending = set(self.descriptors)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            for fd, events in poller.poll(max(1, int(remaining * 1000))):
                if events & _select_api.POLLIN:
                    pending.discard(fd)
                    poller.unregister(fd)
                    try:
                        vars(os)["waitid"](vars(os)["P_PIDFD"], fd, vars(os)["WEXITED"] | vars(os)["WNOHANG"])
                    except ChildProcessError:
                        pass
                else:
                    return False
        return True

    def close(self) -> None:
        if self.queue is not None:
            self.queue.close()
            self.queue = None
        for fd in self.descriptors:
            os.close(fd)
        self.descriptors.clear()
