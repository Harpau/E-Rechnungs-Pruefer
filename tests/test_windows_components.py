from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
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
        "test_windows_package.ps1 -RequireSignature -ConfirmIsolatedEnvironment",
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


def test_windows_installer_offers_removable_per_user_autostart() -> None:
    installer = (PROJECT_ROOT / "packaging/windows/installer.iss").read_text(encoding="utf-8")

    assert 'Name: "autostart"; Description: "Bei Windows-Anmeldung automatisch starten"' in installer
    assert 'Root: HKCU; Subkey: "Software\\Microsoft\\Windows\\CurrentVersion\\Run"' in installer
    assert 'ValueData: """{app}\\{#AppExeName}"" --background"' in installer
    assert "Check: not WizardIsTaskSelected('autostart')" in installer
    assert "Flags: uninsdeletevalue; Tasks: autostart" in installer


def test_windows_installer_stops_running_app_for_update_and_uninstall() -> None:
    installer = (PROJECT_ROOT / "packaging/windows/installer.iss").read_text(encoding="utf-8")

    assert "AppMutex=" not in installer
    assert "CloseApplications=yes" in installer
    assert "RestartApplications=no" in installer
    for expected in (
        "function PrepareToInstall(var NeedsRestart: Boolean): String;",
        "function InitializeUninstall: Boolean;",
        "function OpenEvent(DesiredAccess: DWORD; InheritHandle: BOOL; Name: String): Cardinal;",
        "function SetEvent(EventHandle: Cardinal): BOOL;",
        "function CloseHandle(Handle: Cardinal): BOOL;",
        "ShutdownHandle: Cardinal;",
        "OpenEventW@kernel32.dll",
        "SetEvent@kernel32.dll",
        "CheckForMutexes(AppMutexName)",
        "ShutdownTimeoutMilliseconds = 30000",
        "Sleep(ShutdownPollMilliseconds)",
        "RestartBackgroundAfterUpdate := WasRunning and ExistingInstallation",
        'Parameters: "--background"; Flags: nowait; Check: ShouldRestartBackgroundAfterUpdate',
        "RestartBackgroundAfterUpdate and WizardIsTaskSelected('autostart')",
    ):
        assert expected in installer

    assert ": HANDLE" not in installer

    prepare = installer.index("function PrepareToInstall")
    uninstall = installer.index("function InitializeUninstall")
    stop_helper = installer.index("function StopRunningApplication")
    assert stop_helper < prepare < uninstall


def test_windows_package_test_refuses_existing_state_before_installation() -> None:
    script = (PROJECT_ROOT / "scripts/test_windows_package.ps1").read_text(encoding="utf-8")

    assert script.count("Get-OptionalRegistryValue") == 5
    assert "Get-ItemPropertyValue" not in script
    assert "$RegistryKey.GetValueNames()" in script
    assert "$RegistryKey.GetValueKind($ExistingName)" in script
    assert "$RegistryKey.Dispose()" in script
    assert "[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames" in script
    for expected in (
        "[switch]$ConfirmIsolatedEnvironment",
        "if (-not $ConfirmIsolatedEnvironment)",
        "$DefaultInstallDir",
        "$StartMenuDir",
        "$RuntimeFile",
        "$ApiTokenFile",
        "$StartupErrorFile",
        "$RunKey",
        "$RunValueName",
        "{D33FD9E5-0C5E-48ED-BF0C-E9D2962A45DF}_is1",
        'Get-Process -Name "E-Rechnungs-Pruefer"',
        "if ($ExistingState.Count -gt 0)",
    ):
        assert expected in script

    confirmation_guard = script.index("if (-not $ConfirmIsolatedEnvironment)")
    conflict_guard = script.index("if ($ExistingState.Count -gt 0)")
    test_directory_creation = script.index("New-Item $TestRoot -ItemType Directory")
    installer_start = script.index("Invoke-TestInstaller -Path $Setup")
    assert confirmation_guard < conflict_guard < test_directory_creation < installer_start
    assert "Remove-Item $TestRoot -Recurse" not in script

    preflight = script[script.index("$ExistingState =") : test_directory_creation]
    for forbidden in (
        "Remove-Item ",
        "Remove-ItemProperty",
        "Stop-OwnedProcess",
        "Invoke-TestUninstaller",
        "Invoke-TestInstaller",
        "Start-Process",
    ):
        assert forbidden not in preflight
    assert 'throw @"' in script[conflict_guard:test_directory_creation]

    for state_check in (
        "Test-Path -LiteralPath $DefaultInstallDir",
        "Test-Path -LiteralPath $StartMenuDir",
        "foreach ($StateFile in @($RuntimeFile, $ApiTokenFile, $StartupErrorFile))",
        "Get-OptionalRegistryValue -Path $RunKey -Name $RunValueName",
        "$ExistingAutostartState.Exists",
        "foreach ($UninstallKey in $UninstallKeys)",
        "$ExistingProcesses.Count -gt 0",
    ):
        assert state_check in preflight


def test_windows_package_test_cleans_up_only_owned_state() -> None:
    script = (PROJECT_ROOT / "scripts/test_windows_package.ps1").read_text(encoding="utf-8")

    assert "Stop-Process" not in script
    assert "taskkill" not in script.casefold()
    assert "$OwnedProcess.Kill($true)" in script
    assert "$OwnedProcess.WaitForExit(10000)" in script
    assert "$PackageTestMutex.WaitOne(0)" in script
    assert '"Global\\E-Rechnungs-Pruefer-Package-Test-$CurrentUserSid"' in script
    assert "[Guid]::NewGuid().ToString('N')" in script
    assert "Test-ExpectedStringRegistryValue -State $CurrentAutostartState" in script
    assert "$State.Kind -eq [Microsoft.Win32.RegistryValueKind]::String" in script
    assert "$State.Value -is [string]" in script
    assert script.count("Remove-ItemProperty") == 1
    assert "Remove-Item $RuntimeFile" not in script
    assert "Remove-Item $ApiTokenFile" not in script
    assert "Remove-Item $StartupErrorFile" not in script
    assert "$UninstallerProcess.WaitForExit(120000)" in script
    assert "$InstallerProcess.WaitForExit(300000)" in script
    assert "Restore-ProcessEnvironment" in script
    assert "Resolve-OwnedInstalledProcess" in script
    assert "-ExpectedExecutable $Executable" in script


def test_windows_package_test_exercises_running_update_and_uninstall() -> None:
    script = (PROJECT_ROOT / "scripts/test_windows_package.ps1").read_text(encoding="utf-8")

    for expected in (
        '$UpdateLog = Join-Path $TestRoot "update.log"',
        "$OriginalProcessId = $process.Id",
        "Invoke-TestInstaller -Path $Setup -TargetDirectory $InstallDir -LogPath $UpdateLog",
        'throw "Die laufende Anwendung wurde beim Update nicht kontrolliert beendet."',
        "$restartedProcess = Resolve-OwnedInstalledProcess",
        'throw "Das persistente API-Zugriffstoken wurde beim Update unerwartet geändert."',
        "Invoke-TestUninstaller -Path $Uninstaller -LogPath $UninstallLog",
        'throw "Die laufende Anwendung wurde bei der Deinstallation nicht kontrolliert beendet."',
    ):
        assert expected in script

    update = script.index("Invoke-TestInstaller -Path $Setup -TargetDirectory $InstallDir -LogPath $UpdateLog")
    uninstall = script.index("Invoke-TestUninstaller -Path $Uninstaller -LogPath $UninstallLog")
    assert update < uninstall


def test_windows_package_test_exercises_packaged_pdf_report() -> None:
    script = (PROJECT_ROOT / "scripts/test_windows_package.ps1").read_text(encoding="utf-8")

    for expected in (
        '"http://127.0.0.1:$($runtime.port)/api/report/pdf"',
        '[System.Text.Encoding]::ASCII.GetString($PdfBytes, 0, 5) -ne "%PDF-"',
        'Content-Disposition:\\s*attachment; filename="E-Rechnungs-Pruefbericht\\.pdf"',
        "X-Einvoice-Syntax:\\s*CII",
        "X-Einvoice-Validation-Status:\\s*warning",
        "X-Einvoice-Official-Status:\\s*not-requested",
        "Der installierte PDF-Endpunkt veröffentlicht fachliche Daten in Antwort-Headern.",
    ):
        assert expected in script


def test_windows_package_test_callers_confirm_isolation() -> None:
    ci = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    release = (PROJECT_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    package_docs = (PROJECT_ROOT / "docs/WINDOWS_PACKAGE.md").read_text(encoding="utf-8")
    release_docs = (PROJECT_ROOT / "docs/RELEASE.md").read_text(encoding="utf-8")

    assert ".\\scripts\\test_windows_package.ps1 -ConfirmIsolatedEnvironment" in ci
    assert ".\\scripts\\test_windows_package.ps1 -RequireSignature -ConfirmIsolatedEnvironment" in release
    for documentation in (package_docs, release_docs):
        assert ".\\scripts\\test_windows_package.ps1 -ConfirmIsolatedEnvironment" in documentation
        assert "sauberen, entbehrlichen Windows-VM" in documentation
        assert "API-Token" in documentation
        assert "Autostart" in documentation


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None,
    reason="Die echte Pakettest-Vorabprüfung benötigt PowerShell unter Windows.",
)
def test_windows_package_preflight_preserves_existing_state(tmp_path: Path) -> None:
    local_app_data = tmp_path / "LocalAppData"
    app_data = tmp_path / "AppData"
    runner_temp = tmp_path / "RunnerTemp"
    state_directory = local_app_data / "E-Rechnungs-Pruefer"
    state_directory.mkdir(parents=True)
    sentinel = state_directory / "api-token.txt"
    sentinel.write_text("vorhandener-test-sentinel", encoding="utf-8")

    environment = os.environ.copy()
    environment.update(
        {
            "LOCALAPPDATA": str(local_app_data),
            "APPDATA": str(app_data),
            "RUNNER_TEMP": str(runner_temp),
        }
    )
    result = subprocess.run(
        [
            "pwsh",
            "-NoLogo",
            "-NoProfile",
            "-File",
            str(PROJECT_ROOT / "scripts/test_windows_package.ps1"),
            "-ConfirmIsolatedEnvironment",
            "-PreflightOnly",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert str(sentinel) in output
    assert sentinel.read_text(encoding="utf-8") == "vorhandener-test-sentinel"
    assert not list(runner_temp.glob("e-rechnungs-pruefer-package-test-*"))
