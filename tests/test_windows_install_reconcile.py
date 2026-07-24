from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from app import windows_desktop_migration as desktop
from app import windows_install_reconcile as reconcile
from app import windows_install_transaction as transaction

EXPECTED_EXECUTABLE = Path(r"C:\Program Files\E-Rechnungs-Pruefer-Dienst\service\E-Rechnungs-Pruefer-Dienst.exe")
TRANSACTION_ID = "a" * 32
READER_SID = "S-1-5-21-1000"
SEAL_SHA256 = "b" * 64
RECEIPT = desktop.MigrationReceipt(
    autostart_command=None,
    was_running=False,
    executable=r"C:\Users\Test\App\E-Rechnungs-Pruefer.exe",
    disabled_executable=r"C:\Users\Test\App\E-Rechnungs-Pruefer.exe.service-mode-disabled",
)


def _desktop_binding(
    phase: desktop.MigrationPhase,
    *,
    seal_sha256: str = SEAL_SHA256,
) -> desktop.DesktopMigrationBinding:
    return desktop.DesktopMigrationBinding(
        transaction_id=TRANSACTION_ID,
        reader_sid=READER_SID,
        seal_sha256=seal_sha256,
        token_sha256=None,
        receipt=RECEIPT,
        phase=phase,
    )


def _prepared(
    *,
    service_existed: bool = True,
    service_running: bool = False,
    target_running: bool = False,
) -> transaction.PreparedTransaction:
    return transaction.PreparedTransaction(
        transaction_id=TRANSACTION_ID,
        desktop_reader_sid=READER_SID,
        desktop_seal_sha256=SEAL_SHA256,
        expected_executable=str(EXPECTED_EXECUTABLE),
        service_existed=service_existed,
        service_running=service_running,
        service_metadata={"baseline": True} if service_existed else None,
        machine_before=transaction.MachineBefore(True, True, False),
        target_service_running=target_running,
        token_transfer_consent=False,
    )


def _state(
    phase: transaction.TransactionPhase,
    **prepared_options: bool,
) -> transaction.TransactionState:
    return transaction.TransactionState(
        prepared=_prepared(**prepared_options),
        phase=phase,
    )


BASELINE_STOPPED = transaction.RecoveryObservation(
    transaction.BundleTopology(True, False, False, False),
    transaction.ServiceState.OWNED_STOPPED,
)


@pytest.fixture(autouse=True)
def _no_partial_desktop_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "desktop_migration_state_is_partial",
        lambda: False,
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_partial_prepared_transaction",
        lambda _path: None,
    )
    monkeypatch.setattr(
        reconcile.windows_service_metadata,
        "assert_no_pending_service_uninstall",
        lambda _path: None,
    )


@pytest.mark.parametrize(
    ("desktop_phase", "service_phase", "expected"),
    [
        (desktop.MigrationPhase.ROLLBACKABLE, None, reconcile.ReconcileDirection.ROLLBACK),
        (
            desktop.MigrationPhase.ROLLBACKABLE,
            transaction.TransactionPhase.PREPARED,
            reconcile.ReconcileDirection.ROLLBACK,
        ),
        (
            desktop.MigrationPhase.SERVICE_TRANSITION,
            transaction.TransactionPhase.PREPARED,
            reconcile.ReconcileDirection.ROLLBACK,
        ),
        (
            desktop.MigrationPhase.SERVICE_ROLLBACK_COMPLETE,
            transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE,
            reconcile.ReconcileDirection.ROLLBACK,
        ),
        (
            desktop.MigrationPhase.SERVICE_TRANSITION,
            transaction.TransactionPhase.COMMIT_STARTED,
            reconcile.ReconcileDirection.COMMIT,
        ),
        (
            desktop.MigrationPhase.SERVICE_COMMITTED,
            transaction.TransactionPhase.COMMIT_STARTED,
            reconcile.ReconcileDirection.COMMIT,
        ),
    ],
)
def test_classifier_accepts_only_ordered_bound_phase_pairs(
    desktop_phase: desktop.MigrationPhase,
    service_phase: transaction.TransactionPhase | None,
    expected: reconcile.ReconcileDirection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_state = _state(service_phase) if service_phase is not None else None
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: _desktop_binding(desktop_phase))
    monkeypatch.setattr(reconcile, "_transaction_and_orphan", lambda _path: (service_state, None))
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "plan_recovery",
        lambda state, observation: transaction.RecoveryPlan(
            transaction_id=state.prepared.transaction_id,
            direction=(
                transaction.RecoveryDirection.FORWARD
                if state.phase is transaction.TransactionPhase.COMMIT_STARTED
                else transaction.RecoveryDirection.ROLLBACK
            ),
            observation=observation,
            actions=(),
        ),
    )

    assert (
        reconcile.classify_install_reconcile(
            EXPECTED_EXECUTABLE,
            _observe=lambda _path: BASELINE_STOPPED,
        )
        is expected
    )


def test_classifier_handles_none_cleanup_and_rejects_binding_or_phase_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: None)
    monkeypatch.setattr(reconcile, "_transaction_and_orphan", lambda _path: (None, None))
    assert reconcile.classify_install_reconcile(EXPECTED_EXECUTABLE) is reconcile.ReconcileDirection.NONE

    orphan = transaction.OrphanedCompletionMarker(
        transaction_id=TRANSACTION_ID,
        phase=transaction.TransactionPhase.COMMIT_STARTED,
        prepared_sha256="c" * 64,
    )
    monkeypatch.setattr(reconcile, "_transaction_and_orphan", lambda _path: (None, orphan))
    assert reconcile.classify_install_reconcile(EXPECTED_EXECUTABLE) is reconcile.ReconcileDirection.CLEANUP

    monkeypatch.setattr(
        reconcile,
        "_desktop_binding",
        lambda: _desktop_binding(desktop.MigrationPhase.SERVICE_TRANSITION, seal_sha256="d" * 64),
    )
    monkeypatch.setattr(
        reconcile,
        "_transaction_and_orphan",
        lambda _path: (_state(transaction.TransactionPhase.PREPARED), None),
    )
    with pytest.raises(RuntimeError, match="nicht zur selben"):
        reconcile.classify_install_reconcile(
            EXPECTED_EXECUTABLE,
            _observe=lambda _path: BASELINE_STOPPED,
        )

    monkeypatch.setattr(
        reconcile,
        "_desktop_binding",
        lambda: _desktop_binding(desktop.MigrationPhase.SERVICE_COMMITTED),
    )
    with pytest.raises(RuntimeError, match="widersprechen"):
        reconcile.classify_install_reconcile(
            EXPECTED_EXECUTABLE,
            _observe=lambda _path: BASELINE_STOPPED,
        )


def test_begin_captures_every_baseline_before_advancing_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _desktop_binding(desktop.MigrationPhase.ROLLBACKABLE)
    metadata = {"baseline": True}
    topology = transaction.BundleTopology(True, False, False, False)
    machine = transaction.MachineBefore(True, True, False)
    prepared = _prepared()
    events: list[str] = []
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: binding)
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "protected_desktop_migration_token_path",
        lambda: None,
    )
    monkeypatch.setattr(
        reconcile.windows_service_metadata,
        "inspect_owned_service_metadata",
        Mock(return_value=(metadata, False)),
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "inspect_bundle_topology",
        Mock(return_value=topology),
    )
    monkeypatch.setattr(reconcile, "_machine_before", Mock(return_value=machine))

    def prepare(*_args, **kwargs):
        events.append("prepared")
        assert kwargs["desktop_reader_sid"] == READER_SID
        assert kwargs["desktop_seal_sha256"] == SEAL_SHA256
        return prepared

    monkeypatch.setattr(reconcile.windows_install_transaction, "prepare_transaction", prepare)
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "advance_desktop_migration_phase",
        lambda phase: events.append(f"desktop:{phase.value}"),
    )

    reconcile.begin_service_transition(
        EXPECTED_EXECUTABLE,
        token_transfer_consent=False,
        target_service_running=False,
    )

    assert events == ["prepared", "desktop:service_transition"]


def test_begin_rejects_dual_running_or_token_consent_mismatch_before_prepare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _desktop_binding(desktop.MigrationPhase.ROLLBACKABLE)
    running_receipt = desktop.MigrationReceipt(
        RECEIPT.autostart_command,
        True,
        RECEIPT.executable,
        RECEIPT.disabled_executable,
    )
    binding = desktop.DesktopMigrationBinding(
        base.transaction_id,
        base.reader_sid,
        base.seal_sha256,
        base.token_sha256,
        running_receipt,
        base.phase,
    )
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: binding)
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "protected_desktop_migration_token_path",
        lambda: None,
    )
    monkeypatch.setattr(
        reconcile.windows_service_metadata,
        "inspect_owned_service_metadata",
        lambda _path: ({"baseline": True}, True),
    )
    prepare = Mock()
    monkeypatch.setattr(reconcile.windows_install_transaction, "prepare_transaction", prepare)

    with pytest.raises(RuntimeError, match="gleichzeitig"):
        reconcile.begin_service_transition(
            EXPECTED_EXECUTABLE,
            token_transfer_consent=False,
            target_service_running=True,
        )
    prepare.assert_not_called()

    with pytest.raises(RuntimeError, match="Tokenzustimmung"):
        reconcile.begin_service_transition(
            EXPECTED_EXECUTABLE,
            token_transfer_consent=True,
            target_service_running=True,
        )
    prepare.assert_not_called()


def test_begin_rejects_running_disabled_service_before_manifest_or_desktop_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconcile,
        "_desktop_binding",
        lambda: _desktop_binding(desktop.MigrationPhase.ROLLBACKABLE),
    )
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "protected_desktop_migration_token_path",
        lambda: None,
    )
    monkeypatch.setattr(
        reconcile.windows_service_metadata,
        "inspect_owned_service_metadata",
        lambda _path: ({"start_type": 4}, True),
    )
    prepare = Mock()
    advance = Mock()
    monkeypatch.setattr(reconcile.windows_install_transaction, "prepare_transaction", prepare)
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "advance_desktop_migration_phase",
        advance,
    )

    with pytest.raises(RuntimeError, match="deaktivierter Dienst"):
        reconcile.begin_service_transition(
            EXPECTED_EXECUTABLE,
            token_transfer_consent=False,
            target_service_running=True,
        )

    prepare.assert_not_called()
    advance.assert_not_called()


def test_begin_rejects_running_service_without_service_sid_before_prepared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconcile,
        "_desktop_binding",
        lambda: _desktop_binding(desktop.MigrationPhase.ROLLBACKABLE),
    )
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "protected_desktop_migration_token_path",
        lambda: None,
    )
    monkeypatch.setattr(
        reconcile.windows_service_metadata,
        "inspect_owned_service_metadata",
        lambda _path: ({"start_type": 2, "service_sid_type": 0}, True),
    )
    inspect_topology = Mock()
    prepare = Mock()
    advance = Mock()
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "inspect_bundle_topology",
        inspect_topology,
    )
    monkeypatch.setattr(reconcile.windows_install_transaction, "prepare_transaction", prepare)
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "advance_desktop_migration_phase",
        advance,
    )

    with pytest.raises(RuntimeError, match="ohne Dienst-SID"):
        reconcile.begin_service_transition(
            EXPECTED_EXECUTABLE,
            token_transfer_consent=False,
            target_service_running=True,
        )

    inspect_topology.assert_not_called()
    prepare.assert_not_called()
    advance.assert_not_called()


def test_begin_rejects_pending_uninstall_before_reading_or_persisting_install_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconcile.windows_service_metadata,
        "assert_no_pending_service_uninstall",
        Mock(side_effect=RuntimeError("Deinstallation offen")),
    )
    desktop_binding = Mock()
    prepare = Mock()
    advance = Mock()
    monkeypatch.setattr(reconcile, "_desktop_binding", desktop_binding)
    monkeypatch.setattr(reconcile.windows_install_transaction, "prepare_transaction", prepare)
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "advance_desktop_migration_phase",
        advance,
    )

    with pytest.raises(RuntimeError, match="Deinstallation offen"):
        reconcile.begin_service_transition(
            EXPECTED_EXECUTABLE,
            token_transfer_consent=False,
            target_service_running=True,
        )

    desktop_binding.assert_not_called()
    prepare.assert_not_called()
    advance.assert_not_called()


def test_begin_rechecks_pending_uninstall_after_baseline_capture_before_prepare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconcile.windows_service_metadata,
        "assert_no_pending_service_uninstall",
        Mock(side_effect=[None, RuntimeError("Deinstallation erschien")]),
    )
    monkeypatch.setattr(
        reconcile,
        "_desktop_binding",
        lambda: _desktop_binding(desktop.MigrationPhase.ROLLBACKABLE),
    )
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "protected_desktop_migration_token_path",
        lambda: None,
    )
    monkeypatch.setattr(
        reconcile.windows_service_metadata,
        "inspect_owned_service_metadata",
        lambda _path: ({"start_type": 2}, False),
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "inspect_bundle_topology",
        lambda _path: transaction.BundleTopology(True, False, False, False),
    )
    monkeypatch.setattr(
        reconcile,
        "_machine_before",
        lambda: transaction.MachineBefore(True, True, True),
    )
    prepare = Mock()
    advance = Mock()
    monkeypatch.setattr(reconcile.windows_install_transaction, "prepare_transaction", prepare)
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "advance_desktop_migration_phase",
        advance,
    )

    with pytest.raises(RuntimeError, match="Deinstallation erschien"):
        reconcile.begin_service_transition(
            EXPECTED_EXECUTABLE,
            token_transfer_consent=False,
            target_service_running=False,
        )

    prepare.assert_not_called()
    advance.assert_not_called()


class _Operations:
    def __init__(self, observation: transaction.RecoveryObservation) -> None:
        self.observation = observation

    def observe(self) -> transaction.RecoveryObservation:
        return self.observation

    def stop_service(self) -> None:
        raise AssertionError

    def delete_service(self) -> None:
        raise AssertionError

    def delete_bundle(self, _slot: str) -> None:
        raise AssertionError

    def move_bundle(self, _source: str, _destination: str) -> None:
        raise AssertionError

    def restore_service_metadata(self, _payload) -> None:
        raise AssertionError

    def start_service(self) -> None:
        raise AssertionError

    def purge_machine_state(self) -> None:
        raise AssertionError


def test_finish_persists_service_recovery_before_desktop_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(transaction.TransactionPhase.PREPARED)
    operations = _Operations(BASELINE_STOPPED)
    plan = transaction.RecoveryPlan(
        TRANSACTION_ID,
        transaction.RecoveryDirection.ROLLBACK,
        BASELINE_STOPPED,
        (),
    )
    events: list[str] = []
    monkeypatch.setattr(
        reconcile,
        "classify_install_reconcile",
        lambda *_args, **_kwargs: reconcile.ReconcileDirection.ROLLBACK,
    )
    monkeypatch.setattr(reconcile.windows_install_transaction, "load_transaction", lambda *_args, **_kwargs: state)
    monkeypatch.setattr(reconcile.windows_install_transaction, "plan_recovery", lambda *_args: plan)
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "execute_recovery",
        lambda *_args, **_kwargs: events.append("service-marker"),
    )
    monkeypatch.setattr(
        reconcile,
        "_synchronize_desktop_phase",
        lambda *_args, **_kwargs: events.append("desktop-phase"),
    )

    result = reconcile.finish_install_reconcile(
        EXPECTED_EXECUTABLE,
        _recovery_factory=lambda _path: operations,
    )

    assert result is reconcile.ReconcileDirection.ROLLBACK
    assert events == ["service-marker", "desktop-phase"]


def test_commit_reobserves_then_marks_service_before_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(transaction.TransactionPhase.PREPARED)
    binding = _desktop_binding(desktop.MigrationPhase.SERVICE_TRANSITION)
    observation = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, True),
        transaction.ServiceState.OWNED_STOPPED,
    )
    operations = Mock()
    operations.observe.side_effect = [observation, observation]
    events: list[str] = []
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: binding)
    monkeypatch.setattr(reconcile.windows_install_transaction, "load_transaction", lambda *_args, **_kwargs: state)
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "mark_commit_started",
        lambda *_args, **_kwargs: events.append("service-marker"),
    )
    monkeypatch.setattr(
        reconcile,
        "_synchronize_desktop_phase",
        lambda *_args, **_kwargs: events.append("desktop-phase"),
    )

    result = reconcile.mark_service_committed(
        EXPECTED_EXECUTABLE,
        _recovery_factory=lambda _path: operations,
        _commit_verifier=lambda *_args: events.append("verified"),
    )

    assert result is reconcile.ReconcileDirection.COMMIT
    assert events == ["verified", "service-marker", "desktop-phase"]
    assert operations.observe.call_count == 2


def test_cleanup_handles_orphan_without_an_external_transaction_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orphan = transaction.OrphanedCompletionMarker(
        TRANSACTION_ID,
        transaction.TransactionPhase.COMMIT_STARTED,
        "c" * 64,
    )
    monkeypatch.setattr(
        reconcile,
        "classify_install_reconcile",
        lambda *_args, **_kwargs: reconcile.ReconcileDirection.CLEANUP,
    )
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: None)
    monkeypatch.setattr(reconcile, "_transaction_and_orphan", lambda _path: (None, orphan))
    clear = Mock()
    monkeypatch.setattr(reconcile.windows_install_transaction, "clear_orphaned_completion_marker", clear)

    assert reconcile.finish_install_reconcile(EXPECTED_EXECUTABLE) is reconcile.ReconcileDirection.CLEANUP
    clear.assert_called_once_with(EXPECTED_EXECUTABLE)


def test_partial_preapply_seal_is_cleanup_only_and_removed_without_native_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "desktop_migration_state_is_partial",
        lambda: True,
    )
    monkeypatch.setattr(reconcile, "_transaction_and_orphan", lambda _path: (None, None))
    clear = Mock()
    monkeypatch.setattr(reconcile.windows_desktop_migration, "clear_desktop_migration_seal", clear)
    recovery = Mock(side_effect=AssertionError("native recovery must not run"))

    assert reconcile.classify_install_reconcile(EXPECTED_EXECUTABLE) is reconcile.ReconcileDirection.CLEANUP
    assert (
        reconcile.finish_install_reconcile(
            EXPECTED_EXECUTABLE,
            _recovery_factory=recovery,
        )
        is reconcile.ReconcileDirection.CLEANUP
    )
    clear.assert_called_once_with()
    recovery.assert_not_called()


def test_partial_prepared_publish_tail_rolls_back_desktop_then_cleans_service_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial = transaction.PartialPreparedState(prepared=_prepared())
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_partial_prepared_transaction",
        lambda _path: partial,
    )
    monkeypatch.setattr(
        reconcile,
        "_desktop_binding",
        lambda: _desktop_binding(desktop.MigrationPhase.ROLLBACKABLE),
    )
    monkeypatch.setattr(reconcile, "_transaction_and_orphan", lambda _path: (None, None))

    assert reconcile.classify_install_reconcile(EXPECTED_EXECUTABLE) is reconcile.ReconcileDirection.ROLLBACK

    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: None)
    clear = Mock()
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "clear_partial_prepared_transaction",
        clear,
    )
    recovery = Mock(side_effect=AssertionError("native recovery must not run"))

    assert (
        reconcile.finish_install_reconcile(
            EXPECTED_EXECUTABLE,
            _recovery_factory=recovery,
        )
        is reconcile.ReconcileDirection.CLEANUP
    )
    clear.assert_called_once_with(EXPECTED_EXECUTABLE)
    recovery.assert_not_called()


def test_incomplete_prepared_scratch_is_cleanup_only_but_unknown_store_never_mutates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: None)
    monkeypatch.setattr(reconcile, "_transaction_and_orphan", lambda _path: (None, None))
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_partial_prepared_transaction",
        lambda _path: transaction.PartialPreparedState(prepared=None),
    )
    assert reconcile.classify_install_reconcile(EXPECTED_EXECUTABLE) is reconcile.ReconcileDirection.CLEANUP

    clear = Mock()
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "clear_partial_prepared_transaction",
        clear,
    )
    failure = RuntimeError("unbekannter Eintrag")
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_partial_prepared_transaction",
        Mock(side_effect=failure),
    )

    with pytest.raises(RuntimeError, match="unbekannter Eintrag"):
        reconcile.finish_install_reconcile(EXPECTED_EXECUTABLE)

    clear.assert_not_called()


def test_transaction_reader_prefers_orphan_and_default_boundaries_are_lazy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orphan = transaction.OrphanedCompletionMarker(
        TRANSACTION_ID,
        transaction.TransactionPhase.COMMIT_STARTED,
        "c" * 64,
    )
    load_transaction = Mock(side_effect=AssertionError("manifest must not be read"))
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_orphaned_completion_marker",
        Mock(return_value=orphan),
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_transaction",
        load_transaction,
    )

    assert reconcile._transaction_and_orphan(EXPECTED_EXECUTABLE) == (None, orphan)
    load_transaction.assert_not_called()

    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_orphaned_completion_marker",
        Mock(return_value=None),
    )
    state = _state(transaction.TransactionPhase.PREPARED)
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_transaction",
        Mock(return_value=state),
    )
    assert reconcile._transaction_and_orphan(EXPECTED_EXECUTABLE) == (state, None)

    binding = _desktop_binding(desktop.MigrationPhase.ROLLBACKABLE)
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "load_desktop_migration_binding",
        Mock(return_value=binding),
    )
    assert reconcile._desktop_binding() == binding
    protected = Mock(return_value=Path(r"C:\ProgramData\protected-token"))
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "protected_desktop_migration_token_path",
        protected,
    )
    assert reconcile.protected_token_transfer_path() == Path(r"C:\ProgramData\protected-token")


@pytest.mark.parametrize(
    ("partial_desktop", "partial_prepared", "binding", "state", "orphan", "message"),
    [
        (
            True,
            transaction.PartialPreparedState(prepared=None),
            None,
            None,
            None,
            "partieller Desktop-Seal",
        ),
        (
            False,
            transaction.PartialPreparedState(prepared=None),
            None,
            _state(transaction.TransactionPhase.PREPARED),
            None,
            "partielles PREPARED",
        ),
        (
            False,
            transaction.PartialPreparedState(prepared=_prepared()),
            _desktop_binding(desktop.MigrationPhase.SERVICE_TRANSITION),
            None,
            None,
            "keine rollbackfähige Desktopphase",
        ),
        (
            False,
            None,
            _desktop_binding(desktop.MigrationPhase.ROLLBACKABLE),
            None,
            transaction.OrphanedCompletionMarker(
                TRANSACTION_ID,
                transaction.TransactionPhase.COMMIT_STARTED,
                "c" * 64,
            ),
            "verwaister Dienstmarker",
        ),
        (
            False,
            None,
            _desktop_binding(desktop.MigrationPhase.SERVICE_TRANSITION),
            None,
            None,
            "kein gebundenes Dienstmanifest",
        ),
        (
            False,
            None,
            None,
            _state(transaction.TransactionPhase.PREPARED),
            None,
            "keinen Desktop-Migrationszustand",
        ),
    ],
)
def test_classifier_fails_closed_for_partial_or_unbound_state_combinations(
    partial_desktop: bool,
    partial_prepared: transaction.PartialPreparedState | None,
    binding: desktop.DesktopMigrationBinding | None,
    state: transaction.TransactionState | None,
    orphan: transaction.OrphanedCompletionMarker | None,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "desktop_migration_state_is_partial",
        lambda: partial_desktop,
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_partial_prepared_transaction",
        lambda _path: partial_prepared,
    )
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: binding)
    monkeypatch.setattr(reconcile, "_transaction_and_orphan", lambda _path: (state, orphan))

    with pytest.raises(RuntimeError, match=message):
        reconcile.classify_install_reconcile(EXPECTED_EXECUTABLE)


@pytest.mark.parametrize(
    ("service_phase", "desktop_phase", "message"),
    [
        (
            transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE,
            desktop.MigrationPhase.SERVICE_COMMITTED,
            "zurückgerollte Dienstphase",
        ),
        (
            transaction.TransactionPhase.COMMIT_STARTED,
            desktop.MigrationPhase.ROLLBACKABLE,
            "committed Dienstphase",
        ),
    ],
)
def test_classifier_rejects_terminal_phase_contradictions(
    service_phase: transaction.TransactionPhase,
    desktop_phase: desktop.MigrationPhase,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(service_phase)
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: _desktop_binding(desktop_phase))
    monkeypatch.setattr(reconcile, "_transaction_and_orphan", lambda _path: (state, None))

    with pytest.raises(RuntimeError, match=message):
        reconcile.classify_install_reconcile(
            EXPECTED_EXECUTABLE,
            _observe=lambda _path: BASELINE_STOPPED,
        )


def test_classifier_validates_terminal_service_state_without_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE)
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: None)
    monkeypatch.setattr(reconcile, "_transaction_and_orphan", lambda _path: (state, None))

    assert (
        reconcile.classify_install_reconcile(
            EXPECTED_EXECUTABLE,
            _observe=lambda _path: BASELINE_STOPPED,
        )
        is reconcile.ReconcileDirection.CLEANUP
    )
    with pytest.raises(RuntimeError, match="nicht exakt wiederhergestellt"):
        reconcile.classify_install_reconcile(
            EXPECTED_EXECUTABLE,
            _observe=lambda _path: transaction.RecoveryObservation(
                transaction.BundleTopology(False, False, False, False),
                transaction.ServiceState.ABSENT,
            ),
        )


def test_machine_baseline_and_bundle_baseline_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = Mock()
    paths.configuration = Path("configuration")
    paths.token = Path("token")
    paths.log = Path("logs") / "service.log"
    monkeypatch.setattr(reconcile.windows_service_preflight, "inspect_machine_state", Mock())
    monkeypatch.setattr(reconcile.ServicePaths, "from_environment", Mock(return_value=paths))
    monkeypatch.setattr(
        reconcile,
        "validate_machine_path",
        lambda path, *, directory: (
            (str(path), directory)
            in {
                ("configuration", False),
                ("logs", True),
            }
        ),
    )

    assert reconcile._machine_before() == transaction.MachineBefore(True, False, True)
    reconcile._require_baseline_topology(
        transaction.BundleTopology(False, False, False, False),
        service_existed=False,
    )
    with pytest.raises(RuntimeError, match="keinen eindeutigen Ausgangszustand"):
        reconcile._require_baseline_topology(
            transaction.BundleTopology(True, True, False, False),
            service_existed=True,
        )


@pytest.mark.parametrize(
    ("token_consent", "target_running", "binding_phase", "token_present", "message"),
    [
        (1, False, desktop.MigrationPhase.ROLLBACKABLE, False, "strikt boolesch"),
        (False, False, desktop.MigrationPhase.SERVICE_TRANSITION, False, "rollbackfähige"),
        (False, False, desktop.MigrationPhase.ROLLBACKABLE, False, "Zielzustand"),
        (True, True, desktop.MigrationPhase.ROLLBACKABLE, False, "Tokenzustimmung"),
    ],
)
def test_begin_rejects_invalid_inputs_before_manifest(
    token_consent: object,
    target_running: object,
    binding_phase: desktop.MigrationPhase,
    token_present: bool,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: _desktop_binding(binding_phase))
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "protected_desktop_migration_token_path",
        lambda: Path("token") if token_present else None,
    )
    monkeypatch.setattr(
        reconcile.windows_service_metadata,
        "inspect_owned_service_metadata",
        lambda _path: None,
    )
    prepare = Mock()
    monkeypatch.setattr(reconcile.windows_install_transaction, "prepare_transaction", prepare)

    with pytest.raises(RuntimeError, match=message):
        reconcile.begin_service_transition(
            EXPECTED_EXECUTABLE,
            token_transfer_consent=token_consent,  # type: ignore[arg-type]
            target_service_running=target_running,  # type: ignore[arg-type]
        )
    prepare.assert_not_called()


@pytest.mark.parametrize("changed", ["prepared-metadata", "service", "topology", "machine"])
def test_begin_detects_every_baseline_toctou_before_advancing_desktop(
    changed: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _desktop_binding(desktop.MigrationPhase.ROLLBACKABLE)
    metadata = {"start_type": 2, "service_sid_type": 1}
    topology = transaction.BundleTopology(True, False, False, False)
    machine = transaction.MachineBefore(True, True, True)
    prepared = _prepared()
    if changed == "prepared-metadata":
        prepared = transaction.PreparedTransaction(
            prepared.transaction_id,
            prepared.desktop_reader_sid,
            prepared.desktop_seal_sha256,
            prepared.expected_executable,
            prepared.service_existed,
            prepared.service_running,
            {"changed": True},
            prepared.machine_before,
            prepared.target_service_running,
            prepared.token_transfer_consent,
        )
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: binding)
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "protected_desktop_migration_token_path",
        lambda: None,
    )
    service_values = (
        [(metadata, False), (metadata, False)]
        if changed != "service"
        else [(metadata, False), ({"changed": True}, False)]
    )
    monkeypatch.setattr(
        reconcile.windows_service_metadata,
        "inspect_owned_service_metadata",
        Mock(side_effect=service_values),
    )
    topology_values = [topology, topology]
    if changed == "topology":
        topology_values[1] = transaction.BundleTopology(True, True, False, False)
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "inspect_bundle_topology",
        Mock(side_effect=topology_values),
    )
    machine_values = [machine, machine]
    if changed == "machine":
        machine_values[1] = transaction.MachineBefore(False, True, True)
    monkeypatch.setattr(reconcile, "_machine_before", Mock(side_effect=machine_values))
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "prepare_transaction",
        Mock(return_value=prepared),
    )
    advance = Mock()
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "advance_desktop_migration_phase",
        advance,
    )

    with pytest.raises(RuntimeError, match="Baseline|Dienstzustand|Bundles|Maschinenzustand"):
        reconcile.begin_service_transition(
            EXPECTED_EXECUTABLE,
            token_transfer_consent=False,
            target_service_running=False,
        )
    advance.assert_not_called()


def test_synchronize_desktop_phase_advances_each_durable_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollback_state = _state(transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE)
    phases = [
        _desktop_binding(desktop.MigrationPhase.ROLLBACKABLE),
        _desktop_binding(desktop.MigrationPhase.SERVICE_TRANSITION),
    ]
    advances: list[desktop.MigrationPhase] = []
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_transaction",
        lambda _path: rollback_state,
    )
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: phases.pop(0))
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "advance_desktop_migration_phase",
        advances.append,
    )

    reconcile._synchronize_desktop_phase(
        EXPECTED_EXECUTABLE,
        direction=reconcile.ReconcileDirection.ROLLBACK,
    )
    assert advances == [
        desktop.MigrationPhase.SERVICE_TRANSITION,
        desktop.MigrationPhase.SERVICE_ROLLBACK_COMPLETE,
    ]

    commit_state = _state(transaction.TransactionPhase.COMMIT_STARTED)
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_transaction",
        lambda _path: commit_state,
    )
    monkeypatch.setattr(
        reconcile,
        "_desktop_binding",
        lambda: _desktop_binding(desktop.MigrationPhase.SERVICE_TRANSITION),
    )
    reconcile._synchronize_desktop_phase(
        EXPECTED_EXECUTABLE,
        direction=reconcile.ReconcileDirection.COMMIT,
    )
    assert advances[-1] is desktop.MigrationPhase.SERVICE_COMMITTED


@pytest.mark.parametrize(
    ("state", "binding", "direction", "message"),
    [
        (None, None, reconcile.ReconcileDirection.ROLLBACK, "keine vollständige"),
        (
            _state(transaction.TransactionPhase.PREPARED),
            _desktop_binding(desktop.MigrationPhase.ROLLBACKABLE),
            reconcile.ReconcileDirection.ROLLBACK,
            "erst nach bewiesenem",
        ),
        (
            _state(transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE),
            _desktop_binding(desktop.MigrationPhase.SERVICE_COMMITTED),
            reconcile.ReconcileDirection.ROLLBACK,
            "nicht rollbackfähig",
        ),
        (
            _state(transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE),
            _desktop_binding(desktop.MigrationPhase.SERVICE_TRANSITION),
            reconcile.ReconcileDirection.CLEANUP,
            "unbekannte",
        ),
        (
            _state(transaction.TransactionPhase.PREPARED),
            _desktop_binding(desktop.MigrationPhase.SERVICE_TRANSITION),
            reconcile.ReconcileDirection.COMMIT,
            "erst nach persistentem",
        ),
        (
            _state(transaction.TransactionPhase.COMMIT_STARTED),
            _desktop_binding(desktop.MigrationPhase.ROLLBACKABLE),
            reconcile.ReconcileDirection.COMMIT,
            "nicht commitfähig",
        ),
    ],
)
def test_synchronize_desktop_phase_rejects_missing_or_out_of_order_state(
    state: transaction.TransactionState | None,
    binding: desktop.DesktopMigrationBinding | None,
    direction: reconcile.ReconcileDirection,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_transaction",
        lambda _path: state,
    )
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: binding)
    with pytest.raises(RuntimeError, match=message):
        reconcile._synchronize_desktop_phase(
            EXPECTED_EXECUTABLE,
            direction=direction,
        )


def test_execute_recovery_handles_missing_rollback_but_requires_commit_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_transaction",
        lambda _path: None,
    )
    reconcile._execute_service_recovery(
        EXPECTED_EXECUTABLE,
        direction=reconcile.ReconcileDirection.ROLLBACK,
        recovery_factory=Mock(side_effect=AssertionError),
    )
    with pytest.raises(RuntimeError, match="Commit fehlt"):
        reconcile._execute_service_recovery(
            EXPECTED_EXECUTABLE,
            direction=reconcile.ReconcileDirection.COMMIT,
            recovery_factory=Mock(side_effect=AssertionError),
        )


def test_execute_recovery_rejects_plan_in_opposite_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(transaction.TransactionPhase.PREPARED)
    operations = _Operations(BASELINE_STOPPED)
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_transaction",
        lambda _path: state,
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "plan_recovery",
        lambda *_args: transaction.RecoveryPlan(
            TRANSACTION_ID,
            transaction.RecoveryDirection.FORWARD,
            BASELINE_STOPPED,
            (),
        ),
    )
    with pytest.raises(RuntimeError, match="widerspricht"):
        reconcile._execute_service_recovery(
            EXPECTED_EXECUTABLE,
            direction=reconcile.ReconcileDirection.ROLLBACK,
            recovery_factory=lambda _path: operations,
        )


def test_finish_none_and_terminal_cleanup_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconcile,
        "classify_install_reconcile",
        lambda *_args, **_kwargs: reconcile.ReconcileDirection.NONE,
    )
    assert reconcile.finish_install_reconcile(EXPECTED_EXECUTABLE) is reconcile.ReconcileDirection.NONE

    state = _state(transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE)
    operations = _Operations(BASELINE_STOPPED)
    monkeypatch.setattr(
        reconcile,
        "classify_install_reconcile",
        lambda *_args, **_kwargs: reconcile.ReconcileDirection.CLEANUP,
    )
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: None)
    monkeypatch.setattr(reconcile, "_transaction_and_orphan", lambda _path: (state, None))
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "plan_recovery",
        Mock(
            return_value=transaction.RecoveryPlan(
                TRANSACTION_ID,
                transaction.RecoveryDirection.COMPLETE,
                BASELINE_STOPPED,
                (),
            )
        ),
    )
    execute = Mock()
    finalize = Mock()
    monkeypatch.setattr(reconcile.windows_install_transaction, "execute_recovery", execute)
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_transaction",
        Mock(return_value=state),
    )
    monkeypatch.setattr(reconcile.windows_install_transaction, "finalize_transaction", finalize)

    assert (
        reconcile.finish_install_reconcile(
            EXPECTED_EXECUTABLE,
            _recovery_factory=lambda _path: operations,
        )
        is reconcile.ReconcileDirection.CLEANUP
    )
    execute.assert_called_once()
    finalize.assert_called_once_with(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        observation=BASELINE_STOPPED,
    )


@pytest.mark.parametrize("conflict", ["partial-desktop", "desktop", "partial-service", "lost-terminal"])
def test_finish_cleanup_rechecks_conflicts_before_finalization(
    conflict: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE)
    partial = transaction.PartialPreparedState(prepared=None)
    monkeypatch.setattr(
        reconcile,
        "classify_install_reconcile",
        lambda *_args, **_kwargs: reconcile.ReconcileDirection.CLEANUP,
    )
    monkeypatch.setattr(
        reconcile.windows_desktop_migration,
        "desktop_migration_state_is_partial",
        lambda: conflict == "partial-desktop",
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_partial_prepared_transaction",
        lambda _path: partial if conflict == "partial-service" else None,
    )
    monkeypatch.setattr(
        reconcile,
        "_desktop_binding",
        lambda: _desktop_binding(desktop.MigrationPhase.SERVICE_ROLLBACK_COMPLETE) if conflict == "desktop" else None,
    )
    monkeypatch.setattr(
        reconcile,
        "_transaction_and_orphan",
        lambda _path: (
            state if conflict in {"partial-desktop", "partial-service", "lost-terminal"} else None,
            None,
        ),
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "plan_recovery",
        lambda *_args: transaction.RecoveryPlan(
            TRANSACTION_ID,
            transaction.RecoveryDirection.COMPLETE,
            BASELINE_STOPPED,
            (),
        ),
    )
    monkeypatch.setattr(reconcile.windows_install_transaction, "execute_recovery", Mock())
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_transaction",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError):
        reconcile.finish_install_reconcile(
            EXPECTED_EXECUTABLE,
            _recovery_factory=lambda _path: _Operations(BASELINE_STOPPED),
        )


def test_mark_rollback_complete_handles_none_and_rejects_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classifier = Mock(return_value=reconcile.ReconcileDirection.NONE)
    monkeypatch.setattr(reconcile, "classify_install_reconcile", classifier)
    assert reconcile.mark_service_rollback_complete(EXPECTED_EXECUTABLE) is reconcile.ReconcileDirection.NONE
    classifier.return_value = reconcile.ReconcileDirection.COMMIT
    with pytest.raises(RuntimeError, match="nicht mehr rollbackfähig"):
        reconcile.mark_service_rollback_complete(EXPECTED_EXECUTABLE)


def test_service_check_and_committed_verifier_cover_health_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(reconcile.subprocess, "run", run)
    machine = Mock(existing_state=True)
    monkeypatch.setattr(
        reconcile.windows_service_preflight,
        "inspect_machine_state",
        Mock(return_value=machine),
    )
    reconcile._verify_committed_service_state(
        EXPECTED_EXECUTABLE,
        _prepared(target_running=True),
    )
    assert [call.args[0][1] for call in run.call_args_list] == ["--verify-state", "--health-check"]

    run.return_value = Mock(returncode=5)
    with pytest.raises(RuntimeError, match="fehlgeschlagen"):
        reconcile._run_service_check(EXPECTED_EXECUTABLE, "--verify-state")
    monkeypatch.setattr(
        reconcile.windows_service_preflight,
        "inspect_machine_state",
        Mock(return_value=Mock(existing_state=False)),
    )
    with pytest.raises(RuntimeError, match="keinen vollständigen"):
        reconcile._verify_committed_service_state(
            EXPECTED_EXECUTABLE,
            _prepared(),
        )


@pytest.mark.parametrize(
    ("desktop_binding", "state", "message"),
    [
        (None, _state(transaction.TransactionPhase.PREPARED), "keine vollständige"),
        (
            _desktop_binding(desktop.MigrationPhase.SERVICE_TRANSITION),
            _state(transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE),
            "zurückgerollte",
        ),
        (
            _desktop_binding(desktop.MigrationPhase.ROLLBACKABLE),
            _state(transaction.TransactionPhase.PREPARED),
            "nicht für den Dienst-Commit",
        ),
    ],
)
def test_mark_committed_rejects_missing_or_incompatible_phases(
    desktop_binding: desktop.DesktopMigrationBinding | None,
    state: transaction.TransactionState,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: desktop_binding)
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_transaction",
        lambda _path: state,
    )
    with pytest.raises(RuntimeError, match=message):
        reconcile.mark_service_committed(EXPECTED_EXECUTABLE)


def test_mark_committed_rejects_verification_race_and_nonforward_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _desktop_binding(desktop.MigrationPhase.SERVICE_COMMITTED)
    state = _state(transaction.TransactionPhase.COMMIT_STARTED)
    before = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, True),
        transaction.ServiceState.OWNED_STOPPED,
    )
    changed = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, False),
        transaction.ServiceState.OWNED_STOPPED,
    )
    monkeypatch.setattr(reconcile, "_desktop_binding", lambda: binding)
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_transaction",
        lambda _path: state,
    )
    operations = Mock()
    operations.observe.side_effect = [before, changed]
    with pytest.raises(RuntimeError, match="änderten sich"):
        reconcile.mark_service_committed(
            EXPECTED_EXECUTABLE,
            _recovery_factory=lambda _path: operations,
            _commit_verifier=lambda *_args: None,
        )

    operations.observe.side_effect = [before, before]
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "plan_recovery",
        lambda *_args: transaction.RecoveryPlan(
            TRANSACTION_ID,
            transaction.RecoveryDirection.COMPLETE,
            before,
            (),
        ),
    )
    with pytest.raises(RuntimeError, match="vorwärts bereinigbaren"):
        reconcile.mark_service_committed(
            EXPECTED_EXECUTABLE,
            _recovery_factory=lambda _path: operations,
            _commit_verifier=lambda *_args: None,
        )
