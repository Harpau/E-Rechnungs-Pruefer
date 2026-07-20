from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "prepare_windows_components.py"
SPEC = importlib.util.spec_from_file_location("prepare_windows_components", SCRIPT_PATH)
assert SPEC and SPEC.loader
components = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(components)


def test_repository_component_lock_is_valid() -> None:
    locked = components._load_lock(PROJECT_ROOT / "packaging/windows/components.lock.json")

    assert locked["validator"]["version"] == "KoSIT Validator 1.6.2"
    assert locked["xrechnung"]["version"].endswith("2026-01-31")
    assert locked["java"]["filename"].endswith("windows_hotspot_21.0.11_10.zip")


def test_load_lock_rejects_unknown_schema_and_invalid_digest(tmp_path: Path) -> None:
    path = tmp_path / "lock.json"
    path.write_text(json.dumps({"schema_version": 2, "components": {}}), encoding="utf-8")
    with pytest.raises(components.ComponentError, match="Schema-Version"):
        components._load_lock(path)

    component = {"version": "1", "filename": "a", "url": "https://example.test/a", "sha256": "falsch"}
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "components": {"java": component, "validator": component, "xrechnung": component},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(components.ComponentError, match="Prüfsumme"):
        components._load_lock(path)


def test_safe_extract_rejects_parent_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../ausbruch.txt", "nicht entpacken")

    with pytest.raises(components.ComponentError, match="Unsicherer Pfad"):
        components._safe_extract(archive, tmp_path / "target")

    assert not (tmp_path / "ausbruch.txt").exists()


def test_find_java_root_requires_one_windows_java_executable(tmp_path: Path) -> None:
    root = tmp_path / "jdk-21-jre"
    java = root / "bin/java.exe"
    java.parent.mkdir(parents=True)
    java.write_bytes(b"test")

    assert components._find_java_root(tmp_path) == root

    second = tmp_path / "anderes/bin/java.exe"
    second.parent.mkdir(parents=True)
    second.write_bytes(b"test")
    with pytest.raises(components.ComponentError, match="eindeutige"):
        components._find_java_root(tmp_path)


def test_release_signing_uses_oidc_and_azure_key_vault() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    build_script = (PROJECT_ROOT / "scripts/build_windows.ps1").read_text(encoding="utf-8")

    for expected in (
        "workflow_dispatch:",
        "environment: release",
        "id-token: write",
        "uses: azure/login@v3",
        "client-id: ${{ secrets.AZURE_CLIENT_ID }}",
        "tenant-id: ${{ secrets.AZURE_TENANT_ID }}",
        "subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}",
        "AzureSignTool --tool-path $toolDirectory --version 7.0.1",
        "AZURE_KEY_VAULT_URL",
        "AZURE_CODE_SIGNING_CERTIFICATE",
        "test_windows_package.ps1 -RequireSignature",
        "git merge-base --is-ancestor $env:GITHUB_SHA origin/main",
        "Manuelle Signiertests dürfen nur auf main gestartet werden.",
    ):
        assert expected in workflow

    assert "WINDOWS_SIGNING_CERTIFICATE_BASE64" not in workflow
    assert "WINDOWS_SIGNING_CERTIFICATE_PASSWORD" not in workflow
    assert "AZURE_CLIENT_SECRET" not in workflow
    assert "creds:" not in workflow
    assert "azure/login@v2" not in workflow
    assert "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')" in workflow

    for expected in (
        "EINVOICE_AZURE_SIGN_TOOL",
        "EINVOICE_AZURE_KEY_VAULT_URL",
        "EINVOICE_AZURE_KEY_VAULT_CERTIFICATE",
        "--azure-key-vault-managed-identity",
        "--timestamp-rfc3161",
    ):
        assert expected in build_script
