from __future__ import annotations

import builtins
import stat
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import windows_install_recovery as recovery
from app import windows_install_transaction as transaction
from app.windows_service_config import SERVICE_ACCOUNT, SERVICE_NAME

EXPECTED_EXECUTABLE = Path(r"C:\Program Files\E-Rechnungs-Pruefer-Dienst\service\E-Rechnungs-Pruefer-Dienst.exe")


class _WindowsError(Exception):
    def __init__(self, winerror: int) -> None:
        super().__init__(winerror)
        self.winerror = winerror


class _FakeWin32Service:
    SC_MANAGER_CONNECT = 1
    SERVICE_QUERY_CONFIG = 2
    SERVICE_QUERY_STATUS = 4
    SERVICE_STOP = 8
    SERVICE_START = 16
    SERVICE_CONTROL_STOP = 1
    SERVICE_STOPPED = 1
    SERVICE_START_PENDING = 2
    SERVICE_STOP_PENDING = 3
    SERVICE_RUNNING = 4

    def __init__(
        self,
        expected_executable: Path,
        *,
        present: bool = True,
        image_path: str | None = None,
        account: str = SERVICE_ACCOUNT,
        state: int = SERVICE_STOPPED,
    ) -> None:
        self.present = present
        self.image_path = image_path or f'"{expected_executable}"'
        self.account = account
        self.state = state
        self.opened_names: list[str] = []
        self.control_calls = 0
        self.start_calls = 0
        self.delete_calls = 0
        self.keep_stop_pending = False
        self.keep_start_pending = False

    def OpenSCManager(self, machine: object, database: object, access: int) -> object:
        assert machine is None
        assert database is None
        assert access == self.SC_MANAGER_CONNECT
        return object()

    def OpenService(self, manager: object, name: str, access: int) -> object:
        del manager, access
        self.opened_names.append(name)
        if not self.present:
            raise _WindowsError(recovery.ERROR_SERVICE_DOES_NOT_EXIST)
        return object()

    def QueryServiceConfig(self, service: object) -> tuple[object, ...]:
        del service
        return (0, 2, 0, self.image_path, None, 0, None, self.account, None)

    def QueryServiceStatus(self, service: object) -> tuple[int, ...]:
        del service
        return (0, self.state, 0, 0, 0, 0, 0)

    def ControlService(self, service: object, control: int) -> None:
        del service
        assert control == self.SERVICE_CONTROL_STOP
        self.control_calls += 1
        self.state = self.SERVICE_STOP_PENDING if self.keep_stop_pending else self.SERVICE_STOPPED

    def StartService(self, service: object, arguments: object) -> None:
        del service
        assert arguments is None
        self.start_calls += 1
        self.state = self.SERVICE_START_PENDING if self.keep_start_pending else self.SERVICE_RUNNING

    def DeleteService(self, service: object) -> None:
        del service
        self.delete_calls += 1
        self.present = False

    def CloseServiceHandle(self, handle: object) -> None:
        del handle


def _layout(root: Path) -> recovery.InstallationLayout:
    return recovery.InstallationLayout(
        root=root,
        live=root / "service",
        new=root / "service.new",
        rollback=root / "service.rollback",
        obsolete=root / "service.obsolete",
    )


def _make_operations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    service_state: transaction.ServiceState = transaction.ServiceState.OWNED_STOPPED,
) -> tuple[recovery.WindowsInstallRecoveryOperations, recovery.InstallationLayout]:
    layout = _layout(tmp_path / "E-Rechnungs-Pruefer-Dienst")
    layout.root.mkdir()
    expected = layout.live / recovery.SERVICE_EXECUTABLE_NAME
    monkeypatch.setattr(recovery, "_canonical_layout", Mock(return_value=layout))
    monkeypatch.setattr(recovery, "_observe_service", Mock(return_value=service_state))
    return recovery.WindowsInstallRecoveryOperations(expected), layout


def _create_owned_bundle(path: Path) -> None:
    path.mkdir()
    (path / recovery.SERVICE_EXECUTABLE_NAME).write_bytes(b"MZ")
    nested = path / "assets"
    nested.mkdir()
    (nested / "runtime.dat").write_bytes(b"synthetic")


def test_layout_is_bound_to_fixed_64_bit_program_files_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validate = Mock(return_value=True)
    monkeypatch.setattr(
        recovery,
        "_windows_program_files_directory",
        Mock(return_value=Path(r"C:\Program Files")),
    )
    monkeypatch.setattr(recovery, "validate_machine_path", validate)

    layout = recovery._canonical_layout(EXPECTED_EXECUTABLE)

    assert str(layout.root) == r"C:\Program Files\E-Rechnungs-Pruefer-Dienst"
    assert str(layout.live) == r"C:\Program Files\E-Rechnungs-Pruefer-Dienst\service"
    assert str(layout.new) == r"C:\Program Files\E-Rechnungs-Pruefer-Dienst\service.new"
    assert validate.call_count == 2

    with pytest.raises(RuntimeError, match="festen 64-Bit-Program-Files"):
        recovery._canonical_layout(
            Path(r"D:\Program Files\E-Rechnungs-Pruefer-Dienst\service\E-Rechnungs-Pruefer-Dienst.exe")
        )


def test_observe_inventories_exact_bundle_slots_and_reports_incomplete_fixed_bundles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations, layout = _make_operations(monkeypatch, tmp_path)
    _create_owned_bundle(layout.live)
    layout.new.mkdir()
    (layout.new / "partially-extracted.bin").write_bytes(b"partial")
    _create_owned_bundle(layout.rollback)

    observation = operations.observe()

    assert observation == transaction.RecoveryObservation(
        bundles=transaction.BundleTopology(
            live=True,
            new=True,
            rollback=True,
            obsolete=False,
        ),
        service=transaction.ServiceState.OWNED_STOPPED,
    )

    (layout.live / recovery.SERVICE_EXECUTABLE_NAME).unlink()
    incomplete = operations.observe()
    assert incomplete.bundles.live is True
    assert incomplete.incomplete_bundles == frozenset({"live"})


def test_observe_rejects_reparse_or_symlink_inside_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations, layout = _make_operations(monkeypatch, tmp_path)
    _create_owned_bundle(layout.live)
    target = tmp_path / "outside"
    target.mkdir()
    link = layout.live / "redirect"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks stehen in dieser Testumgebung nicht zur Verfügung.")

    with pytest.raises(RuntimeError, match="Reparse-Point|Symlink"):
        operations.observe()


@pytest.mark.parametrize(
    ("present", "image_path", "account", "native_state", "expected"),
    [
        (
            False,
            None,
            SERVICE_ACCOUNT,
            _FakeWin32Service.SERVICE_STOPPED,
            transaction.ServiceState.ABSENT,
        ),
        (
            True,
            None,
            SERVICE_ACCOUNT,
            _FakeWin32Service.SERVICE_STOPPED,
            transaction.ServiceState.OWNED_STOPPED,
        ),
        (
            True,
            None,
            SERVICE_ACCOUNT,
            _FakeWin32Service.SERVICE_RUNNING,
            transaction.ServiceState.OWNED_RUNNING,
        ),
        (
            True,
            r'"C:\Program Files\Fremd\fremd.exe"',
            SERVICE_ACCOUNT,
            _FakeWin32Service.SERVICE_STOPPED,
            transaction.ServiceState.FOREIGN,
        ),
        (
            True,
            None,
            r"NT AUTHORITY\SYSTEM",
            _FakeWin32Service.SERVICE_STOPPED,
            transaction.ServiceState.FOREIGN,
        ),
        (
            True,
            None,
            SERVICE_ACCOUNT,
            _FakeWin32Service.SERVICE_STOP_PENDING,
            transaction.ServiceState.UNSTABLE,
        ),
    ],
)
def test_scm_observation_classifies_exact_name_image_account_and_stable_state(
    monkeypatch: pytest.MonkeyPatch,
    present: bool,
    image_path: str | None,
    account: str,
    native_state: int,
    expected: transaction.ServiceState,
) -> None:
    native = _FakeWin32Service(
        EXPECTED_EXECUTABLE,
        present=present,
        image_path=image_path,
        account=account,
        state=native_state,
    )
    monkeypatch.setattr(recovery, "_win32service", Mock(return_value=native))

    assert recovery._observe_service(EXPECTED_EXECUTABLE) is expected
    assert native.opened_names == [SERVICE_NAME]


def test_bundle_delete_and_move_are_exact_idempotent_and_detect_toctou(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations, layout = _make_operations(monkeypatch, tmp_path)
    _create_owned_bundle(layout.live)
    layout.new.mkdir()
    (layout.new / "partial.bin").write_bytes(b"partial")
    _create_owned_bundle(layout.rollback)
    operations.observe()

    (layout.new / "changed-after-plan.bin").write_bytes(b"race")
    with pytest.raises(RuntimeError, match="seit der Recovery-Planung"):
        operations.delete_bundle("new")
    assert layout.new.exists()

    operations.observe()
    operations.delete_bundle("new")
    operations.delete_bundle("new")
    assert not layout.new.exists()

    operations.delete_bundle("live")
    operations.move_bundle("rollback", "live")
    operations.move_bundle("rollback", "live")
    assert layout.live.is_dir()
    assert not layout.rollback.exists()
    assert (layout.live / recovery.SERVICE_EXECUTABLE_NAME).read_bytes() == b"MZ"

    with pytest.raises(RuntimeError, match="nicht zulässig"):
        operations.move_bundle("new", "live")


def test_partial_regular_bundle_tree_can_be_reinventoried_and_deleted_after_power_loss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations, layout = _make_operations(monkeypatch, tmp_path)
    _create_owned_bundle(layout.live)
    _create_owned_bundle(layout.obsolete)
    (layout.obsolete / recovery.SERVICE_EXECUTABLE_NAME).unlink()

    observation = operations.observe()

    assert observation == transaction.RecoveryObservation(
        bundles=transaction.BundleTopology(True, False, False, True),
        service=transaction.ServiceState.OWNED_STOPPED,
        incomplete_bundles=frozenset({"obsolete"}),
    )
    operations.delete_bundle("obsolete")
    assert not layout.obsolete.exists()
    assert layout.live.exists()


@pytest.mark.parametrize("service_executable_remains", [False, True])
def test_committed_forward_recovery_executes_partial_obsolete_delete_to_completion(
    service_executable_remains: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations, layout = _make_operations(monkeypatch, tmp_path)
    _create_owned_bundle(layout.live)
    if service_executable_remains:
        _create_owned_bundle(layout.obsolete)
        (layout.obsolete / "assets" / "runtime.dat").unlink()
        (layout.obsolete / "assets").rmdir()
    else:
        layout.obsolete.mkdir()
        (layout.obsolete / "remaining-after-power-loss.bin").write_bytes(b"partial")
    state = transaction.TransactionState(
        prepared=transaction.PreparedTransaction(
            transaction_id="a" * 32,
            desktop_reader_sid="S-1-5-21-1000",
            desktop_seal_sha256="b" * 64,
            expected_executable=str(operations.expected_executable),
            service_existed=True,
            service_running=False,
            service_metadata={"synthetic": "baseline"},
            machine_before=transaction.MachineBefore(True, True, True),
            target_service_running=False,
            token_transfer_consent=False,
        ),
        phase=transaction.TransactionPhase.COMMIT_STARTED,
    )
    observation = operations.observe()
    plan = transaction.plan_recovery(state, observation)
    expected_incomplete = frozenset() if service_executable_remains else frozenset({"obsolete"})

    assert observation.incomplete_bundles == expected_incomplete
    assert plan.actions == (transaction.RecoveryAction.DELETE_OBSOLETE,)
    transaction.execute_recovery(
        operations.expected_executable,
        state=state,
        plan=plan,
        operations=operations,
    )

    assert not layout.obsolete.exists()
    assert layout.live.exists()


def test_partial_backup_is_never_accepted_as_atomic_move_source_or_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations, layout = _make_operations(monkeypatch, tmp_path)
    layout.rollback.mkdir()
    (layout.rollback / "remaining.bin").write_bytes(b"partial")
    operations.observe()

    with pytest.raises(RuntimeError, match="vollständiges altes Backup"):
        operations.move_bundle("rollback", "live")
    assert layout.rollback.exists()
    assert not layout.live.exists()

    layout.rollback.rename(layout.live)
    operations.observe()
    with pytest.raises(RuntimeError, match="bereits verschobene Live-Bundle"):
        operations.move_bundle("rollback", "live")


def test_live_bundle_mutation_rejects_running_service_but_obsolete_cleanup_is_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations, layout = _make_operations(
        monkeypatch,
        tmp_path,
        service_state=transaction.ServiceState.OWNED_RUNNING,
    )
    _create_owned_bundle(layout.live)
    _create_owned_bundle(layout.obsolete)
    operations.observe()

    with pytest.raises(RuntimeError, match="laufenden Dienstes"):
        operations.delete_bundle("live")

    operations.delete_bundle("obsolete")
    assert layout.live.exists()
    assert not layout.obsolete.exists()


def test_metadata_restore_and_machine_purge_use_only_secure_public_helpers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations, _layout_value = _make_operations(monkeypatch, tmp_path)
    states: Iterator[transaction.ServiceState] = iter(
        (
            transaction.ServiceState.OWNED_STOPPED,
            transaction.ServiceState.OWNED_STOPPED,
            transaction.ServiceState.ABSENT,
            transaction.ServiceState.ABSENT,
        )
    )
    monkeypatch.setattr(operations, "_service_state", lambda: next(states))
    restore = Mock()
    purge = Mock()
    monkeypatch.setattr(recovery.windows_service_metadata, "restore_service_metadata_payload", restore)
    monkeypatch.setattr(recovery.windows_service_preflight, "purge_machine_state", purge)
    payload = {"synthetic": "baseline"}

    operations.restore_service_metadata(payload)
    operations.purge_machine_state()

    restore.assert_called_once_with(operations.expected_executable, payload)
    purge.assert_called_once_with()


def test_service_actions_revalidate_ownership_and_wait_for_stable_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path / "E-Rechnungs-Pruefer-Dienst")
    layout.root.mkdir()
    expected = layout.live / recovery.SERVICE_EXECUTABLE_NAME
    monkeypatch.setattr(recovery, "_canonical_layout", Mock(return_value=layout))
    native = _FakeWin32Service(expected, state=_FakeWin32Service.SERVICE_RUNNING)
    monkeypatch.setattr(recovery, "_win32service", Mock(return_value=native))
    operations = recovery.WindowsInstallRecoveryOperations(
        expected,
        wait_seconds=1,
        poll_seconds=0.1,
    )

    operations.stop_service()
    operations.stop_service()
    assert native.control_calls == 1
    assert native.state == native.SERVICE_STOPPED

    operations.start_service()
    operations.start_service()
    assert native.start_calls == 1
    assert native.state == native.SERVICE_RUNNING

    operations.stop_service()
    operations.delete_service()
    operations.delete_service()
    assert native.delete_calls == 1
    assert native.present is False
    assert all(name == SERVICE_NAME for name in native.opened_names)


def test_service_wait_is_bounded_and_foreign_service_is_never_mutated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path / "E-Rechnungs-Pruefer-Dienst")
    layout.root.mkdir()
    expected = layout.live / recovery.SERVICE_EXECUTABLE_NAME
    monkeypatch.setattr(recovery, "_canonical_layout", Mock(return_value=layout))
    native = _FakeWin32Service(expected, state=_FakeWin32Service.SERVICE_RUNNING)
    native.keep_stop_pending = True
    monkeypatch.setattr(recovery, "_win32service", Mock(return_value=native))
    now = [0.0]

    def advance(seconds: float) -> None:
        now[0] += seconds

    operations = recovery.WindowsInstallRecoveryOperations(
        expected,
        wait_seconds=0.5,
        poll_seconds=0.25,
        _clock=lambda: now[0],
        _sleep=advance,
    )
    with pytest.raises(RuntimeError, match="begrenzten|rechtzeitig"):
        operations.stop_service()
    assert now[0] == pytest.approx(0.5)

    native.keep_stop_pending = False
    native.state = native.SERVICE_STOPPED
    native.image_path = r'"C:\Program Files\Fremd\fremd.exe"'
    with pytest.raises(RuntimeError, match="fremd"):
        operations.start_service()
    assert native.start_calls == 0


def test_layout_slot_and_windows_boundaries_reject_unknown_or_nonwindows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    assert layout.bundle("live") == layout.live
    with pytest.raises(RuntimeError, match="unbekannter"):
        layout.bundle("foreign")

    assert isinstance(recovery.sys_platform(), str)
    monkeypatch.setattr(recovery, "sys_platform", lambda: "darwin")
    with pytest.raises(OSError, match="nur unter Windows"):
        recovery._windows_program_files_directory()
    with pytest.raises(OSError, match="nur unter Windows"):
        recovery._win32service()


def test_win32service_reports_missing_native_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recovery, "sys_platform", lambda: "win32")
    original_import = builtins.__import__

    def missing_pywin32(name: str, *args: object, **kwargs: object) -> object:
        if name == "win32service":
            raise ImportError("synthetic")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_pywin32)
    with pytest.raises(RuntimeError, match="pywin32 fehlt"):
        recovery._win32service()


@pytest.mark.parametrize(
    "invalid",
    [
        Path("relative.exe"),
        Path(r"C:\Program Files\service\bad.exe\..\bad.exe"),
        Path('C:\\Program Files\\service\\"bad.exe'),
    ],
)
def test_canonical_windows_path_rejects_noncanonical_values(invalid: Path) -> None:
    with pytest.raises(RuntimeError, match="kanonischer Windows-Pfad"):
        recovery._canonical_windows_path(invalid, description="Testpfad")


@pytest.mark.parametrize("missing", ["program-files", "root"])
def test_layout_requires_secure_existing_program_files_and_install_root(
    missing: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program_files = Path(r"C:\Program Files")
    monkeypatch.setattr(recovery, "_windows_program_files_directory", lambda: program_files)

    def validate(path: Path, *, directory: bool) -> bool:
        assert directory is True
        if missing == "program-files":
            return False
        return str(path).casefold() == str(program_files).casefold()

    monkeypatch.setattr(recovery, "validate_machine_path", validate)
    with pytest.raises(RuntimeError, match="Programmdateipfad fehlt|Installationsverzeichnis fehlt"):
        recovery._canonical_layout(EXPECTED_EXECUTABLE)


def test_service_classifier_rejects_malformed_or_unreadable_native_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _FakeWin32Service(EXPECTED_EXECUTABLE)
    native.QueryServiceConfig = Mock(return_value=(1, 2))
    assert recovery._classify_service_handle(object(), native, EXPECTED_EXECUTABLE) is transaction.ServiceState.UNSTABLE

    native.QueryServiceConfig = Mock(side_effect=OSError("synthetic"))
    with pytest.raises(RuntimeError, match="Dienstkonfiguration"):
        recovery._classify_service_handle(object(), native, EXPECTED_EXECUTABLE)

    native.QueryServiceConfig = Mock(
        return_value=(0, 2, 0, f'"{EXPECTED_EXECUTABLE}"', None, 0, None, SERVICE_ACCOUNT, None)
    )
    native.QueryServiceStatus = Mock(side_effect=OSError("synthetic"))
    with pytest.raises(RuntimeError, match="Dienststatus"):
        recovery._classify_service_handle(object(), native, EXPECTED_EXECUTABLE)

    for malformed in ((0,), (0, True), (0, "4")):
        native.QueryServiceStatus = Mock(return_value=malformed)
        assert (
            recovery._classify_service_handle(object(), native, EXPECTED_EXECUTABLE)
            is transaction.ServiceState.UNSTABLE
        )


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        (recovery.ERROR_SERVICE_MARKED_FOR_DELETE, transaction.ServiceState.UNSTABLE),
        (5, None),
    ],
)
def test_service_observation_handles_marked_for_delete_and_unknown_open_errors(
    error_code: int,
    expected: transaction.ServiceState | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _FakeWin32Service(EXPECTED_EXECUTABLE)
    native.OpenService = Mock(side_effect=_WindowsError(error_code))
    native.CloseServiceHandle = Mock()
    monkeypatch.setattr(recovery, "_win32service", lambda: native)

    if expected is None:
        with pytest.raises(RuntimeError, match="nicht sicher geöffnet"):
            recovery._observe_service(EXPECTED_EXECUTABLE)
    else:
        assert recovery._observe_service(EXPECTED_EXECUTABLE) is expected
    native.CloseServiceHandle.assert_called_once()


def test_path_record_rejects_lstat_race_hardlinks_and_special_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "entry"
    monkeypatch.setattr(recovery.os, "lstat", Mock(side_effect=OSError("gone")))
    with pytest.raises(RuntimeError, match="geändert"):
        recovery._path_record(candidate, tmp_path)

    monkeypatch.setattr(
        recovery.os,
        "lstat",
        lambda _path: SimpleNamespace(
            st_mode=stat.S_IFREG,
            st_file_attributes=0,
            st_nlink=2,
            st_dev=1,
            st_ino=2,
            st_size=3,
        ),
    )
    with pytest.raises(RuntimeError, match="Hardlink"):
        recovery._path_record(candidate, tmp_path)

    monkeypatch.setattr(
        recovery.os,
        "lstat",
        lambda _path: SimpleNamespace(
            st_mode=stat.S_IFIFO,
            st_file_attributes=0,
            st_nlink=1,
            st_dev=1,
            st_ino=2,
            st_size=3,
        ),
    )
    with pytest.raises(RuntimeError, match="unzulässigen Dateityp"):
        recovery._path_record(candidate, tmp_path)


def test_tree_inventory_and_required_executable_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    monkeypatch.setattr(recovery, "validate_machine_path", lambda *_args, **_kwargs: False)
    with pytest.raises(RuntimeError, match="Bundle fehlt"):
        recovery._secure_tree_snapshot(root)

    monkeypatch.setattr(recovery, "validate_machine_path", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(recovery.os, "scandir", Mock(side_effect=OSError("synthetic")))
    with pytest.raises(RuntimeError, match="vollständig inventarisiert"):
        recovery._secure_tree_snapshot(root)

    monkeypatch.undo()
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(
        recovery,
        "validate_machine_path",
        lambda path, *, directory: path.exists() and path.is_dir() == directory,
    )
    with pytest.raises(RuntimeError, match="erwartete eigene Dienstdatei"):
        recovery._bundle_snapshot(root, require_executable=True)


def test_remove_secure_tree_rechecks_snapshot_and_reports_delete_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    _create_owned_bundle(root)
    snapshot = recovery._secure_tree_snapshot(root)
    (root / "race.bin").write_bytes(b"race")
    with pytest.raises(RuntimeError, match="vor dem Löschen verändert"):
        recovery._remove_secure_tree(root, snapshot)

    snapshot = recovery._secure_tree_snapshot(root)
    monkeypatch.setattr(Path, "unlink", Mock(side_effect=OSError("denied")))
    with pytest.raises(RuntimeError, match="konnte nicht sicher gelöscht"):
        recovery._remove_secure_tree(root, snapshot)


@pytest.mark.parametrize(
    ("wait_seconds", "poll_seconds"),
    [
        (True, 0.1),
        (1, False),
        (float("nan"), 0.1),
        (1, float("inf")),
        (0, 0.1),
        (1, 2),
    ],
)
def test_recovery_adapter_rejects_unbounded_or_invalid_waits(
    wait_seconds: object,
    poll_seconds: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = Mock(side_effect=AssertionError("layout must not be read"))
    monkeypatch.setattr(recovery, "_canonical_layout", canonical)
    with pytest.raises(RuntimeError, match="Dienstwartezeit"):
        recovery.WindowsInstallRecovery(
            EXPECTED_EXECUTABLE,
            wait_seconds=wait_seconds,  # type: ignore[arg-type]
            poll_seconds=poll_seconds,  # type: ignore[arg-type]
        )
    canonical.assert_not_called()


def test_adapter_revalidates_its_fixed_layout_before_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = _layout(tmp_path / "first")
    second = _layout(tmp_path / "second")
    canonical = Mock(side_effect=[first, second])
    monkeypatch.setattr(recovery, "_canonical_layout", canonical)
    operations = recovery.WindowsInstallRecovery(EXPECTED_EXECUTABLE)
    with pytest.raises(RuntimeError, match="unerwartet geändert"):
        operations.observe()


@pytest.mark.parametrize("failure", ["missing", "unknown", "classify"])
def test_open_owned_service_closes_handles_on_every_failure(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations, _layout_value = _make_operations(monkeypatch, tmp_path)
    native = _FakeWin32Service(operations.expected_executable)
    if failure == "missing":
        native.OpenService = Mock(side_effect=_WindowsError(recovery.ERROR_SERVICE_DOES_NOT_EXIST))
    elif failure == "unknown":
        native.OpenService = Mock(side_effect=_WindowsError(5))
    else:
        native.QueryServiceConfig = Mock(side_effect=OSError("synthetic"))
    native.CloseServiceHandle = Mock()
    monkeypatch.setattr(recovery, "_win32service", lambda: native)

    with pytest.raises(RuntimeError):
        operations._open_owned_service(native.SERVICE_QUERY_CONFIG)
    assert native.CloseServiceHandle.call_count >= 1


@pytest.mark.parametrize(
    ("states", "message"),
    [
        (
            [transaction.ServiceState.FOREIGN],
            "Eigentümerschaft",
        ),
        (
            [transaction.ServiceState.OWNED_STOPPED],
            "unerwarteten Zustand",
        ),
    ],
)
def test_wait_for_owned_state_rejects_foreign_or_wrong_stable_state(
    states: list[transaction.ServiceState],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations, _layout_value = _make_operations(monkeypatch, tmp_path)
    monkeypatch.setattr(recovery, "_classify_service_handle", Mock(side_effect=states))
    with pytest.raises(RuntimeError, match=message):
        operations._wait_for_owned_state(
            object(),
            object(),
            transaction.ServiceState.OWNED_RUNNING,
        )


def test_start_stop_native_errors_are_wrapped_but_benign_races_are_tolerated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path / "E-Rechnungs-Pruefer-Dienst")
    layout.root.mkdir()
    expected = layout.live / recovery.SERVICE_EXECUTABLE_NAME
    monkeypatch.setattr(recovery, "_canonical_layout", Mock(return_value=layout))
    native = _FakeWin32Service(expected, state=_FakeWin32Service.SERVICE_RUNNING)
    monkeypatch.setattr(recovery, "_win32service", lambda: native)
    operations = recovery.WindowsInstallRecovery(expected, wait_seconds=1, poll_seconds=0.1)

    native.ControlService = Mock(side_effect=_WindowsError(5))
    with pytest.raises(RuntimeError, match="kontrolliert gestoppt"):
        operations.stop_service()

    native.state = native.SERVICE_STOPPED
    native.StartService = Mock(side_effect=_WindowsError(5))
    with pytest.raises(RuntimeError, match="kontrolliert gestartet"):
        operations.start_service()

    def already_running(_service: object, _arguments: object) -> None:
        native.state = native.SERVICE_RUNNING
        raise _WindowsError(recovery.ERROR_SERVICE_ALREADY_RUNNING)

    native.StartService = already_running
    operations.start_service()
    assert native.state == native.SERVICE_RUNNING


def test_delete_service_rejects_running_and_wraps_native_delete_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path / "E-Rechnungs-Pruefer-Dienst")
    layout.root.mkdir()
    expected = layout.live / recovery.SERVICE_EXECUTABLE_NAME
    monkeypatch.setattr(recovery, "_canonical_layout", Mock(return_value=layout))
    native = _FakeWin32Service(expected, state=_FakeWin32Service.SERVICE_RUNNING)
    monkeypatch.setattr(recovery, "_win32service", lambda: native)
    operations = recovery.WindowsInstallRecovery(expected, wait_seconds=1, poll_seconds=0.1)
    with pytest.raises(RuntimeError, match="gestoppter eigener Dienst"):
        operations.delete_service()

    native.state = native.SERVICE_STOPPED
    native.DeleteService = Mock(side_effect=_WindowsError(5))
    with pytest.raises(RuntimeError, match="kontrolliert gelöscht"):
        operations.delete_service()


def test_wait_until_service_absent_rejects_unsafe_state_and_times_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations, _layout_value = _make_operations(monkeypatch, tmp_path)
    monkeypatch.setattr(
        operations,
        "_service_state",
        lambda: transaction.ServiceState.OWNED_RUNNING,
    )
    with pytest.raises(RuntimeError, match="unsicheren Zustand"):
        operations._wait_until_service_absent()

    now = [0.0]
    operations._clock = lambda: now[0]
    operations._sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
    operations._wait_seconds = 0.5
    operations._poll_seconds = 0.25
    monkeypatch.setattr(
        operations,
        "_service_state",
        lambda: transaction.ServiceState.OWNED_STOPPED,
    )
    with pytest.raises(RuntimeError, match="begrenzten Wartezeit"):
        operations._wait_until_service_absent()


@pytest.mark.parametrize(
    "current",
    [
        transaction.ServiceState.FOREIGN,
        transaction.ServiceState.UNSTABLE,
        transaction.ServiceState.OWNED_RUNNING,
    ],
)
def test_bundle_mutation_rejects_state_races_foreign_and_running_live(
    current: transaction.ServiceState,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations, _layout_value = _make_operations(monkeypatch, tmp_path)
    operations._observed_service_state = transaction.ServiceState.OWNED_STOPPED
    monkeypatch.setattr(operations, "_service_state", lambda: current)
    with pytest.raises(RuntimeError, match="verändert|fremder|laufenden"):
        operations._require_safe_service_for_bundle_mutation(live=True)


def test_move_bundle_rejects_ambiguous_destination_and_atomic_rename_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations, layout = _make_operations(monkeypatch, tmp_path)
    _create_owned_bundle(layout.live)
    _create_owned_bundle(layout.rollback)
    operations.observe()
    with pytest.raises(RuntimeError, match="gleichzeitig vorhanden"):
        operations.move_bundle("rollback", "live")

    operations.delete_bundle("live")
    monkeypatch.setattr(recovery.os, "rename", Mock(side_effect=OSError("synthetic")))
    with pytest.raises(RuntimeError, match="nicht atomar"):
        operations.move_bundle("rollback", "live")


@pytest.mark.parametrize(
    ("method", "states", "message"),
    [
        (
            "restore",
            [transaction.ServiceState.OWNED_RUNNING],
            "SCM-Metadaten",
        ),
        (
            "restore",
            [
                transaction.ServiceState.OWNED_STOPPED,
                transaction.ServiceState.OWNED_RUNNING,
            ],
            "nach der SCM-Restaurierung",
        ),
        (
            "purge",
            [transaction.ServiceState.OWNED_STOPPED],
            "erst nach",
        ),
        (
            "purge",
            [
                transaction.ServiceState.ABSENT,
                transaction.ServiceState.OWNED_STOPPED,
            ],
            "unerwartet ein Dienst",
        ),
    ],
)
def test_metadata_and_machine_mutations_require_stable_pre_and_postconditions(
    method: str,
    states: list[transaction.ServiceState],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations, _layout_value = _make_operations(monkeypatch, tmp_path)
    iterator = iter(states)
    monkeypatch.setattr(operations, "_service_state", lambda: next(iterator))
    monkeypatch.setattr(recovery.windows_service_metadata, "restore_service_metadata_payload", Mock())
    monkeypatch.setattr(recovery.windows_service_preflight, "purge_machine_state", Mock())
    with pytest.raises(RuntimeError, match=message):
        if method == "restore":
            operations.restore_service_metadata({"baseline": True})
        else:
            operations.purge_machine_state()


def test_public_recovery_helpers_observe_execute_and_require_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = transaction.RecoveryObservation(
        transaction.BundleTopology(True, False, False, False),
        transaction.ServiceState.OWNED_STOPPED,
    )
    operations = Mock()
    operations.observe.return_value = observation
    constructor = Mock(return_value=operations)
    monkeypatch.setattr(recovery, "WindowsInstallRecovery", constructor)
    assert recovery.observe_installation(EXPECTED_EXECUTABLE) == observation

    monkeypatch.setattr(
        recovery.windows_install_transaction,
        "load_transaction",
        Mock(return_value=None),
    )
    with pytest.raises(RuntimeError, match="keine Recovery-Transaktion"):
        recovery.execute_install_recovery(
            EXPECTED_EXECUTABLE,
            transaction_id="a" * 32,
        )

    state = transaction.TransactionState(
        transaction.PreparedTransaction(
            "a" * 32,
            "S-1-5-21-1000",
            "b" * 64,
            str(EXPECTED_EXECUTABLE),
            True,
            False,
            {"baseline": True},
            transaction.MachineBefore(True, True, True),
            False,
            False,
        ),
        transaction.TransactionPhase.COMMIT_STARTED,
    )
    plan = transaction.RecoveryPlan(
        "a" * 32,
        transaction.RecoveryDirection.FORWARD,
        observation,
        (),
    )
    monkeypatch.setattr(
        recovery.windows_install_transaction,
        "load_transaction",
        Mock(return_value=state),
    )
    monkeypatch.setattr(
        recovery.windows_install_transaction,
        "plan_recovery",
        Mock(return_value=plan),
    )
    execute = Mock()
    monkeypatch.setattr(recovery.windows_install_transaction, "execute_recovery", execute)
    assert (
        recovery.execute_install_recovery(
            EXPECTED_EXECUTABLE,
            transaction_id="a" * 32,
        )
        == plan
    )
    execute.assert_called_once()
