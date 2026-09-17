"""Two shared leases own upload, isolated work, response and confirmed cleanup."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, replace
from typing import Any

from ..configuration import Settings, settings_to_snapshot
from ..upload_ingress import ReceivedUpload
from .budgets import ProcessingBudgets, ProcessingError, timed_out, unavailable, worker_failed
from .native import Child, PreparedRole, ProcessTree, spawn_role
from .protocol import DATA_LIMIT, FrameKind, ProtocolError, read_control, read_frame, write_control, write_payload
from .ready import validate_ready
from .result import validate_result_metadata


@dataclass(frozen=True, slots=True)
class BufferedResult:
    chunks: list[bytes]
    body_size: int
    media_type: str
    headers: dict[str, str]


def _processing_error(message: dict[str, Any]) -> ProcessingError:
    message = dict(message)
    diagnostic = message.pop("diagnostic", None)
    if diagnostic is not None and (
        not isinstance(diagnostic, dict)
        or set(diagnostic) != {"phase", "class"}
        or any(
            not isinstance(value, str) or not value.isascii() or not value.replace("_", "").isalnum() or len(value) > 64
            for value in diagnostic.values()
        )
    ):
        raise ProtocolError("Ungültiger interner Fehlerkontext.")
    allowed = {
        (422, "invoice_input_error"),
        (422, "processing_limit_error"),
        (503, "processing_unavailable_error"),
        (504, "processing_timeout_error"),
        (500, "processing_worker_error"),
    }
    if set(message) != {"type", "status", "error_type", "detail"} or type(message["status"]) is not int:
        raise ProtocolError("Ungültige Fehlermetadaten.")
    if (
        (message["status"], message["error_type"]) not in allowed
        or not isinstance(message["detail"], str)
        or not 0 < len(message["detail"]) <= 4096
    ):
        raise ProtocolError("Unzulässiger Prozessfehler.")
    error = ProcessingError(message["status"], message["error_type"], message["detail"])
    error.diagnostic = diagnostic
    return error


class Lease:
    def __init__(self, owner: ProcessingManager) -> None:
        self.owner = owner
        self.tree = ProcessTree()
        self.poisoned = False
        self.running = False
        self.released = False
        self.ready = threading.Event()
        self.python_deadline: float | None = None
        self.cleanup_deadline: float | None = None
        self.ready_manifest: dict[str, Any] | None = None
        self.thread: threading.Thread | None = None
        self._retained_context: Any = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task[Any] | None = None
        self._bind_request()

    def _bind_request(self) -> None:
        if self.loop is not None and self.task is not None:
            return
        try:
            self.loop = asyncio.get_running_loop()
            self.task = asyncio.current_task()
        except RuntimeError:
            pass

    async def run(self, upload: ReceivedUpload, app_settings: Settings) -> BufferedResult:
        if self.running or self.released or self.tree.cancelled.is_set():
            raise unavailable()
        if not 0 < app_settings.max_upload_bytes <= 25 * 1024**2:
            raise unavailable()
        self._bind_request()
        budgets = self.owner.budgets
        official = upload.options.official and upload.operation != "export_xml"
        try:
            duration = budgets.job_seconds(official=official, kosit_seconds=app_settings.kosit_timeout_seconds)
            settings_to_snapshot(app_settings)
        except ValueError as exc:
            raise unavailable() from exc
        started = time.monotonic()
        promise: concurrent.futures.Future[BufferedResult] = concurrent.futures.Future()
        self.running = True

        def execute() -> None:
            try:
                value = self._execute(upload, app_settings, deadline=started + duration)
            except BaseException as exc:
                self.running = False
                promise.set_exception(exc)
            else:
                self.running = False
                promise.set_result(value)

        with self.owner.lock:
            if not self.owner.accepting or self.tree.cancelled.is_set():
                self.running = False
                raise unavailable()
            self.thread = threading.Thread(target=execute, name="invoice-processing-owner", daemon=True)
            self.thread.start()
        future = asyncio.wrap_future(promise)
        try:
            while True:
                done, _ = await asyncio.wait({future}, timeout=0.025)
                if done:
                    return future.result()
                now = time.monotonic()
                if self.cleanup_deadline is not None and now >= self.cleanup_deadline:
                    raise unavailable()
                if self.python_deadline is not None and now >= self.python_deadline:
                    raise timed_out()
                if now >= started + duration:
                    raise timed_out()
                if not self.ready.is_set() and now >= started + budgets.start_seconds:
                    raise unavailable()
                if not self.tree.healthy():
                    raise worker_failed()
        except BaseException:
            self.tree.cancel()
            cleanup_deadline = self.cleanup_deadline or (
                (self.tree.cancelled_at or time.monotonic()) + budgets.cleanup_seconds
            )
            done, _ = await asyncio.wait({future}, timeout=max(0, cleanup_deadline - time.monotonic()))
            if not done:
                self.poisoned = True
                # Retrieve a late exception without ever releasing an unproven slot.
                future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            else:
                future.exception()
            raise

    def _execute(self, upload: ReceivedUpload, app_settings: Settings, *, deadline: float) -> BufferedResult:
        from ..validators.kosit import KositValidator, _kosit_temporary_directory

        budgets = self.owner.budgets
        state = KositValidator(app_settings).configuration_state()
        java_enabled = bool(state["configured"] and upload.options.official and upload.operation != "export_xml")
        if java_enabled:
            executable = shutil.which(app_settings.kosit_java_bin)
            if executable is None:
                raise unavailable()
            app_settings = replace(app_settings, kosit_java_bin=os.path.abspath(executable))
        context = _kosit_temporary_directory() if java_enabled else None
        temporary = None
        prepared_roles: list[PreparedRole] = []
        console_descriptors: list[int] = []
        descriptor_failure = False

        def close_console_descriptors() -> None:
            nonlocal descriptor_failure
            while console_descriptors:
                # Never retry a numeric descriptor after a failed close: the
                # kernel may already have reassigned it to another thread.
                descriptor = console_descriptors.pop()
                try:
                    os.close(descriptor)
                except OSError:
                    descriptor_failure = True

        try:
            if context is not None:
                temporary = context.__enter__()
            if sys.platform == "win32":
                from .windows import WindowsJob

                self.tree.outer_job = WindowsJob(
                    budgets.python_start_memory_bytes + budgets.java_memory_bytes + budgets.supervisor_memory_bytes,
                    active_processes=4,
                )
            worker_pipe = PreparedRole()
            prepared_roles.append(worker_pipe)
            extra = list(worker_pipe.broker_descriptors())
            java_pipe = None
            if java_enabled:
                java_pipe = PreparedRole()
                prepared_roles.append(java_pipe)
                extra.extend(java_pipe.broker_descriptors())
                stdout_read, stdout_write = os.pipe()
                console_descriptors.extend((stdout_read, stdout_write))
                stderr_read, stderr_write = os.pipe()
                console_descriptors.extend((stderr_read, stderr_write))
                extra.extend((stdout_read, stderr_read))

            def start_role(
                role: str,
                *,
                group: int,
                memory: int,
                prepared: PreparedRole | None = None,
                extra_fds: tuple[int, ...] = (),
            ) -> Child:
                if self.tree.cancelled.is_set():
                    raise unavailable()
                if sys.platform == "win32":
                    role_job = WindowsJob(memory, active_processes=2 if role == "java" else 1)
                    job_handles = (self.tree.outer_job.handle,)
                else:
                    role_job, job_handles = None, ()
                try:
                    child = spawn_role(
                        role, group=group, job=role_job, job_handles=job_handles, prepared=prepared, extra_fds=extra_fds
                    )
                except BaseException:
                    if role_job is not None:
                        role_job.close()
                    raise
                if role == "supervisor":
                    self.tree.install(child)
                else:
                    self.tree.install_role(role, child)
                    # Only the broker holds these pipe ends from now on. The
                    # parent owns process identities/jobs, never invoice pipes.
                    if not child.close_channels():
                        raise unavailable()
                return child

            supervisor = start_role(
                "supervisor", group=0, memory=budgets.supervisor_memory_bytes, extra_fds=tuple(extra)
            )
            if self.tree.cancelled.is_set():
                raise unavailable()
            if sys.platform == "darwin":
                watcher = spawn_role(
                    "watchdog",
                    group=supervisor.pid,
                    arguments=(str(os.getpgrp()), str(supervisor.pid), str(os.getsid(0)), str(deadline)),
                )
                self.tree.install_watchdog(watcher)
                ready = read_control(watcher.incoming)
                if ready != {
                    "type": "ready",
                    "role": "watchdog",
                    "protocol": 1,
                    "pid": watcher.pid,
                    "supervisor_pid": supervisor.pid,
                    "parent_pid": os.getpid(),
                }:
                    raise ProtocolError("Die unabhängige Prozessüberwachung ist nicht bestätigt.")
            worker = start_role(
                "worker", group=supervisor.pid, memory=budgets.python_start_memory_bytes, prepared=worker_pipe
            )
            java = None
            if java_pipe is not None:
                java = start_role(
                    "java",
                    group=supervisor.pid,
                    memory=budgets.java_memory_bytes,
                    prepared=java_pipe,
                    extra_fds=(stdout_write, stderr_write),
                )
            close_console_descriptors()
            if descriptor_failure:
                raise unavailable()
            setup = {
                "type": "setup",
                "settings": settings_to_snapshot(app_settings),
                "budgets": asdict(budgets),
                "operation": upload.operation,
                "filename": upload.filename,
                "media_type": upload.media_type,
                "official": upload.options.official,
                "scope": upload.options.scope,
                "official_state": state,
                "java_enabled": java_enabled,
                "temporary_directory": str(temporary) if temporary is not None else None,
                "role_pids": [worker.pid, *([java.pid] if java is not None else [])],
            }
            write_control(supervisor.outgoing, setup)
            ready = read_control(supervisor.incoming)
            if ready.get("type") == "error":
                raise _processing_error(ready)
            reserved = {os.getpid(), supervisor.pid}
            if self.tree.watchdog is not None:
                reserved.add(self.tree.watchdog.pid)
            children = validate_ready(
                ready,
                budgets=budgets,
                java_enabled=java_enabled,
                kosit_seconds=app_settings.kosit_timeout_seconds,
                windows=sys.platform == "win32",
                reserved_pids=reserved,
            )
            if list(children) != setup["role_pids"]:
                raise ProtocolError("Das Startinventar entspricht nicht den gebundenen Prozessen.")
            if worker.job is not None:
                worker.job.lower_memory_limit(budgets.python_memory_bytes)
            self.ready_manifest = dict(ready)
            if worker.job is not None:
                self.ready_manifest["worker_runtime_limits"] = {"job_memory_bytes": budgets.python_memory_bytes}
            self.ready.set()
            write_control(supervisor.outgoing, {"type": "input", "size": upload.size})
            write_payload(supervisor.outgoing, upload.payload)
            if read_control(supervisor.incoming) != {"type": "input_received"}:
                raise ProtocolError("Rechnungseingang wurde nicht bestätigt.")
            upload.close()
            self.python_deadline = time.monotonic() + budgets.python_seconds
            response = read_control(supervisor.incoming)
            if response == {"type": "java_wait"}:
                if java is None:
                    raise ProtocolError("Nicht angeforderter Java-Prozess.")
                remaining_python = self.python_deadline - time.monotonic()
                if remaining_python <= 0:
                    raise timed_out()
                self.python_deadline = None
                java_timeout = False
                try:
                    returncode = self._wait_for_java(java, worker, app_settings.kosit_timeout_seconds)
                    if java.job is not None and java.job.active_process_count():
                        # A dead Python launcher is not proof that its inherited
                        # JVM has stopped writing. End the complete role first.
                        java_timeout = True
                        if not java.finish(self._role_cleanup_deadline()):
                            raise unavailable()
                except (subprocess.TimeoutExpired, TimeoutError):
                    java_timeout = True
                    if not java.finish(self._role_cleanup_deadline()):
                        raise unavailable() from None
                    returncode = -1
                write_control(
                    supervisor.outgoing, {"type": "java_exit", "returncode": returncode, "timed_out": java_timeout}
                )
                self.python_deadline = time.monotonic() + remaining_python
                response = read_control(supervisor.incoming)
            if response.get("type") == "error":
                raise _processing_error(response)
            if set(response) != {"type", "result"} or response["type"] != "result":
                raise ProtocolError("Vollständiges Verarbeitungsergebnis fehlt.")
            result = validate_result_metadata(
                response["result"], operation=upload.operation, expected_scope=upload.options.scope
            )
            remaining = result["body_size"]
            chunks = []
            while remaining:
                kind, payload = read_frame(supervisor.incoming)
                if kind is not FrameKind.DATA or len(payload) != min(DATA_LIMIT, remaining):
                    raise ProtocolError("Ungültige Ergebnisdaten.")
                chunks.append(payload)
                remaining -= len(payload)
            if read_frame(supervisor.incoming) != (FrameKind.END, b"") or read_control(supervisor.incoming) != {
                "type": "complete"
            }:
                raise ProtocolError("Ergebnis wurde nicht vollständig bestätigt.")
            if not self.tree.healthy():
                raise worker_failed()
            self.python_deadline = None
            return BufferedResult(chunks, result["body_size"], result["media_type"], result["headers"])
        except ProcessingError:
            raise
        except Exception as exc:
            raise (worker_failed() if self.ready.is_set() else unavailable()) from exc
        finally:
            for prepared in prepared_roles:
                try:
                    prepared.close()
                except Exception:
                    descriptor_failure = True
            close_console_descriptors()
            try:
                self._cleanup(context)
            finally:
                if descriptor_failure:
                    self.poisoned = True
            if descriptor_failure:
                raise unavailable()

    def _wait_for_java(self, java: Child, worker: Child, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        while True:
            if self.tree.cancelled.is_set():
                raise unavailable()
            # Only the bound worker is polled/reaped here. The supervisor must
            # remain unreaped until the process tree's last group signal.
            if worker.process.poll() is not None:
                raise worker_failed()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Die KoSIT-Prozessfrist ist abgelaufen.")
            try:
                returncode = java.process.wait(timeout=min(0.05, remaining))
            except (subprocess.TimeoutExpired, TimeoutError):
                continue
            if worker.process.poll() is not None:
                raise worker_failed()
            return int(returncode)

    def _cleanup(self, context: Any) -> None:
        # Native reaping and temporary-directory removal share one deadline.
        # A blocked filesystem call remains in this owner thread; the async
        # monitor returns an error and retains its slot instead of claiming
        # cleanup or releasing a possibly still-owned temporary directory.
        started = self.tree.cancelled_at or time.monotonic()
        self.cleanup_deadline = started + self.owner.budgets.cleanup_seconds
        self._retained_context = context
        try:
            cleaned = self.tree.cleanup(self.owner.budgets.cleanup_seconds)
        except Exception:
            cleaned = False
        if not cleaned:
            self.poisoned = True
            raise unavailable()
        if context is not None:
            try:
                context.__exit__(None, None, None)
            except Exception:
                self.poisoned = True
                raise unavailable() from None
        if time.monotonic() >= self.cleanup_deadline:
            raise unavailable()
        self._retained_context = None

    def _role_cleanup_deadline(self) -> float:
        return (self.tree.cancelled_at or time.monotonic()) + self.owner.budgets.cleanup_seconds

    def cancel(self) -> None:
        try:
            self.tree.cancel()
        except Exception:
            self.poisoned = True
            raise
        finally:
            if self.loop is not None and self.task is not None and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.task.cancel)

    def release(self) -> None:
        with self.owner.lock:
            if self.released:
                return
            owns_native_resources = (
                self.tree.supervisor is not None
                or self.tree.watchdog is not None
                or bool(self.tree.roles)
                or self.tree.outer_job is not None
            )
            if self.running or self.poisoned or (owns_native_resources and not self.tree.cleaned):
                self.poisoned = True
                return
            self.released = True
            self.owner.leases.discard(self)


class ProcessingManager:
    def __init__(self, budgets: ProcessingBudgets | None = None) -> None:
        self.budgets = budgets or ProcessingBudgets.for_platform()
        self.lock = threading.Lock()
        self.leases: set[Lease] = set()
        self.accepting = True

    @property
    def active_count(self) -> int:
        with self.lock:
            return len(self.leases)

    def try_acquire(self) -> Lease | None:
        with self.lock:
            if not self.accepting or len(self.leases) >= self.budgets.max_jobs:
                return None
            lease = Lease(self)
            self.leases.add(lease)
            return lease

    def startup(self) -> None:
        with self.lock:
            if self.leases:
                raise RuntimeError("Ein vorheriger Verarbeitungsauftrag wurde nicht bereinigt.")
            self.accepting = True

    def shutdown(self) -> None:
        with self.lock:
            self.accepting = False
            leases = tuple(self.leases)
        deadline = time.monotonic() + self.budgets.cleanup_seconds
        failures: list[Exception] = []
        for lease in leases:
            try:
                lease.cancel()
            except Exception as exc:
                lease.poisoned = True
                failures.append(exc)
        for lease in leases:
            if lease.thread is not None:
                lease.thread.join(timeout=max(0, deadline - time.monotonic()))
                if lease.thread.is_alive():
                    lease.poisoned = True
        if failures:
            raise RuntimeError("Mindestens ein Prozessabbruch konnte nicht bestätigt werden.") from failures[0]


manager = ProcessingManager()
