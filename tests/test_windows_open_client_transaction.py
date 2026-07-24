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
        ["--setup-diagnostic", "setup-action-diagnostic-v1.txt"],
    ]

    for arguments in invalid:
        with pytest.raises(SystemExit):
            windows_open_client._parse_arguments(arguments)


def test_internal_setup_failure_writes_only_bounded_sanitized_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SecretWinError(OSError):
        def __init__(self) -> None:
            super().__init__(5, "token=must-never-be-written C:\\Secret\\invoice.xml")
            self.winerror = 5

    target = tmp_path / windows_open_client._SETUP_DIAGNOSTIC_FILE_NAME
    message = Mock()
    monkeypatch.setattr(windows_open_client.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_open_client,
        "_validated_setup_diagnostic_path",
        Mock(return_value=target),
    )
    monkeypatch.setattr(
        windows_open_client,
        "preflight_loopback_port",
        Mock(side_effect=_SecretWinError()),
    )
    monkeypatch.setattr(windows_open_client, "_show_message", message)

    assert (
        windows_open_client.main(
            [
                "--preflight-port",
                "--setup-diagnostic",
                str(target),
            ]
        )
        == 1
    )

    payload = target.read_bytes()
    assert payload == (
        b"ERP_SETUP_DIAGNOSTIC_V1|stage=preflight-port|error=os-error|origin=unknown|detail=none|winerror=5"
    )
    assert len(payload) <= windows_open_client._SETUP_DIAGNOSTIC_MAX_BYTES
    assert b"token" not in payload
    assert b"Secret" not in payload
    assert b"invoice" not in payload
    message.assert_not_called()


def test_setup_diagnostic_target_is_exactly_bound_to_protected_transfer_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ProgramData" / "E-Rechnungs-Pruefer-Installer-Transfer"
    leaf = root / "is-ABCD.tmp"
    target = leaf / windows_open_client._SETUP_DIAGNOSTIC_FILE_NAME
    verify = Mock()
    monkeypatch.setattr(
        windows_open_client._desktop_migration,
        "_desktop_migration_transfer_root",
        Mock(return_value=root),
    )
    monkeypatch.setattr(
        windows_open_client._desktop_migration,
        "_verify_transfer_staging_path",
        verify,
    )
    monkeypatch.setattr(
        windows_open_client._desktop_migration,
        "validate_machine_path",
        Mock(return_value=False),
    )

    assert windows_open_client._validated_setup_diagnostic_path(str(target)) == target
    assert verify.call_args_list == [
        ((root,), {"directory": True, "kind": "root"}),
        ((leaf,), {"directory": True, "kind": "leaf"}),
    ]

    for invalid in (
        root / windows_open_client._SETUP_DIAGNOSTIC_FILE_NAME,
        leaf / "other.txt",
        root.parent / "foreign" / windows_open_client._SETUP_DIAGNOSTIC_FILE_NAME,
        leaf / ".." / windows_open_client._SETUP_DIAGNOSTIC_FILE_NAME,
    ):
        with pytest.raises(RuntimeError):
            windows_open_client._validated_setup_diagnostic_path(str(invalid))


def test_setup_diagnostic_never_overwrites_an_existing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / windows_open_client._SETUP_DIAGNOSTIC_FILE_NAME
    target.write_bytes(b"preexisting")
    monkeypatch.setattr(
        windows_open_client,
        "_validated_setup_diagnostic_path",
        Mock(return_value=target),
    )

    windows_open_client._write_setup_diagnostic(
        str(target),
        stage="preflight-port",
        exc=RuntimeError("secret"),
    )

    assert target.read_bytes() == b"preexisting"


def test_setup_diagnostic_extracts_only_numeric_pywin32_error_code() -> None:
    class error(Exception):
        pass

    error.__module__ = "pywintypes"
    failure = error(5, "RegUnLoadKey", "secret path and token")

    assert windows_open_client._setup_error_code(failure) == "windows-api-error"
    assert windows_open_client._setup_winerror(failure) == 5

    invalid = error("5", "RegUnLoadKey", "secret path and token")
    assert windows_open_client._setup_error_code(invalid) == "internal-error"
    assert windows_open_client._setup_winerror(invalid) is None


def test_setup_diagnostic_origin_uses_only_fixed_allowlisted_function_codes() -> None:
    expected_origins = {
        "_canonicalize_profile_hive_recovery_tail": "hive-canonicalize-tail",
        "_canonicalize_profile_hive_support_file": "hive-canonicalize-file",
        "_validate_profile_hive_recovery_directory": "hive-recovery-directory",
        "_wait_for_profile_hive_recovery_tail_empty": "hive-wait-empty",
        "_wait_for_profile_hive_snapshot_absent": "hive-wait-absent",
    }
    assert {
        function_name: windows_open_client._SETUP_DIAGNOSTIC_ORIGINS[function_name]
        for function_name in expected_origins
    } == expected_origins

    def _verify_profile_hive_support_file() -> None:
        raise LookupError("secret path and token")

    with pytest.raises(LookupError) as caught:
        _verify_profile_hive_support_file()

    assert windows_open_client._setup_error_origin(caught.value) == "hive-support-file"
    assert "secret" not in windows_open_client._setup_error_origin(caught.value)
    assert windows_open_client._setup_error_origin(LookupError("secret")) == "unknown"


def test_setup_diagnostic_detail_uses_only_exact_fixed_canonicalization_codes() -> None:
    migration = windows_open_client._desktop_migration
    expected = {"none", *(failure.value for failure in migration._ProfileHiveCanonicalizationFailure)}
    assert windows_open_client._SETUP_DIAGNOSTIC_DETAILS == expected

    for failure in migration._ProfileHiveCanonicalizationFailure:
        error = migration._ProfileHiveCanonicalizationError(failure)
        assert windows_open_client._setup_error_detail(error) == failure.value
        assert "S-1-" not in str(error)

    class _LookalikeCanonicalizationError(RuntimeError):
        failure = migration._ProfileHiveCanonicalizationFailure.OWNER

    assert windows_open_client._setup_error_detail(_LookalikeCanonicalizationError("secret")) == "none"
    assert windows_open_client._setup_error_detail(RuntimeError(r"secret C:\\path S-1-5-21-1000")) == "none"


def test_setup_diagnostic_serializes_canonicalization_detail_without_cause_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = windows_open_client._desktop_migration
    target = tmp_path / windows_open_client._SETUP_DIAGNOSTIC_FILE_NAME
    monkeypatch.setattr(
        windows_open_client,
        "_validated_setup_diagnostic_path",
        Mock(return_value=target),
    )
    secret = OSError(5, r"token=secret C:\\invoice.xml S-1-5-21-1000")
    secret.winerror = 5  # type: ignore[attr-defined]
    try:
        raise migration._ProfileHiveCanonicalizationError(
            migration._ProfileHiveCanonicalizationFailure.ACE_FLAGS
        ) from secret
    except migration._ProfileHiveCanonicalizationError as error:
        windows_open_client._write_setup_diagnostic(
            str(target),
            stage="verify-applied-migration",
            exc=error,
        )

    assert target.read_bytes() == (
        b"ERP_SETUP_DIAGNOSTIC_V1|stage=verify-applied-migration|error=runtime-error"
        b"|origin=unknown|detail=ace-flags|winerror=5"
    )
