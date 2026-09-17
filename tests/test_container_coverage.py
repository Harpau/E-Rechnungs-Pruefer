from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_container_coverage", ROOT / "scripts/check_container_coverage.py")
assert SPEC and SPEC.loader
coverage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coverage)
REAL_SECURITY_SUPPORT = coverage.security_support

SECURITY_PATH = "/usr/local/share/e-rechnung-pruefer/cpython-security.json"
SECURITY_TARGET = "/usr/local/lib/python3.14/urllib/request.py"
SECURITY_CONTENT = b"# Synthetic security-backported stdlib fixture\n"


def security_receipt() -> dict:
    return {
        "schema_version": 1,
        "cpython_version": "3.14.7",
        "advisory": "CVE-2026-15806",
        "upstream_commit": "a0d023fbd23773e24b35d8368789470e22cda5d8",
        "relative_file": "urllib/request.py",
        "input_sha256": "1" * 64,
        "output_sha256": hashlib.sha256(SECURITY_CONTENT).hexdigest(),
        "canonical_before_sha256": "1" * 64,
        "canonical_after_sha256": hashlib.sha256(SECURITY_CONTENT).hexdigest(),
        "patch_sha256": "2" * 64,
        "metadata_sha256": "3" * 64,
        "helper_sha256": "4" * 64,
        "line_endings": "lf",
    }


@pytest.fixture(autouse=True)
def synthetic_security_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    # This suite tests integration with synthetic image bytes. The canonical
    # helper separately tests the actual upstream patch and credential behavior.
    def validate(receipt: dict, *, target: Path | None = None) -> dict:
        if receipt != security_receipt() or (target is not None and target.read_bytes() != SECURITY_CONTENT):
            raise RuntimeError("Synthetic canonical security receipt/file mismatch")
        return receipt

    monkeypatch.setattr(coverage, "security_support", lambda: SimpleNamespace(validate_receipt=validate), raising=False)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fixture(tmp_path: Path) -> tuple[dict, dict, dict, Path]:
    if os.name != "posix":
        pytest.skip("Synthetic Linux rootfs requires POSIX paths, modes and symlinks")
    image = tmp_path / "image"
    image.mkdir()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "base_image": "python:3.14.7-slim-trixie@sha256:" + "a" * 64,
        "cpython_version": "3.14.7",
        "builder_script_sha256": "b" * 64,
        "runtime_lock_sha256": "c" * 64,
        "runtime_metadata_sha256": "d" * 64,
        "manifest_self_excluded": True,
        "packages": {},
        "files": {},
    }

    def put(path: str, data: bytes, provenance: dict, mode: int = 0o644) -> None:
        target = image / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
        manifest["files"][path] = {
            "type": "file",
            "sha256": digest(data),
            "size": len(data),
            "mode": mode,
            "elf": data.startswith(b"\x7fELF"),
            "provenance": provenance,
        }
        if data.startswith(b"\x7fELF"):
            manifest["files"][path]["elf_dependencies"] = []

    os_packages = []
    status = b""
    for name in ("libc6", "zlib1g"):
        key = name + ":amd64"
        data = (
            f"Package: {name}\nStatus: install ok installed\nArchitecture: amd64\n"
            "Version: 1.2-3+b1\nSource: example-source (1.2-3)\nDescription: synthetic\n continuation\n"
        ).encode()
        path = "/var/lib/dpkg/status.d/" + key
        put(path, data, {"kind": "generated", "reason": "unmodified installed dpkg stanza"})
        put("/usr/lib/" + name + ".so", b"\x7fELFsynthetic library", {"kind": "debian", "packages": [key]})
        manifest["packages"][key] = {
            "name": name,
            "version": "1.2-3+b1",
            "architecture": "amd64",
            "source_name": "example-source",
            "source_version": "1.2-3",
            "status_path": path,
            "status_sha256": digest(data),
        }
        os_packages.append(
            {
                "ID": name + "@1.2-3+b1",
                "Name": name,
                "Version": "1.2",
                "Release": "3+b1",
                "Arch": "amd64",
                "SrcName": "example-source",
                "SrcVersion": "1.2",
                "SrcRelease": "3",
                "AnalyzedBy": "dpkg",
            }
        )
        status += data + b"\n"
    put("/var/lib/dpkg/status", status, {"kind": "generated", "reason": "unmodified retained-package dpkg stanzas"})
    python_origin = {"kind": "cpython", "version": "3.14.7", "base_image": manifest["base_image"]}
    put("/usr/local/bin/python3.14", b"\x7fELFsynthetic interpreter", python_origin, 0o755)
    put("/usr/local/lib/libpython3.14.so.1.0", b"\x7fELFsynthetic libpython", python_origin, 0o755)
    receipt = security_receipt()
    receipt_bytes = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode()
    binding = {"receipt_path": SECURITY_PATH, "receipt_sha256": digest(receipt_bytes), "receipt": receipt}
    manifest["cpython_security"] = binding
    common = {
        "version": "3.14.7",
        "base_image": manifest["base_image"],
        "receipt_sha256": binding["receipt_sha256"],
        **{
            key: receipt[key]
            for key in (
                "advisory",
                "upstream_commit",
                "input_sha256",
                "output_sha256",
                "patch_sha256",
                "metadata_sha256",
                "helper_sha256",
            )
        },
    }
    put(SECURITY_TARGET, SECURITY_CONTENT, {"kind": "cpython-security-backport", **common})
    put(SECURITY_PATH, receipt_bytes, {"kind": "cpython-security-receipt", **common})
    python_packages = []
    installed = []
    original = json.loads((ROOT / "packaging/docker/requirements-linux-amd64.txt.metadata.json").read_text())
    for package in original["packages"]:
        if package["name"] == "pip":
            continue
        name, version = package["name"], package["version"]
        info = f"/opt/runtime/lib/python3.14/site-packages/{name}-{version}.dist-info"
        record = b"synthetic-record\n"
        origin = {
            "kind": "wheel",
            "name": name,
            "version": version,
            "wheel_sha256": package["sha256"],
            "wheel_filename": package["filename"],
            "record_sha256": digest(record),
        }
        put(info + "/METADATA", f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n".encode(), origin)
        put(info + "/WHEEL", b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n", origin)
        put(info + "/RECORD", record, origin)
        python_packages.append(
            {
                "Name": name,
                "Version": version,
                "FilePath": info.lstrip("/") + "/METADATA",
                "AnalyzedBy": "python-pkg",
            }
        )
        installed.append({"name": name, "version": version})
    for path in coverage.ENGINE_NETWORK_FILES:
        put(path, b"", {"kind": "generated", "reason": coverage.ENGINE_NETWORK_REASON})
    inventory = {
        "schema_version": 1,
        "environment": {
            "python": "3.14.7",
            "implementation": "CPython",
            "sys_platform": "linux",
            "machine": "x86_64",
            "gil_disabled": False,
        },
        "packages": installed,
        "inventory_sha256": digest(
            json.dumps(sorted((p["name"], p["version"]) for p in installed), separators=(",", ":")).encode()
        ),
        "excluded_editable": None,
    }
    scan = {
        "SchemaVersion": 2,
        "ArtifactType": "container_image",
        "Metadata": {
            "OS": {"Family": "debian", "Name": "13.7"},
            "ImageID": "sha256:" + "e" * 64,
            "ImageConfig": {"architecture": "amd64"},
        },
        "Results": [
            {"Class": "os-pkgs", "Type": "debian", "Packages": os_packages},
            {"Class": "lang-pkgs", "Type": "python-pkg", "Packages": python_packages},
        ],
    }
    return manifest, scan, inventory, image


def test_exact_runtime_manifest_inventory_and_trivy_coverage_pass(tmp_path: Path) -> None:
    manifest, scan, inventory, image = fixture(tmp_path)
    report = coverage.check_coverage(manifest, scan, inventory)
    assert report["coverage_passed"] is True
    assert report["debian_packages"] == 2
    assert report["python_distributions"] == 27
    assert report["cpython_version"] == "3.14.7"
    assert coverage.verify_payload(manifest, image)["payload_passed"] is True


@pytest.mark.parametrize(
    "change",
    [
        "missing_binding",
        "wrong_receipt_path",
        "wrong_receipt_hash",
        "wrong_policy",
        "unmodified_origin",
        "wrong_target_hash",
        "wrong_receipt_type",
        "backport_elsewhere",
        "changed_receipt_bytes",
        "changed_target_bytes",
    ],
)
def test_cpython_backport_requires_exact_policy_receipt_payload_and_provenance(tmp_path: Path, change: str) -> None:
    manifest, _, _, image = fixture(tmp_path)
    binding = manifest["cpython_security"]
    if change == "missing_binding":
        del manifest["cpython_security"]
    elif change == "wrong_receipt_path":
        binding["receipt_path"] = "/etc/alternate-security.json"
    elif change == "wrong_receipt_hash":
        binding["receipt_sha256"] = "0" * 64
    elif change == "wrong_policy":
        binding["receipt"]["upstream_commit"] = "0" * 40
    elif change == "unmodified_origin":
        manifest["files"][SECURITY_TARGET]["provenance"]["kind"] = "cpython"
    elif change == "wrong_target_hash":
        manifest["files"][SECURITY_TARGET]["sha256"] = "0" * 64
    elif change == "wrong_receipt_type":
        manifest["files"][SECURITY_PATH]["type"] = "symlink"
    elif change == "backport_elsewhere":
        manifest["files"]["/usr/local/lib/python3.14/other.py"] = copy.deepcopy(manifest["files"][SECURITY_TARGET])
    elif change == "changed_receipt_bytes":
        (image / SECURITY_PATH.lstrip("/")).write_bytes(b"{}\n")
    else:
        (image / SECURITY_TARGET.lstrip("/")).write_bytes(b"unpatched source\n")
    with pytest.raises(coverage.CoverageError):
        coverage.verify_payload(manifest, image)


def test_offline_payload_check_never_claims_native_cpython_behavior(tmp_path: Path) -> None:
    manifest, _, _, image = fixture(tmp_path)
    result = coverage.verify_payload(manifest, image)
    assert result["cpython_security"]["behavior_checked"] is False


def test_real_security_helper_accepts_only_the_canonical_patched_manifest_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _, _, image = fixture(tmp_path)
    security = REAL_SECURITY_SUPPORT()
    before = (Path(__file__).parent / "fixtures/cpython3147/urllib-request.source").read_bytes()
    after = security.patch_bytes(before)
    receipt = security.make_receipt(before, after)
    receipt_bytes = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode()
    binding = {"receipt_path": SECURITY_PATH, "receipt_sha256": digest(receipt_bytes), "receipt": receipt}
    manifest["cpython_security"] = binding
    for path, content in [(SECURITY_TARGET, after), (SECURITY_PATH, receipt_bytes)]:
        (image / path.lstrip("/")).write_bytes(content)
        entry = manifest["files"][path]
        entry.update(sha256=digest(content), size=len(content))
        entry["provenance"].update(receipt_sha256=binding["receipt_sha256"])
        entry["provenance"].update({key: receipt[key] for key in coverage.SECURITY_FIELDS})
    monkeypatch.setattr(coverage, "security_support", REAL_SECURITY_SUPPORT)
    assert coverage.verify_payload(manifest, image)["payload_passed"] is True
    binding["receipt"]["helper_sha256"] = "0" * 64
    with pytest.raises(coverage.CoverageError, match="pinned provenance"):
        coverage.verify_payload(manifest, image)


@pytest.mark.parametrize("change", [None, "behavior", "module", "hash", "receipt_hash", "receipt", "error"])
def test_native_cpython_behavior_is_bound_to_actual_module_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str | None
) -> None:
    manifest, _, _, _ = fixture(tmp_path)
    binding = manifest["cpython_security"]
    result = {
        "behavior_passed": True,
        "module_path": SECURITY_TARGET,
        "relative_file": "urllib/request.py",
        "output_sha256": security_receipt()["output_sha256"],
        "receipt_sha256": binding["receipt_sha256"],
        "receipt": security_receipt(),
        "cases": [{"name": "synthetic behavior", "passed": True}],
    }
    field = {
        "behavior": "behavior_passed",
        "module": "module_path",
        "hash": "output_sha256",
        "receipt_hash": "receipt_sha256",
        "receipt": "receipt",
    }.get(change)
    if field:
        result[field] = False if change == "behavior" else "invalid"

    def verify() -> dict:
        if change == "error":
            raise RuntimeError("Runtime security regression failed")
        return result

    monkeypatch.setattr(coverage, "security_support", lambda: SimpleNamespace(verify_runtime=verify))
    if change is None:
        report = coverage.verify_security_runtime(binding, Path("/"))
        assert report["behavior_checked"] is True and report["runtime"] == result
    else:
        with pytest.raises(coverage.CoverageError):
            coverage.verify_security_runtime(binding, Path("/"))


@pytest.mark.parametrize(
    ("binary", "source", "expected_binary", "expected_source"),
    [
        ({"Version": "20250419"}, {"SrcVersion": "20250419"}, "20250419", "20250419"),
        (
            {"Version": "1.1.0", "Release": "2+b7"},
            {"SrcVersion": "1.1.0", "SrcRelease": "2"},
            "1.1.0-2+b7",
            "1.1.0-2",
        ),
        (
            {"Epoch": 1, "Version": "1.3.dfsg+really1.3.1", "Release": "1+b1"},
            {"SrcEpoch": 1, "SrcVersion": "1.3.dfsg+really1.3.1", "SrcRelease": "1"},
            "1:1.3.dfsg+really1.3.1-1+b1",
            "1:1.3.dfsg+really1.3.1-1",
        ),
        (
            {"Epoch": 2, "Version": "3.110", "Release": "1+deb13u4"},
            {"SrcEpoch": 1, "SrcVersion": "3.110", "SrcRelease": "1~deb13u4"},
            "2:3.110-1+deb13u4",
            "1:3.110-1~deb13u4",
        ),
        (
            {"Epoch": 0, "Version": "1.2-beta", "Release": "3"},
            {"SrcEpoch": 0, "SrcVersion": "1.2-beta", "SrcRelease": "3"},
            "1.2-beta-3",
            "1.2-beta-3",
        ),
    ],
)
def test_trivy_debian_json_reconstructs_binary_and_source_versions_independently(
    binary: dict, source: dict, expected_binary: str, expected_source: str
) -> None:
    package = {
        "ID": "synthetic@" + expected_binary,
        "Name": "synthetic",
        "Arch": "arm64",
        "SrcName": "synthetic-source",
        **binary,
        **source,
    }
    assert coverage.package_identity(package, trivy=True) == (
        "synthetic",
        expected_binary,
        "arm64",
        "synthetic-source",
        expected_source,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Epoch", -1),
        ("Epoch", True),
        ("Epoch", "1"),
        ("Epoch", None),
        ("SrcEpoch", -1),
        ("SrcEpoch", False),
        ("SrcEpoch", "1"),
        ("SrcEpoch", None),
        ("Version", ""),
        ("Version", None),
        ("Version", "1.2\n"),
        ("SrcVersion", ""),
        ("SrcVersion", None),
        ("SrcVersion", "1.2\n"),
        ("Release", None),
        ("Release", 3),
        ("Release", "3\n"),
        ("SrcRelease", None),
        ("SrcRelease", 3),
        ("SrcRelease", "3\n"),
        ("ID", None),
        ("ID", "different@1:1.2-3+b1"),
        ("Release", "4"),
        ("Epoch", 2),
        ("Version", "1:1.2-3+b1"),
    ],
)
def test_trivy_debian_json_rejects_malformed_or_conflicting_version_fields(field: str, value: Any) -> None:
    package = {
        "ID": "synthetic@1:1.2-3+b1",
        "Name": "synthetic",
        "Arch": "arm64",
        "Version": "1.2",
        "Epoch": 1,
        "Release": "3+b1",
        "SrcName": "synthetic-source",
        "SrcVersion": "1.2",
        "SrcEpoch": 1,
        "SrcRelease": "3",
    }
    package[field] = value
    with pytest.raises(coverage.CoverageError):
        coverage.package_identity(package, trivy=True)


@pytest.mark.parametrize("field", ["Version", "Epoch", "Release", "SrcVersion", "ID"])
def test_trivy_debian_json_never_recovers_missing_version_fields_from_id(field: str) -> None:
    package = {
        "ID": "synthetic@1:1.2-3+b1",
        "Name": "synthetic",
        "Arch": "arm64",
        "Version": "1.2",
        "Epoch": 1,
        "Release": "3+b1",
        "SrcName": "synthetic-source",
        "SrcVersion": "1.2",
        "SrcEpoch": 1,
        "SrcRelease": "3",
    }
    del package[field]
    with pytest.raises(coverage.CoverageError):
        coverage.package_identity(package, trivy=True)


@pytest.mark.parametrize("field", ["Release", "SrcRelease", "SrcEpoch"])
def test_debian_coverage_rejects_missing_revision_or_wrong_source_epoch(tmp_path: Path, field: str) -> None:
    manifest, scan, inventory, _ = fixture(tmp_path)
    package = scan["Results"][0]["Packages"][0]
    if field == "SrcEpoch":
        package[field] = 1
    else:
        del package[field]
    with pytest.raises(coverage.CoverageError):
        coverage.check_coverage(manifest, scan, inventory)


def test_only_exact_unmanifested_docker_mtab_symlink_is_reported(tmp_path: Path) -> None:
    manifest, _, _, image = fixture(tmp_path)
    assert coverage.verify_payload(manifest, image)["engine_symlink_exceptions"] == []
    (image / "etc/mtab").symlink_to("/proc/mounts")
    report = coverage.verify_payload(manifest, image)
    assert report["payload_passed"] is True
    assert report["engine_symlink_exceptions"] == [{"path": "/etc/mtab", "type": "symlink", "target": "/proc/mounts"}]


@pytest.mark.parametrize("kind", ["file", "directory", "relative", "other_target", "other_path", "manifested"])
def test_docker_mtab_exception_rejects_other_types_targets_paths_and_manifest_overrides(
    tmp_path: Path, kind: str
) -> None:
    manifest, _, _, image = fixture(tmp_path)
    target = image / "etc/mtab"
    if kind == "file":
        target.write_bytes(b"synthetic mounts")
    elif kind == "directory":
        target.mkdir()
    elif kind == "relative":
        target.symlink_to("../proc/mounts")
    elif kind == "other_target":
        target.symlink_to("/proc/self/mounts")
    elif kind == "other_path":
        (image / "etc/extra-mtab").symlink_to("/proc/mounts")
    else:
        target.symlink_to("/proc/mounts")
        manifest["files"]["/etc/mtab"] = {
            "type": "symlink",
            "target": "/usr/lib/libc6.so",
            "provenance": {"kind": "debian", "packages": ["libc6:amd64"]},
        }
    with pytest.raises(coverage.CoverageError):
        coverage.verify_payload(manifest, image)


@pytest.mark.parametrize("change", ["wheel_hash", "record_hash", "package_name", "file_type", "missing_manifest_entry"])
def test_standard_wheel_file_requires_exact_distribution_and_record_binding(tmp_path: Path, change: str) -> None:
    manifest, _, _, image = fixture(tmp_path)
    path = next(path for path in manifest["files"] if path.endswith(".dist-info/WHEEL"))
    entry = manifest["files"][path]
    entry["provenance"] = copy.deepcopy(entry["provenance"])
    if change == "wheel_hash":
        entry["provenance"]["wheel_sha256"] = "0" * 64
    elif change == "record_hash":
        entry["provenance"]["record_sha256"] = "0" * 64
    elif change == "package_name":
        entry["provenance"]["name"] = "wheel"
    elif change == "file_type":
        entry["type"] = "directory"
    else:
        del manifest["files"][path]
    with pytest.raises(coverage.CoverageError):
        coverage.verify_payload(manifest, image)


@pytest.mark.parametrize(
    "relative",
    [
        "wheel/__init__.py",
        "wheel-0.48.0.dist-info/WHEEL",
        "unbound-1.0.dist-info/WHEEL",
        "annotated-doc-0.0.5.dist-info/wheel",
        "annotated-doc-0.0.5.dist-info/nested/WHEEL",
    ],
)
def test_wheel_metadata_exception_never_allows_modules_distributions_or_unbound_files(
    tmp_path: Path, relative: str
) -> None:
    manifest, _, _, image = fixture(tmp_path)
    path = image / "opt/runtime/lib/python3.14/site-packages" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic forbidden wheel payload")
    with pytest.raises(coverage.CoverageError):
        coverage.verify_payload(manifest, image)


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "missing",
        "extra",
        "version",
        "architecture",
        "source",
        "source_version",
        "analyzer",
        "wrong_os",
    ],
)
def test_os_coverage_rejects_missing_extra_ambiguous_or_misidentified_packages(tmp_path: Path, change: str) -> None:
    manifest, scan, inventory, _ = fixture(tmp_path)
    packages = scan["Results"][0]["Packages"]
    if change == "empty":
        packages.clear()
    elif change == "missing":
        packages.pop()
    elif change == "extra":
        packages.append(packages[0] | {"Name": "bash"})
    elif change == "wrong_os":
        scan["Metadata"]["OS"]["Family"] = "alpine"
    else:
        field = {
            "version": "Version",
            "architecture": "Arch",
            "source": "SrcName",
            "source_version": "SrcVersion",
            "analyzer": "AnalyzedBy",
        }[change]
        packages[0][field] = "wrong"
    with pytest.raises(coverage.CoverageError):
        coverage.check_coverage(manifest, scan, inventory)


@pytest.mark.parametrize(
    "change", ["missing", "extra", "sbom_only", "forged_path", "wrong_version", "pkg_resources", "pip"]
)
def test_python_coverage_requires_actual_installed_wheel_metadata(tmp_path: Path, change: str) -> None:
    manifest, scan, inventory, _ = fixture(tmp_path)
    packages = scan["Results"][1]["Packages"]
    if change == "missing":
        packages.pop()
    elif change in {"extra", "pkg_resources", "pip"}:
        packages.append({"Name": change, "Version": "1.0", "AnalyzedBy": "sbom"})
    elif change == "sbom_only":
        packages[0]["AnalyzedBy"] = "sbom"
        packages[0].pop("FilePath")
    elif change == "forged_path":
        packages[0]["FilePath"] = "app/fake.dist-info/METADATA"
    else:
        packages[0]["Version"] = "0.0.1"
    with pytest.raises(coverage.CoverageError):
        coverage.check_coverage(manifest, scan, inventory)


def test_sbom_corroboration_cannot_replace_installed_metadata(tmp_path: Path) -> None:
    manifest, scan, inventory, _ = fixture(tmp_path)
    item = scan["Results"][1]["Packages"][0]
    scan["Results"][1]["Packages"].append({"Name": item["Name"], "Version": item["Version"], "AnalyzedBy": "sbom"})
    assert coverage.check_coverage(manifest, scan, inventory)["coverage_passed"] is True


def test_identical_status_and_status_d_scan_records_deduplicate_but_conflicts_fail(tmp_path: Path) -> None:
    manifest, scan, inventory, _ = fixture(tmp_path)
    packages = scan["Results"][0]["Packages"]
    packages.append(copy.deepcopy(packages[0]))
    assert coverage.check_coverage(manifest, scan, inventory)["coverage_passed"] is True
    packages[-1]["SrcVersion"] = "conflicting"
    with pytest.raises(coverage.CoverageError):
        coverage.check_coverage(manifest, scan, inventory)


@pytest.mark.parametrize(
    "change", ["empty", "extra", "missing", "duplicate", "digest", "python", "architecture", "abi", "editable"]
)
def test_inventory_is_complete_and_bound_to_cpython_and_architecture(tmp_path: Path, change: str) -> None:
    manifest, scan, inventory, _ = fixture(tmp_path)
    if change == "empty":
        inventory["packages"] = []
    elif change == "extra":
        inventory["packages"].append({"name": "pip", "version": "26.2.1"})
    elif change == "missing":
        inventory["packages"].pop()
    elif change == "duplicate":
        inventory["packages"].append(copy.deepcopy(inventory["packages"][0]))
    elif change == "digest":
        inventory["inventory_sha256"] = "0" * 64
    elif change == "python":
        inventory["environment"]["python"] = "3.14.6"
    elif change == "architecture":
        inventory["environment"]["machine"] = "aarch64"
    elif change == "abi":
        inventory["environment"]["gil_disabled"] = True
    else:
        inventory["excluded_editable"] = {"name": "hidden"}
    with pytest.raises(coverage.CoverageError):
        coverage.check_coverage(manifest, scan, inventory)


@pytest.mark.parametrize("change", ["file", "missing", "mode", "extra", "symlink", "status"])
def test_payload_rejects_modified_missing_or_unmanifested_runtime_files(tmp_path: Path, change: str) -> None:
    manifest, _, _, image = fixture(tmp_path)
    target = image / "usr/lib/libc6.so"
    if change == "file":
        target.write_bytes(b"tampered")
    elif change == "missing":
        target.unlink()
    elif change == "mode":
        target.chmod(0o755)
    elif change == "extra":
        (target.parent / "unexpected.so").write_bytes(b"extra")
    elif change == "symlink":
        target.unlink()
        target.symlink_to("/usr/lib/zlib1g.so")
    else:
        path = "/var/lib/dpkg/status.d/libc6:amd64"
        value = (image / path.lstrip("/")).read_bytes().replace(b"example-source", b"forged--source")
        (image / path.lstrip("/")).write_bytes(value)
        manifest["files"][path]["sha256"] = digest(value)
        manifest["files"][path]["size"] = len(value)
        manifest["packages"]["libc6:amd64"]["status_sha256"] = digest(value)
    with pytest.raises(coverage.CoverageError):
        coverage.verify_payload(manifest, image)


@pytest.mark.parametrize(
    "path",
    [
        "/usr/local/lib/python3.14/ensurepip/_bundled/pip-26.2.1-py3-none-any.whl",
        "/opt/runtime/bin/pip3.14",
        "/opt/runtime/lib/python3.14/site-packages/pip/__init__.py",
        "/app/hidden/setuptools-70.3.0.dist-info/METADATA",
        "/app/vendor/pkg_resources/__init__.py",
    ],
)
def test_bootstrap_payload_is_rejected_even_outside_distribution_inventory(tmp_path: Path, path: str) -> None:
    manifest, _, _, image = fixture(tmp_path)
    target = image / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"hidden bootstrap")
    with pytest.raises(coverage.CoverageError):
        coverage.verify_payload(manifest, image)


def test_repository_packaging_data_is_allowed_but_installed_or_hidden_tools_are_not(tmp_path: Path) -> None:
    manifest, _, _, image = fixture(tmp_path)
    data = image / "app/packaging/docker/requirements-runtime-amd64.txt"
    data.parent.mkdir(parents=True)
    data.write_text("# Synthetic repository lock data\n")
    assert coverage.verify_payload(manifest, image)["payload_passed"] is True
    for path in (
        "/opt/runtime/lib/python3.14/site-packages/packaging/__init__.py",
        "/app/packaging/hidden/pip-26.2.1-py3-none-any.whl",
        "/app/packaging/pip/__init__.py",
        "/app/packaging/__init__.py",
    ):
        assert coverage.forbidden_payload(path)


def test_payload_fails_when_any_runtime_subtree_cannot_be_inventoried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _, _, image = fixture(tmp_path)

    def inaccessible_walk(*args, **kwargs):
        error = PermissionError("synthetic unreadable runtime subtree")
        if kwargs.get("onerror"):
            kwargs["onerror"](error)
        return iter(())

    monkeypatch.setattr(coverage.os, "walk", inaccessible_walk)
    with pytest.raises(coverage.CoverageError, match="inventoried"):
        coverage.verify_payload(manifest, image)


def test_only_exact_engine_network_files_can_change_with_explicit_provenance(tmp_path: Path) -> None:
    manifest, _, _, image = fixture(tmp_path)
    for path in coverage.ENGINE_NETWORK_FILES:
        (image / path.lstrip("/")).write_bytes(b"synthetic Docker runtime mount\n")
    report = coverage.verify_payload(manifest, image)
    assert set(report["engine_network_exceptions"]) == set(coverage.ENGINE_NETWORK_FILES)
    manifest["files"]["/etc/hosts"]["provenance"]["reason"] = "ignore arbitrary content"
    with pytest.raises(coverage.CoverageError):
        coverage.verify_payload(manifest, image)


def test_absolute_symlinks_resolve_inside_image_and_loops_fail(tmp_path: Path) -> None:
    manifest, _, _, image = fixture(tmp_path)
    link = image / "lib"
    link.symlink_to("/usr/lib")
    manifest["files"]["/lib"] = {
        "type": "symlink",
        "target": "/usr/lib",
        "provenance": {"kind": "merged-usr-layout", "base_image": manifest["base_image"]},
    }
    assert coverage.verify_payload(manifest, image)["payload_passed"] is True
    link.unlink()
    link.symlink_to("/lib")
    manifest["files"]["/lib"]["target"] = "/lib"
    with pytest.raises(coverage.CoverageError):
        coverage.verify_payload(manifest, image)


@pytest.mark.parametrize(
    "change", ["empty_os", "empty_files", "unknown_owner", "cpython", "status_hash", "traversal", "missing_record"]
)
def test_manifest_cannot_claim_coverage_with_incomplete_or_unbound_provenance(tmp_path: Path, change: str) -> None:
    manifest, scan, inventory, _ = fixture(tmp_path)
    if change == "empty_os":
        manifest["packages"] = {}
    elif change == "empty_files":
        manifest["files"] = {}
    elif change == "unknown_owner":
        manifest["files"]["/usr/lib/libc6.so"]["provenance"]["packages"] = ["unknown:amd64"]
    elif change == "cpython":
        manifest["cpython_version"] = "3.14.6"
    elif change == "status_hash":
        manifest["packages"]["libc6:amd64"]["status_sha256"] = "0" * 64
    elif change == "traversal":
        manifest["files"]["/usr/../../escape"] = manifest["files"].pop("/usr/lib/libc6.so")
    else:
        manifest["files"].pop(next(path for path in manifest["files"] if path.endswith("/RECORD")))
    with pytest.raises(coverage.CoverageError):
        coverage.check_coverage(manifest, scan, inventory)


def test_coverage_does_not_hide_reported_vulnerabilities(tmp_path: Path) -> None:
    manifest, scan, inventory, _ = fixture(tmp_path)
    scan["Results"][0]["Vulnerabilities"] = [{"VulnerabilityID": "CVE-SYNTHETIC", "PkgName": "libc6"}]
    report = coverage.check_coverage(manifest, scan, inventory)
    assert report["coverage_passed"] is True
    assert report["trivy_image_vulnerabilities"] == 1


def test_cli_writes_bound_json_and_failure_is_nonzero(tmp_path: Path) -> None:
    manifest, scan, inventory, image = fixture(tmp_path)
    paths = {
        "manifest": tmp_path / "manifest.json",
        "trivy-image": tmp_path / "trivy.json",
        "inventory": tmp_path / "inventory.json",
    }
    for key, data in (("manifest", manifest), ("trivy-image", scan), ("inventory", inventory)):
        paths[key].write_text(json.dumps(data))
    output = tmp_path / "coverage.json"
    args = [
        "check",
        *(value for name, path in paths.items() for value in ("--" + name, str(path))),
        "--output",
        str(output),
    ]
    assert coverage.main(args) == 0
    report = json.loads(output.read_text())
    assert report["inputs"]["manifest_sha256"] == digest(paths["manifest"].read_bytes())
    assert coverage.main(["verify-payload", "--manifest", str(paths["manifest"]), "--root", str(image)]) == 0
    paths["trivy-image"].write_text("{}")
    assert coverage.main(args) != 0
    assert json.loads(output.read_text())["coverage_passed"] is False
