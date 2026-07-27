from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from app import windows_open_client

EXPECTED_SERVICE_EXE = r"C:\Program Files\E-Rechnungs-Pruefer-Dienst\service\E-Rechnungs-Pruefer-Dienst.exe"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--begin-service-transition"],
        ["--mark-service-rollback-complete"],
        ["--mark-service-committed"],
        ["--prepare-install-reconcile"],
        ["--finish-install-reconcile"],
        ["--snapshot-service-metadata"],
        ["--restore-service-metadata"],
        ["--clear-service-metadata"],
        ["--reconcile-service-uninstall"],
        ["--assert-no-pending-service-uninstall"],
        ["--disable-service-delayed-start"],
    ],
)
def test_service_actions_require_expected_executable(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        windows_open_client._parse_arguments(arguments)


def test_begin_requires_and_owns_target_running_parameter() -> None:
    with pytest.raises(SystemExit):
        windows_open_client._parse_arguments(
            ["--begin-service-transition", "--expected-service-exe", EXPECTED_SERVICE_EXE]
        )
    with pytest.raises(SystemExit):
        windows_open_client._parse_arguments(["--probe", "--target-service-running", "1"])

    parsed = windows_open_client._parse_arguments(
        [
            "--begin-service-transition",
            "--expected-service-exe",
            EXPECTED_SERVICE_EXE,
            "--target-service-running",
            "0",
        ]
    )
    assert parsed.target_service_running == "0"


@pytest.mark.parametrize(
    "removed_argument",
    [
        "--plan-desktop-migration",
        "--apply-desktop-migration",
        "--verify-migration-context",
        "--token-transfer-consent",
        "--setup-diagnostic",
    ],
)
def test_removed_migration_arguments_are_not_accepted(removed_argument: str) -> None:
    with pytest.raises(SystemExit):
        windows_open_client._parse_arguments([removed_argument])


def test_conflict_action_is_internal_and_parameterless() -> None:
    parsed = windows_open_client._parse_arguments(["--assert-no-desktop-installation"])
    assert windows_open_client._internal_action_stage(parsed) == "assert-no-desktop-installation"
    with pytest.raises(SystemExit):
        windows_open_client._parse_arguments(
            ["--assert-no-desktop-installation", "--expected-service-exe", EXPECTED_SERVICE_EXE]
        )


def test_offline_hive_worker_is_internal_and_requires_a_path() -> None:
    parsed = windows_open_client._parse_arguments(["--inspect-offline-profile-hive", r"C:\Users\Fixture\NTUSER.DAT"])
    assert windows_open_client._internal_action_stage(parsed) == "inspect-offline-profile-hive"
    with pytest.raises(SystemExit):
        windows_open_client._parse_arguments(["--inspect-offline-profile-hive"])


def test_main_routes_desktop_conflict_check_in_administrative_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    elevated = Mock()
    inventory = Mock()
    monkeypatch.setattr(windows_open_client, "verify_administrative_context", elevated)
    monkeypatch.setattr(windows_open_client, "assert_no_desktop_installation", inventory)

    assert windows_open_client.main(["--assert-no-desktop-installation"]) == 0
    elevated.assert_called_once_with()
    inventory.assert_called_once_with()


@pytest.mark.parametrize(("conflict", "exit_code"), [(False, 0), (True, 10)])
def test_main_routes_isolated_offline_hive_worker(
    conflict: bool,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    elevated = Mock()
    inspect = Mock(return_value=conflict)
    monkeypatch.setattr(windows_open_client, "verify_administrative_context", elevated)
    monkeypatch.setattr(windows_open_client, "inspect_offline_profile_hive", inspect)

    path = r"C:\Users\Fixture\NTUSER.DAT"
    assert windows_open_client.main(["--inspect-offline-profile-hive", path]) == exit_code
    elevated.assert_called_once_with()
    inspect.assert_called_once_with(Path(path))


def test_internal_failure_returns_one_without_user_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(windows_open_client, "verify_administrative_context", lambda: None)
    monkeypatch.setattr(
        windows_open_client,
        "assert_no_desktop_installation",
        Mock(side_effect=RuntimeError("desktop")),
    )
    show = Mock()
    monkeypatch.setattr(windows_open_client, "_show_message", show)
    assert windows_open_client.main(["--assert-no-desktop-installation"]) == 1
    show.assert_not_called()


def test_main_routes_service_transaction_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(windows_open_client, "verify_administrative_context", lambda: None)
    begin = Mock()
    rollback = Mock(return_value=10)
    commit = Mock(return_value=11)
    classify = Mock(return_value=12)
    finish = Mock(return_value=0)
    monkeypatch.setattr(windows_open_client, "begin_service_transition", begin)
    monkeypatch.setattr(windows_open_client, "mark_service_rollback_complete", rollback)
    monkeypatch.setattr(windows_open_client, "mark_service_committed", commit)
    monkeypatch.setattr(windows_open_client, "classify_install_reconcile", classify)
    monkeypatch.setattr(windows_open_client, "finish_install_reconcile", finish)

    assert (
        windows_open_client.main(
            [
                "--begin-service-transition",
                "--expected-service-exe",
                EXPECTED_SERVICE_EXE,
                "--target-service-running",
                "1",
            ]
        )
        == 0
    )
    begin.assert_called_once_with(Path(EXPECTED_SERVICE_EXE), target_service_running=True)
    assert (
        windows_open_client.main(["--mark-service-rollback-complete", "--expected-service-exe", EXPECTED_SERVICE_EXE])
        == 10
    )
    assert windows_open_client.main(["--mark-service-committed", "--expected-service-exe", EXPECTED_SERVICE_EXE]) == 11
    assert (
        windows_open_client.main(["--prepare-install-reconcile", "--expected-service-exe", EXPECTED_SERVICE_EXE]) == 12
    )
    assert windows_open_client.main(["--finish-install-reconcile", "--expected-service-exe", EXPECTED_SERVICE_EXE]) == 0


@pytest.mark.parametrize(
    ("argument", "attribute"),
    [
        ("--preflight-machine", "preflight_machine"),
        ("--preflight-port", "preflight_loopback_port"),
        ("--purge-runtime-state", "purge_runtime_state"),
        ("--purge-machine-state", "purge_machine_state"),
    ],
)
def test_main_routes_machine_actions(
    argument: str,
    attribute: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(windows_open_client, "verify_administrative_context", lambda: None)
    action = Mock()
    monkeypatch.setattr(windows_open_client, attribute, action)
    assert windows_open_client.main([argument]) == 0
    action.assert_called_once_with()


def test_default_action_opens_browser_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(windows_open_client, "request_browser_url", lambda: "http://127.0.0.1:8080/")
    opened = Mock(return_value=True)
    monkeypatch.setattr(windows_open_client.webbrowser, "open", opened)
    assert windows_open_client.main([]) == 0
    opened.assert_called_once_with("http://127.0.0.1:8080/")


def test_probe_does_not_open_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(windows_open_client, "request_browser_url", lambda: "http://127.0.0.1:8080/")
    opened = Mock()
    monkeypatch.setattr(windows_open_client.webbrowser, "open", opened)
    assert windows_open_client.main(["--probe"]) == 0
    opened.assert_not_called()
