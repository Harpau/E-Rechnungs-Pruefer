from __future__ import annotations

import subprocess
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path

from . import (
    windows_desktop_migration,
    windows_install_transaction,
    windows_service_metadata,
    windows_service_preflight,
)
from .windows_service_config import ServicePaths, validate_machine_path

RecoveryFactory = Callable[[Path], windows_install_transaction.RecoveryOperations]
ObservationReader = Callable[[Path], windows_install_transaction.RecoveryObservation]
CommitVerifier = Callable[[Path, windows_install_transaction.PreparedTransaction], None]


class ReconcileDirection(IntEnum):
    NONE = 0
    ROLLBACK = 10
    COMMIT = 11
    CLEANUP = 12


def _default_recovery_factory(path: Path) -> windows_install_transaction.RecoveryOperations:
    from .windows_install_recovery import WindowsInstallRecovery

    return WindowsInstallRecovery(path)


def _default_observation_reader(path: Path) -> windows_install_transaction.RecoveryObservation:
    from .windows_install_recovery import observe_installation

    return observe_installation(path)


def _desktop_binding() -> windows_desktop_migration.DesktopMigrationBinding | None:
    return windows_desktop_migration.load_desktop_migration_binding(require_current_user=False)


def _transaction_and_orphan(
    expected_executable: Path,
) -> tuple[
    windows_install_transaction.TransactionState | None,
    windows_install_transaction.OrphanedCompletionMarker | None,
]:
    orphan = windows_install_transaction.load_orphaned_completion_marker(expected_executable)
    if orphan is not None:
        return None, orphan
    return windows_install_transaction.load_transaction(expected_executable), None


def _require_matching_binding(
    desktop: windows_desktop_migration.DesktopMigrationBinding,
    state: windows_install_transaction.TransactionState,
) -> None:
    _require_matching_prepared_binding(desktop, state.prepared)


def _require_matching_prepared_binding(
    desktop: windows_desktop_migration.DesktopMigrationBinding,
    prepared: windows_install_transaction.PreparedTransaction,
) -> None:
    if (
        prepared.transaction_id != desktop.transaction_id
        or prepared.desktop_reader_sid != desktop.reader_sid
        or prepared.desktop_seal_sha256 != desktop.seal_sha256
    ):
        raise RuntimeError("Desktop- und Dienstzustand gehören nicht zur selben geschützten Installations-Transaktion.")


def _expected_terminal_observation(
    prepared: windows_install_transaction.PreparedTransaction,
    *,
    committed: bool,
) -> windows_install_transaction.RecoveryObservation:
    if committed:
        return windows_install_transaction.RecoveryObservation(
            bundles=windows_install_transaction.BundleTopology(True, False, False, False),
            service=(
                windows_install_transaction.ServiceState.OWNED_RUNNING
                if prepared.target_service_running
                else windows_install_transaction.ServiceState.OWNED_STOPPED
            ),
        )
    if prepared.service_existed:
        return windows_install_transaction.RecoveryObservation(
            bundles=windows_install_transaction.BundleTopology(True, False, False, False),
            service=(
                windows_install_transaction.ServiceState.OWNED_RUNNING
                if prepared.service_running
                else windows_install_transaction.ServiceState.OWNED_STOPPED
            ),
        )
    return windows_install_transaction.RecoveryObservation(
        bundles=windows_install_transaction.BundleTopology(False, False, False, False),
        service=windows_install_transaction.ServiceState.ABSENT,
    )


def _validate_observation_for_phase(
    state: windows_install_transaction.TransactionState,
    observation: windows_install_transaction.RecoveryObservation,
) -> None:
    if state.phase is windows_install_transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE:
        expected = _expected_terminal_observation(state.prepared, committed=False)
        if observation != expected:
            raise RuntimeError("Der als zurückgerollt markierte Dienstzustand ist nicht exakt wiederhergestellt.")
        return
    windows_install_transaction.plan_recovery(state, observation)


def classify_install_reconcile(
    expected_executable: Path,
    *,
    _observe: ObservationReader = _default_observation_reader,
) -> ReconcileDirection:
    """Classify a pending durable transaction without changing external state."""

    partial_desktop = windows_desktop_migration.desktop_migration_state_is_partial()
    partial_prepared = windows_install_transaction.load_partial_prepared_transaction(expected_executable)
    desktop = None if partial_desktop else _desktop_binding()
    state, orphan = _transaction_and_orphan(expected_executable)
    if partial_desktop:
        if partial_prepared is not None or state is not None or orphan is not None:
            raise RuntimeError("Ein partieller Desktop-Seal steht einem Dienst-Transaktionszustand gegenüber.")
        return ReconcileDirection.CLEANUP
    if partial_prepared is not None:
        if state is not None or orphan is not None:
            raise RuntimeError("Ein partielles PREPARED-Manifest steht einem Dienst-Transaktionszustand gegenüber.")
        if desktop is None:
            return ReconcileDirection.CLEANUP
        if partial_prepared.prepared is not None:
            _require_matching_prepared_binding(desktop, partial_prepared.prepared)
        if desktop.phase is not windows_desktop_migration.MigrationPhase.ROLLBACKABLE:
            raise RuntimeError("Ein partielles PREPARED-Manifest besitzt keine rollbackfähige Desktopphase.")
        return ReconcileDirection.ROLLBACK
    if orphan is not None:
        if desktop is not None:
            raise RuntimeError("Ein verwaister Dienstmarker steht einem Desktop-Migrationszustand gegenüber.")
        return ReconcileDirection.CLEANUP
    if state is None:
        if desktop is None:
            return ReconcileDirection.NONE
        if desktop.phase is windows_desktop_migration.MigrationPhase.ROLLBACKABLE:
            return ReconcileDirection.ROLLBACK
        raise RuntimeError("Eine fortgeschrittene Desktopmigration besitzt kein gebundenes Dienstmanifest.")
    if desktop is None:
        if state.phase is windows_install_transaction.TransactionPhase.PREPARED:
            raise RuntimeError("Ein rollbackfähiges Dienstmanifest besitzt keinen Desktop-Migrationszustand.")
        observation = _observe(expected_executable)
        _validate_observation_for_phase(state, observation)
        return ReconcileDirection.CLEANUP

    _require_matching_binding(desktop, state)
    observation = _observe(expected_executable)
    _validate_observation_for_phase(state, observation)
    if state.phase is windows_install_transaction.TransactionPhase.PREPARED:
        if desktop.phase not in {
            windows_desktop_migration.MigrationPhase.ROLLBACKABLE,
            windows_desktop_migration.MigrationPhase.SERVICE_TRANSITION,
        }:
            raise RuntimeError("Desktop- und Dienstphase widersprechen sich vor dem Commit.")
        return ReconcileDirection.ROLLBACK
    if state.phase is windows_install_transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE:
        if desktop.phase not in {
            windows_desktop_migration.MigrationPhase.ROLLBACKABLE,
            windows_desktop_migration.MigrationPhase.SERVICE_TRANSITION,
            windows_desktop_migration.MigrationPhase.SERVICE_ROLLBACK_COMPLETE,
        }:
            raise RuntimeError("Eine zurückgerollte Dienstphase widerspricht der Desktopphase.")
        return ReconcileDirection.ROLLBACK
    if desktop.phase not in {
        windows_desktop_migration.MigrationPhase.SERVICE_TRANSITION,
        windows_desktop_migration.MigrationPhase.SERVICE_COMMITTED,
    }:
        raise RuntimeError("Eine committed Dienstphase widerspricht der Desktopphase.")
    return ReconcileDirection.COMMIT


def _machine_before() -> windows_install_transaction.MachineBefore:
    windows_service_preflight.inspect_machine_state()
    paths = ServicePaths.from_environment()
    return windows_install_transaction.MachineBefore(
        configuration=validate_machine_path(paths.configuration, directory=False),
        token=validate_machine_path(paths.token, directory=False),
        logs=validate_machine_path(paths.log.parent, directory=True),
    )


def _require_baseline_topology(
    topology: windows_install_transaction.BundleTopology,
    *,
    service_existed: bool,
) -> None:
    expected = (
        windows_install_transaction.BundleTopology(True, False, False, False)
        if service_existed
        else windows_install_transaction.BundleTopology(False, False, False, False)
    )
    if topology != expected:
        raise RuntimeError("Die Dienst-Bundles bilden vor Transaktionsbeginn keinen eindeutigen Ausgangszustand.")


def begin_service_transition(
    expected_executable: Path,
    *,
    token_transfer_consent: bool,
    target_service_running: bool,
) -> None:
    """Seal a fresh service baseline before the first service-side mutation."""

    if type(token_transfer_consent) is not bool or type(target_service_running) is not bool:
        raise RuntimeError("Die Zielparameter der Diensttransaktion müssen strikt boolesch sein.")
    windows_service_metadata.assert_no_pending_service_uninstall(expected_executable)
    desktop = _desktop_binding()
    if desktop is None or desktop.phase is not windows_desktop_migration.MigrationPhase.ROLLBACKABLE:
        raise RuntimeError("Der Dienstübergang benötigt eine rollbackfähige geschützte Desktopmigration.")

    token_path = windows_desktop_migration.protected_desktop_migration_token_path()
    if token_transfer_consent != (token_path is not None):
        raise RuntimeError("Tokenzustimmung und geschützter Desktop-Tokenbeleg stimmen nicht überein.")

    owned_service = windows_service_metadata.inspect_owned_service_metadata(expected_executable)
    service_existed = owned_service is not None
    service_running = bool(owned_service is not None and owned_service[1])
    if service_running and owned_service is not None:
        service_metadata = owned_service[0]
        if service_metadata.get("start_type") == 4:
            raise RuntimeError(
                "Ein laufender, zugleich deaktivierter Dienst besitzt keine rollbackfähige SCM-Baseline."
            )
        if service_metadata.get("service_sid_type") == 0:
            raise RuntimeError("Ein laufender Dienst ohne Dienst-SID besitzt keine rollbackfähige SCM-Baseline.")
    if desktop.receipt.was_running and service_running:
        raise RuntimeError("Desktop-App und Windows-Dienst dürfen vor der Migration nicht gleichzeitig laufen.")
    expected_target_running = not service_existed or service_running
    if target_service_running != expected_target_running:
        raise RuntimeError("Der angeforderte Dienst-Zielzustand bewahrt den stabilen Ausgangszustand nicht.")

    topology = windows_install_transaction.inspect_bundle_topology(expected_executable)
    _require_baseline_topology(topology, service_existed=service_existed)
    machine_before = _machine_before()
    windows_service_metadata.assert_no_pending_service_uninstall(expected_executable)
    prepared = windows_install_transaction.prepare_transaction(
        expected_executable,
        transaction_id=desktop.transaction_id,
        desktop_reader_sid=desktop.reader_sid,
        desktop_seal_sha256=desktop.seal_sha256,
        service_existed=service_existed,
        service_running=service_running,
        machine_before=machine_before,
        target_service_running=target_service_running,
        token_transfer_consent=token_transfer_consent,
    )
    if owned_service is not None and prepared.service_metadata != owned_service[0]:
        raise RuntimeError("Die SCM-Baseline änderte sich während der geschützten Übernahme.")
    if windows_service_metadata.inspect_owned_service_metadata(expected_executable) != owned_service:
        raise RuntimeError("Der Dienstzustand änderte sich während der geschützten Übernahme.")
    if windows_install_transaction.inspect_bundle_topology(expected_executable) != topology:
        raise RuntimeError("Die Dienst-Bundles änderten sich während der geschützten Übernahme.")
    if _machine_before() != machine_before:
        raise RuntimeError("Der Maschinenzustand änderte sich während der geschützten Übernahme.")
    windows_desktop_migration.advance_desktop_migration_phase(
        windows_desktop_migration.MigrationPhase.SERVICE_TRANSITION
    )


def _synchronize_desktop_phase(
    expected_executable: Path,
    *,
    direction: ReconcileDirection,
) -> None:
    state = windows_install_transaction.load_transaction(expected_executable)
    desktop = _desktop_binding()
    if state is None or desktop is None:
        raise RuntimeError("Der nachzuweisende Phasenübergang besitzt keine vollständige Transaktionsbindung.")
    _require_matching_binding(desktop, state)
    if direction is ReconcileDirection.ROLLBACK:
        if state.phase is not windows_install_transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE:
            raise RuntimeError("Die Desktop-Rollbackphase darf erst nach bewiesenem Dienst-Rollback fortschreiten.")
        if desktop.phase is windows_desktop_migration.MigrationPhase.ROLLBACKABLE:
            windows_desktop_migration.advance_desktop_migration_phase(
                windows_desktop_migration.MigrationPhase.SERVICE_TRANSITION
            )
            desktop = _desktop_binding()
            assert desktop is not None
        if desktop.phase is windows_desktop_migration.MigrationPhase.SERVICE_TRANSITION:
            windows_desktop_migration.advance_desktop_migration_phase(
                windows_desktop_migration.MigrationPhase.SERVICE_ROLLBACK_COMPLETE
            )
        elif desktop.phase is not windows_desktop_migration.MigrationPhase.SERVICE_ROLLBACK_COMPLETE:
            raise RuntimeError("Die Desktopphase ist nicht rollbackfähig.")
        return
    if direction is not ReconcileDirection.COMMIT:
        raise RuntimeError("Eine unbekannte Desktop-Synchronisationsrichtung wurde angefordert.")
    if state.phase is not windows_install_transaction.TransactionPhase.COMMIT_STARTED:
        raise RuntimeError("Die Desktop-Commitphase darf erst nach persistentem Dienst-Commit fortschreiten.")
    if desktop.phase is windows_desktop_migration.MigrationPhase.SERVICE_TRANSITION:
        windows_desktop_migration.advance_desktop_migration_phase(
            windows_desktop_migration.MigrationPhase.SERVICE_COMMITTED
        )
    elif desktop.phase is not windows_desktop_migration.MigrationPhase.SERVICE_COMMITTED:
        raise RuntimeError("Die Desktopphase ist nicht commitfähig.")


def _execute_service_recovery(
    expected_executable: Path,
    *,
    direction: ReconcileDirection,
    recovery_factory: RecoveryFactory,
) -> None:
    state = windows_install_transaction.load_transaction(expected_executable)
    if state is None:
        if direction is ReconcileDirection.ROLLBACK:
            return
        raise RuntimeError("Für den Dienst-Commit fehlt das persistente Transaktionsmanifest.")
    operations = recovery_factory(expected_executable)
    plan = windows_install_transaction.plan_recovery(state, operations.observe())
    expected_direction = (
        windows_install_transaction.RecoveryDirection.ROLLBACK
        if direction is ReconcileDirection.ROLLBACK
        else windows_install_transaction.RecoveryDirection.FORWARD
    )
    if plan.direction not in {expected_direction, windows_install_transaction.RecoveryDirection.COMPLETE}:
        raise RuntimeError("Der Recovery-Plan widerspricht der persistenten Transaktionsrichtung.")
    windows_install_transaction.execute_recovery(
        expected_executable,
        state=state,
        plan=plan,
        operations=operations,
    )
    _synchronize_desktop_phase(expected_executable, direction=direction)


def finish_install_reconcile(
    expected_executable: Path,
    *,
    _recovery_factory: RecoveryFactory = _default_recovery_factory,
    _observe: ObservationReader = _default_observation_reader,
) -> ReconcileDirection:
    """Execute one idempotent elevated recovery/finalization step."""

    direction = classify_install_reconcile(expected_executable, _observe=_observe)
    if direction is ReconcileDirection.NONE:
        return direction
    if direction in {ReconcileDirection.ROLLBACK, ReconcileDirection.COMMIT}:
        _execute_service_recovery(
            expected_executable,
            direction=direction,
            recovery_factory=_recovery_factory,
        )
        return direction

    partial_desktop = windows_desktop_migration.desktop_migration_state_is_partial()
    partial_prepared = windows_install_transaction.load_partial_prepared_transaction(expected_executable)
    if partial_desktop:
        state, orphan = _transaction_and_orphan(expected_executable)
        if partial_prepared is not None or state is not None or orphan is not None:
            raise RuntimeError("Ein partieller Desktop-Seal darf keine Diensttransaktion finalisieren.")
        windows_desktop_migration.clear_desktop_migration_seal()
        return ReconcileDirection.CLEANUP
    desktop = _desktop_binding()
    if desktop is not None:
        raise RuntimeError("Die Diensttransaktion darf nicht vor Abschluss der Desktopmigration finalisiert werden.")
    state, orphan = _transaction_and_orphan(expected_executable)
    if partial_prepared is not None:
        if state is not None or orphan is not None:
            raise RuntimeError("Ein partielles PREPARED-Manifest darf keinen weiteren Dienstzustand begleiten.")
        windows_install_transaction.clear_partial_prepared_transaction(expected_executable)
        return ReconcileDirection.CLEANUP
    if orphan is not None:
        windows_install_transaction.clear_orphaned_completion_marker(expected_executable)
        return ReconcileDirection.CLEANUP
    if state is None:
        return ReconcileDirection.NONE
    operations = _recovery_factory(expected_executable)
    plan = windows_install_transaction.plan_recovery(state, operations.observe())
    windows_install_transaction.execute_recovery(
        expected_executable,
        state=state,
        plan=plan,
        operations=operations,
    )
    terminal = windows_install_transaction.load_transaction(
        expected_executable,
        transaction_id=state.prepared.transaction_id,
    )
    if terminal is None:
        raise RuntimeError("Der terminale Dienstzustand ging während der Finalisierung verloren.")
    observation = operations.observe()
    windows_install_transaction.finalize_transaction(
        expected_executable,
        transaction_id=terminal.prepared.transaction_id,
        observation=observation,
    )
    return ReconcileDirection.CLEANUP


def mark_service_rollback_complete(
    expected_executable: Path,
    *,
    _recovery_factory: RecoveryFactory = _default_recovery_factory,
    _observe: ObservationReader = _default_observation_reader,
) -> ReconcileDirection:
    direction = classify_install_reconcile(expected_executable, _observe=_observe)
    if direction is ReconcileDirection.NONE:
        return direction
    if direction is not ReconcileDirection.ROLLBACK:
        raise RuntimeError("Die persistente Installation ist nicht mehr rollbackfähig.")
    return finish_install_reconcile(
        expected_executable,
        _recovery_factory=_recovery_factory,
        _observe=_observe,
    )


def _run_service_check(expected_executable: Path, argument: str) -> None:
    completed = subprocess.run(
        [str(expected_executable), argument],
        check=False,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Der Dienstnachweis {argument} ist fehlgeschlagen.")


def _verify_committed_service_state(
    expected_executable: Path,
    prepared: windows_install_transaction.PreparedTransaction,
) -> None:
    machine = windows_service_preflight.inspect_machine_state()
    if not machine.existing_state:
        raise RuntimeError("Der committed Dienst besitzt keinen vollständigen geschützten Maschinenzustand.")
    _run_service_check(expected_executable, "--verify-state")
    if prepared.target_service_running:
        _run_service_check(expected_executable, "--health-check")


def mark_service_committed(
    expected_executable: Path,
    *,
    _recovery_factory: RecoveryFactory = _default_recovery_factory,
    _commit_verifier: CommitVerifier = _verify_committed_service_state,
) -> ReconcileDirection:
    desktop = _desktop_binding()
    state = windows_install_transaction.load_transaction(expected_executable)
    if desktop is None or state is None:
        raise RuntimeError("Der Dienst-Commit besitzt keine vollständige Desktop-/Diensttransaktion.")
    _require_matching_binding(desktop, state)
    if state.phase is windows_install_transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE:
        raise RuntimeError("Eine zurückgerollte Diensttransaktion darf nicht committed werden.")
    if desktop.phase not in {
        windows_desktop_migration.MigrationPhase.SERVICE_TRANSITION,
        windows_desktop_migration.MigrationPhase.SERVICE_COMMITTED,
    }:
        raise RuntimeError("Die Desktopphase ist nicht für den Dienst-Commit vorbereitet.")
    operations = _recovery_factory(expected_executable)
    before_verification = operations.observe()
    _commit_verifier(expected_executable, state.prepared)
    observation = operations.observe()
    if observation != before_verification:
        raise RuntimeError("Dienst oder Bundles änderten sich während des Commit-Nachweises.")
    if state.phase is windows_install_transaction.TransactionPhase.COMMIT_STARTED:
        plan = windows_install_transaction.plan_recovery(state, observation)
        if plan.direction is not windows_install_transaction.RecoveryDirection.FORWARD:
            raise RuntimeError("Der persistente Commitmarker besitzt keinen vorwärts bereinigbaren Dienstzustand.")
    windows_install_transaction.mark_commit_started(
        expected_executable,
        transaction_id=state.prepared.transaction_id,
        observation=observation,
    )
    _synchronize_desktop_phase(expected_executable, direction=ReconcileDirection.COMMIT)
    return ReconcileDirection.COMMIT


def protected_token_transfer_path() -> Path | None:
    """Expose only the hash-verified, admin-protected token source for setup."""

    return windows_desktop_migration.protected_desktop_migration_token_path()
