from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import windows_install_reconcile as reconcile
from app import windows_install_transaction as transaction

EXPECTED_EXECUTABLE = Path(r"C:\Program Files\E-Rechnungs-Pruefer-Dienst\service\E-Rechnungs-Pruefer-Dienst.exe")
TRANSACTION_ID = "a" * 32


def _prepared(
    *,
    service_existed: bool = True,
    service_running: bool = False,
    target_running: bool = False,
) -> transaction.PreparedTransaction:
    return transaction.PreparedTransaction(
        transaction_id=TRANSACTION_ID,
        expected_executable=str(EXPECTED_EXECUTABLE),
        service_existed=service_existed,
        service_running=service_running,
        service_metadata={"baseline": True} if service_existed else None,
        machine_before=transaction.MachineBefore(True, True, False),
        target_service_running=target_running,
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
FIRST_INSTALL_BASELINE = transaction.RecoveryObservation(
    transaction.BundleTopology(False, False, False, False),
    transaction.ServiceState.ABSENT,
)
COMMITTED_STOPPED = BASELINE_STOPPED


@pytest.fixture(autouse=True)
def _stable_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_partial_prepared_transaction",
        lambda _path: None,
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_orphaned_completion_marker",
        lambda _path: None,
    )
    monkeypatch.setattr(
        reconcile.windows_service_metadata,
        "assert_no_pending_service_uninstall",
        lambda _path: None,
    )


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (transaction.TransactionPhase.PREPARED, reconcile.ReconcileDirection.ROLLBACK),
        (
            transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE,
            reconcile.ReconcileDirection.CLEANUP,
        ),
    ],
)
def test_classifier_maps_service_only_phases(
    phase: transaction.TransactionPhase,
    expected: reconcile.ReconcileDirection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(phase)
    monkeypatch.setattr(reconcile.windows_install_transaction, "load_transaction", lambda _path: state)
    plan = Mock()
    monkeypatch.setattr(reconcile.windows_install_transaction, "plan_recovery", plan)

    assert (
        reconcile.classify_install_reconcile(
            EXPECTED_EXECUTABLE,
            _observe=lambda _path: BASELINE_STOPPED,
        )
        is expected
    )
    if phase is transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE:
        plan.assert_not_called()
    else:
        plan.assert_called_once_with(state, BASELINE_STOPPED)


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (COMMITTED_STOPPED, reconcile.ReconcileDirection.CLEANUP),
        (
            transaction.RecoveryObservation(
                transaction.BundleTopology(True, False, False, True),
                transaction.ServiceState.OWNED_STOPPED,
            ),
            reconcile.ReconcileDirection.COMMIT,
        ),
    ],
)
def test_classifier_distinguishes_pending_and_completed_commit(
    observation: transaction.RecoveryObservation,
    expected: reconcile.ReconcileDirection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(transaction.TransactionPhase.COMMIT_STARTED)
    monkeypatch.setattr(reconcile.windows_install_transaction, "load_transaction", lambda _path: state)

    assert (
        reconcile.classify_install_reconcile(
            EXPECTED_EXECUTABLE,
            _observe=lambda _path: observation,
        )
        is expected
    )


def test_classifier_handles_none_partial_and_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconcile.windows_install_transaction, "load_transaction", lambda _path: None)
    assert reconcile.classify_install_reconcile(EXPECTED_EXECUTABLE) is reconcile.ReconcileDirection.NONE

    partial = transaction.PartialPreparedState(prepared=None)
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_partial_prepared_transaction",
        lambda _path: partial,
    )
    assert reconcile.classify_install_reconcile(EXPECTED_EXECUTABLE) is reconcile.ReconcileDirection.CLEANUP

    orphan = transaction.OrphanedCompletionMarker(
        transaction_id=TRANSACTION_ID,
        phase=transaction.TransactionPhase.COMMIT_STARTED,
        prepared_sha256="b" * 64,
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_partial_prepared_transaction",
        lambda _path: None,
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_orphaned_completion_marker",
        lambda _path: orphan,
    )
    assert reconcile.classify_install_reconcile(EXPECTED_EXECUTABLE) is reconcile.ReconcileDirection.CLEANUP


def test_classifier_rejects_partial_manifest_beside_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_partial_prepared_transaction",
        lambda _path: transaction.PartialPreparedState(prepared=_prepared()),
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_transaction",
        lambda _path: _state(transaction.TransactionPhase.PREPARED),
    )
    with pytest.raises(RuntimeError, match="partielles PREPARED"):
        reconcile.classify_install_reconcile(EXPECTED_EXECUTABLE)


def test_classifier_validates_completed_rollback_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE)
    monkeypatch.setattr(reconcile.windows_install_transaction, "load_transaction", lambda _path: state)
    with pytest.raises(RuntimeError, match="nicht exakt wiederhergestellt"):
        reconcile.classify_install_reconcile(
            EXPECTED_EXECUTABLE,
            _observe=lambda _path: FIRST_INSTALL_BASELINE,
        )


def _patch_begin_baseline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    owned_service: tuple[dict[str, object], bool] | None,
    topology: transaction.BundleTopology,
    machine: transaction.MachineBefore,
) -> tuple[Mock, Mock]:
    service_reader = Mock(return_value=owned_service)
    topology_reader = Mock(return_value=topology)
    monkeypatch.setattr(
        reconcile.windows_service_metadata,
        "inspect_owned_service_metadata",
        service_reader,
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "inspect_bundle_topology",
        topology_reader,
    )
    monkeypatch.setattr(reconcile, "_machine_before", Mock(return_value=machine))
    return service_reader, topology_reader


@pytest.mark.parametrize(
    ("owned_service", "topology", "target_running"),
    [
        (None, transaction.BundleTopology(False, False, False, False), True),
        (
            ({"start_type": 2, "service_sid_type": 1}, True),
            transaction.BundleTopology(True, False, False, False),
            True,
        ),
        (
            ({"start_type": 3, "service_sid_type": 1}, False),
            transaction.BundleTopology(True, False, False, False),
            False,
        ),
    ],
)
def test_begin_captures_service_only_baseline(
    owned_service: tuple[dict[str, object], bool] | None,
    topology: transaction.BundleTopology,
    target_running: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = transaction.MachineBefore(True, True, True)
    service_reader, topology_reader = _patch_begin_baseline(
        monkeypatch,
        owned_service=owned_service,
        topology=topology,
        machine=machine,
    )
    prepared = _prepared(
        service_existed=owned_service is not None,
        service_running=bool(owned_service and owned_service[1]),
        target_running=target_running,
    )
    if owned_service is not None:
        prepared = transaction.PreparedTransaction(
            transaction_id=prepared.transaction_id,
            expected_executable=prepared.expected_executable,
            service_existed=True,
            service_running=prepared.service_running,
            service_metadata=owned_service[0],
            machine_before=prepared.machine_before,
            target_service_running=prepared.target_service_running,
        )
    prepare = Mock(return_value=prepared)
    monkeypatch.setattr(reconcile.windows_install_transaction, "prepare_transaction", prepare)
    monkeypatch.setattr(reconcile.secrets, "token_hex", lambda size: TRANSACTION_ID if size == 16 else "")

    reconcile.begin_service_transition(
        EXPECTED_EXECUTABLE,
        target_service_running=target_running,
    )

    assert service_reader.call_count == 2
    assert topology_reader.call_count == 2
    prepare.assert_called_once_with(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        service_existed=owned_service is not None,
        service_running=bool(owned_service and owned_service[1]),
        machine_before=machine,
        target_service_running=target_running,
    )


@pytest.mark.parametrize(
    ("owned_service", "target", "message"),
    [
        (None, False, "Zielzustand"),
        (({"start_type": 4, "service_sid_type": 1}, True), True, "deaktivierter Dienst"),
        (({"start_type": 2, "service_sid_type": 0}, True), True, "ohne Dienst-SID"),
    ],
)
def test_begin_rejects_unsafe_service_baselines(
    owned_service: tuple[dict[str, object], bool] | None,
    target: bool,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconcile.windows_service_metadata,
        "inspect_owned_service_metadata",
        lambda _path: owned_service,
    )
    with pytest.raises(RuntimeError, match=message):
        reconcile.begin_service_transition(EXPECTED_EXECUTABLE, target_service_running=target)


def test_begin_rejects_non_boolean_and_ambiguous_bundle_before_prepare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare = Mock()
    monkeypatch.setattr(reconcile.windows_install_transaction, "prepare_transaction", prepare)
    with pytest.raises(RuntimeError, match="strikt boolesch"):
        reconcile.begin_service_transition(
            EXPECTED_EXECUTABLE,
            target_service_running=1,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        reconcile.windows_service_metadata,
        "inspect_owned_service_metadata",
        lambda _path: None,
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "inspect_bundle_topology",
        lambda _path: transaction.BundleTopology(True, False, False, False),
    )
    with pytest.raises(RuntimeError, match="keinen eindeutigen Ausgangszustand"):
        reconcile.begin_service_transition(EXPECTED_EXECUTABLE, target_service_running=True)
    prepare.assert_not_called()


@pytest.mark.parametrize("changed", ["service", "topology", "machine", "metadata"])
def test_begin_fails_closed_on_baseline_toctou(
    changed: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = ({"start_type": 3, "service_sid_type": 1}, False)
    topology = transaction.BundleTopology(True, False, False, False)
    machine = transaction.MachineBefore(True, True, True)
    service_values = [owned, None] if changed == "service" else [owned, owned]
    topology_values = (
        [topology, transaction.BundleTopology(True, True, False, False)]
        if changed == "topology"
        else [topology, topology]
    )
    machine_values = (
        [machine, transaction.MachineBefore(True, False, True)] if changed == "machine" else [machine, machine]
    )
    monkeypatch.setattr(
        reconcile.windows_service_metadata,
        "inspect_owned_service_metadata",
        Mock(side_effect=service_values),
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "inspect_bundle_topology",
        Mock(side_effect=topology_values),
    )
    monkeypatch.setattr(reconcile, "_machine_before", Mock(side_effect=machine_values))
    prepared = _prepared()
    if changed == "metadata":
        prepared = transaction.PreparedTransaction(
            transaction_id=TRANSACTION_ID,
            expected_executable=str(EXPECTED_EXECUTABLE),
            service_existed=True,
            service_running=False,
            service_metadata={"different": True},
            machine_before=machine,
            target_service_running=False,
        )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "prepare_transaction",
        lambda *_args, **_kwargs: prepared,
    )

    with pytest.raises(RuntimeError, match="änderte sich"):
        reconcile.begin_service_transition(EXPECTED_EXECUTABLE, target_service_running=False)


def test_begin_checks_pending_uninstall_before_and_after_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = Mock(side_effect=[None, RuntimeError("pending")])
    monkeypatch.setattr(reconcile.windows_service_metadata, "assert_no_pending_service_uninstall", pending)
    _patch_begin_baseline(
        monkeypatch,
        owned_service=None,
        topology=transaction.BundleTopology(False, False, False, False),
        machine=transaction.MachineBefore(False, False, False),
    )
    prepare = Mock()
    monkeypatch.setattr(reconcile.windows_install_transaction, "prepare_transaction", prepare)
    with pytest.raises(RuntimeError, match="pending"):
        reconcile.begin_service_transition(EXPECTED_EXECUTABLE, target_service_running=True)
    assert pending.call_count == 2
    prepare.assert_not_called()


def test_machine_before_validates_all_machine_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = SimpleNamespace(
        configuration=Path("service.json"),
        token=Path("token.txt"),
        log=Path("logs/service.log"),
    )
    monkeypatch.setattr(reconcile.windows_service_preflight, "inspect_machine_state", Mock())
    monkeypatch.setattr(reconcile.ServicePaths, "from_environment", lambda: paths)
    validate = Mock(side_effect=[True, False, True])
    monkeypatch.setattr(reconcile, "validate_machine_path", validate)
    assert reconcile._machine_before() == transaction.MachineBefore(True, False, True)
    assert validate.call_args_list[2].args == (Path("logs"),)


class _Operations:
    def __init__(self, observation: transaction.RecoveryObservation) -> None:
        self.observation = observation

    def observe(self) -> transaction.RecoveryObservation:
        return self.observation


def test_execute_service_recovery_requires_matching_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(transaction.TransactionPhase.PREPARED)
    monkeypatch.setattr(reconcile.windows_install_transaction, "load_transaction", lambda _path: state)
    plan = transaction.RecoveryPlan(
        TRANSACTION_ID,
        transaction.RecoveryDirection.FORWARD,
        BASELINE_STOPPED,
        (),
    )
    monkeypatch.setattr(reconcile.windows_install_transaction, "plan_recovery", lambda *_args: plan)
    execute = Mock()
    monkeypatch.setattr(reconcile.windows_install_transaction, "execute_recovery", execute)
    with pytest.raises(RuntimeError, match="widerspricht"):
        reconcile._execute_service_recovery(
            EXPECTED_EXECUTABLE,
            direction=reconcile.ReconcileDirection.ROLLBACK,
            recovery_factory=lambda _path: _Operations(BASELINE_STOPPED),
        )
    execute.assert_not_called()


def test_execute_service_recovery_allows_missing_noop_rollback_but_not_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconcile.windows_install_transaction, "load_transaction", lambda _path: None)
    reconcile._execute_service_recovery(
        EXPECTED_EXECUTABLE,
        direction=reconcile.ReconcileDirection.ROLLBACK,
        recovery_factory=Mock(side_effect=AssertionError),
    )
    with pytest.raises(RuntimeError, match="Transaktionsmanifest"):
        reconcile._execute_service_recovery(
            EXPECTED_EXECUTABLE,
            direction=reconcile.ReconcileDirection.COMMIT,
            recovery_factory=Mock(side_effect=AssertionError),
        )


@pytest.mark.parametrize(
    "direction",
    [
        reconcile.ReconcileDirection.ROLLBACK,
        reconcile.ReconcileDirection.COMMIT,
    ],
)
def test_finish_executes_directional_recovery_and_leaves_terminal_marker(
    direction: reconcile.ReconcileDirection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconcile,
        "classify_install_reconcile",
        lambda *_args, **_kwargs: direction,
    )
    execute = Mock()
    monkeypatch.setattr(reconcile, "_execute_service_recovery", execute)
    assert reconcile.finish_install_reconcile(EXPECTED_EXECUTABLE) is direction
    execute.assert_called_once_with(
        EXPECTED_EXECUTABLE,
        direction=direction,
        recovery_factory=reconcile._default_recovery_factory,
    )


def test_finish_cleans_partial_and_orphan_tails(monkeypatch: pytest.MonkeyPatch) -> None:
    partial = transaction.PartialPreparedState(prepared=None)
    monkeypatch.setattr(
        reconcile,
        "classify_install_reconcile",
        lambda *_args, **_kwargs: reconcile.ReconcileDirection.CLEANUP,
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_partial_prepared_transaction",
        Mock(return_value=partial),
    )
    clear_partial = Mock()
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "clear_partial_prepared_transaction",
        clear_partial,
    )
    assert reconcile.finish_install_reconcile(EXPECTED_EXECUTABLE) is reconcile.ReconcileDirection.CLEANUP
    clear_partial.assert_called_once_with(EXPECTED_EXECUTABLE)

    orphan = transaction.OrphanedCompletionMarker(
        TRANSACTION_ID,
        transaction.TransactionPhase.COMMIT_STARTED,
        "b" * 64,
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_partial_prepared_transaction",
        lambda _path: None,
    )
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "load_orphaned_completion_marker",
        lambda _path: orphan,
    )
    clear_orphan = Mock()
    monkeypatch.setattr(
        reconcile.windows_install_transaction,
        "clear_orphaned_completion_marker",
        clear_orphan,
    )
    assert reconcile.finish_install_reconcile(EXPECTED_EXECUTABLE) is reconcile.ReconcileDirection.CLEANUP
    clear_orphan.assert_called_once_with(EXPECTED_EXECUTABLE)


@pytest.mark.parametrize(
    ("phase", "plan_direction"),
    [
        (
            transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE,
            transaction.RecoveryDirection.COMPLETE,
        ),
        (
            transaction.TransactionPhase.COMMIT_STARTED,
            transaction.RecoveryDirection.FORWARD,
        ),
    ],
)
def test_finish_finalizes_terminal_service_transaction(
    phase: transaction.TransactionPhase,
    plan_direction: transaction.RecoveryDirection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(phase)
    monkeypatch.setattr(
        reconcile,
        "classify_install_reconcile",
        lambda *_args, **_kwargs: reconcile.ReconcileDirection.CLEANUP,
    )
    monkeypatch.setattr(reconcile.windows_install_transaction, "load_transaction", lambda *_args, **_kwargs: state)
    operations = _Operations(BASELINE_STOPPED)
    plan = transaction.RecoveryPlan(
        TRANSACTION_ID,
        plan_direction,
        BASELINE_STOPPED,
        (),
    )
    monkeypatch.setattr(reconcile.windows_install_transaction, "plan_recovery", lambda *_args: plan)
    execute = Mock()
    finalize = Mock()
    monkeypatch.setattr(reconcile.windows_install_transaction, "execute_recovery", execute)
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


def test_mark_rollback_complete_handles_none_and_rejects_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classify = Mock(return_value=reconcile.ReconcileDirection.NONE)
    monkeypatch.setattr(reconcile, "classify_install_reconcile", classify)
    assert reconcile.mark_service_rollback_complete(EXPECTED_EXECUTABLE) is reconcile.ReconcileDirection.NONE
    classify.return_value = reconcile.ReconcileDirection.COMMIT
    with pytest.raises(RuntimeError, match="nicht mehr rollbackfähig"):
        reconcile.mark_service_rollback_complete(EXPECTED_EXECUTABLE)


def test_service_check_and_committed_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(reconcile.subprocess, "run", run)
    machine = SimpleNamespace(existing_state=True)
    monkeypatch.setattr(reconcile.windows_service_preflight, "inspect_machine_state", lambda: machine)
    reconcile._verify_committed_service_state(
        EXPECTED_EXECUTABLE,
        _prepared(target_running=True),
    )
    assert [call.args[0][1] for call in run.call_args_list] == ["--verify-state", "--health-check"]

    run.return_value = SimpleNamespace(returncode=1)
    with pytest.raises(RuntimeError, match="fehlgeschlagen"):
        reconcile._run_service_check(EXPECTED_EXECUTABLE, "--verify-state")
    machine.existing_state = False
    with pytest.raises(RuntimeError, match="Maschinenzustand"):
        reconcile._verify_committed_service_state(EXPECTED_EXECUTABLE, _prepared())


def test_mark_committed_sets_service_marker_without_desktop_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(transaction.TransactionPhase.PREPARED)
    monkeypatch.setattr(reconcile.windows_install_transaction, "load_transaction", lambda _path: state)
    operations = _Operations(COMMITTED_STOPPED)
    verifier = Mock()
    marker = Mock()
    monkeypatch.setattr(reconcile.windows_install_transaction, "mark_commit_started", marker)
    assert (
        reconcile.mark_service_committed(
            EXPECTED_EXECUTABLE,
            _recovery_factory=lambda _path: operations,
            _commit_verifier=verifier,
        )
        is reconcile.ReconcileDirection.COMMIT
    )
    verifier.assert_called_once_with(EXPECTED_EXECUTABLE, state.prepared)
    marker.assert_called_once_with(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        observation=COMMITTED_STOPPED,
    )


def test_mark_committed_rejects_missing_rolled_back_and_verification_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconcile.windows_install_transaction, "load_transaction", lambda _path: None)
    with pytest.raises(RuntimeError, match="keine vollständige"):
        reconcile.mark_service_committed(EXPECTED_EXECUTABLE)

    state = _state(transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE)
    monkeypatch.setattr(reconcile.windows_install_transaction, "load_transaction", lambda _path: state)
    with pytest.raises(RuntimeError, match="zurückgerollte"):
        reconcile.mark_service_committed(EXPECTED_EXECUTABLE)

    state = _state(transaction.TransactionPhase.PREPARED)
    monkeypatch.setattr(reconcile.windows_install_transaction, "load_transaction", lambda _path: state)
    operations = Mock()
    operations.observe.side_effect = [BASELINE_STOPPED, FIRST_INSTALL_BASELINE]
    with pytest.raises(RuntimeError, match="änderten sich"):
        reconcile.mark_service_committed(
            EXPECTED_EXECUTABLE,
            _recovery_factory=lambda _path: operations,
            _commit_verifier=Mock(),
        )


def test_mark_committed_retry_requires_forward_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(transaction.TransactionPhase.COMMIT_STARTED)
    monkeypatch.setattr(reconcile.windows_install_transaction, "load_transaction", lambda _path: state)
    operations = _Operations(COMMITTED_STOPPED)
    plan = transaction.RecoveryPlan(
        TRANSACTION_ID,
        transaction.RecoveryDirection.ROLLBACK,
        COMMITTED_STOPPED,
        (),
    )
    monkeypatch.setattr(reconcile.windows_install_transaction, "plan_recovery", lambda *_args: plan)
    with pytest.raises(RuntimeError, match="vorwärts"):
        reconcile.mark_service_committed(
            EXPECTED_EXECUTABLE,
            _recovery_factory=lambda _path: operations,
            _commit_verifier=Mock(),
        )
