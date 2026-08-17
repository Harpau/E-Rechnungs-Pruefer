from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "release_evidence.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("test_release_evidence_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _paths(root: Path) -> tuple[Path, Path]:
    return root / "FINAL-EVIDENCE-INVENTORY.json", root / "FINAL-EVIDENCE-INVENTORY.sha256"


def test_create_is_canonical_and_verify_ignores_its_own_outputs(tmp_path: Path):
    module = _load_module()
    root = tmp_path / "evidence"
    (root / "empty").mkdir(parents=True)
    (root / "nested").mkdir()
    executable = root / "nested" / "check.sh"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o750)
    (root / "note-ä.txt").write_text("synthetische Evidence\n", encoding="utf-8")
    inventory, checksum = _paths(root)

    document = module.create_inventory(root, inventory, checksum)

    content = inventory.read_bytes()
    assert content == (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    assert checksum.read_text(encoding="utf-8") == f"{sha256(content).hexdigest()}  {inventory.name}\n"
    assert inventory.stat().st_mode & 0o777 == 0o600
    assert checksum.stat().st_mode & 0o777 == 0o600
    assert [entry["path"] for entry in document["entries"]] == [
        ".",
        "empty",
        "nested",
        "nested/check.sh",
        "note-ä.txt",
    ]
    executable_entry = next(entry for entry in document["entries"] if entry["path"] == "nested/check.sh")
    assert executable_entry == {
        "path": "nested/check.sh",
        "type": "file",
        "mode": "0750",
        "size": 17,
        "sha256": sha256(executable.read_bytes()).hexdigest(),
    }
    assert module.verify_inventory(root, inventory, checksum) == []


def test_verify_reports_missing_changed_and_unexpected_entries(tmp_path: Path):
    module = _load_module()
    root = tmp_path / "evidence"
    root.mkdir()
    missing = root / "missing.txt"
    missing.write_text("wird entfernt", encoding="utf-8")
    changed = root / "changed.txt"
    changed.write_text("vorher", encoding="utf-8")
    inventory, checksum = _paths(root)
    module.create_inventory(root, inventory, checksum)

    missing.unlink()
    changed.write_text("nachher", encoding="utf-8")
    (root / "unexpected-empty-directory").mkdir()

    differences = module.verify_inventory(root, inventory, checksum)

    assert "Fehlt: missing.txt" in differences
    assert "Geändert: changed.txt (size, sha256)" in differences
    assert "Unerwartet: unexpected-empty-directory" in differences


def test_verify_rejects_inventory_without_matching_detached_checksum(tmp_path: Path):
    module = _load_module()
    root = tmp_path / "evidence"
    root.mkdir()
    (root / "result.txt").write_text("PASS\n", encoding="utf-8")
    inventory, checksum = _paths(root)
    module.create_inventory(root, inventory, checksum)
    inventory.write_bytes(inventory.read_bytes() + b" ")

    with pytest.raises(module.InventoryError, match="Detached SHA-256"):
        module.verify_inventory(root, inventory, checksum)


def test_create_refuses_to_overwrite_without_explicit_force(tmp_path: Path):
    module = _load_module()
    root = tmp_path / "evidence"
    root.mkdir()
    inventory, checksum = _paths(root)
    inventory.write_text("bestehend", encoding="utf-8")

    with pytest.raises(module.InventoryError, match="--force"):
        module.create_inventory(root, inventory, checksum)


def test_create_does_not_create_a_missing_evidence_root(tmp_path: Path):
    module = _load_module()
    root = tmp_path / "missing-evidence"
    inventory, checksum = _paths(root)

    with pytest.raises(module.InventoryError, match="Metadaten können nicht sicher gelesen werden"):
        module.create_inventory(root, inventory, checksum)

    assert not root.exists()


def test_symlink_fails_closed_during_create_and_verify(tmp_path: Path):
    module = _load_module()
    root = tmp_path / "evidence"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("synthetisch", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(target.name)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Symlinks stehen nicht zur Verfügung: {exc}")
    inventory, checksum = _paths(root)

    with pytest.raises(module.InventoryError, match="symbolischer Link"):
        module.create_inventory(root, inventory, checksum)

    link.unlink()
    module.create_inventory(root, inventory, checksum)
    link.symlink_to(target.name)
    with pytest.raises(module.InventoryError, match="symbolischer Link"):
        module.verify_inventory(root, inventory, checksum)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO-Sonderdateien sind auf dieser Plattform nicht verfügbar")
def test_special_file_fails_closed(tmp_path: Path):
    module = _load_module()
    root = tmp_path / "evidence"
    root.mkdir()
    fifo = root / "unsafe.fifo"
    os.mkfifo(fifo)

    with pytest.raises(module.InventoryError, match="Sonderdatei"):
        module.build_inventory(root)


def test_cli_returns_nonzero_for_unexpected_file(tmp_path: Path):
    root = tmp_path / "evidence"
    root.mkdir()
    (root / "receipt.txt").write_text("PASS\n", encoding="utf-8")
    inventory, checksum = _paths(root)
    create = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "create",
            str(root),
            "--inventory",
            str(inventory),
            "--checksum",
            str(checksum),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert create.returncode == 0, create.stderr

    unexpected = root / "late.txt"
    unexpected.write_text("zu spät", encoding="utf-8")
    verify = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "verify",
            str(root),
            "--inventory",
            str(inventory),
            "--checksum",
            str(checksum),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert verify.returncode == 1
    assert "Unerwartet: late.txt" in verify.stderr


def test_mode_changes_are_detected(tmp_path: Path):
    if os.name == "nt":
        pytest.skip("Windows bildet POSIX-Modusänderungen nicht zuverlässig ab.")
    module = _load_module()
    root = tmp_path / "evidence"
    root.mkdir()
    receipt = root / "receipt.txt"
    receipt.write_text("PASS\n", encoding="utf-8")
    receipt.chmod(0o600)
    inventory, checksum = _paths(root)
    module.create_inventory(root, inventory, checksum)

    receipt.chmod(stat.S_IMODE(receipt.stat().st_mode) | stat.S_IXUSR)

    assert "Geändert: receipt.txt (mode)" in module.verify_inventory(root, inventory, checksum)
