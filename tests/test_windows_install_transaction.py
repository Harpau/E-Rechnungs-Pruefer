from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import windows_install_transaction as transaction
from app.windows_service_config import SERVICE_ACCOUNT, SERVICE_NAME

EXPECTED_EXECUTABLE = Path(r"C:\Program Files\E-Rechnungs-Pruefer-Dienst\service\E-Rechnungs-Pruefer-Dienst.exe")
TRANSACTION_ID = "8607fab862d54d109b9950b7c8ba3a27"
DESKTOP_READER_SID = "S-1-5-21-1000"
DESKTOP_SEAL_SHA256 = "a" * 64
EXISTING_MACHINE_STATE = transaction.MachineBefore(True, True, True)
EMPTY_MACHINE_STATE = transaction.MachineBefore(False, False, False)


def _metadata() -> dict[str, object]:
    return {
        "schema_version": 1,
        "service_name": SERVICE_NAME,
        "expected_executable": str(EXPECTED_EXECUTABLE),
        "service_account": SERVICE_ACCOUNT,
        "start_type": 2,
        "description": "Eigene Baseline",
        "delayed_start": True,
        "service_sid_type": 1,
        "failure_actions": {
            "ResetPeriod": 86400,
            "RebootMsg": "",
            "Command": "",
            "Actions": [[1, 1000], [0, 0]],
        },
        "failure_actions_flag": True,
    }


def _first_install_prepared_bytes() -> bytes:
    prepared = transaction.PreparedTransaction(
        transaction_id=TRANSACTION_ID,
        desktop_reader_sid=DESKTOP_READER_SID,
        desktop_seal_sha256=DESKTOP_SEAL_SHA256,
        expected_executable=str(EXPECTED_EXECUTABLE),
        service_existed=False,
        service_running=False,
        service_metadata=None,
        machine_before=EMPTY_MACHINE_STATE,
        target_service_running=True,
        token_transfer_consent=False,
    )
    return transaction._canonical_json(transaction._prepared_payload(prepared))


class _MemoryStore:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []

    def read(self, name: str) -> bytes | None:
        self.calls.append(("read", name))
        return self.files.get(name)

    def create(self, name: str, payload: bytes) -> None:
        self.calls.append(("create", name))
        if name in self.files:
            raise FileExistsError(name)
        self.files[name] = payload

    def delete(self, name: str) -> None:
        self.calls.append(("delete", name))
        self.files.pop(name, None)

    def remove_directory_if_empty(self) -> None:
        self.calls.append(("rmdir", ""))


def _prepare_update(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: _MemoryStore | None = None,
    running: bool = True,
    target_running: bool = True,
    machine_before: transaction.MachineBefore = EXISTING_MACHINE_STATE,
) -> tuple[_MemoryStore, transaction.TransactionState]:
    state_store = store or _MemoryStore()
    capture = Mock(return_value=_metadata())
    monkeypatch.setattr(transaction.windows_service_metadata, "capture_service_metadata", capture)
    transaction.prepare_transaction(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        desktop_reader_sid=DESKTOP_READER_SID,
        desktop_seal_sha256=DESKTOP_SEAL_SHA256,
        service_existed=True,
        service_running=running,
        machine_before=machine_before,
        target_service_running=target_running,
        token_transfer_consent=False,
        _state_store=state_store,
    )
    state = transaction.load_transaction(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        _state_store=state_store,
    )
    assert state is not None
    return state_store, state


def _prepare_first(
    *,
    store: _MemoryStore | None = None,
    machine_before: transaction.MachineBefore = EMPTY_MACHINE_STATE,
    target_running: bool = True,
    token_consent: bool = False,
) -> tuple[_MemoryStore, transaction.TransactionState]:
    state_store = store or _MemoryStore()
    transaction.prepare_transaction(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        desktop_reader_sid=DESKTOP_READER_SID,
        desktop_seal_sha256=DESKTOP_SEAL_SHA256,
        service_existed=False,
        service_running=False,
        machine_before=machine_before,
        target_service_running=target_running,
        token_transfer_consent=token_consent,
        _state_store=state_store,
    )
    state = transaction.load_transaction(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        _state_store=state_store,
    )
    assert state is not None
    return state_store, state


def test_prepare_persists_immutable_canonical_manifest_with_full_scm_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, state = _prepare_update(monkeypatch)

    encoded = store.files[transaction.PREPARED_FILE_NAME]
    payload = json.loads(encoded)
    assert encoded.endswith(b"\n")
    assert payload["transaction_id"] == TRANSACTION_ID
    assert payload["desktop_binding"] == {
        "reader_sid": DESKTOP_READER_SID,
        "seal_sha256": DESKTOP_SEAL_SHA256,
    }
    assert payload["service_before"] == {
        "existed": True,
        "running": True,
        "metadata": _metadata(),
    }
    assert payload["machine_before"] == {"configuration": True, "token": True, "logs": True}
    assert payload["target"] == {"service_running": True, "token_transfer_consent": False}
    assert state.phase is transaction.TransactionPhase.PREPARED

    # The exact same request is idempotent, but neither overwrites nor adopts
    # a different transaction/baseline.
    before_creates = store.calls.count(("create", transaction.PREPARED_FILE_NAME))
    _prepare_update(monkeypatch, store=store)
    assert store.calls.count(("create", transaction.PREPARED_FILE_NAME)) == before_creates
    with pytest.raises(RuntimeError, match="abweichendes PREPARED"):
        transaction.prepare_transaction(
            EXPECTED_EXECUTABLE,
            transaction_id="19b685f4adcd46b3a93ab739957b3a1e",
            desktop_reader_sid=DESKTOP_READER_SID,
            desktop_seal_sha256=DESKTOP_SEAL_SHA256,
            service_existed=True,
            service_running=True,
            machine_before=transaction.MachineBefore(True, True, True),
            target_service_running=True,
            token_transfer_consent=False,
            _state_store=store,
        )


@pytest.mark.parametrize(
    "invalid_id",
    [
        "",
        "8607fab8-62d5-4d10-9b99-50b7c8ba3a27",
        "8607FAB862D54D109B9950B7C8BA3A27",
        "not-a-128-bit-token-value-00000",
    ],
)
def test_prepare_rejects_noncanonical_128_bit_transaction_ids_before_capture(
    invalid_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = Mock()
    monkeypatch.setattr(transaction.windows_service_metadata, "capture_service_metadata", capture)

    with pytest.raises(RuntimeError, match="Transaktions-ID"):
        transaction.prepare_transaction(
            EXPECTED_EXECUTABLE,
            transaction_id=invalid_id,
            desktop_reader_sid=DESKTOP_READER_SID,
            desktop_seal_sha256=DESKTOP_SEAL_SHA256,
            service_existed=True,
            service_running=False,
            machine_before=transaction.MachineBefore(True, True, False),
            target_service_running=False,
            token_transfer_consent=False,
            _state_store=_MemoryStore(),
        )

    capture.assert_not_called()


def test_load_rejects_noncanonical_duplicate_unknown_and_oversized_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _state = _prepare_update(monkeypatch)
    canonical = store.files[transaction.PREPARED_FILE_NAME]
    invalid_payloads = [
        canonical.rstrip(b"\n"),
        canonical.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'),
        canonical.replace(b'"schema_version":1', b'"foreign":1,"schema_version":1'),
        b"{" + b"x" * transaction.MAXIMUM_TRANSACTION_BYTES + b"}",
    ]

    for invalid in invalid_payloads:
        store.files[transaction.PREPARED_FILE_NAME] = invalid
        with pytest.raises(RuntimeError):
            transaction.load_transaction(EXPECTED_EXECUTABLE, _state_store=store)


def test_first_install_manifest_rejects_token_overwrite_and_never_captures_scm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = Mock()
    monkeypatch.setattr(transaction.windows_service_metadata, "capture_service_metadata", capture)
    with pytest.raises(RuntimeError, match="Maschinentoken"):
        _prepare_first(
            machine_before=transaction.MachineBefore(True, True, False),
            token_consent=True,
        )
    capture.assert_not_called()


@pytest.mark.parametrize(
    ("topology", "expected_bundle_actions"),
    [
        (transaction.BundleTopology(True, False, False, False), ()),
        (
            transaction.BundleTopology(True, True, False, False),
            (transaction.RecoveryAction.DELETE_NEW,),
        ),
        (
            transaction.BundleTopology(False, True, True, False),
            (
                transaction.RecoveryAction.DELETE_NEW,
                transaction.RecoveryAction.MOVE_ROLLBACK_TO_LIVE,
            ),
        ),
        (
            transaction.BundleTopology(True, False, True, False),
            (
                transaction.RecoveryAction.DELETE_LIVE,
                transaction.RecoveryAction.MOVE_ROLLBACK_TO_LIVE,
            ),
        ),
        (
            transaction.BundleTopology(True, False, False, True),
            (
                transaction.RecoveryAction.DELETE_LIVE,
                transaction.RecoveryAction.MOVE_OBSOLETE_TO_LIVE,
            ),
        ),
        (
            transaction.BundleTopology(False, False, True, False),
            (transaction.RecoveryAction.MOVE_ROLLBACK_TO_LIVE,),
        ),
        (
            transaction.BundleTopology(False, False, False, True),
            (transaction.RecoveryAction.MOVE_OBSOLETE_TO_LIVE,),
        ),
    ],
)
def test_update_rollback_planner_covers_original_and_interrupted_bundle_matrix(
    topology: transaction.BundleTopology,
    expected_bundle_actions: tuple[transaction.RecoveryAction, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, state = _prepare_update(monkeypatch, running=True)
    observation = transaction.RecoveryObservation(topology, transaction.ServiceState.OWNED_STOPPED)

    plan = transaction.plan_recovery(state, observation)

    assert plan.direction is transaction.RecoveryDirection.ROLLBACK
    assert plan.actions == (
        *expected_bundle_actions,
        transaction.RecoveryAction.RESTORE_SERVICE_METADATA,
        transaction.RecoveryAction.START_SERVICE,
    )


@pytest.mark.parametrize(
    "topology",
    [
        transaction.BundleTopology(False, False, False, False),
        transaction.BundleTopology(False, True, False, False),
        transaction.BundleTopology(True, False, True, True),
        transaction.BundleTopology(False, True, True, True),
    ],
)
def test_update_planner_rejects_missing_backup_or_ambiguous_topologies_without_actions(
    topology: transaction.BundleTopology,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, state = _prepare_update(monkeypatch)

    with pytest.raises(RuntimeError, match="rollbackfähig"):
        transaction.plan_recovery(
            state,
            transaction.RecoveryObservation(topology, transaction.ServiceState.OWNED_STOPPED),
        )


@pytest.mark.parametrize("service", [transaction.ServiceState.ABSENT, transaction.ServiceState.FOREIGN])
def test_update_planner_rejects_missing_or_foreign_scm_service(
    service: transaction.ServiceState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, state = _prepare_update(monkeypatch)
    with pytest.raises(RuntimeError):
        transaction.plan_recovery(
            state,
            transaction.RecoveryObservation(
                transaction.BundleTopology(True, False, False, False),
                service,
            ),
        )


def test_first_install_rollback_deletes_only_owned_service_and_new_state() -> None:
    _store, state = _prepare_first()
    observation = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, False),
        transaction.ServiceState.OWNED_RUNNING,
    )

    plan = transaction.plan_recovery(state, observation)

    assert plan.actions == (
        transaction.RecoveryAction.STOP_SERVICE,
        transaction.RecoveryAction.DELETE_SERVICE,
        transaction.RecoveryAction.DELETE_LIVE,
        transaction.RecoveryAction.PURGE_MACHINE_STATE,
    )


def test_first_install_purge_gate_preserves_any_preexisting_machine_state() -> None:
    _store, state = _prepare_first(machine_before=transaction.MachineBefore(configuration=True, token=True, logs=False))
    plan = transaction.plan_recovery(
        state,
        transaction.RecoveryObservation(
            transaction.BundleTopology(False, True, False, False),
            transaction.ServiceState.ABSENT,
        ),
    )

    assert plan.actions == (transaction.RecoveryAction.DELETE_NEW,)
    assert transaction.RecoveryAction.PURGE_MACHINE_STATE not in plan.actions


@pytest.mark.parametrize(
    "topology",
    [
        transaction.BundleTopology(True, True, False, False),
        transaction.BundleTopology(False, False, True, False),
        transaction.BundleTopology(False, False, False, True),
    ],
)
def test_first_install_rejects_ambiguous_or_update_backup_topologies(
    topology: transaction.BundleTopology,
) -> None:
    _store, state = _prepare_first()
    with pytest.raises(RuntimeError, match="Backup-Bundles"):
        transaction.plan_recovery(
            state,
            transaction.RecoveryObservation(topology, transaction.ServiceState.ABSENT),
        )


def test_commit_marker_requires_exact_topology_and_target_state_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _state = _prepare_update(monkeypatch, target_running=True)
    invalid = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, True, False),
        transaction.ServiceState.OWNED_RUNNING,
    )
    with pytest.raises(RuntimeError, match="Commit bewiesen"):
        transaction.mark_commit_started(
            EXPECTED_EXECUTABLE,
            transaction_id=TRANSACTION_ID,
            observation=invalid,
            _state_store=store,
        )
    assert transaction.PHASE_FILE_NAME not in store.files

    proof = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, True),
        transaction.ServiceState.OWNED_RUNNING,
    )
    transaction.mark_commit_started(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        observation=proof,
        _state_store=store,
    )
    encoded = store.files[transaction.PHASE_FILE_NAME]
    marker = json.loads(encoded)
    assert marker["phase"] == transaction.TransactionPhase.COMMIT_STARTED
    assert marker["transaction_id"] == TRANSACTION_ID
    assert marker["prepared_sha256"] == transaction._prepared_digest(store.files[transaction.PREPARED_FILE_NAME])

    transaction.mark_commit_started(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        observation=proof,
        _state_store=store,
    )
    assert store.files[transaction.PHASE_FILE_NAME] == encoded


@pytest.mark.parametrize("incomplete_slot", ["live", "obsolete"])
def test_commit_marker_rejects_incomplete_live_or_old_backup_before_no_return(
    incomplete_slot: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _state = _prepare_update(monkeypatch, target_running=True)
    partial = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, True),
        transaction.ServiceState.OWNED_RUNNING,
        incomplete_bundles=frozenset({incomplete_slot}),
    )

    with pytest.raises(RuntimeError, match="nicht vollständig"):
        transaction.mark_commit_started(
            EXPECTED_EXECUTABLE,
            transaction_id=TRANSACTION_ID,
            observation=partial,
            _state_store=store,
        )

    assert transaction.PHASE_FILE_NAME not in store.files


def test_commit_marker_is_bound_to_exact_immutable_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _state = _prepare_update(monkeypatch)
    proof = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, True),
        transaction.ServiceState.OWNED_RUNNING,
    )
    transaction.mark_commit_started(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        observation=proof,
        _state_store=store,
    )
    prepared = json.loads(store.files[transaction.PREPARED_FILE_NAME])
    prepared["target"]["service_running"] = False
    store.files[transaction.PREPARED_FILE_NAME] = transaction._canonical_json(prepared)

    with pytest.raises(RuntimeError, match="nicht an das PREPARED"):
        transaction.load_transaction(EXPECTED_EXECUTABLE, _state_store=store)


def test_committed_update_plans_forward_cleanup_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _state = _prepare_update(monkeypatch)
    proof = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, True),
        transaction.ServiceState.OWNED_RUNNING,
    )
    transaction.mark_commit_started(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        observation=proof,
        _state_store=store,
    )
    state = transaction.load_transaction(EXPECTED_EXECUTABLE, _state_store=store)
    assert state is not None

    plan = transaction.plan_recovery(state, proof)

    assert plan.direction is transaction.RecoveryDirection.FORWARD
    assert plan.actions == (transaction.RecoveryAction.DELETE_OBSOLETE,)


def test_committed_update_resumes_partial_obsolete_delete_but_requires_complete_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _state = _prepare_update(monkeypatch)
    proof = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, True),
        transaction.ServiceState.OWNED_RUNNING,
    )
    transaction.mark_commit_started(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        observation=proof,
        _state_store=store,
    )
    state = transaction.load_transaction(EXPECTED_EXECUTABLE, _state_store=store)
    assert state is not None

    partial_obsolete = transaction.RecoveryObservation(
        proof.bundles,
        proof.service,
        incomplete_bundles=frozenset({"obsolete"}),
    )
    assert transaction.plan_recovery(state, partial_obsolete).actions == (transaction.RecoveryAction.DELETE_OBSOLETE,)

    partial_live = transaction.RecoveryObservation(
        proof.bundles,
        proof.service,
        incomplete_bundles=frozenset({"live"}),
    )
    with pytest.raises(RuntimeError, match="committed Live-Bundle"):
        transaction.plan_recovery(state, partial_live)


def test_prepared_first_install_resumes_partial_live_delete() -> None:
    _store, state = _prepare_first()
    observation = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, False),
        transaction.ServiceState.ABSENT,
        incomplete_bundles=frozenset({"live"}),
    )

    plan = transaction.plan_recovery(state, observation)

    assert plan.direction is transaction.RecoveryDirection.ROLLBACK
    assert plan.actions == (
        transaction.RecoveryAction.DELETE_LIVE,
        transaction.RecoveryAction.PURGE_MACHINE_STATE,
    )


@pytest.mark.parametrize("backup", ["rollback", "obsolete"])
def test_prepared_update_allows_partial_live_only_with_complete_old_backup(
    backup: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, state = _prepare_update(monkeypatch, running=False, target_running=False)
    topology = transaction.BundleTopology(
        live=True,
        new=False,
        rollback=backup == "rollback",
        obsolete=backup == "obsolete",
    )
    observation = transaction.RecoveryObservation(
        topology,
        transaction.ServiceState.OWNED_STOPPED,
        incomplete_bundles=frozenset({"live"}),
    )

    plan = transaction.plan_recovery(state, observation)

    expected_move = (
        transaction.RecoveryAction.MOVE_ROLLBACK_TO_LIVE
        if backup == "rollback"
        else transaction.RecoveryAction.MOVE_OBSOLETE_TO_LIVE
    )
    assert plan.actions[:2] == (transaction.RecoveryAction.DELETE_LIVE, expected_move)


def test_prepared_update_rejects_partial_live_without_baseline_and_partial_backups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, state = _prepare_update(monkeypatch, running=False, target_running=False)
    partial_live_without_backup = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, False),
        transaction.ServiceState.OWNED_STOPPED,
        incomplete_bundles=frozenset({"live"}),
    )
    with pytest.raises(RuntimeError, match="keine vollständige rollbackfähige Baseline"):
        transaction.plan_recovery(state, partial_live_without_backup)

    for backup in ("rollback", "obsolete"):
        partial_backup = transaction.RecoveryObservation(
            transaction.BundleTopology(
                live=True,
                new=False,
                rollback=backup == "rollback",
                obsolete=backup == "obsolete",
            ),
            transaction.ServiceState.OWNED_STOPPED,
            incomplete_bundles=frozenset({backup}),
        )
        with pytest.raises(RuntimeError, match="vollständiges altes Backup"):
            transaction.plan_recovery(state, partial_backup)


def test_recovery_rejects_incomplete_marker_for_absent_or_unknown_bundle() -> None:
    _store, state = _prepare_first()
    for incomplete in (frozenset({"live"}), frozenset({"new"})):
        observation = transaction.RecoveryObservation(
            transaction.BundleTopology(False, False, False, False),
            transaction.ServiceState.ABSENT,
            incomplete_bundles=incomplete,
        )
        with pytest.raises(RuntimeError, match="unvollständigen|nicht vorhanden"):
            transaction.plan_recovery(state, observation)


def test_planner_retries_every_recursive_delete_when_the_executable_still_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A power loss can happen after sibling files were removed but before the
    # executable. Such a remainder has the same presence/completeness flags as
    # the pre-delete tree and must therefore retain the original DELETE action.
    _first_store, first = _prepare_first()
    first_live = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, False),
        transaction.ServiceState.ABSENT,
    )
    first_new = transaction.RecoveryObservation(
        transaction.BundleTopology(False, True, False, False),
        transaction.ServiceState.ABSENT,
    )
    assert transaction.RecoveryAction.DELETE_LIVE in transaction.plan_recovery(first, first_live).actions
    assert transaction.RecoveryAction.DELETE_NEW in transaction.plan_recovery(first, first_new).actions

    update_store, update = _prepare_update(monkeypatch, running=False, target_running=False)
    update_live = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, True, False),
        transaction.ServiceState.OWNED_STOPPED,
    )
    update_new = transaction.RecoveryObservation(
        transaction.BundleTopology(True, True, False, False),
        transaction.ServiceState.OWNED_STOPPED,
    )
    assert transaction.plan_recovery(update, update_live).actions[0] is transaction.RecoveryAction.DELETE_LIVE
    assert transaction.plan_recovery(update, update_new).actions[0] is transaction.RecoveryAction.DELETE_NEW

    proof = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, True),
        transaction.ServiceState.OWNED_STOPPED,
    )
    transaction.mark_commit_started(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        observation=proof,
        _state_store=update_store,
    )
    committed = transaction.load_transaction(EXPECTED_EXECUTABLE, _state_store=update_store)
    assert committed is not None
    assert transaction.plan_recovery(committed, proof).actions == (transaction.RecoveryAction.DELETE_OBSOLETE,)


class _FakeOperations:
    def __init__(self, observation: transaction.RecoveryObservation) -> None:
        self.current = observation
        self.calls: list[object] = []

    def observe(self) -> transaction.RecoveryObservation:
        return self.current

    def stop_service(self) -> None:
        self.calls.append("stop")
        self.current = transaction.RecoveryObservation(
            self.current.bundles,
            transaction.ServiceState.OWNED_STOPPED,
        )

    def delete_service(self) -> None:
        self.calls.append("delete_service")
        self.current = transaction.RecoveryObservation(self.current.bundles, transaction.ServiceState.ABSENT)

    def delete_bundle(self, slot: str) -> None:
        self.calls.append(("delete", slot))
        values = {
            "live": self.current.bundles.live,
            "new": self.current.bundles.new,
            "rollback": self.current.bundles.rollback,
            "obsolete": self.current.bundles.obsolete,
        }
        values[slot] = False
        self.current = transaction.RecoveryObservation(
            transaction.BundleTopology(**values),
            self.current.service,
        )

    def move_bundle(self, source: str, destination: str) -> None:
        self.calls.append(("move", source, destination))
        values = {
            "live": self.current.bundles.live,
            "new": self.current.bundles.new,
            "rollback": self.current.bundles.rollback,
            "obsolete": self.current.bundles.obsolete,
        }
        assert values[source] and not values[destination]
        values[source] = False
        values[destination] = True
        self.current = transaction.RecoveryObservation(
            transaction.BundleTopology(**values),
            self.current.service,
        )

    def restore_service_metadata(self, payload: Mapping[str, object]) -> None:
        self.calls.append(("restore", dict(payload)))

    def start_service(self) -> None:
        self.calls.append("start")
        self.current = transaction.RecoveryObservation(
            self.current.bundles,
            transaction.ServiceState.OWNED_RUNNING,
        )

    def purge_machine_state(self) -> None:
        self.calls.append("purge")


def test_executor_rechecks_whole_observation_before_first_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, state = _prepare_update(monkeypatch)
    original = transaction.RecoveryObservation(
        transaction.BundleTopology(True, True, False, False),
        transaction.ServiceState.OWNED_STOPPED,
    )
    plan = transaction.plan_recovery(state, original)
    operations = _FakeOperations(
        transaction.RecoveryObservation(
            transaction.BundleTopology(True, False, False, False),
            transaction.ServiceState.OWNED_STOPPED,
        )
    )

    with pytest.raises(RuntimeError, match="Neuplanung"):
        transaction.execute_recovery(
            EXPECTED_EXECUTABLE,
            state=state,
            plan=plan,
            operations=operations,
            _state_store=store,
        )

    assert operations.calls == []
    assert transaction.PHASE_FILE_NAME not in store.files


def test_executor_restores_update_then_persists_rollback_complete_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, state = _prepare_update(monkeypatch, running=True)
    observation = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, True),
        transaction.ServiceState.OWNED_RUNNING,
    )
    plan = transaction.plan_recovery(state, observation)
    operations = _FakeOperations(observation)

    transaction.execute_recovery(
        EXPECTED_EXECUTABLE,
        state=state,
        plan=plan,
        operations=operations,
        _state_store=store,
    )

    assert operations.calls == [
        "stop",
        ("delete", "live"),
        ("move", "obsolete", "live"),
        ("restore", _metadata()),
        "start",
    ]
    recovered = transaction.load_transaction(EXPECTED_EXECUTABLE, _state_store=store)
    assert recovered is not None
    assert recovered.phase is transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE


def test_executor_first_install_deletes_service_before_purge_and_marks_completion() -> None:
    store, state = _prepare_first()
    observation = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, False),
        transaction.ServiceState.OWNED_RUNNING,
    )
    operations = _FakeOperations(observation)

    transaction.execute_recovery(
        EXPECTED_EXECUTABLE,
        state=state,
        plan=transaction.plan_recovery(state, observation),
        operations=operations,
        _state_store=store,
    )

    assert operations.calls == ["stop", "delete_service", ("delete", "live"), "purge"]
    recovered = transaction.load_transaction(EXPECTED_EXECUTABLE, _state_store=store)
    assert recovered is not None
    assert recovered.phase is transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE


def test_finalize_requires_terminal_phase_and_deletes_records_after_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, state = _prepare_update(monkeypatch, running=False, target_running=False)
    observation = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, False),
        transaction.ServiceState.OWNED_STOPPED,
    )
    operations = _FakeOperations(observation)
    transaction.execute_recovery(
        EXPECTED_EXECUTABLE,
        state=state,
        plan=transaction.plan_recovery(state, observation),
        operations=operations,
        _state_store=store,
    )
    store.calls.clear()

    transaction.finalize_transaction(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        observation=observation,
        _state_store=store,
    )

    deletion_calls = [item for item in store.calls if item[0] in {"delete", "rmdir"}]
    assert deletion_calls == [
        ("delete", transaction.PREPARED_FILE_NAME),
        ("delete", transaction.PHASE_FILE_NAME),
        ("rmdir", ""),
    ]
    assert store.files == {}


def test_orphaned_terminal_marker_after_final_manifest_delete_is_safely_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _state = _prepare_update(monkeypatch)
    proof = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, True),
        transaction.ServiceState.OWNED_RUNNING,
    )
    transaction.mark_commit_started(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        observation=proof,
        _state_store=store,
    )
    store.delete(transaction.PREPARED_FILE_NAME)

    with pytest.raises(RuntimeError, match="verwaister Abschlussmarker"):
        transaction.load_transaction(EXPECTED_EXECUTABLE, _state_store=store)
    marker = transaction.load_orphaned_completion_marker(
        EXPECTED_EXECUTABLE,
        _state_store=store,
    )
    assert marker == transaction.OrphanedCompletionMarker(
        transaction_id=TRANSACTION_ID,
        phase=transaction.TransactionPhase.COMMIT_STARTED,
        prepared_sha256=json.loads(store.files[transaction.PHASE_FILE_NAME])["prepared_sha256"],
    )
    with pytest.raises(RuntimeError, match="anderen Transaktion"):
        transaction.clear_orphaned_completion_marker(
            EXPECTED_EXECUTABLE,
            transaction_id="19b685f4adcd46b3a93ab739957b3a1e",
            _state_store=store,
        )
    assert transaction.PHASE_FILE_NAME in store.files

    transaction.clear_orphaned_completion_marker(
        EXPECTED_EXECUTABLE,
        _state_store=store,
    )
    assert store.files == {}


def test_protected_store_creates_fixed_secure_temp_and_atomically_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".installer-state"
    state_directory.mkdir()
    store = object.__new__(transaction._ProtectedTransactionStore)
    store._state_directory = state_directory
    secure_write = Mock(side_effect=lambda path, payload: path.write_bytes(payload))
    publish = Mock(side_effect=lambda source, destination: source.rename(destination))
    monkeypatch.setattr(transaction, "validate_machine_path", lambda path, *, directory: path.exists())
    monkeypatch.setattr(transaction.windows_service_metadata, "_verify_administrative_path", Mock())
    monkeypatch.setattr(transaction.windows_service_metadata, "_write_secure_snapshot", secure_write)
    monkeypatch.setattr(transaction, "_atomic_publish", publish)

    store.create(transaction.PREPARED_FILE_NAME, b"protected\n")

    destination = state_directory / transaction.PREPARED_FILE_NAME
    assert destination.read_bytes() == b"protected\n"
    temporary = secure_write.call_args.args[0]
    assert temporary.parent == state_directory
    assert temporary.name.startswith(f".{transaction.PREPARED_FILE_NAME}.")
    publish.assert_called_once_with(temporary, destination)


def test_protected_store_rejects_unknown_entries_but_consumes_valid_publish_tails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".installer-state"
    state_directory.mkdir()
    prepared = transaction.PreparedTransaction(
        transaction_id=TRANSACTION_ID,
        desktop_reader_sid=DESKTOP_READER_SID,
        desktop_seal_sha256=DESKTOP_SEAL_SHA256,
        expected_executable=str(EXPECTED_EXECUTABLE),
        service_existed=False,
        service_running=False,
        service_metadata=None,
        machine_before=EMPTY_MACHINE_STATE,
        target_service_running=True,
        token_transfer_consent=False,
    )
    encoded = transaction._canonical_json(transaction._prepared_payload(prepared))
    prepared_path = state_directory / transaction.PREPARED_FILE_NAME
    prepared_path.write_bytes(encoded)
    temporary = state_directory / f".{transaction.PREPARED_FILE_NAME}.{'d' * 32}.tmp"
    temporary.write_bytes(encoded)
    store = object.__new__(transaction._ProtectedTransactionStore)
    store._state_directory = state_directory
    store._installation_directory_present = True
    store._expected_executable = EXPECTED_EXECUTABLE
    monkeypatch.setattr(
        transaction,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    monkeypatch.setattr(transaction.windows_service_metadata, "_verify_administrative_path", Mock())

    assert store.read(transaction.PREPARED_FILE_NAME) == encoded

    unknown = state_directory / "fremd.txt"
    unknown.write_text("fremd", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unbekannten Eintrag"):
        store.read(transaction.PREPARED_FILE_NAME)
    unknown.unlink()

    temporary.write_bytes(b"partial")
    with pytest.raises(RuntimeError, match="Transaktionsbeleg"):
        store.read(transaction.PREPARED_FILE_NAME)
    temporary.write_bytes(encoded)

    store.delete(transaction.PREPARED_FILE_NAME)
    assert not temporary.exists()
    assert not prepared_path.exists()


def test_protected_store_tolerates_truncated_phase_scratch_only_beside_authoritative_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".installer-state"
    state_directory.mkdir()
    encoded = _first_install_prepared_bytes()
    prepared_path = state_directory / transaction.PREPARED_FILE_NAME
    prepared_path.write_bytes(encoded)
    phase_path = state_directory / transaction.PHASE_FILE_NAME
    phase_path.write_bytes(
        transaction._canonical_json(
            transaction._phase_payload(
                TRANSACTION_ID,
                transaction.TransactionPhase.COMMIT_STARTED,
                encoded,
            )
        )
    )
    temporary = state_directory / f".{transaction.PHASE_FILE_NAME}.{'d' * 32}.tmp"
    temporary.write_bytes(b'{"schema_version":')
    store = object.__new__(transaction._ProtectedTransactionStore)
    store._state_directory = state_directory
    store._installation_directory_present = True
    store._expected_executable = EXPECTED_EXECUTABLE
    monkeypatch.setattr(
        transaction,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    monkeypatch.setattr(transaction.windows_service_metadata, "_verify_administrative_path", Mock())

    state = transaction.load_transaction(EXPECTED_EXECUTABLE, _state_store=store)

    assert state is not None
    assert state.phase is transaction.TransactionPhase.COMMIT_STARTED
    assert temporary.read_bytes() == b'{"schema_version":'

    temporary.write_bytes(
        transaction._canonical_json(
            transaction._phase_payload(
                TRANSACTION_ID,
                transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE,
                encoded,
            )
        )
    )
    with pytest.raises(RuntimeError, match="widerspricht"):
        transaction.load_transaction(EXPECTED_EXECUTABLE, _state_store=store)

    temporary.write_bytes(b"")
    store.delete(transaction.PREPARED_FILE_NAME)
    assert not temporary.exists()


def test_new_service_phase_removes_revalidated_abandoned_phase_scratch_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / ".installer-state"
    state_directory.mkdir()
    encoded = _first_install_prepared_bytes()
    (state_directory / transaction.PREPARED_FILE_NAME).write_bytes(encoded)
    abandoned = state_directory / f".{transaction.PHASE_FILE_NAME}.{'d' * 32}.tmp"
    abandoned.write_bytes(
        transaction._canonical_json(
            transaction._phase_payload(
                TRANSACTION_ID,
                transaction.TransactionPhase.COMMIT_STARTED,
                encoded,
            )
        )
    )
    store = object.__new__(transaction._ProtectedTransactionStore)
    store._state_directory = state_directory
    store._installation_directory_present = True
    store._expected_executable = EXPECTED_EXECUTABLE
    monkeypatch.setattr(
        transaction,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    monkeypatch.setattr(transaction.windows_service_metadata, "_verify_administrative_path", Mock())
    monkeypatch.setattr(
        transaction.windows_service_metadata,
        "_write_secure_snapshot",
        lambda path, payload: path.write_bytes(payload),
    )
    monkeypatch.setattr(transaction, "_atomic_publish", lambda source, target: source.rename(target))

    transaction._write_phase(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        phase=transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE,
        _state_store=store,
    )

    assert not abandoned.exists()
    state = transaction.load_transaction(EXPECTED_EXECUTABLE, _state_store=store)
    assert state is not None
    assert state.phase is transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE


def test_read_only_store_treats_a_truly_absent_install_root_as_no_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = tmp_path / "missing-installation"
    state_directory = installation / ".installer-state"
    monkeypatch.setattr(
        transaction,
        "_transaction_directories",
        lambda _path: (installation, state_directory),
    )
    monkeypatch.setattr(
        transaction,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )

    store = transaction._ProtectedTransactionStore(EXPECTED_EXECUTABLE, create=False)

    assert store.read(transaction.PREPARED_FILE_NAME) is None
    assert store.read(transaction.PHASE_FILE_NAME) is None


@pytest.mark.parametrize(
    ("payload", "has_decoded_manifest"),
    [
        (b"", False),
        (b'{"schema_version":1', False),
        (_first_install_prepared_bytes(), True),
    ],
)
def test_partial_prepared_store_recognizes_and_clears_only_an_isolated_publish_tail(
    payload: bytes,
    has_decoded_manifest: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = tmp_path / "installation"
    state_directory = installation / ".installer-state"
    state_directory.mkdir(parents=True)
    temporary = state_directory / f".{transaction.PREPARED_FILE_NAME}.{'d' * 32}.tmp"
    temporary.write_bytes(payload)
    monkeypatch.setattr(
        transaction,
        "_transaction_directories",
        lambda _path: (installation, state_directory),
    )
    monkeypatch.setattr(
        transaction,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    monkeypatch.setattr(transaction.windows_service_metadata, "_verify_administrative_path", Mock())

    partial = transaction.load_partial_prepared_transaction(EXPECTED_EXECUTABLE)

    assert partial is not None
    assert (partial.prepared is not None) is has_decoded_manifest
    assert temporary.read_bytes() == payload

    transaction.clear_partial_prepared_transaction(EXPECTED_EXECUTABLE)

    assert not state_directory.exists()


def test_partial_prepared_store_recognizes_and_removes_an_empty_secure_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = tmp_path / "installation"
    state_directory = installation / ".installer-state"
    state_directory.mkdir(parents=True)
    monkeypatch.setattr(
        transaction,
        "_transaction_directories",
        lambda _path: (installation, state_directory),
    )
    monkeypatch.setattr(
        transaction,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    monkeypatch.setattr(transaction.windows_service_metadata, "_verify_administrative_path", Mock())

    assert transaction.load_partial_prepared_transaction(EXPECTED_EXECUTABLE) == (
        transaction.PartialPreparedState(prepared=None)
    )

    transaction.clear_partial_prepared_transaction(EXPECTED_EXECUTABLE)

    assert not state_directory.exists()


@pytest.mark.parametrize("contamination", ["unknown", "phase-temp", "multiple-prepared"])
def test_partial_prepared_store_rejects_ambiguous_inventory_without_mutation(
    contamination: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = tmp_path / "installation"
    state_directory = installation / ".installer-state"
    state_directory.mkdir(parents=True)
    prepared_temporary = state_directory / f".{transaction.PREPARED_FILE_NAME}.{'d' * 32}.tmp"
    prepared_temporary.write_bytes(b"partial")
    if contamination == "unknown":
        (state_directory / "fremd.txt").write_bytes(b"do-not-delete")
    elif contamination == "phase-temp":
        prepared_temporary.unlink()
        (state_directory / f".{transaction.PHASE_FILE_NAME}.{'e' * 32}.tmp").write_bytes(b"partial")
    else:
        (state_directory / f".{transaction.PREPARED_FILE_NAME}.{'e' * 32}.tmp").write_bytes(b"partial")
    monkeypatch.setattr(
        transaction,
        "_transaction_directories",
        lambda _path: (installation, state_directory),
    )
    monkeypatch.setattr(
        transaction,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    monkeypatch.setattr(transaction.windows_service_metadata, "_verify_administrative_path", Mock())
    before = {path.name: path.read_bytes() for path in state_directory.iterdir()}

    with pytest.raises(RuntimeError):
        transaction.load_partial_prepared_transaction(EXPECTED_EXECUTABLE)
    with pytest.raises(RuntimeError):
        transaction.clear_partial_prepared_transaction(EXPECTED_EXECUTABLE)

    assert {path.name: path.read_bytes() for path in state_directory.iterdir()} == before


@pytest.mark.parametrize(
    ("validator", "value", "message"),
    [
        (transaction._strict_desktop_reader_sid, "S-1-5-18", "Benutzeridentität"),
        (transaction._strict_desktop_reader_sid, "not-a-sid", "Benutzeridentität"),
        (
            lambda value: transaction._strict_sha256(value, description="Testhash"),
            "A" * 64,
            "Testhash",
        ),
        (
            lambda value: transaction._canonical_expected_executable(Path(str(value))),
            r"C:\Program Files\Product\other\service.exe",
            "Dienstpfad",
        ),
    ],
)
def test_strict_identity_hash_and_path_validators_fail_closed(
    validator,
    value: object,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        validator(value)


def test_bundle_path_resolution_and_inventory_cover_all_fixed_slots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    resolved = transaction.bundle_paths(EXPECTED_EXECUTABLE)
    assert resolved.executable_name == "E-Rechnungs-Pruefer-Dienst.exe"
    assert str(resolved.rollback).endswith("service.rollback")

    root = tmp_path / "installation"
    paths = transaction.BundlePaths(
        root / "service",
        root / "service.new",
        root / "service.rollback",
        root / "service.obsolete",
        "service.exe",
    )
    for slot in (paths.live, paths.rollback, paths.obsolete):
        slot.mkdir(parents=True)
        (slot / paths.executable_name).write_bytes(b"MZ")
        nested = slot / "assets"
        nested.mkdir()
        (nested / "runtime.bin").write_bytes(b"data")
    paths.new.mkdir()
    (paths.new / "partial.bin").write_bytes(b"partial")
    monkeypatch.setattr(transaction, "bundle_paths", lambda _path: paths)
    monkeypatch.setattr(
        transaction,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )

    assert transaction.inspect_bundle_topology(EXPECTED_EXECUTABLE) == transaction.BundleTopology(
        True,
        True,
        True,
        True,
    )


def test_bundle_inventory_rejects_unsafe_races_and_ambiguous_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    entry = bundle / "entry"
    entry.write_bytes(b"x")

    monkeypatch.setattr(transaction, "validate_machine_path", lambda *_args, **_kwargs: False)
    with pytest.raises(RuntimeError, match="kein sicherer Produktordner"):
        transaction._validate_bundle_tree(
            bundle,
            executable_name="service.exe",
            require_executable=False,
        )

    monkeypatch.setattr(transaction, "validate_machine_path", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(transaction.os, "scandir", Mock(side_effect=OSError("scan race")))
    with pytest.raises(RuntimeError, match="vollständig inventarisiert"):
        transaction._validate_bundle_tree(
            bundle,
            executable_name="service.exe",
            require_executable=False,
        )

    monkeypatch.undo()
    monkeypatch.setattr(transaction, "validate_machine_path", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(transaction.os, "lstat", Mock(side_effect=OSError("lstat race")))
    with pytest.raises(RuntimeError, match="nicht sicher geprüft"):
        transaction._validate_bundle_tree(
            bundle,
            executable_name="service.exe",
            require_executable=False,
        )

    monkeypatch.setattr(
        transaction.os,
        "lstat",
        lambda _path: SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=2, st_file_attributes=0),
    )
    with pytest.raises(RuntimeError, match="eindeutige reguläre Datei"):
        transaction._validate_bundle_tree(
            bundle,
            executable_name="service.exe",
            require_executable=False,
        )


@pytest.mark.parametrize(
    ("owned_service", "expected_state"),
    [
        (None, transaction.ServiceState.ABSENT),
        (({"metadata": True}, False), transaction.ServiceState.OWNED_STOPPED),
        (({"metadata": True}, True), transaction.ServiceState.OWNED_RUNNING),
    ],
)
def test_recovery_observation_rechecks_topology_around_scm_read(
    owned_service: object,
    expected_state: transaction.ServiceState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = transaction.BundleTopology(True, False, False, False)
    monkeypatch.setattr(transaction, "inspect_bundle_topology", Mock(return_value=topology))
    monkeypatch.setattr(
        transaction.windows_service_metadata,
        "inspect_owned_service_metadata",
        Mock(return_value=owned_service),
    )
    assert transaction.inspect_recovery_observation(EXPECTED_EXECUTABLE) == transaction.RecoveryObservation(
        topology,
        expected_state,
    )

    monkeypatch.setattr(
        transaction,
        "inspect_bundle_topology",
        Mock(side_effect=[topology, transaction.BundleTopology(False, False, False, False)]),
    )
    with pytest.raises(RuntimeError, match="während der Recovery-Inventur"):
        transaction.inspect_recovery_observation(EXPECTED_EXECUTABLE)


def test_canonical_json_rejects_unrepresentable_large_and_noncanonical_documents() -> None:
    with pytest.raises(RuntimeError, match="nicht als striktes JSON"):
        transaction._canonical_json({"value": object()})
    with pytest.raises(RuntimeError, match="unzulässige Größe"):
        transaction._canonical_json({"value": "x" * transaction.MAXIMUM_TRANSACTION_BYTES})
    for encoded in (b"", b"\xff", b"[]\n", b'{"b":1, "a":2}\n'):
        with pytest.raises(RuntimeError):
            transaction._decode_canonical_json(encoded)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update({"unknown": True}), "unbekanntes Format"),
        (lambda payload: payload.update({"schema_version": 99}), "Version"),
        (lambda payload: payload.update({"desktop_binding": []}), "Desktopbindung"),
        (lambda payload: payload.update({"expected_executable": r"C:\wrong\service.exe"}), "Dienstpfad"),
        (lambda payload: payload.update({"service_before": {}}), "Dienst-Baselineblock"),
        (lambda payload: payload.update({"machine_before": {}}), "Maschinen-Baselineblock"),
        (lambda payload: payload.update({"target": {}}), "Zielzustandsblock"),
    ],
)
def test_prepared_decoder_rejects_unknown_or_malformed_structural_blocks(
    mutation,
    message: str,
) -> None:
    payload = json.loads(_first_install_prepared_bytes())
    mutation(payload)
    with pytest.raises(RuntimeError, match=message):
        transaction._decode_prepared(
            transaction._canonical_json(payload),
            EXPECTED_EXECUTABLE,
        )


def test_prepared_decoder_enforces_service_and_token_baseline_invariants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(_first_install_prepared_bytes())
    payload["service_before"]["running"] = True
    with pytest.raises(RuntimeError, match="Erstinstallation"):
        transaction._decode_prepared(transaction._canonical_json(payload), EXPECTED_EXECUTABLE)

    payload = json.loads(_first_install_prepared_bytes())
    payload["service_before"] = {"existed": True, "running": False, "metadata": None}
    with pytest.raises(RuntimeError, match="SCM-Baseline"):
        transaction._decode_prepared(transaction._canonical_json(payload), EXPECTED_EXECUTABLE)

    monkeypatch.setattr(
        transaction.windows_service_metadata,
        "validate_service_metadata",
        lambda _path, metadata: metadata,
    )
    payload["service_before"]["metadata"] = {"baseline": True}
    payload["target"]["token_transfer_consent"] = True
    with pytest.raises(RuntimeError, match="Update-Transaktion"):
        transaction._decode_prepared(transaction._canonical_json(payload), EXPECTED_EXECUTABLE)

    payload = json.loads(_first_install_prepared_bytes())
    payload["machine_before"]["token"] = True
    payload["target"]["token_transfer_consent"] = True
    with pytest.raises(RuntimeError, match="Maschinentoken"):
        transaction._decode_prepared(transaction._canonical_json(payload), EXPECTED_EXECUTABLE)

    payload = json.loads(_first_install_prepared_bytes())
    payload["target"]["service_running"] = 1
    with pytest.raises(RuntimeError, match="boolesche"):
        transaction._decode_prepared(transaction._canonical_json(payload), EXPECTED_EXECUTABLE)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update({"unknown": True}), "unbekanntes Format"),
        (lambda payload: payload.update({"schema_version": 99}), "Version"),
        (lambda payload: payload.update({"transaction_id": "c" * 32}), "anderen Transaktion"),
        (lambda payload: payload.update({"prepared_sha256": "c" * 64}), "nicht an das PREPARED"),
        (lambda payload: payload.update({"phase": 1}), "unbekannte Phase"),
        (lambda payload: payload.update({"phase": "unknown"}), "unbekannte Phase"),
        (lambda payload: payload.update({"phase": "prepared"}), "PREPARED-Phase"),
    ],
)
def test_phase_decoder_rejects_unbound_or_nonterminal_markers(
    mutation,
    message: str,
) -> None:
    prepared = _first_install_prepared_bytes()
    payload = transaction._phase_payload(
        TRANSACTION_ID,
        transaction.TransactionPhase.COMMIT_STARTED,
        prepared,
    )
    mutation(payload)
    with pytest.raises(RuntimeError, match=message):
        transaction._decode_phase(
            transaction._canonical_json(payload),
            prepared,
            TRANSACTION_ID,
        )


def test_protected_store_initialization_requires_secure_fixed_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installation = tmp_path / "installation"
    state_directory = installation / ".installer-state"
    monkeypatch.setattr(
        transaction,
        "_transaction_directories",
        lambda _path: (installation, state_directory),
    )
    monkeypatch.setattr(
        transaction,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    assert (
        transaction._ProtectedTransactionStore(EXPECTED_EXECUTABLE, create=False).read(transaction.PREPARED_FILE_NAME)
        is None
    )
    with pytest.raises(RuntimeError, match="Installationsverzeichnis.*fehlt"):
        transaction._ProtectedTransactionStore(EXPECTED_EXECUTABLE, create=True)

    installation.write_bytes(b"unsafe")
    with pytest.raises(RuntimeError, match="unsicher"):
        transaction._ProtectedTransactionStore(EXPECTED_EXECUTABLE, create=False)


def test_protected_store_prepares_new_admin_directory_and_rejects_unknown_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installation = tmp_path / "installation"
    installation.mkdir()
    state_directory = installation / ".installer-state"
    monkeypatch.setattr(
        transaction,
        "_transaction_directories",
        lambda _path: (installation, state_directory),
    )
    monkeypatch.setattr(
        transaction,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    create_directory = Mock(side_effect=lambda path, _attributes: Path(path).mkdir())
    monkeypatch.setattr(
        transaction.windows_service_metadata,
        "_windows_file_modules",
        lambda: (None, None, SimpleNamespace(CreateDirectoryW=create_directory), None, None),
    )
    monkeypatch.setattr(
        transaction.windows_service_metadata,
        "_administrative_security_attributes",
        lambda *, directory: object(),
    )
    verify = Mock()
    monkeypatch.setattr(transaction.windows_service_metadata, "_verify_administrative_path", verify)

    store = transaction._ProtectedTransactionStore(EXPECTED_EXECUTABLE, create=True)
    assert state_directory.is_dir()
    create_directory.assert_called_once()
    with pytest.raises(RuntimeError, match="unbekannter Transaktionsdateiname"):
        store.read("foreign.json")


def test_atomic_publish_is_exclusive_and_wraps_filesystem_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"payload")
    monkeypatch.setattr(transaction.sys, "platform", "darwin")
    transaction._atomic_publish(source, destination)
    assert destination.read_bytes() == b"payload"
    assert not source.exists()

    source.write_bytes(b"second")
    with pytest.raises(FileExistsError):
        transaction._atomic_publish(source, destination)

    destination.unlink()
    monkeypatch.setattr(transaction.os, "link", Mock(side_effect=OSError("synthetic")))
    with pytest.raises(RuntimeError, match="nicht atomar veröffentlicht"):
        transaction._atomic_publish(source, destination)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"service_existed": 1}, "strikt boolesch"),
        ({"target_service_running": 1}, "strikt boolesch"),
        ({"service_existed": False, "service_running": True}, "nicht vorhandener Dienst"),
        ({"service_existed": True, "token_transfer_consent": True}, "Update-Transaktion"),
    ],
)
def test_prepare_rejects_inconsistent_boolean_service_inputs(
    options: dict[str, object],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: dict[str, object] = {
        "service_existed": False,
        "service_running": False,
        "target_service_running": True,
        "token_transfer_consent": False,
    }
    values.update(options)
    capture = Mock()
    monkeypatch.setattr(transaction.windows_service_metadata, "capture_service_metadata", capture)
    with pytest.raises(RuntimeError, match=message):
        transaction.prepare_transaction(
            EXPECTED_EXECUTABLE,
            transaction_id=TRANSACTION_ID,
            desktop_reader_sid=DESKTOP_READER_SID,
            desktop_seal_sha256=DESKTOP_SEAL_SHA256,
            machine_before=EMPTY_MACHINE_STATE,
            _state_store=_MemoryStore(),
            **values,  # type: ignore[arg-type]
        )
    capture.assert_not_called()


def test_prepare_and_phase_publish_fail_closed_on_competing_or_unstable_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RacingStore(_MemoryStore):
        def create(self, name: str, payload: bytes) -> None:
            self.files[name] = b"competing\n"
            raise FileExistsError(name)

    with pytest.raises(RuntimeError, match="konkurrierendes PREPARED"):
        _prepare_first(store=RacingStore())

    store, _state = _prepare_first()
    store.files[transaction.PHASE_FILE_NAME] = b"foreign\n"
    with pytest.raises(RuntimeError, match="bereits eine andere"):
        transaction._write_phase(
            EXPECTED_EXECUTABLE,
            transaction_id=TRANSACTION_ID,
            phase=transaction.TransactionPhase.COMMIT_STARTED,
            _state_store=store,
        )

    store.files.pop(transaction.PHASE_FILE_NAME)
    with pytest.raises(RuntimeError, match="Für PREPARED"):
        transaction._write_phase(
            EXPECTED_EXECUTABLE,
            transaction_id=TRANSACTION_ID,
            phase=transaction.TransactionPhase.PREPARED,
            _state_store=store,
        )

    with pytest.raises(RuntimeError, match="Transaktions-ID"):
        transaction._write_phase(
            EXPECTED_EXECUTABLE,
            transaction_id="c" * 32,
            phase=transaction.TransactionPhase.COMMIT_STARTED,
            _state_store=store,
        )


def test_load_clear_and_orphan_helpers_cover_absent_and_mismatched_records() -> None:
    store = _MemoryStore()
    assert transaction.load_transaction(EXPECTED_EXECUTABLE, _state_store=store) is None
    assert transaction.load_orphaned_completion_marker(EXPECTED_EXECUTABLE, _state_store=store) is None
    transaction.clear_orphaned_completion_marker(EXPECTED_EXECUTABLE, _state_store=store)

    store.files[transaction.PHASE_FILE_NAME] = transaction._canonical_json(
        {
            "schema_version": 1,
            "transaction_id": TRANSACTION_ID,
            "phase": transaction.TransactionPhase.COMMIT_STARTED.value,
            "prepared_sha256": "b" * 64,
        }
    )
    with pytest.raises(RuntimeError, match="verwaister Abschlussmarker"):
        transaction.load_transaction(EXPECTED_EXECUTABLE, _state_store=store)

    store.files[transaction.PREPARED_FILE_NAME] = _first_install_prepared_bytes()
    with pytest.raises(RuntimeError, match="nicht getrennt"):
        transaction.clear_orphaned_completion_marker(EXPECTED_EXECUTABLE, _state_store=store)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"schema_version": 1}, "unbekanntes Format"),
        (
            {
                "schema_version": 99,
                "transaction_id": TRANSACTION_ID,
                "phase": "commit_started",
                "prepared_sha256": "b" * 64,
            },
            "Version",
        ),
        (
            {
                "schema_version": 1,
                "transaction_id": TRANSACTION_ID,
                "phase": 1,
                "prepared_sha256": "b" * 64,
            },
            "terminale Phase",
        ),
        (
            {
                "schema_version": 1,
                "transaction_id": TRANSACTION_ID,
                "phase": "prepared",
                "prepared_sha256": "b" * 64,
            },
            "terminale Phase",
        ),
    ],
)
def test_orphan_marker_decoder_requires_a_supported_terminal_phase(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        transaction._decode_orphaned_completion_marker(transaction._canonical_json(payload))


def test_planner_covers_completed_and_forward_terminal_topologies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, prepared_state = _prepare_update(monkeypatch, running=False, target_running=False)
    rolled_back = transaction.TransactionState(
        prepared_state.prepared,
        transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE,
    )
    baseline = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, False),
        transaction.ServiceState.OWNED_STOPPED,
    )
    assert transaction.plan_recovery(rolled_back, baseline).direction is transaction.RecoveryDirection.COMPLETE
    with pytest.raises(RuntimeError, match="nicht vollständig"):
        transaction.plan_recovery(
            rolled_back,
            transaction.RecoveryObservation(
                transaction.BundleTopology(False, False, False, False),
                transaction.ServiceState.ABSENT,
            ),
        )

    committed_update = transaction.TransactionState(
        prepared_state.prepared,
        transaction.TransactionPhase.COMMIT_STARTED,
    )
    assert transaction.plan_recovery(committed_update, baseline).actions == ()
    with pytest.raises(RuntimeError, match="Zielzustand"):
        transaction.plan_recovery(
            committed_update,
            transaction.RecoveryObservation(
                baseline.bundles,
                transaction.ServiceState.OWNED_RUNNING,
            ),
        )

    _first_store, first = _prepare_first(target_running=True)
    committed_first = transaction.TransactionState(first.prepared, transaction.TransactionPhase.COMMIT_STARTED)
    first_proof = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, False),
        transaction.ServiceState.OWNED_RUNNING,
    )
    assert transaction.plan_recovery(committed_first, first_proof).actions == ()
    with pytest.raises(RuntimeError, match="unbekannten Bundlezustand"):
        transaction.plan_recovery(
            committed_first,
            transaction.RecoveryObservation(
                transaction.BundleTopology(True, True, False, False),
                transaction.ServiceState.OWNED_RUNNING,
            ),
        )


def test_executor_covers_new_rollback_move_and_rejects_malicious_plans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, state = _prepare_update(monkeypatch, running=False, target_running=False)
    observation = transaction.RecoveryObservation(
        transaction.BundleTopology(False, True, True, False),
        transaction.ServiceState.OWNED_STOPPED,
    )
    operations = _FakeOperations(observation)
    transaction.execute_recovery(
        EXPECTED_EXECUTABLE,
        state=state,
        plan=transaction.plan_recovery(state, observation),
        operations=operations,
        _state_store=store,
    )
    assert operations.calls == [
        ("delete", "new"),
        ("move", "rollback", "live"),
        ("restore", _metadata()),
    ]

    wrong_id = transaction.RecoveryPlan(
        "c" * 32,
        transaction.RecoveryDirection.ROLLBACK,
        operations.current,
        (),
    )
    with pytest.raises(RuntimeError, match="nicht zur selben"):
        transaction.execute_recovery(
            EXPECTED_EXECUTABLE,
            state=state,
            plan=wrong_id,
            operations=operations,
        )

    complete = transaction.RecoveryPlan(
        TRANSACTION_ID,
        transaction.RecoveryDirection.COMPLETE,
        operations.current,
        (),
    )
    transaction.execute_recovery(
        EXPECTED_EXECUTABLE,
        state=state,
        plan=complete,
        operations=Mock(side_effect=AssertionError),
    )

    missing_metadata = transaction.TransactionState(
        transaction.PreparedTransaction(
            state.prepared.transaction_id,
            state.prepared.desktop_reader_sid,
            state.prepared.desktop_seal_sha256,
            state.prepared.expected_executable,
            True,
            False,
            None,
            state.prepared.machine_before,
            False,
            False,
        ),
        transaction.TransactionPhase.PREPARED,
    )
    malicious = transaction.RecoveryPlan(
        TRANSACTION_ID,
        transaction.RecoveryDirection.ROLLBACK,
        operations.current,
        (transaction.RecoveryAction.RESTORE_SERVICE_METADATA,),
    )
    with pytest.raises(RuntimeError, match="SCM-Baseline"):
        transaction.execute_recovery(
            EXPECTED_EXECUTABLE,
            state=missing_metadata,
            plan=malicious,
            operations=operations,
        )


def test_finalize_rejects_prepared_or_mismatched_terminal_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _state = _prepare_update(monkeypatch, running=False, target_running=False)
    baseline = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, False),
        transaction.ServiceState.OWNED_STOPPED,
    )
    with pytest.raises(RuntimeError, match="nicht abgeschlossene"):
        transaction.finalize_transaction(
            EXPECTED_EXECUTABLE,
            transaction_id=TRANSACTION_ID,
            observation=baseline,
            _state_store=store,
        )

    transaction._write_phase(
        EXPECTED_EXECUTABLE,
        transaction_id=TRANSACTION_ID,
        phase=transaction.TransactionPhase.SERVICE_ROLLBACK_COMPLETE,
        _state_store=store,
    )
    with pytest.raises(RuntimeError, match="finale Dienstzustand"):
        transaction.finalize_transaction(
            EXPECTED_EXECUTABLE,
            transaction_id=TRANSACTION_ID,
            observation=transaction.RecoveryObservation(
                transaction.BundleTopology(False, False, False, False),
                transaction.ServiceState.ABSENT,
            ),
            _state_store=store,
        )
