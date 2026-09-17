from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import processing_lifecycle_probe as probe


def test_fixed_platform_catalog_has_no_arbitrary_target() -> None:
    assert probe.cases_for_platform("linux") == ("cancel", "disconnect", "worker", "supervisor", "parent")
    assert probe.cases_for_platform("win32") == probe.cases_for_platform("linux")
    assert probe.cases_for_platform("darwin")[-1] == "watchdog"
    with pytest.raises(ValueError):
        probe.cases_for_platform("unsupported")


def test_atomic_evidence_does_not_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    probe.record(path, {"first": True})
    with pytest.raises(FileExistsError):
        probe.record(path, {"first": False})
    assert json.loads(path.read_text()) == {"first": True}


def test_cleanup_requires_bound_identity_and_confirmed_end(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    job = runtime / "einvoice-kosit-synthetic"
    job.mkdir(mode=0o700)
    (job / "invoice.xml").write_bytes(b"<synthetic/>")
    (job / "invoice.xml").chmod(0o600)
    identity = probe.directory_identity(job)
    with pytest.raises(RuntimeError, match="native end"):
        probe.cleanup_owned_temp(runtime, identity, ended=False)
    wrong = {**identity, "ino": identity["ino"] + 1}
    with pytest.raises(RuntimeError, match="identity"):
        probe.cleanup_owned_temp(runtime, wrong, ended=True)
    assert job.exists()
    result = probe.cleanup_owned_temp(runtime, identity, ended=True)
    assert result["present_after_native_end"] is True
    assert result["harness_removed"] is True
    assert not job.exists()


def test_cleanup_rejects_foreign_directory(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    foreign = tmp_path / "einvoice-kosit-foreign"
    foreign.mkdir(mode=0o700)
    with pytest.raises(RuntimeError, match="scope"):
        probe.cleanup_owned_temp(runtime, probe.directory_identity(foreign), ended=True)
    assert foreign.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symbolic-link fixture")
def test_cleanup_rejects_replaced_symlink(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    job = runtime / "einvoice-kosit-synthetic"
    job.mkdir(mode=0o700)
    identity = probe.directory_identity(job)
    job.rmdir()
    job.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RuntimeError):
        probe.cleanup_owned_temp(runtime, identity, ended=True)
    assert job.is_symlink()


def test_bounded_json_reader_rejects_excessive_marker(tmp_path: Path) -> None:
    path = tmp_path / "too-large.json"
    path.write_bytes(b" " * (probe.RECORD_LIMIT + 1))
    with pytest.raises(RuntimeError, match="record"):
        probe.read_record(path)


def test_multipart_is_fixed_synthetic_and_bounded() -> None:
    payload = b"<synthetic/>"
    body = probe.multipart(payload, official=True)
    assert b'name="official"' in body and b"true" in body
    assert body.count(payload) == 1
    assert b'name="official"' not in probe.multipart(payload, official=False)


def test_public_cli_requires_new_output(tmp_path: Path) -> None:
    output = tmp_path / "existing.json"
    output.write_text("historical")
    with pytest.raises(FileExistsError):
        probe.run_catalog(output)
    assert output.read_text() == "historical"


def test_late_exit_observation_never_counts_as_five_second_pass() -> None:
    assert probe.ended_in_budget(True, 10.0, 15.00001) is False
    assert probe.ended_in_budget(True, 10.0, 15.0) is True
    assert probe.ended_in_budget(False, 10.0, 11.0) is False


def test_only_parent_case_permits_retained_private_temporary_files() -> None:
    retained = {"present_after_native_end": True}
    assert probe.temporary_result_allowed("parent", retained)
    for case in ("cancel", "disconnect", "worker", "supervisor", "watchdog"):
        assert not probe.temporary_result_allowed(case, retained)
        assert probe.temporary_result_allowed(case, {"present_after_native_end": False})


def test_active_fault_recheck_rejects_ended_request_or_role() -> None:
    with pytest.raises(RuntimeError, match="no longer active"):
        probe.require_active(request_done=True, all_running=True)
    with pytest.raises(RuntimeError, match="no longer active"):
        probe.require_active(request_done=False, all_running=False)
    probe.require_active(request_done=False, all_running=True)
