from __future__ import annotations

import argparse
import ctypes
import os
import sys
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from . import windows_desktop_migration as _desktop_migration
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

_SETUP_DIAGNOSTIC_FILE_NAME = "setup-action-diagnostic-v1.txt"
_SETUP_DIAGNOSTIC_HEADER = "ERP_SETUP_DIAGNOSTIC_V1"
_SETUP_DIAGNOSTIC_MAX_BYTES = 256
_INTERNAL_ACTION_STAGES = (
    ("prepare_desktop_migration_transfer", "prepare-transfer"),
    ("clear_desktop_migration_transfer", "clear-transfer"),
    ("plan_desktop_migration", "plan-migration"),
    ("seal_desktop_migration", "seal-migration"),
    ("apply_desktop_migration", "apply-migration"),
    ("verify_applied_desktop_migration", "verify-applied-migration"),
    ("verify_desktop_migration_owner", "verify-migration-owner"),
    ("rollback_desktop_migration", "rollback-migration"),
    ("commit_desktop_migration", "commit-migration"),
    ("clear_desktop_migration_seal", "clear-migration-seal"),
    ("begin_service_transition", "begin-service-transition"),
    ("mark_service_rollback_complete", "mark-service-rollback"),
    ("mark_service_committed", "mark-service-committed"),
    ("prepare_install_reconcile", "prepare-install-reconcile"),
    ("finish_install_reconcile", "finish-install-reconcile"),
    ("probe", "probe-service"),
    ("preflight_machine", "preflight-machine"),
    ("preflight_port", "preflight-port"),
    ("snapshot_service_metadata", "snapshot-service-metadata"),
    ("restore_service_metadata", "restore-service-metadata"),
    ("clear_service_metadata", "clear-service-metadata"),
    ("reconcile_service_uninstall", "reconcile-service-uninstall"),
    ("assert_no_pending_service_uninstall", "assert-no-pending-uninstall"),
    ("disable_service_delayed_start", "disable-delayed-start"),
    ("verify_migration_context", "verify-migration-context"),
    ("purge_runtime_state", "purge-runtime-state"),
    ("purge_machine_state", "purge-machine-state"),
)
_SETUP_DIAGNOSTIC_ORIGINS = {
    "_clear_migration_state": "clear-state",
    "_canonicalize_profile_hive_recovery_tail": "hive-canonicalize-tail",
    "_canonicalize_profile_hive_support_file": "hive-canonicalize-file",
    "_enable_registry_hive_privileges": "hive-privileges",
    "_locked_local_path": "locked-path",
    "_migration_state_entries": "state-inventory",
    "_profile_audit_mounts": "hive-mount-inventory",
    "_profile_installation_candidates": "profile-inventory",
    "_recover_orphaned_profile_audit_state": "hive-recovery",
    "_remove_profile_hive_snapshot": "hive-remove",
    "_validate_profile_hive_recovery_directory": "hive-recovery-directory",
    "_validate_profile_hive_recovery_tail": "hive-recovery-tail",
    "_validate_profile_hive_snapshot": "hive-validate",
    "_verify_profile_hive_support_file": "hive-support-file",
    "_wait_for_profile_hive_recovery_tail_empty": "hive-wait-empty",
    "_wait_for_profile_hive_snapshot_absent": "hive-wait-absent",
    "verify_no_legacy_desktop_conflicts": "legacy-conflict-check",
}


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
    parser.add_argument("--setup-diagnostic", help=argparse.SUPPRESS)
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
    if options.setup_diagnostic and not _is_internal_action(options):
        parser.error("--setup-diagnostic ist nur für interne Setup-Aktionen zulässig.")
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


def _internal_action_stage(options: argparse.Namespace) -> str | None:
    for attribute, stage in _INTERNAL_ACTION_STAGES:
        if getattr(options, attribute):
            return stage
    return None


def _is_internal_action(options: argparse.Namespace) -> bool:
    return _internal_action_stage(options) is not None


def _validated_setup_diagnostic_path(value: str) -> Path:
    path = Path(value)
    root = _desktop_migration._desktop_migration_transfer_root()
    parent = path.parent
    if (
        path.name != _SETUP_DIAGNOSTIC_FILE_NAME
        or any(part in {".", ".."} for part in path.parts)
        or not path.is_absolute()
        or _desktop_migration._transfer_path_key(parent.parent) != _desktop_migration._transfer_path_key(root)
    ):
        raise RuntimeError("Das interne Setup-Diagnoseziel liegt nicht am erwarteten Transferpfad.")
    _desktop_migration._validate_transfer_component(
        parent.name,
        description="Das Desktop-Migrationstransferverzeichnis",
    )
    _desktop_migration._verify_transfer_staging_path(root, directory=True, kind="root")
    _desktop_migration._verify_transfer_staging_path(parent, directory=True, kind="leaf")
    if _desktop_migration.validate_machine_path(path, directory=False):
        raise RuntimeError("Das interne Setup-Diagnoseziel ist bereits vorhanden.")
    return path


def _setup_error_code(exc: BaseException) -> str:
    if _pywin32_error_code(exc) is not None:
        return "windows-api-error"
    if isinstance(exc, PermissionError):
        return "permission-error"
    if isinstance(exc, FileExistsError):
        return "file-exists"
    if isinstance(exc, FileNotFoundError):
        return "file-not-found"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OSError):
        return "os-error"
    if isinstance(exc, ValueError):
        return "value-error"
    if isinstance(exc, RuntimeError):
        return "runtime-error"
    return "internal-error"


def _pywin32_error_code(exc: BaseException) -> int | None:
    error_type = type(exc)
    if error_type.__module__ != "pywintypes" or error_type.__name__ != "error":
        return None
    arguments = getattr(exc, "args", ())
    if not isinstance(arguments, tuple) or not arguments:
        return None
    value = arguments[0]
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFFFFFFFF:
        return value
    return None


def _setup_winerror(exc: BaseException) -> int | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        try:
            value = getattr(current, "winerror", None)
        except Exception:
            value = None
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFFFFFFFF:
            return value
        pywin32_error = _pywin32_error_code(current)
        if pywin32_error is not None:
            return pywin32_error
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return None


def _setup_error_origin(exc: BaseException) -> str:
    origin = "unknown"
    current: BaseException | None = exc
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        traceback = current.__traceback__
        while traceback is not None:
            candidate = _SETUP_DIAGNOSTIC_ORIGINS.get(traceback.tb_frame.f_code.co_name)
            if candidate is not None:
                origin = candidate
            traceback = traceback.tb_next
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return origin


def _write_setup_diagnostic(value: str, *, stage: str, exc: BaseException) -> None:
    try:
        path = _validated_setup_diagnostic_path(value)
        winerror = _setup_winerror(exc)
        payload = (
            f"{_SETUP_DIAGNOSTIC_HEADER}|stage={stage}|error={_setup_error_code(exc)}"
            f"|origin={_setup_error_origin(exc)}"
            f"|winerror={winerror if winerror is not None else 'none'}"
        ).encode("ascii")
        if len(payload) > _SETUP_DIAGNOSTIC_MAX_BYTES:
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(payload)
                handle.flush()
        finally:
            os.close(descriptor)
    except Exception:
        # Diagnostics must never replace the original setup exit status, display
        # a second UI, or write outside the validated protected transfer leaf.
        return


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
        stage = _internal_action_stage(options)
        if stage is not None:
            if options.setup_diagnostic:
                _write_setup_diagnostic(options.setup_diagnostic, stage=stage, exc=exc)
            return 1
        _show_message(f"Die lokale Browseroberfläche konnte nicht geöffnet werden:\n\n{exc}", error=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
