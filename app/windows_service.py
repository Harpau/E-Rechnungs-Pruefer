from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any, Protocol

from .desktop_security import API_TOKEN_ENV, get_service_browser_sessions
from .server_runtime import LoopbackServer
from .windows_acl import WindowsServiceAcl
from .windows_service_config import (
    SERVICE_DISPLAY_NAME,
    SERVICE_NAME,
    ServiceConfiguration,
    ServicePaths,
    TokenStore,
    activate_service_environment,
    load_or_create_configuration,
    service_shutdown_timeout,
    validate_machine_path,
)
from .windows_service_config import (
    load_configuration as load_service_configuration,
)
from .windows_service_ipc import BrowserPipeServer, IpcErrorCallback, IpcServerDiagnostic
from .windows_service_preflight import purge_runtime_state
from .windows_sync import BackendMutex, create_backend_mutex

SERVICE_LOG_MAX_BYTES = 2 * 1024 * 1024
SERVICE_LOG_BACKUPS = 3
START_WAIT_HINT_MILLISECONDS = 45_000
LIVENESS_POLL_SECONDS = 0.5
SERVICE_CONTROLLER_CONNECT_ERROR = 1063
DIRECT_START_EXIT_CODE = 2
_MANAGED_ENVIRONMENT_KEYS = (
    "EINVOICE_SERVICE_MODE",
    "EINVOICE_DESKTOP_TOKEN",
    "EINVOICE_DESKTOP_PORT",
    "EINVOICE_API_TOKEN",
    "HOST",
    "PORT",
    "KOSIT_ENABLED",
    "KOSIT_TIMEOUT_SECONDS",
)


class _Server(Protocol):
    port: int

    def start(self) -> None: ...

    def request_stop(self) -> None: ...

    def stop(self, *, timeout: float) -> None: ...

    def is_alive(self) -> bool: ...


class _IpcServer(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def is_alive(self) -> bool: ...


StatusReporter = Callable[[str, int | None], None]


class _AclProtectedRotatingFileHandler(RotatingFileHandler):
    def __init__(
        self,
        path: Path,
        *,
        acl: WindowsServiceAcl,
        max_bytes: int,
        backup_count: int,
    ) -> None:
        self._service_acl = acl
        self._service_log_path = path
        super().__init__(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )

    def doRollover(self) -> None:
        super().doRollover()
        # Rotated files keep their protected descriptor when renamed. The new
        # active file is opened after the rename and must be protected again.
        self._service_acl.protect_log(self._service_log_path)


def _cancel_kosit_processes(timeout: float) -> int:
    from .validators.kosit import cancel_running_kosit_processes

    return cancel_running_kosit_processes(timeout)


def _allow_kosit_processes() -> None:
    from .validators.kosit import allow_kosit_process_starts

    allow_kosit_process_starts()


def ensure_service_import_order() -> None:
    imported_too_early = [name for name in ("app.settings", "app.main") if name in sys.modules]
    if imported_too_early:
        raise RuntimeError(
            "Die Dienstkonfiguration wurde zu spät aktiviert; bereits importiert: " + ", ".join(imported_too_early)
        )


def configure_service_logging(path: Path, acl: WindowsServiceAcl) -> logging.Logger:
    acl.protect_directory(path.parent, allow_local_service_owner=True)
    for candidate in (path, *(path.with_name(f"{path.name}.{index}") for index in range(1, SERVICE_LOG_BACKUPS + 1))):
        if validate_machine_path(candidate, directory=False):
            acl.protect_log(candidate)
    handler = _AclProtectedRotatingFileHandler(
        path,
        acl=acl,
        max_bytes=SERVICE_LOG_MAX_BYTES,
        backup_count=SERVICE_LOG_BACKUPS,
    )
    acl.protect_log(path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("e_rechnungs_pruefer.windows_service")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


class WindowsServiceHost:
    """Platform-neutral orchestration behind the thin pywin32 SCM class."""

    def __init__(
        self,
        *,
        configuration_loader: Callable[[], ServiceConfiguration],
        token_loader: Callable[[], str],
        acl_verifier: Callable[[], None],
        mutex_factory: Callable[[], BackendMutex],
        server_factory: Callable[[ServiceConfiguration, str], _Server],
        ipc_factory: Callable[[_Server], _IpcServer],
        stop_event: Event,
        status_reporter: StatusReporter,
        kosit_stopper: Callable[[float], int] = _cancel_kosit_processes,
    ) -> None:
        self.configuration_loader = configuration_loader
        self.token_loader = token_loader
        self.acl_verifier = acl_verifier
        self.mutex_factory = mutex_factory
        self.server_factory = server_factory
        self.ipc_factory = ipc_factory
        self.stop_event = stop_event
        self.status_reporter = status_reporter
        self.kosit_stopper = kosit_stopper
        self.server: _Server | None = None
        self.ipc: _IpcServer | None = None

    def run(self) -> None:
        mutex: BackendMutex | None = None
        configuration: ServiceConfiguration | None = None
        shutdown_deadline: float | None = None
        previous_environment = {key: os.environ.get(key) for key in _MANAGED_ENVIRONMENT_KEYS}
        self.status_reporter("start-pending", START_WAIT_HINT_MILLISECONDS)
        try:
            mutex = self.mutex_factory()
            if mutex.already_exists:
                raise RuntimeError(
                    "Eine andere Betriebsart des E-Rechnungs-Prüfers läuft bereits; der Dienst startet fail-closed."
                )
            self.acl_verifier()
            configuration = self.configuration_loader()
            token = self.token_loader()
            activate_service_environment(configuration, token)
            _allow_kosit_processes()
            self.server = self.server_factory(configuration, token)
            try:
                self.server.start()
            finally:
                os.environ.pop(API_TOKEN_ENV, None)
            self.ipc = self.ipc_factory(self.server)
            self.ipc.start()
            self.status_reporter("running", None)
            while not self.stop_event.wait(LIVENESS_POLL_SECONDS):
                if not self.server.is_alive():
                    raise RuntimeError("Der lokale Webserver wurde unerwartet beendet.")
                if not self.ipc.is_alive():
                    raise RuntimeError("Der lokale IPC-Server wurde unerwartet beendet.")
            timeout = service_shutdown_timeout(configuration)
            shutdown_deadline = time.monotonic() + timeout
            self.status_reporter("stop-pending", int(timeout * 1000))
            self.server.request_stop()
            self.kosit_stopper(min(5.0, max(0.0, shutdown_deadline - time.monotonic())))
            self.ipc.stop()
            self.ipc = None
            self.server.stop(timeout=max(0.0, shutdown_deadline - time.monotonic()))
            self.server = None
        finally:
            cleanup_deadline = shutdown_deadline or (
                time.monotonic() + service_shutdown_timeout(configuration or ServiceConfiguration())
            )
            if self.server is not None:
                self.server.request_stop()
            if self.ipc is not None:
                try:
                    self.ipc.stop()
                except Exception:
                    pass
            if self.server is not None:
                try:
                    self.kosit_stopper(min(5.0, max(0.0, cleanup_deadline - time.monotonic())))
                    self.server.stop(timeout=max(0.0, cleanup_deadline - time.monotonic()))
                except Exception:
                    pass
            if mutex is not None:
                mutex.close()
            os.environ.pop(API_TOKEN_ENV, None)
            for key, value in previous_environment.items():
                if key == API_TOKEN_ENV:
                    continue
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def request_stop(self) -> None:
        if self.server is not None:
            self.server.request_stop()
        self.stop_event.set()


def _create_default_host(
    status_reporter: StatusReporter,
    stop_event: Event,
    *,
    ipc_error_callback: IpcErrorCallback | None = None,
) -> WindowsServiceHost:
    paths = ServicePaths.from_environment()
    acl = WindowsServiceAcl()

    def load_configuration() -> ServiceConfiguration:
        return load_service_configuration(paths.configuration)

    token_store = TokenStore(
        paths.token,
        protect_directory=acl.protect_directory,
        protect_file=acl.protect_token,
    )

    def create_server(configuration: ServiceConfiguration, _token: str) -> LoopbackServer:
        return LoopbackServer(
            port=configuration.port,
            thread_name="E-Rechnungs-Pruefer-Dienst-Webserver",
            daemon_thread=True,
        )

    def create_ipc(server: _Server) -> BrowserPipeServer:
        return BrowserPipeServer(
            get_service_browser_sessions(),
            server.port,
            error_callback=ipc_error_callback,
        )

    def verify_acl_and_remove_stale_runtime() -> None:
        acl.repair_explorer_directory_aces(paths)
        purge_runtime_state(paths=paths, acl=acl, require_stopped=False)

    return WindowsServiceHost(
        configuration_loader=load_configuration,
        token_loader=token_store.load,
        acl_verifier=verify_acl_and_remove_stale_runtime,
        mutex_factory=create_backend_mutex,
        server_factory=create_server,
        ipc_factory=create_ipc,
        stop_event=stop_event,
        status_reporter=status_reporter,
    )


def _log_ipc_diagnostic(logger: logging.Logger, diagnostic: IpcServerDiagnostic) -> None:
    """Write only bounded, non-secret IPC metadata to the protected service log."""

    logger.warning(
        "Lokaler IPC-Fehler: phase=%s exception=%s winerror=%s.",
        diagnostic.phase,
        diagnostic.exception_type,
        diagnostic.winerror if diagnostic.winerror is not None else "none",
    )


if TYPE_CHECKING:
    import servicemanager
    import win32service
    import win32serviceutil

    class _ServiceFrameworkBase:
        def __init__(self, _args: Sequence[str]) -> None:
            pass

        def ReportServiceStatus(self, *_args: Any, **_kwargs: Any) -> None:
            pass
else:
    try:
        if sys.platform != "win32":
            raise ImportError
        import servicemanager
        import win32service
        import win32serviceutil

        _ServiceFrameworkBase = win32serviceutil.ServiceFramework
    except ImportError:
        servicemanager = None
        win32service = None
        win32serviceutil = None

        class _ServiceFrameworkBase:
            def __init__(self, _args: Sequence[str]) -> None:
                pass

            def ReportServiceStatus(self, *_args: Any, **_kwargs: Any) -> None:
                pass


class ERechnungsPrueferService(_ServiceFrameworkBase):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY_NAME
    _svc_description_ = "Lokaler Prüf- und Berichtsdienst für strukturierte elektronische Rechnungen."

    def __init__(self, args: Sequence[str]) -> None:
        super().__init__(args)
        self._stop_event = Event()
        self._host: WindowsServiceHost | None = None
        self._current_status = "start-pending"
        self._current_wait_hint = START_WAIT_HINT_MILLISECONDS

    def SvcRun(self) -> None:
        """Let SvcDoRun report RUNNING only after HTTP and IPC are ready."""

        self.SvcDoRun()

    def _report_status(self, status: str, wait_hint: int | None = None) -> None:
        self._current_status = status
        self._current_wait_hint = wait_hint or 0
        if win32service is None:
            return
        mapping = {
            "start-pending": win32service.SERVICE_START_PENDING,
            "running": win32service.SERVICE_RUNNING,
            "stop-pending": win32service.SERVICE_STOP_PENDING,
        }
        self.ReportServiceStatus(mapping[status], waitHint=wait_hint or 0)

    def SvcInterrogate(self) -> None:
        """Repeat the actual pending/running state instead of assuming RUNNING."""

        self._report_status(self._current_status, self._current_wait_hint)

    def SvcStop(self) -> None:
        self._report_status("stop-pending", 20_000)
        if self._host is not None:
            self._host.request_stop()
        else:
            self._stop_event.set()

    def SvcShutdown(self) -> None:
        self.SvcStop()

    def SvcDoRun(self) -> None:
        paths = ServicePaths.from_environment()
        acl = WindowsServiceAcl()
        logger: logging.Logger | None = None
        try:
            self._report_status("start-pending", START_WAIT_HINT_MILLISECONDS)
            ensure_service_import_order()
            # Explorer may add one explicit full-control ACE for the direct
            # administrator who confirmed access to a protected directory.
            # The repair path accepts only that narrow shape, changes only a
            # directory that actually needs repair and then verifies strictly.
            acl.repair_explorer_directory_aces(paths)
            logger = configure_service_logging(paths.log, acl)
            logger.info("Dienststart angefordert.")
            self._host = _create_default_host(
                self._report_status,
                self._stop_event,
                ipc_error_callback=lambda diagnostic: _log_ipc_diagnostic(logger, diagnostic),
            )
            self._host.run()
            logger.info("Dienst wurde geordnet beendet.")
        except Exception as exc:
            if logger is not None:
                logger.error("Technischer Dienstfehler (%s).", type(exc).__name__)
            if servicemanager is not None:
                servicemanager.LogErrorMsg(f"{SERVICE_DISPLAY_NAME}: technischer Fehler ({type(exc).__name__})")
            raise


def _service_is_stopped() -> bool:
    if win32service is None:
        raise OSError("Die SCM-Abfrage ist ausschließlich unter Windows verfügbar.")
    manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    service = None
    try:
        service = win32service.OpenService(manager, SERVICE_NAME, win32service.SERVICE_QUERY_STATUS)
        status = win32service.QueryServiceStatus(service)
        return int(status[1]) == win32service.SERVICE_STOPPED
    finally:
        if service is not None:
            win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(manager)


def _manage_service(options: argparse.Namespace) -> None:
    paths = ServicePaths.from_environment()
    if options.import_token is not None and not options.consent_token_import:
        raise RuntimeError("Die Tokenübernahme benötigt eine ausdrückliche Zustimmung.")
    acl = WindowsServiceAcl(administrative=not options.verify_state)
    if options.verify_state:
        acl.repair_explorer_directory_aces(paths)
    else:
        acl.verify_existing_service_paths(paths)
    stopped_required = bool(options.initialize or options.rotate_token or options.preflight_port)
    if stopped_required and not _service_is_stopped():
        raise RuntimeError("Dieser Verwaltungsbefehl ist nur bei gestopptem Dienst zulässig.")
    if options.initialize:
        acl.protect_directory(paths.data_directory)
        if validate_machine_path(paths.configuration, directory=False):
            acl.protect_configuration(paths.configuration)
        if validate_machine_path(paths.token, directory=False):
            acl.protect_token_preserving_readers(paths.token)
        load_or_create_configuration(paths.configuration, protect=acl.protect_configuration)
        store = TokenStore(
            paths.token,
            protect_directory=acl.protect_directory,
            protect_file=acl.protect_token,
        )
        if options.import_token is not None:
            from .windows_desktop_migration import read_desktop_migration_token

            token = read_desktop_migration_token(Path(options.import_token))
            store.import_value(token, consent=True)
        else:
            store.load_or_create()
        return

    configuration = load_service_configuration(paths.configuration)
    if options.rotate_token:
        TokenStore(paths.token).load()
        acl.protect_directory(paths.data_directory)
        acl.protect_configuration(paths.configuration)
        acl.protect_token_preserving_readers(paths.token)
        preserving_store = TokenStore(
            paths.token,
            protect_directory=acl.protect_directory,
            protect_file=acl.token_protector_preserving(paths.token),
        )
        preserving_store.rotate()
    elif options.grant_token_read:
        TokenStore(paths.token).load()
        acl.grant_token_reader(paths.token, options.grant_token_read)
    elif options.verify_state:
        TokenStore(paths.token).load()
        acl.verify_service_paths(paths)
    elif options.preflight_port:
        from .server_runtime import reserve_loopback_socket

        listener, _port = reserve_loopback_socket(configuration.port)
        listener.close()
    elif options.health_check:
        from .server_runtime import health_is_ready

        if not health_is_ready(configuration.port, timeout=5.0):
            raise RuntimeError("Der Dienst-Healthcheck ist nicht erreichbar.")
    else:
        raise RuntimeError("Es wurde kein Dienstverwaltungsbefehl angegeben.")


def _parse_management_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="E-Rechnungs-Pruefer-Dienst")
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--initialize", action="store_true")
    commands.add_argument("--rotate-token", action="store_true")
    commands.add_argument("--grant-token-read", metavar="WINDOWS-KONTO")
    commands.add_argument("--verify-state", action="store_true")
    commands.add_argument("--preflight-port", action="store_true")
    commands.add_argument("--health-check", action="store_true")
    parser.add_argument("--import-token", metavar="DATEI")
    parser.add_argument("--consent-token-import", action="store_true")
    options = parser.parse_args(argv)
    if options.import_token is not None and not options.initialize:
        parser.error("--import-token ist nur zusammen mit --initialize zulässig.")
    if options.import_token is not None and not options.consent_token_import:
        parser.error("--import-token benötigt --consent-token-import.")
    if options.consent_token_import and options.import_token is None:
        parser.error("--consent-token-import ist nur zusammen mit --import-token zulässig.")
    return options


def _is_direct_service_start_error(exc: BaseException) -> bool:
    """Recognize only the SCM dispatcher error raised for an interactive launch."""

    winerror = getattr(exc, "winerror", None)
    if isinstance(winerror, int):
        return winerror == SERVICE_CONTROLLER_CONNECT_ERROR
    return bool(exc.args) and exc.args[0] == SERVICE_CONTROLLER_CONNECT_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    if sys.platform != "win32" or servicemanager is None:
        raise SystemExit("Der Dienst-Entrypoint ist ausschließlich für Windows vorgesehen.")
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        try:
            _manage_service(_parse_management_arguments(arguments))
        except SystemExit:
            raise
        except Exception:
            return 1
        return 0
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(ERechnungsPrueferService)
    try:
        servicemanager.StartServiceCtrlDispatcher()
    except Exception as exc:
        if _is_direct_service_start_error(exc):
            return DIRECT_START_EXIT_CODE
        raise
    return 0


if __name__ == "__main__":
    main()
