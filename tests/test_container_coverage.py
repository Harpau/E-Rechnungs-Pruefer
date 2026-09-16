from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_container_coverage", ROOT / "scripts/check_container_coverage.py")
assert SPEC and SPEC.loader
coverage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coverage)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fixture(tmp_path: Path) -> tuple[dict, dict, dict, Path]:
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
                "Name": name,
                "Version": "1.2-3+b1",
                "Arch": "amd64",
                "SrcName": "example-source",
                "SrcVersion": "1.2-3",
                "AnalyzedBy": "dpkg",
            }
        )
        status += data + b"\n"
    put("/var/lib/dpkg/status", status, {"kind": "generated", "reason": "unmodified retained-package dpkg stanzas"})
    python_origin = {"kind": "cpython", "version": "3.14.7", "base_image": manifest["base_image"]}
    put("/usr/local/bin/python3.14", b"\x7fELFsynthetic interpreter", python_origin, 0o755)
    put("/usr/local/lib/libpython3.14.so.1.0", b"\x7fELFsynthetic libpython", python_origin, 0o755)
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
