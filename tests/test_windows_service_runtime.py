from __future__ import annotations

import os
import sys
from argparse import Namespace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import ANY, Mock, call

import pytest

from app import server_runtime, windows_desktop_migration, windows_service
from app.windows_service import ERechnungsPrueferService, WindowsServiceHost
from app.windows_service_config import ServiceConfiguration
from app.windows_service_ipc import IpcServerDiagnostic
from app.windows_sync import BACKEND_MUTEX_NAME, BACKEND_MUTEX_SECURITY_SDDL


def test_loopback_server_applies_environment_before_import_and_never_changes_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = Mock()
    listener.getsockname.return_value = ("127.0.0.1", 18080)
    reserve = Mock(return_value=(listener, 18080))
    monkeypatch.setattr(server_runtime, "reserve_loopback_socket", reserve)
    monkeypatch.setattr(server_runtime, "health_is_ready", lambda _port: True)
    fake_server = SimpleNamespace(run=Mock(), should_exit=False, force_exit=False)
    config_factory = Mock(return_value=object())
    monkeypatch.setattr(server_runtime.uvicorn, "Config", config_factory)
    monkeypatch.setattr(server_runtime.uvicorn, "Server", Mock(return_value=fake_server))
    monkeypatch.delenv("SERVICE_IMPORT_ORDER_TEST", raising=False)

    def load_app():
        assert os.environ["SERVICE_IMPORT_ORDER_TEST"] == "configured-before-import"
        return object()

    server = server_runtime.LoopbackServer(
        port=18080,
        environment={"SERVICE_IMPORT_ORDER_TEST": "configured-before-import"},
        app_loader=load_app,
        thread_name="test-service",
    )
    server.start()
    server.stop(timeout=1)

    reserve.assert_called_once_with(18080)
    config_factory.assert_called_once_with(
        ANY,
        host="127.0.0.1",
        port=18080,
        access_log=False,
        log_config=None,
        log_level="warning",
    )
    listener.close.assert_called_once_with()


def test_loopback_reservation_fails_closed_on_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = Mock()
    listener.bind.side_effect = OSError("Port belegt")
    monkeypatch.setattr(server_runtime.socket, "socket", Mock(return_value=listener))

    with pytest.raises(OSError, match="Port belegt"):
        server_runtime.reserve_loopback_socket(8080)

    listener.bind.assert_called_once_with(("127.0.0.1", 8080))
    listener.close.assert_called_once_with()


def test_health_probe_bypasses_environment_proxy() -> None:
    proxy_handlers = [
        handler
        for handler in server_runtime.DIRECT_HTTP_OPENER.handlers
        if isinstance(handler, __import__("urllib.request").request.ProxyHandler)
    ]
    assert proxy_handlers == []


def test_backend_mutex_is_machine_wide_and_has_explicit_local_dacl() -> None:
    assert BACKEND_MUTEX_NAME == r"Global\E-Rechnungs-Pruefer-Backend"
    for principal in ("SY", "BA", "LS", "IU"):
        assert f";;;{principal})" in BACKEND_MUTEX_SECURITY_SDDL


def test_service_host_reports_scm_lifecycle_and_stops_with_bounded_timeout(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del tmp_path
    statuses: list[tuple[str, int | None]] = []
    stop_event = Mock()
    stop_event.wait.return_value = True
    mutex = SimpleNamespace(already_exists=False, close=Mock())
    server = Mock()
    ipc = Mock()
    kosit_stopper = Mock(return_value=1)
    configuration = ServiceConfiguration(kosit_timeout_seconds=30)
    imported = ModuleType("app.main")
    imported.app = object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.main", imported)

    host = WindowsServiceHost(
        configuration_loader=lambda: configuration,
        token_loader=lambda: "a" * 43,
        acl_verifier=lambda: None,
        mutex_factory=lambda: mutex,
        server_factory=lambda _configuration, _token: server,
        ipc_factory=lambda _server: ipc,
        stop_event=stop_event,
        status_reporter=lambda status, wait_hint=None: statuses.append((status, wait_hint)),
        kosit_stopper=kosit_stopper,
    )

    host.run()

    assert statuses == [
        ("start-pending", 45_000),
        ("running", None),
        ("stop-pending", 45_000),
    ]
    server.start.assert_called_once_with()
    ipc.start.assert_called_once_with()
    ipc.stop.assert_called_once_with()
    server.request_stop.assert_called_once_with()
    kosit_stopper.assert_called_once_with(5.0)
    assert server.stop.call_args.kwargs["timeout"] == pytest.approx(45.0, abs=0.1)
    mutex.close.assert_called_once_with()


def test_service_host_rejects_existing_desktop_backend_before_server_start() -> None:
    mutex = SimpleNamespace(already_exists=True, close=Mock())
    server_factory = Mock()
    host = WindowsServiceHost(
        configuration_loader=ServiceConfiguration,
        token_loader=lambda: "a" * 43,
        acl_verifier=lambda: None,
        mutex_factory=lambda: mutex,
        server_factory=server_factory,
        ipc_factory=Mock(),
        stop_event=Mock(),
        status_reporter=Mock(),
    )

    with pytest.raises(RuntimeError, match="andere Betriebsart"):
        host.run()

    server_factory.assert_not_called()
    mutex.close.assert_called_once_with()


def test_service_host_verifies_programdata_before_loading_configuration_or_token() -> None:
    mutex = SimpleNamespace(already_exists=False, close=Mock())
    configuration_loader = Mock()
    token_loader = Mock()
    acl_verifier = Mock(side_effect=RuntimeError("unsicherer Maschinenpfad"))
    host = WindowsServiceHost(
        configuration_loader=configuration_loader,
        token_loader=token_loader,
        acl_verifier=acl_verifier,
        mutex_factory=lambda: mutex,
        server_factory=Mock(),
        ipc_factory=Mock(),
        stop_event=Mock(),
        status_reporter=Mock(),
    )

    with pytest.raises(RuntimeError, match="unsicherer Maschinenpfad"):
        host.run()

    configuration_loader.assert_not_called()
    token_loader.assert_not_called()
    mutex.close.assert_called_once_with()


def test_service_module_cannot_open_interactive_ui() -> None:
    import app.windows_service as service_module

    source = Path(service_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("webbrowser", "pystray", "MessageBox", "_show_windows_message"):
        assert forbidden not in source


def test_scm_adapter_does_not_report_running_before_host_readiness() -> None:
    service = ERechnungsPrueferService(["test"])
    service.SvcDoRun = Mock()  # type: ignore[method-assign]
    service.ReportServiceStatus = Mock()  # type: ignore[method-assign]

    service.SvcRun()

    service.SvcDoRun.assert_called_once_with()
    service.ReportServiceStatus.assert_not_called()


def test_scm_shutdown_uses_the_same_orderly_stop_path() -> None:
    service = ERechnungsPrueferService(["test"])
    service.SvcStop = Mock()  # type: ignore[method-assign]

    service.SvcShutdown()

    service.SvcStop.assert_called_once_with()


def test_service_import_order_rejects_settings_or_application_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    imported = ModuleType("app.settings")
    monkeypatch.setitem(sys.modules, "app.settings", imported)

    with pytest.raises(RuntimeError, match="app.settings"):
        windows_service.ensure_service_import_order()


def test_service_logging_is_rotating_and_acl_protected(tmp_path: Path) -> None:
    acl = Mock()
    log_path = tmp_path / "logs" / "service.log"
    acl.protect_directory.side_effect = lambda path, **_kwargs: path.mkdir(parents=True)

    logger = windows_service.configure_service_logging(log_path, acl)
    logger.info("Technischer Testeintrag")
    for handler in logger.handlers:
        handler.flush()

    assert "Technischer Testeintrag" in log_path.read_text(encoding="utf-8")
    acl.protect_directory.assert_called_once_with(log_path.parent, allow_local_service_owner=True)
    acl.protect_log.assert_called_once_with(log_path)
    assert logger.propagate is False
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def test_service_logging_acl_postconditions_hold_before_running_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "ProgramData" / "E-Rechnungs-Pruefer" / "logs" / "service.log"
    log_path.parent.mkdir(parents=True)
    paths = SimpleNamespace(
        data_directory=log_path.parent.parent,
        configuration=log_path.parent.parent / "service.json",
        token=log_path.parent.parent / "api-token.txt",
        log=log_path,
    )
    events: list[tuple[str, Path | None]] = []
    acl = windows_service.WindowsServiceAcl()
    monkeypatch.setattr(
        acl,
        "repair_explorer_directory_aces",
        lambda actual: events.append(("machine-repaired", actual.data_directory)),
    )
    monkeypatch.setattr(
        acl,
        "_set",
        lambda path, **_kwargs: events.append(("protected", path)),
    )
    monkeypatch.setattr(
        acl,
        "_verify",
        lambda path, **_kwargs: events.append(("postcondition", path)),
    )
    monkeypatch.setattr(
        windows_service,
        "ServicePaths",
        SimpleNamespace(from_environment=Mock(return_value=paths)),
    )
    monkeypatch.setattr(windows_service, "WindowsServiceAcl", Mock(return_value=acl))
    monkeypatch.setattr(windows_service, "ensure_service_import_order", Mock())

    service = ERechnungsPrueferService(["test"])
    service._report_status = lambda status, wait_hint=None: events.append((status, wait_hint))  # type: ignore[method-assign]

    def create_host(status_reporter, _stop_event, **_kwargs):
        return SimpleNamespace(run=lambda: status_reporter("running", None))

    monkeypatch.setattr(windows_service, "_create_default_host", create_host)

    try:
        service.SvcDoRun()
    finally:
        logger = windows_service.logging.getLogger("e_rechnungs_pruefer.windows_service")
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)

    assert events[0] == ("start-pending", windows_service.START_WAIT_HINT_MILLISECONDS)
    assert events[1] == ("machine-repaired", paths.data_directory)
    assert ("protected", log_path.parent) in events
    assert ("protected", log_path) in events
    assert events.index(("postcondition", log_path.parent)) < events.index(("running", None))
    assert events.index(("postcondition", log_path)) < events.index(("running", None))


def test_service_logging_records_only_safe_ipc_diagnostic_fields(tmp_path: Path) -> None:
    acl = Mock()
    log_path = tmp_path / "logs" / "service.log"
    acl.protect_directory.side_effect = lambda path, **_kwargs: path.mkdir(parents=True)
    logger = windows_service.configure_service_logging(log_path, acl)

    windows_service._log_ipc_diagnostic(
        logger,
        IpcServerDiagnostic(
            phase="write-response",
            exception_type="_WinError",
            winerror=233,
        ),
    )
    for handler in logger.handlers:
        handler.flush()

    contents = log_path.read_text(encoding="utf-8")
    assert "Lokaler IPC-Fehler: phase=write-response exception=_WinError winerror=233." in contents
    assert "token=" not in contents
    assert "desktop/bootstrap" not in contents
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def test_default_service_host_wires_ipc_diagnostics_to_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = SimpleNamespace(
        data_directory=tmp_path,
        configuration=tmp_path / "service.json",
        token=tmp_path / "api-token.txt",
    )
    sessions = object()
    callback = Mock()
    pipe_server = Mock()
    pipe_server_factory = Mock(return_value=pipe_server)
    monkeypatch.setattr(
        windows_service,
        "ServicePaths",
        SimpleNamespace(from_environment=Mock(return_value=paths)),
    )
    acl = Mock()
    monkeypatch.setattr(windows_service, "WindowsServiceAcl", Mock(return_value=acl))
    monkeypatch.setattr(windows_service, "TokenStore", Mock(return_value=Mock()))
    monkeypatch.setattr(windows_service, "get_service_browser_sessions", Mock(return_value=sessions))
    monkeypatch.setattr(windows_service, "BrowserPipeServer", pipe_server_factory)
    purge_runtime = Mock()
    monkeypatch.setattr(windows_service, "purge_runtime_state", purge_runtime)

    host = windows_service._create_default_host(
        Mock(),
        Mock(),
        ipc_error_callback=callback,
    )

    assert host.ipc_factory(SimpleNamespace(port=18080)) is pipe_server
    host.acl_verifier()
    pipe_server_factory.assert_called_once_with(
        sessions,
        18080,
        error_callback=callback,
    )
    acl.repair_explorer_directory_aces.assert_called_once_with(paths)
    purge_runtime.assert_called_once_with(paths=paths, acl=acl, require_stopped=False)


def test_service_logging_reprotects_new_active_file_after_rollover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acl = Mock()
    log_path = tmp_path / "logs" / "service.log"
    acl.protect_directory.side_effect = lambda path, **_kwargs: path.mkdir(parents=True)
    # Keep the first formatted record below the limit and make the second one
    # trigger exactly one rollover on every supported Python version.
    monkeypatch.setattr(windows_service, "SERVICE_LOG_MAX_BYTES", 128)

    logger = windows_service.configure_service_logging(log_path, acl)
    logger.info("x" * 64)
    logger.info("y" * 64)
    for handler in logger.handlers:
        handler.flush()

    assert log_path.with_name("service.log.1").exists()
    assert acl.protect_log.call_args_list.count(call(log_path)) == 2
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def test_service_host_cleans_up_partial_ipc_start_and_restores_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOST", "vorher")
    mutex = SimpleNamespace(already_exists=False, close=Mock())
    server = Mock()
    ipc = Mock()
    ipc.start.side_effect = RuntimeError("IPC defekt")
    statuses: list[tuple[str, int | None]] = []
    configuration = ServiceConfiguration(port=18081)

    host = WindowsServiceHost(
        configuration_loader=lambda: configuration,
        token_loader=lambda: "a" * 43,
        acl_verifier=Mock(),
        mutex_factory=lambda: mutex,
        server_factory=lambda _configuration, _token: server,
        ipc_factory=lambda _server: ipc,
        stop_event=Mock(),
        status_reporter=lambda status, wait_hint=None: statuses.append((status, wait_hint)),
    )

    with pytest.raises(RuntimeError, match="IPC defekt"):
        host.run()

    assert statuses == [("start-pending", 45_000)]
    ipc.stop.assert_called_once_with()
    server.request_stop.assert_called_once_with()
    assert server.stop.call_args.kwargs["timeout"] == pytest.approx(75.0, abs=0.1)
    mutex.close.assert_called_once_with()
    assert os.environ["HOST"] == "vorher"


def test_service_host_removes_api_token_before_ipc_and_never_restores_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EINVOICE_API_TOKEN", "previous-secret")
    mutex = SimpleNamespace(already_exists=False, close=Mock())
    configuration = ServiceConfiguration()
    stop_event = Mock()
    stop_event.wait.return_value = True
    server = Mock()
    ipc = Mock()

    def assert_token_available_during_app_initialization() -> None:
        assert os.environ["EINVOICE_API_TOKEN"] == "a" * 43

    def assert_token_removed_before_ipc_start() -> None:
        assert "EINVOICE_API_TOKEN" not in os.environ

    server.start.side_effect = assert_token_available_during_app_initialization
    ipc.start.side_effect = assert_token_removed_before_ipc_start
    host = WindowsServiceHost(
        configuration_loader=lambda: configuration,
        token_loader=lambda: "a" * 43,
        acl_verifier=Mock(),
        mutex_factory=lambda: mutex,
        server_factory=lambda _configuration, _token: server,
        ipc_factory=lambda _server: ipc,
        stop_event=stop_event,
        status_reporter=Mock(),
        kosit_stopper=Mock(return_value=0),
    )

    host.run()

    assert "EINVOICE_API_TOKEN" not in os.environ


def test_service_host_request_stop_signals_event_and_active_server() -> None:
    stop_event = Mock()
    host = WindowsServiceHost(
        configuration_loader=ServiceConfiguration,
        token_loader=lambda: "a" * 43,
        acl_verifier=Mock(),
        mutex_factory=Mock(),
        server_factory=Mock(),
        ipc_factory=Mock(),
        stop_event=stop_event,
        status_reporter=Mock(),
    )
    server = Mock()
    host.server = server

    host.request_stop()

    server.request_stop.assert_called_once_with()
    stop_event.set.assert_called_once_with()


@pytest.mark.parametrize("dead_component", ["server", "ipc"])
def test_service_host_fails_for_scm_recovery_when_a_runtime_thread_dies(
    dead_component: str,
) -> None:
    mutex = SimpleNamespace(already_exists=False, close=Mock())
    server = Mock()
    ipc = Mock()
    server.is_alive.return_value = dead_component != "server"
    ipc.is_alive.return_value = dead_component != "ipc"
    stop_event = Mock()
    stop_event.wait.return_value = False
    statuses: list[tuple[str, int | None]] = []

    host = WindowsServiceHost(
        configuration_loader=ServiceConfiguration,
        token_loader=lambda: "a" * 43,
        acl_verifier=Mock(),
        mutex_factory=lambda: mutex,
        server_factory=lambda _configuration, _token: server,
        ipc_factory=lambda _server: ipc,
        stop_event=stop_event,
        status_reporter=lambda status, wait_hint=None: statuses.append((status, wait_hint)),
        kosit_stopper=Mock(),
    )

    with pytest.raises(RuntimeError, match="unerwartet beendet"):
        host.run()

    assert statuses == [("start-pending", 45_000), ("running", None)]
    server.request_stop.assert_called()
    ipc.stop.assert_called_once_with()
    mutex.close.assert_called_once_with()


def test_scm_status_mapping_and_stop_before_host_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_win32service = SimpleNamespace(
        SERVICE_START_PENDING=1,
        SERVICE_RUNNING=2,
        SERVICE_STOP_PENDING=3,
        SERVICE_STOPPED=4,
    )
    monkeypatch.setattr(windows_service, "win32service", fake_win32service)
    service = ERechnungsPrueferService(["test"])
    service.ReportServiceStatus = Mock()  # type: ignore[method-assign]
    service._stop_event = Mock()

    service._report_status("running")
    service.SvcStop()
    service.SvcInterrogate()

    assert service.ReportServiceStatus.call_args_list == [
        call(2, waitHint=0),
        call(3, waitHint=20_000),
        call(3, waitHint=20_000),
    ]
    service._stop_event.set.assert_called_once_with()


def test_service_state_query_closes_scm_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_service = object()
    fake_manager = object()
    fake_win32service = SimpleNamespace(
        SC_MANAGER_CONNECT=1,
        SERVICE_QUERY_STATUS=2,
        SERVICE_STOPPED=3,
        OpenSCManager=Mock(return_value=fake_manager),
        OpenService=Mock(return_value=fake_service),
        QueryServiceStatus=Mock(return_value=(0, 3)),
        CloseServiceHandle=Mock(),
    )
    monkeypatch.setattr(windows_service, "win32service", fake_win32service)

    assert windows_service._service_is_stopped() is True
    assert fake_win32service.CloseServiceHandle.call_args_list == [
        call(fake_service),
        call(fake_manager),
    ]


def _management_options(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "initialize": False,
        "rotate_token": False,
        "grant_token_read": None,
        "verify_state": False,
        "preflight_port": False,
        "health_check": False,
        "import_token": None,
        "consent_token_import": False,
    }
    values.update(overrides)
    return Namespace(**values)


def _mock_management_dependencies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    paths = SimpleNamespace(
        data_directory=tmp_path,
        configuration=tmp_path / "service.json",
        token=tmp_path / "token.txt",
        log=tmp_path / "logs" / "service.log",
    )
    acl = Mock()
    store = Mock()
    configuration = ServiceConfiguration(port=18082)
    monkeypatch.setattr(
        windows_service,
        "ServicePaths",
        SimpleNamespace(from_environment=Mock(return_value=paths)),
    )
    monkeypatch.setattr(windows_service, "WindowsServiceAcl", Mock(return_value=acl))
    token_store_factory = Mock(return_value=store)
    monkeypatch.setattr(windows_service, "TokenStore", token_store_factory)
    monkeypatch.setattr(windows_service, "load_or_create_configuration", Mock(return_value=configuration))
    monkeypatch.setattr(windows_service, "load_service_configuration", Mock(return_value=configuration))
    return paths, acl, store, token_store_factory, configuration


@pytest.mark.parametrize(
    ("options", "expected_method", "expected_arguments"),
    [
        (_management_options(initialize=True), "load_or_create", ()),
        (
            _management_options(
                initialize=True,
                import_token="desktop-token.txt",
                consent_token_import=True,
            ),
            "import_value",
            ("m" * 43,),
        ),
        (_management_options(grant_token_read=r"DOMAIN\\NodeRed"), "load", ()),
        (_management_options(verify_state=True), "load", ()),
    ],
)
def test_service_management_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: Namespace,
    expected_method: str,
    expected_arguments: tuple[object, ...],
) -> None:
    paths, acl, store, _factory, _configuration = _mock_management_dependencies(monkeypatch, tmp_path)
    if options.initialize:
        monkeypatch.setattr(windows_service, "_service_is_stopped", Mock(return_value=True))
    token_reader = Mock(return_value="m" * 43)
    monkeypatch.setattr(windows_desktop_migration, "read_desktop_migration_token", token_reader)

    windows_service._manage_service(options)

    method = getattr(store, expected_method)
    if expected_method == "import_value":
        method.assert_called_once_with(*expected_arguments, consent=True)
        token_reader.assert_called_once_with(Path("desktop-token.txt"))
    else:
        method.assert_called_once_with(*expected_arguments)
    if options.grant_token_read:
        acl.grant_token_reader.assert_called_once_with(paths.token, options.grant_token_read)
    if options.verify_state:
        acl.verify_service_paths.assert_called_once_with(paths)
    acl.verify_existing_service_paths.assert_called_once_with(paths)
    if options.initialize:
        windows_service.load_or_create_configuration.assert_called_once_with(  # type: ignore[attr-defined]
            paths.configuration,
            protect=acl.protect_configuration,
        )
        windows_service.load_service_configuration.assert_not_called()  # type: ignore[attr-defined]
    else:
        windows_service.load_service_configuration.assert_called_once_with(paths.configuration)  # type: ignore[attr-defined]
        windows_service.load_or_create_configuration.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "options",
    [
        _management_options(initialize=True),
        _management_options(
            initialize=True,
            import_token="desktop-token.txt",
            consent_token_import=True,
        ),
    ],
)
def test_initialization_requires_stopped_service_before_programdata_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: Namespace,
) -> None:
    paths, acl, store, _factory, _configuration = _mock_management_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(windows_service, "_service_is_stopped", Mock(return_value=False))

    with pytest.raises(RuntimeError, match="gestopptem Dienst"):
        windows_service._manage_service(options)

    acl.verify_existing_service_paths.assert_called_once_with(paths)
    acl.protect_directory.assert_not_called()
    windows_service.load_or_create_configuration.assert_not_called()  # type: ignore[attr-defined]
    windows_service.load_service_configuration.assert_not_called()  # type: ignore[attr-defined]
    store.load_or_create.assert_not_called()
    store.import_value.assert_not_called()


def test_token_import_requires_consent_before_source_read_or_programdata_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _paths, acl, store, _factory, _configuration = _mock_management_dependencies(monkeypatch, tmp_path)
    token_reader = Mock()
    monkeypatch.setattr(windows_desktop_migration, "read_desktop_migration_token", token_reader)

    with pytest.raises(RuntimeError, match="ausdrückliche Zustimmung"):
        windows_service._manage_service(
            _management_options(
                initialize=True,
                import_token="desktop-token.txt",
                consent_token_import=False,
            )
        )

    token_reader.assert_not_called()
    acl.verify_existing_service_paths.assert_not_called()
    acl.protect_directory.assert_not_called()
    store.import_value.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [
        _management_options(grant_token_read=r"DOMAIN\\NodeRed"),
        _management_options(verify_state=True),
    ],
)
def test_existing_state_commands_never_create_or_normalize_configuration_and_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: Namespace,
) -> None:
    paths, acl, store, _factory, _configuration = _mock_management_dependencies(monkeypatch, tmp_path)
    paths.configuration.touch()
    paths.token.touch()
    stopped = Mock()
    monkeypatch.setattr(windows_service, "_service_is_stopped", stopped)

    windows_service._manage_service(options)

    windows_service.load_service_configuration.assert_called_once_with(paths.configuration)  # type: ignore[attr-defined]
    windows_service.load_or_create_configuration.assert_not_called()  # type: ignore[attr-defined]
    store.load.assert_called_once_with()
    store.load_or_create.assert_not_called()
    acl.protect_directory.assert_not_called()
    acl.protect_configuration.assert_not_called()
    acl.protect_token_preserving_readers.assert_not_called()
    stopped.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [
        _management_options(grant_token_read=r"DOMAIN\\NodeRed"),
        _management_options(verify_state=True),
    ],
)
@pytest.mark.parametrize("missing", ["configuration", "token"])
def test_existing_state_commands_fail_for_missing_state_without_creating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: Namespace,
    missing: str,
) -> None:
    _paths, acl, store, factory, _configuration = _mock_management_dependencies(monkeypatch, tmp_path)
    if missing == "configuration":
        windows_service.load_service_configuration.side_effect = FileNotFoundError  # type: ignore[attr-defined]
    else:
        store.load.side_effect = FileNotFoundError

    with pytest.raises(FileNotFoundError):
        windows_service._manage_service(options)

    windows_service.load_or_create_configuration.assert_not_called()  # type: ignore[attr-defined]
    store.load_or_create.assert_not_called()
    acl.protect_directory.assert_not_called()
    acl.protect_configuration.assert_not_called()
    acl.protect_token_preserving_readers.assert_not_called()
    acl.grant_token_reader.assert_not_called()
    acl.verify_service_paths.assert_not_called()
    if missing == "configuration":
        factory.assert_not_called()


def test_management_never_launders_an_untrusted_existing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _paths, acl, store, _factory, _configuration = _mock_management_dependencies(monkeypatch, tmp_path)
    acl.verify_existing_service_paths.side_effect = RuntimeError("nicht vertrauenswürdig")

    with pytest.raises(RuntimeError, match="nicht vertrauenswürdig"):
        windows_service._manage_service(_management_options(initialize=True))

    acl.protect_directory.assert_not_called()
    acl.protect_token_preserving_readers.assert_not_called()
    store.load_or_create.assert_not_called()


def test_token_rotation_requires_stopped_service_and_preserves_readers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, acl, _store, factory, _configuration = _mock_management_dependencies(monkeypatch, tmp_path)
    existing_store = Mock()
    preserving_store = Mock()
    factory.side_effect = [existing_store, preserving_store]
    monkeypatch.setattr(windows_service, "_service_is_stopped", Mock(return_value=True))

    windows_service._manage_service(_management_options(rotate_token=True))

    existing_store.load.assert_called_once_with()
    acl.token_protector_preserving.assert_called_once_with(paths.token)
    preserving_store.rotate.assert_called_once_with()

    monkeypatch.setattr(windows_service, "_service_is_stopped", Mock(return_value=False))
    factory.side_effect = None
    factory.return_value = Mock()
    with pytest.raises(RuntimeError, match="gestopptem Dienst"):
        windows_service._manage_service(_management_options(rotate_token=True))


def test_port_preflight_and_health_management_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _paths, _acl, _store, _factory, configuration = _mock_management_dependencies(monkeypatch, tmp_path)
    listener = Mock()
    reserve = Mock(return_value=(listener, configuration.port))
    monkeypatch.setattr(server_runtime, "reserve_loopback_socket", reserve)
    monkeypatch.setattr(windows_service, "_service_is_stopped", Mock(return_value=True))

    windows_service._manage_service(_management_options(preflight_port=True))

    reserve.assert_called_once_with(configuration.port)
    listener.close.assert_called_once_with()

    health = Mock(return_value=False)
    monkeypatch.setattr(server_runtime, "health_is_ready", health)
    with pytest.raises(RuntimeError, match="Healthcheck"):
        windows_service._manage_service(_management_options(health_check=True))


def test_management_argument_contract_and_windows_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    parsed = windows_service._parse_management_arguments(
        ["--initialize", "--import-token", "token.txt", "--consent-token-import"]
    )
    assert parsed.initialize is True
    assert parsed.import_token == "token.txt"
    assert parsed.consent_token_import is True

    with pytest.raises(SystemExit):
        windows_service._parse_management_arguments(["--health-check", "--import-token", "token.txt"])
    with pytest.raises(SystemExit):
        windows_service._parse_management_arguments(["--initialize", "--import-token", "token.txt"])
    with pytest.raises(SystemExit):
        windows_service._parse_management_arguments(["--initialize", "--consent-token-import"])

    with pytest.raises(SystemExit, match="ausschließlich für Windows"):
        windows_service.main(["--health-check"])

    manager = Mock()
    fake_service_manager = SimpleNamespace(
        Initialize=Mock(),
        PrepareToHostSingle=Mock(),
        StartServiceCtrlDispatcher=Mock(),
    )
    monkeypatch.setattr(windows_service.sys, "platform", "win32")
    monkeypatch.setattr(windows_service, "servicemanager", fake_service_manager)
    monkeypatch.setattr(windows_service, "_manage_service", manager)

    assert windows_service.main(["--health-check"]) == 0
    manager.assert_called_once()

    manager.side_effect = RuntimeError("verwaltungsfehler")
    assert windows_service.main(["--health-check"]) == 1

    assert windows_service.main([]) == 0
    fake_service_manager.Initialize.assert_called_once_with()
    fake_service_manager.PrepareToHostSingle.assert_called_once_with(ERechnungsPrueferService)
    fake_service_manager.StartServiceCtrlDispatcher.assert_called_once_with()


def test_windows_service_direct_start_is_controlled_but_other_dispatcher_errors_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher = Mock(side_effect=OSError(1063, "StartServiceCtrlDispatcher"))
    fake_service_manager = SimpleNamespace(
        Initialize=Mock(),
        PrepareToHostSingle=Mock(),
        StartServiceCtrlDispatcher=dispatcher,
    )
    monkeypatch.setattr(windows_service.sys, "platform", "win32")
    monkeypatch.setattr(windows_service, "servicemanager", fake_service_manager)

    assert windows_service.main([]) == windows_service.DIRECT_START_EXIT_CODE
    fake_service_manager.Initialize.assert_called_once_with()
    fake_service_manager.PrepareToHostSingle.assert_called_once_with(ERechnungsPrueferService)
    dispatcher.assert_called_once_with()

    dispatcher.reset_mock()
    dispatcher.side_effect = OSError(5, "StartServiceCtrlDispatcher")
    with pytest.raises(OSError) as exc_info:
        windows_service.main([])
    assert exc_info.value.args[0] == 5
    dispatcher.assert_called_once_with()


def test_direct_start_error_recognizes_winerror_attribute() -> None:
    error = RuntimeError("SCM nicht verbunden")
    error.winerror = windows_service.SERVICE_CONTROLLER_CONNECT_ERROR  # type: ignore[attr-defined]

    assert windows_service._is_direct_service_start_error(error) is True
    assert windows_service._is_direct_service_start_error(RuntimeError("anderer Fehler")) is False
