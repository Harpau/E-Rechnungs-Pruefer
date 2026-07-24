from __future__ import annotations

import argparse
import ctypes
import sys
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from .windows_desktop_migration import (
    apply_desktop_migration,
    clear_desktop_migration_seal,
    clear_desktop_migration_transfer,
    commit_desktop_migration,
    plan_desktop_migration,
    prepare_desktop_migration_transfer,
    rollback_desktop_migration,
    seal_desktop_migration,
    validate_desktop_migration_transfer,
    verify_desktop_migration_owner,
    verify_no_legacy_desktop_conflicts,
)
from .windows_install_reconcile import (
    begin_service_transition,
    classify_install_reconcile,
    finish_install_reconcile,
    mark_service_committed,
    mark_service_rollback_complete,
)
from .windows_service_ipc import request_browser_url
from .windows_service_metadata import (
    assert_no_pending_service_uninstall,
    clear_service_metadata,
    disable_service_delayed_start,
    reconcile_service_uninstall,
    restore_service_metadata,
    snapshot_service_metadata,
)
from .windows_service_preflight import (
    preflight_loopback_port,
    preflight_machine,
    purge_machine_state,
    purge_runtime_state,
)


def _show_message(message: str, *, error: bool) -> None:
    flags = 0x10 if error else 0x40
    message_box = ctypes.CDLL("user32").MessageBoxW
    message_box.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    message_box.restype = ctypes.c_int
    message_box(None, message, "E-Rechnungs-Prüfer", flags)


def _parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="E-Rechnungs-Pruefer-Oeffnen")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--prepare-desktop-migration-transfer", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--clear-desktop-migration-transfer", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--plan-desktop-migration", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--seal-desktop-migration", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--apply-desktop-migration", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--verify-applied-desktop-migration", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--verify-desktop-migration-owner", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--rollback-desktop-migration", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--commit-desktop-migration", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--clear-desktop-migration-seal", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--begin-service-transition", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--mark-service-rollback-complete", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--mark-service-committed", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--prepare-install-reconcile", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--finish-install-reconcile", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--preflight-machine", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--preflight-port", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--snapshot-service-metadata", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--restore-service-metadata", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--clear-service-metadata", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--reconcile-service-uninstall", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--assert-no-pending-service-uninstall", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--disable-service-delayed-start", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--verify-migration-context", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--purge-runtime-state", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--purge-machine-state", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--receipt", help=argparse.SUPPRESS)
    parser.add_argument("--token-transfer", help=argparse.SUPPRESS)
    parser.add_argument("--transfer-directory", help=argparse.SUPPRESS)
    parser.add_argument("--client-source", help=argparse.SUPPRESS)
    parser.add_argument("--client-name", help=argparse.SUPPRESS)
    parser.add_argument("--expected-service-exe", help=argparse.SUPPRESS)
    parser.add_argument("--target-service-running", choices=("0", "1"), help=argparse.SUPPRESS)
    parser.add_argument("--token-transfer-consent", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    receipt_action = options.plan_desktop_migration or options.seal_desktop_migration
    if receipt_action and not options.receipt:
        parser.error("Für Plan oder Seal der internen Desktopmigration fehlt --receipt.")
    if options.receipt and not receipt_action:
        parser.error("--receipt ist nur für Plan oder Seal der internen Desktopmigration zulässig.")
    if options.token_transfer and not receipt_action:
        parser.error("--token-transfer ist nur für Plan oder Seal der Desktopmigration zulässig.")
    transfer_action = (
        options.prepare_desktop_migration_transfer
        or options.seal_desktop_migration
        or options.clear_desktop_migration_transfer
    )
    if transfer_action and not options.transfer_directory:
        parser.error("Für das interne Desktop-Transfer-Staging fehlt --transfer-directory.")
    if transfer_action and not options.client_name:
        parser.error("Für das interne Desktop-Transfer-Staging fehlt --client-name.")
    if options.transfer_directory and not transfer_action:
        parser.error("--transfer-directory ist nur für das interne Desktop-Transfer-Staging zulässig.")
    if options.client_name and not transfer_action:
        parser.error("--client-name ist nur für das interne Desktop-Transfer-Staging zulässig.")
    if options.prepare_desktop_migration_transfer and not options.client_source:
        parser.error("Für die Vorbereitung des Desktop-Transfer-Stagings fehlt --client-source.")
    if options.client_source and not options.prepare_desktop_migration_transfer:
        parser.error("--client-source ist nur für die Vorbereitung des Desktop-Transfer-Stagings zulässig.")
    metadata_action = (
        options.snapshot_service_metadata
        or options.restore_service_metadata
        or options.clear_service_metadata
        or options.reconcile_service_uninstall
        or options.assert_no_pending_service_uninstall
        or options.disable_service_delayed_start
    )
    transaction_action = (
        options.begin_service_transition
        or options.mark_service_rollback_complete
        or options.mark_service_committed
        or options.prepare_install_reconcile
        or options.finish_install_reconcile
    )
    expected_executable_action = metadata_action or transaction_action
    if expected_executable_action and not options.expected_service_exe:
        parser.error("Für die interne Dienstverwaltung fehlt --expected-service-exe.")
    if options.expected_service_exe and not expected_executable_action:
        parser.error("Der Dienstpfad ist nur zusammen mit einer Dienstverwaltungsaktion zulässig.")
    if options.begin_service_transition and options.target_service_running is None:
        parser.error("Beim Beginn der Diensttransaktion fehlt --target-service-running {0|1}.")
    if options.target_service_running is not None and not options.begin_service_transition:
        parser.error("--target-service-running ist nur beim Beginn der Diensttransaktion zulässig.")
    if options.token_transfer_consent and not options.begin_service_transition:
        parser.error("--token-transfer-consent ist nur beim Beginn der Diensttransaktion zulässig.")
    return options


def is_process_elevated() -> bool:
    if sys.platform != "win32":
        raise OSError("Der Windows-Sicherheitskontext kann nur unter Windows geprüft werden.")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    is_user_an_admin = shell32.IsUserAnAdmin
    is_user_an_admin.argtypes = []
    is_user_an_admin.restype = ctypes.c_bool
    return bool(is_user_an_admin())


def verify_migration_context() -> None:
    if is_process_elevated():
        raise RuntimeError(
            "Die Desktopmigration darf nicht in einem erhöhten Benutzerkontext laufen. "
            "Starten Sie das Setup normal und bestätigen Sie anschließend die UAC-Abfrage."
        )


def verify_administrative_context() -> None:
    if not is_process_elevated():
        raise RuntimeError("Die interne Maschinenoperation benötigt Administratorrechte.")


def _is_internal_action(options: argparse.Namespace) -> bool:
    return bool(
        options.prepare_desktop_migration_transfer
        or options.clear_desktop_migration_transfer
        or options.plan_desktop_migration
        or options.seal_desktop_migration
        or options.apply_desktop_migration
        or options.verify_applied_desktop_migration
        or options.verify_desktop_migration_owner
        or options.rollback_desktop_migration
        or options.commit_desktop_migration
        or options.clear_desktop_migration_seal
        or options.begin_service_transition
        or options.mark_service_rollback_complete
        or options.mark_service_committed
        or options.prepare_install_reconcile
        or options.finish_install_reconcile
        or options.probe
        or options.preflight_machine
        or options.preflight_port
        or options.snapshot_service_metadata
        or options.restore_service_metadata
        or options.clear_service_metadata
        or options.reconcile_service_uninstall
        or options.assert_no_pending_service_uninstall
        or options.disable_service_delayed_start
        or options.verify_migration_context
        or options.purge_runtime_state
        or options.purge_machine_state
    )


def main(argv: Sequence[str] | None = None) -> int:
    options = _parse_arguments(list(sys.argv[1:] if argv is None else argv))
    if sys.platform != "win32":
        raise SystemExit("Der Öffnen-Client ist ausschließlich für Windows vorgesehen.")
    try:
        if options.prepare_desktop_migration_transfer:
            verify_administrative_context()
            prepare_desktop_migration_transfer(
                transfer_directory=Path(options.transfer_directory),
                client_source=Path(options.client_source),
                client_name=options.client_name,
            )
            return 0
        if options.clear_desktop_migration_transfer:
            verify_administrative_context()
            clear_desktop_migration_transfer(
                transfer_directory=Path(options.transfer_directory),
                client_name=options.client_name,
            )
            return 0
        if options.plan_desktop_migration:
            plan_desktop_migration(
                receipt_path=Path(options.receipt),
                token_transfer_path=Path(options.token_transfer) if options.token_transfer else None,
            )
            return 0
        if options.seal_desktop_migration:
            verify_administrative_context()
            validate_desktop_migration_transfer(
                transfer_directory=Path(options.transfer_directory),
                receipt_path=Path(options.receipt),
                token_transfer_path=Path(options.token_transfer) if options.token_transfer else None,
                client_name=options.client_name,
            )
            seal_desktop_migration(
                receipt_path=Path(options.receipt),
                token_transfer_path=Path(options.token_transfer) if options.token_transfer else None,
            )
            return 0
        if options.apply_desktop_migration:
            apply_desktop_migration()
            return 0
        if options.verify_applied_desktop_migration:
            verify_administrative_context()
            verify_no_legacy_desktop_conflicts()
            return 0
        if options.verify_desktop_migration_owner:
            verify_desktop_migration_owner()
            return 0
        if options.rollback_desktop_migration:
            rollback_desktop_migration()
            return 0
        if options.commit_desktop_migration:
            commit_desktop_migration()
            return 0
        if options.clear_desktop_migration_seal:
            verify_administrative_context()
            clear_desktop_migration_seal()
            return 0
        if options.begin_service_transition:
            verify_administrative_context()
            begin_service_transition(
                Path(options.expected_service_exe),
                token_transfer_consent=options.token_transfer_consent,
                target_service_running=options.target_service_running == "1",
            )
            return 0
        if options.mark_service_rollback_complete:
            verify_administrative_context()
            return int(mark_service_rollback_complete(Path(options.expected_service_exe)))
        if options.mark_service_committed:
            verify_administrative_context()
            return int(mark_service_committed(Path(options.expected_service_exe)))
        if options.prepare_install_reconcile:
            verify_administrative_context()
            return int(classify_install_reconcile(Path(options.expected_service_exe)))
        if options.finish_install_reconcile:
            verify_administrative_context()
            return int(finish_install_reconcile(Path(options.expected_service_exe)))
        if options.preflight_machine:
            verify_administrative_context()
            preflight_machine()
            return 0
        if options.preflight_port:
            preflight_loopback_port()
            return 0
        if options.snapshot_service_metadata:
            verify_administrative_context()
            snapshot_service_metadata(Path(options.expected_service_exe))
            return 0
        if options.restore_service_metadata:
            verify_administrative_context()
            restore_service_metadata(Path(options.expected_service_exe))
            return 0
        if options.clear_service_metadata:
            verify_administrative_context()
            clear_service_metadata(Path(options.expected_service_exe))
            return 0
        if options.reconcile_service_uninstall:
            verify_administrative_context()
            reconcile_service_uninstall(Path(options.expected_service_exe))
            return 0
        if options.assert_no_pending_service_uninstall:
            verify_administrative_context()
            assert_no_pending_service_uninstall(Path(options.expected_service_exe))
            return 0
        if options.disable_service_delayed_start:
            verify_administrative_context()
            disable_service_delayed_start(Path(options.expected_service_exe))
            return 0
        if options.verify_migration_context:
            verify_migration_context()
            return 0
        if options.purge_runtime_state:
            verify_administrative_context()
            purge_runtime_state()
            return 0
        if options.purge_machine_state:
            verify_administrative_context()
            purge_machine_state()
            return 0
        url = request_browser_url()
        if options.probe:
            return 0
        if not webbrowser.open(url):
            raise RuntimeError("Der Standardbrowser konnte nicht geöffnet werden.")
    except Exception as exc:
        if _is_internal_action(options):
            return 1
        _show_message(f"Die lokale Browseroberfläche konnte nicht geöffnet werden:\n\n{exc}", error=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
