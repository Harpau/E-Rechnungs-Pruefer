from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from app import windows_open_client

EXPECTED_SERVICE_EXE = (
    r"C:\Program Files\E-Rechnungs-Pruefer-Dienst"
    r"\service\E-Rechnungs-Pruefer-Dienst.exe"
)
TRANSFER_DIRECTORY = (
    r"C:\ProgramData\E-Rechnungs-Pruefer-Installer-Transfer"
    r"\is-ABCD.tmp"
)
CLIENT_SOURCE = r"C:\Users\Tester\AppData\Local\Temp\is-ABCD.tmp\E-Rechnungs-Pruefer-Oeffnen.exe"
CLIENT_NAME = "E-Rechnungs-Pruefer-Oeffnen.exe"


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "--prepare-desktop-migration-transfer",
            "--transfer-directory",
            TRANSFER_DIRECTORY,
            "--client-source",
            CLIENT_SOURCE,
            "--client-name",
            CLIENT_NAME,
        ],
        [
            "--clear-desktop-migration-transfer",
            "--transfer-directory",
            TRANSFER_DIRECTORY,
            "--client-name",
            CLIENT_NAME,
        ],
        [
            "--seal-desktop-migration",
            "--receipt",
            "plan.json",
            "--transfer-directory",
            TRANSFER_DIRECTORY,
            "--client-name",
            CLIENT_NAME,
        ],
        ["--verify-applied-desktop-migration"],
        [
            "--begin-service-transition",
            "--expected-service-exe",
            EXPECTED_SERVICE_EXE,
            "--target-service-running",
            "1",
        ],
        ["--mark-service-rollback-complete", "--expected-service-exe", EXPECTED_SERVICE_EXE],
        ["--mark-service-committed", "--expected-service-exe", EXPECTED_SERVICE_EXE],
        ["--prepare-install-reconcile", "--expected-service-exe", EXPECTED_SERVICE_EXE],
        ["--finish-install-reconcile", "--expected-service-exe", EXPECTED_SERVICE_EXE],
        ["--clear-desktop-migration-seal"],
    ],
)
def test_administrative_transaction_actions_reject_an_unelevated_process(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(windows_open_client, "is_process_elevated", Mock(return_value=False))
    message = Mock()
    monkeypatch.setattr(windows_open_client, "_show_message", message)

    assert windows_open_client.main(arguments) == 1
    message.assert_not_called()


def test_open_client_routes_plan_seal_apply_and_applied_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    plan = Mock()
    validate_transfer = Mock(side_effect=lambda **_: calls.append("validate"))
    seal = Mock(side_effect=lambda **_: calls.append("seal"))
    apply = Mock()
    verify = Mock()
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(windows_open_client, "plan_desktop_migration", plan, raising=False)
    monkeypatch.setattr(
        windows_open_client,
        "validate_desktop_migration_transfer",
        validate_transfer,
        raising=False,
    )
    monkeypatch.setattr(windows_open_client, "seal_desktop_migration", seal, raising=False)
    monkeypatch.setattr(windows_open_client, "apply_desktop_migration", apply, raising=False)
    monkeypatch.setattr(
        windows_open_client,
        "verify_no_legacy_desktop_conflicts",
        verify,
    )

    monkeypatch.setattr(windows_open_client, "is_process_elevated", Mock(return_value=False))
    assert (
        windows_open_client.main(
            [
                "--plan-desktop-migration",
                "--receipt",
                "plan.json",
                "--token-transfer",
                "token.txt",
            ]
        )
        == 0
    )
    assert windows_open_client.main(["--apply-desktop-migration"]) == 0

    monkeypatch.setattr(windows_open_client, "is_process_elevated", Mock(return_value=True))
    assert (
        windows_open_client.main(
            [
                "--seal-desktop-migration",
                "--receipt",
                "plan.json",
                "--token-transfer",
                "token.txt",
                "--transfer-directory",
                TRANSFER_DIRECTORY,
                "--client-name",
                CLIENT_NAME,
            ]
        )
        == 0
    )
    assert windows_open_client.main(["--verify-applied-desktop-migration"]) == 0

    plan.assert_called_once_with(
        receipt_path=Path("plan.json"),
        token_transfer_path=Path("token.txt"),
    )
    validate_transfer.assert_called_once_with(
        transfer_directory=Path(TRANSFER_DIRECTORY),
        receipt_path=Path("plan.json"),
        token_transfer_path=Path("token.txt"),
        client_name=CLIENT_NAME,
    )
    seal.assert_called_once_with(
        receipt_path=Path("plan.json"),
        token_transfer_path=Path("token.txt"),
    )
    assert calls == ["validate", "seal"]
    apply.assert_called_once_with()
    verify.assert_called_once_with()


def test_open_client_routes_administrative_transfer_preparation_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare = Mock(return_value=Path(TRANSFER_DIRECTORY) / CLIENT_NAME)
    clear = Mock()
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(windows_open_client, "is_process_elevated", Mock(return_value=True))
    monkeypatch.setattr(
        windows_open_client,
        "prepare_desktop_migration_transfer",
        prepare,
        raising=False,
    )
    monkeypatch.setattr(
        windows_open_client,
        "clear_desktop_migration_transfer",
        clear,
        raising=False,
    )

    assert (
        windows_open_client.main(
            [
                "--prepare-desktop-migration-transfer",
                "--transfer-directory",
                TRANSFER_DIRECTORY,
                "--client-source",
                CLIENT_SOURCE,
                "--client-name",
                CLIENT_NAME,
            ]
        )
        == 0
    )
    assert (
        windows_open_client.main(
            [
                "--clear-desktop-migration-transfer",
                "--transfer-directory",
                TRANSFER_DIRECTORY,
                "--client-name",
                CLIENT_NAME,
            ]
        )
        == 0
    )

    prepare.assert_called_once_with(
        transfer_directory=Path(TRANSFER_DIRECTORY),
        client_source=Path(CLIENT_SOURCE),
        client_name=CLIENT_NAME,
    )
    clear.assert_called_once_with(
        transfer_directory=Path(TRANSFER_DIRECTORY),
        client_name=CLIENT_NAME,
    )


def test_open_client_routes_bound_service_transition_and_proof_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    begin = Mock()
    rollback = Mock(return_value=10)
    committed = Mock(return_value=11)
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(windows_open_client, "is_process_elevated", Mock(return_value=True))
    monkeypatch.setattr(windows_open_client, "begin_service_transition", begin, raising=False)
    monkeypatch.setattr(
        windows_open_client,
        "mark_service_rollback_complete",
        rollback,
        raising=False,
    )
    monkeypatch.setattr(windows_open_client, "mark_service_committed", committed, raising=False)

    assert (
        windows_open_client.main(
            [
                "--begin-service-transition",
                "--expected-service-exe",
                EXPECTED_SERVICE_EXE,
                "--target-service-running",
                "1",
                "--token-transfer-consent",
            ]
        )
        == 0
    )
    assert (
        windows_open_client.main(["--mark-service-rollback-complete", "--expected-service-exe", EXPECTED_SERVICE_EXE])
        == 10
    )
    assert windows_open_client.main(["--mark-service-committed", "--expected-service-exe", EXPECTED_SERVICE_EXE]) == 11

    expected = Path(EXPECTED_SERVICE_EXE)
    begin.assert_called_once_with(
        expected,
        token_transfer_consent=True,
        target_service_running=True,
    )
    rollback.assert_called_once_with(expected)
    committed.assert_called_once_with(expected)


@pytest.mark.parametrize(
    ("direction", "expected_exit_code"),
    [(0, 0), (10, 10), (11, 11), (12, 12)],
)
def test_open_client_preserves_reconcile_classifier_exit_codes(
    direction: int,
    expected_exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classify = Mock(return_value=direction)
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(windows_open_client, "is_process_elevated", Mock(return_value=True))
    monkeypatch.setattr(windows_open_client, "classify_install_reconcile", classify, raising=False)

    assert (
        windows_open_client.main(["--prepare-install-reconcile", "--expected-service-exe", EXPECTED_SERVICE_EXE])
        == expected_exit_code
    )
    classify.assert_called_once_with(Path(EXPECTED_SERVICE_EXE))


def test_open_client_transaction_arguments_are_strict() -> None:
    invalid = [
        ["--plan-desktop-migration"],
        ["--apply-desktop-migration", "--receipt", "plan.json"],
        [
            "--seal-desktop-migration",
            "--receipt",
            "plan.json",
            "--transfer-directory",
            TRANSFER_DIRECTORY,
            "--client-name",
            CLIENT_NAME,
            "--target-service-running",
        ],
        ["--seal-desktop-migration", "--receipt", "plan.json"],
        [
            "--prepare-desktop-migration-transfer",
            "--transfer-directory",
            TRANSFER_DIRECTORY,
            "--client-name",
            CLIENT_NAME,
        ],
        [
            "--clear-desktop-migration-transfer",
            "--transfer-directory",
            TRANSFER_DIRECTORY,
        ],
        [
            "--clear-desktop-migration-transfer",
            "--transfer-directory",
            TRANSFER_DIRECTORY,
            "--client-name",
            CLIENT_NAME,
            "--client-source",
            CLIENT_SOURCE,
        ],
        ["--probe", "--transfer-directory", TRANSFER_DIRECTORY],
        ["--begin-service-transition"],
        ["--preflight-machine", "--expected-service-exe", EXPECTED_SERVICE_EXE],
        ["--mark-service-committed", "--token-transfer-consent"],
        ["--rollback-desktop-migration", "--require-seal"],
    ]

    for arguments in invalid:
        with pytest.raises(SystemExit):
            windows_open_client._parse_arguments(arguments)
