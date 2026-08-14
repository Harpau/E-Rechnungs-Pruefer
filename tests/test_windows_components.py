from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "prepare_windows_components.py"
SPEC = importlib.util.spec_from_file_location("prepare_windows_components", SCRIPT_PATH)
assert SPEC and SPEC.loader
components = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(components)


def test_repository_component_lock_is_valid() -> None:
    locked = components._load_lock(PROJECT_ROOT / "packaging/windows/components.lock.json")
    kosit_lock = json.loads((PROJECT_ROOT / "packaging/kosit/components.lock.json").read_text(encoding="utf-8"))

    assert locked["validator"]["version"] == "KoSIT Validator 1.6.2"
    assert locked["xrechnung"]["version"].endswith("2026-01-31")
    assert locked["java"]["filename"].endswith("windows_hotspot_21.0.11_10.zip")
    assert locked["validator"] == kosit_lock["components"]["validator"]
    assert locked["xrechnung"] == kosit_lock["components"]["xrechnung"]
    assert kosit_lock["standards"]["cen_en16931"] == "1.3.15"


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
        "uses: azure/login@532459ea530d8321f2fb9bb10d1e0bcf23869a43",
        "client-id: ${{ secrets.AZURE_CLIENT_ID }}",
        "tenant-id: ${{ secrets.AZURE_TENANT_ID }}",
        "subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}",
        "AzureSignTool --tool-path $toolDirectory --version 7.0.1",
        "AZURE_KEY_VAULT_URL",
        "AZURE_CODE_SIGNING_CERTIFICATE",
        "test_windows_package.ps1 -RequireSignature -ConfirmIsolatedEnvironment",
        "git merge-base --is-ancestor $env:GITHUB_SHA origin/main",
        "git cat-file -t $env:GITHUB_REF_NAME",
        "Der Release-Tag muss annotiert sein.",
        "Manuelle Signiertests dürfen nur auf main gestartet werden.",
        'python-version: "3.13.14"',
        "python -m pip install --require-hashes --only-binary=:all:",
        "-r packaging/windows/requirements-release.txt",
        "python -m pip install --no-deps --no-build-isolation -e .",
    ):
        assert expected in workflow

    assert "WINDOWS_SIGNING_CERTIFICATE_BASE64" not in workflow
    assert "WINDOWS_SIGNING_CERTIFICATE_PASSWORD" not in workflow
    assert "AZURE_CLIENT_SECRET" not in workflow
    assert "creds:" not in workflow
    assert "azure/login@v2" not in workflow
    assert "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')" in workflow
    early_gate = workflow.index("Verify release ref before running repository code", workflow.index("windows-release:"))
    dependency_install = workflow.index("Install application and Windows build dependencies")
    azure_login = workflow.index("Authenticate to Azure with OIDC")
    assert early_gate < dependency_install < azure_login
    assert 'Get-Content -LiteralPath "VERSION" -Raw' in workflow[early_gate:dependency_install]
    assert "persist-credentials: false" in workflow
    draft_create = workflow.index('gh release create "${GITHUB_REF_NAME}"')
    asset_upload = workflow.index('gh release upload "${GITHUB_REF_NAME}"')
    assert draft_create < asset_upload
    assert "--draft" in workflow[draft_create:asset_upload]
    assert "--verify-tag" in workflow[draft_create:asset_upload]
    assert 'gh release edit "${GITHUB_REF_NAME}" --draft=false' not in workflow
    assert "artifact-ids: ${{ needs.source-release.outputs.artifact-id }}" in workflow
    assert "artifact-ids: ${{ needs.windows-release.outputs.artifact-id }}" in workflow
    assert 'release_state="$(gh release view' in workflow
    assert "cmp -s" in workflow
    assert "unerwartete Datei" in workflow
    assert "--clobber" not in workflow

    for expected in (
        "EINVOICE_AZURE_SIGN_TOOL",
        "EINVOICE_AZURE_KEY_VAULT_URL",
        "EINVOICE_AZURE_KEY_VAULT_CERTIFICATE",
        "--azure-key-vault-managed-identity",
        "--timestamp-rfc3161",
        "verify /pa /all /tw",
        "$Signature.TimeStamperCertificate",
    ):
        assert expected in build_script
    assert "$signature.TimeStamperCertificate" in workflow


def test_manual_release_preview_uploads_internal_recovery_installer_separately() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    release_docs = (PROJECT_ROOT / "docs/RELEASE.md").read_text(encoding="utf-8")

    inspect_start = workflow.index("      - name: Inspect all owned executable signatures")
    production_upload = workflow.index(
        "          name: windows-release-${{ github.run_id }}-${{ github.run_attempt }}", inspect_start
    )
    internal_upload = workflow.index("      - name: Upload signed internal recovery test installer")
    publish_start = workflow.index("\n  publish:")
    inspect_block = workflow[inspect_start:production_upload]
    production_block = workflow[production_upload:internal_upload]
    internal_block = workflow[internal_upload:publish_start]
    publish_block = workflow[publish_start:]

    assert "id: inspect_signatures" in inspect_block
    assert r"build\windows\test-installer\E-Rechnungs-Pruefer-$version-Windows-x64-Dienst-Setup.exe" in inspect_block
    assert "$internalTestInstaller" in inspect_block
    assert '$env:GITHUB_EVENT_NAME -eq "workflow_dispatch"' in inspect_block
    assert '$env:GITHUB_REF -eq "refs/heads/main"' in inspect_block
    assert "internal_test_installer=$internalTestInstaller" in inspect_block
    assert "$env:GITHUB_OUTPUT" in inspect_block

    assert "test-installer" not in production_block
    assert "INTERNAL-TEST-windows-recovery" not in production_block

    assert "if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'" in internal_block
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in internal_block
    assert "name: INTERNAL-TEST-windows-recovery-${{ github.run_id }}-${{ github.run_attempt }}" in internal_block
    assert "retention-days: 1" in internal_block
    assert "if-no-files-found: error" in internal_block
    assert "path: ${{ steps.inspect_signatures.outputs.internal_test_installer }}" in internal_block
    assert inspect_start < production_upload < internal_upload < publish_start

    assert "if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')" in publish_block
    assert "artifact-ids: ${{ needs.windows-release.outputs.artifact-id }}" in publish_block
    assert "artifact-ids: ${{ needs.source-release.outputs.artifact-id }}" in publish_block
    assert "name: Stage draft GitHub release" in publish_block
    assert "Stage retry-safe draft GitHub release" in publish_block
    assert 'gh release edit "${GITHUB_REF_NAME}" --draft=false' not in publish_block
    assert "INTERNAL-TEST-windows-recovery" not in publish_block
    assert "test-installer" not in publish_block

    source_upload = workflow[workflow.index("  source-release:") : workflow.index("  windows-release:")]
    assert "name: source-release-${{ github.run_id }}-${{ github.run_attempt }}" in source_upload
    for product_block in (source_upload, production_block):
        assert "retention-days: 14" in product_block
        assert "if-no-files-found: error" in product_block

    for expected in (
        "INTERNAL-TEST-windows-recovery-",
        "nur einen Tag",
        "/ALLOWELEVATEDTESTCONTEXT=1",
        "nicht als Produktinstaller",
        r"build\windows\test-installer\E-Rechnungs-Pruefer-<Version>-Windows-x64-Dienst-Setup.exe",
        r"bundle\desktop\*",
        r"build\windows\bundle\E-Rechnungs-Pruefer\*",
        r"build\windows\bundle\E-Rechnungs-Pruefer\E-Rechnungs-Pruefer.exe",
        r"dist\E-Rechnungs-Pruefer-<Version>-Windows-x64-Dienst-Setup.exe",
        "niemals von Tag-Läufen",
        "Windows 10 22H2 x64",
        "Best-Effort-Kompatibilität",
        "mehr als 260 Zeichen",
        "Windows 11 x64",
        "vorherige Patchversion → Zielversion",
        "Für 2.0.2 sind das",
        "nur bei den jeweils genannten Auslösern",
        "ja/nein",
        "taggenauen Artefakte",
        "einfacher synthetischer Analyseaufruf",
        "Ein Fehler sperrt die Veröffentlichung des Drafts",
        "veröffentlicht diesen Draft ausdrücklich nicht automatisch",
    ):
        assert expected in release_docs
    assert "Windows 10 ist für Patchreleases ein eigenes Pflichtsystem" not in release_docs
    assert "| Windows 10 x64 | Desktop | 1.5.0 → Zielversion" not in release_docs


def test_windows_release_dependencies_are_exactly_pinned_and_hashed() -> None:
    lock_path = PROJECT_ROOT / "packaging/windows/requirements-release.txt"
    requirement = re.compile(
        r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s]+) "
        r"--hash=sha256:(?P<digest>[0-9a-f]{64})"
    )
    locked: dict[str, str] = {}

    for line in lock_path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        match = requirement.fullmatch(line)
        assert match is not None, line
        name = match.group("name").lower().replace("_", "-")
        assert name not in locked
        locked[name] = match.group("version")

    assert len(locked) >= 40
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime_names = {
        re.split(r"[\[<>=!~; ]", dependency, maxsplit=1)[0].lower().replace("_", "-")
        for dependency in project["project"]["dependencies"]
    }
    assert runtime_names <= locked.keys()

    build_requirements = (PROJECT_ROOT / "packaging/windows/requirements-build.txt").read_text(encoding="utf-8")
    for line in build_requirements.splitlines():
        name, version = line.split("==", maxsplit=1)
        assert locked[name.lower().replace("_", "-")] == version

    for expected in ("pip", "pytest", "httpx", "httpx2", "setuptools", "wheel"):
        assert expected in locked


def test_pypdf_runtime_and_windows_lock_require_patched_version() -> None:
    minimum = Version("6.15.0")
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirement_files = [
        Requirement(value) for value in project["project"]["dependencies"] if Requirement(value).name.lower() == "pypdf"
    ]
    requirement_files.extend(
        Requirement(line)
        for line in (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#") and Requirement(line).name.lower() == "pypdf"
    )

    assert len(requirement_files) == 2
    assert all(minimum in requirement.specifier for requirement in requirement_files)
    assert all(Version("7.0.0") not in requirement.specifier for requirement in requirement_files)

    lock = (PROJECT_ROOT / "packaging/windows/requirements-release.txt").read_text(encoding="utf-8")
    match = re.search(
        r"^pypdf==(?P<version>[^\s]+) --hash=sha256:(?P<digest>[0-9a-f]{64})$",
        lock,
        flags=re.MULTILINE,
    )
    assert match is not None
    assert Version(match.group("version")) >= minimum
    assert match.group("digest") == "14e001d6504822cb1ca9c7ed9a69bccb320f59b320730f55af804361abe4d5ee"


def test_windows_desktop_package_covers_ui_revision_contract() -> None:
    package_test = (PROJECT_ROOT / "scripts/test_windows_package.ps1").read_text(encoding="utf-8")

    assert 'data-ui-revision="(?<revision>[0-9a-f]{64})"' in package_test
    assert "X-Einvoice-UI-Revision: $UiRevision" in package_test
    assert '"ui_version_mismatch"' in package_test
    assert "Bootstrap und Startseite besitzen nicht jeweils den sicheren no-store-Cachevertrag." in package_test
    assert "$NoStoreHeaderCount -ne 2" in package_test
    assert "public, max-age=31536000, immutable" in package_test
    asset_headers = package_test.index("--dump-header $JavascriptHeaders")
    asset_start = package_test.rfind("& curl.exe", 0, asset_headers)
    asset_end = package_test.index('"http://127.0.0.1:$($runtime.port)/static/$UiRevision/app.js"', asset_start)
    assert "--cookie $CookieFile" in package_test[asset_start:asset_end]
    bearer_start = package_test.index("Authorization: Bearer $ApiToken")
    bearer_end = package_test.index('"http://127.0.0.1:$($runtime.port)/api/analyze"', bearer_start)
    assert "X-Einvoice-UI-Revision" not in package_test[bearer_start:bearer_end]


def test_windows_service_package_covers_ui_revision_contract() -> None:
    package_test = (PROJECT_ROOT / "scripts/test_windows_service_package.ps1").read_text(encoding="utf-8")

    assert "from app.windows_service_ipc import request_browser_url" in package_test
    assert 'data-ui-revision="(?<revision>[0-9a-f]{64})"' in package_test
    assert "$NoStoreHeaderCount -ne 2" in package_test
    assert "public, max-age=31536000, immutable" in package_test
    assert '"http://127.0.0.1:$Port/api/examples/cii"' in package_test
    assert '$MissingBrowserRevisionStatus -ne "409"' in package_test
    assert "ui_version_mismatch" in package_test
    assert "X-Einvoice-UI-Revision: $BrowserRevision" in package_test
    assert "Remove-Item -LiteralPath $BrowserSecretPath" in package_test


def test_windows_installer_offers_removable_per_user_autostart() -> None:
    installer = (PROJECT_ROOT / "packaging/windows/installer.iss").read_text(encoding="utf-8")

    assert 'Name: "autostart"; Description: "Bei Windows-Anmeldung automatisch starten"' in installer
    assert 'Root: HKCU; Subkey: "Software\\Microsoft\\Windows\\CurrentVersion\\Run"' in installer
    assert 'ValueData: """{app}\\{#AppExeName}"" --background"' in installer
    assert "Check: not WizardIsTaskSelected('autostart')" in installer
    assert "Flags: uninsdeletevalue; Tasks: autostart" in installer


def test_windows_installer_stops_running_app_for_update_and_uninstall() -> None:
    installer = (PROJECT_ROOT / "packaging/windows/installer.iss").read_text(encoding="utf-8")
    launcher = (PROJECT_ROOT / "app/windows_launcher.py").read_text(encoding="utf-8")

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

    assert r'WINDOWS_MUTEX_NAME = "Local\\E-Rechnungs-Pruefer-Desktop"' in launcher
    assert "class WindowsProcessLifetimeMutex:" in launcher
    lifetime_mutex = launcher[
        launcher.index("class WindowsProcessLifetimeMutex") : launcher.index("class WindowsShutdownEvent")
    ]
    assert "def close" not in lifetime_mutex
    launcher_main = launcher[launcher.index("def main(") :]
    assert "\n                mutex.close()" not in launcher_main


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
        '"/DIR=`"$TargetDirectory`""',
        'throw "Der Desktop-Installer ignorierte den expliziten benutzerdefinierten /DIR-Zielpfad."',
        '$UpdateLog = Join-Path $TestRoot "update.log"',
        "$OriginalProcessId = $process.Id",
        "Invoke-TestInstaller -Path $Setup -TargetDirectory $InstallDir -LogPath $UpdateLog",
        'throw "Die laufende Anwendung wurde beim Update nicht kontrolliert beendet."',
        "$restartedProcess = Resolve-OwnedInstalledProcess",
        'throw "Das persistente API-Zugriffstoken wurde beim Update unerwartet geändert."',
        "Assert-NativeDesktopModulesLoaded -OwnedProcess $restartedProcess -InstallDirectory $InstallDir",
        "Invoke-TestUninstaller -Path $Uninstaller -LogPath $UninstallLog",
        'throw "Die laufende Anwendung wurde bei der Deinstallation nicht kontrolliert beendet."',
        "Assert-InstallTreeRemoved",
        "-Path $InstallDir",
        "-DiagnosticPath $UninstallDiagnostic",
        "-UninstallLogPath $UninstallLog",
    ):
        assert expected in script

    update = script.index("Invoke-TestInstaller -Path $Setup -TargetDirectory $InstallDir -LogPath $UpdateLog")
    native_modules = script.index(
        "Assert-NativeDesktopModulesLoaded -OwnedProcess $restartedProcess -InstallDirectory $InstallDir"
    )
    uninstall = script.index("Invoke-TestUninstaller -Path $Uninstaller -LogPath $UninstallLog")
    process_exit = script.index("$restartedProcess.WaitForExit(10000)", uninstall)
    complete_tree = script.index("Assert-InstallTreeRemoved `", process_exit)
    assert update < native_modules < uninstall < process_exit < complete_tree


def test_windows_package_test_preserves_evidence_for_incomplete_uninstall() -> None:
    script = (PROJECT_ROOT / "scripts/test_windows_package.ps1").read_text(encoding="utf-8")

    wait_helper = script[
        script.index("function Wait-SetupUninstallMutexReleased") : script.index(
            "function Assert-NativeDesktopModulesLoaded"
        )
    ]
    assert '"Global\\E-Rechnungs-Pruefer-Setup-Uninstall"' in wait_helper
    assert "$Mutex.WaitOne($Seconds * 1000)" in wait_helper
    assert "Start-Sleep" not in wait_helper

    invoke_uninstaller = script[
        script.index("function Invoke-TestUninstaller") : script.index("function Invoke-TestInstaller")
    ]
    assert invoke_uninstaller.index("$UninstallerProcess.WaitForExit(120000)") < invoke_uninstaller.index(
        "Wait-SetupUninstallMutexReleased"
    )

    native_helper = script[
        script.index("function Assert-NativeDesktopModulesLoaded") : script.index("function Assert-InstallTreeRemoved")
    ]
    assert "_internal\\watchfiles\\_rust_notify*.pyd" in native_helper
    assert "_internal\\websockets\\speedups*.pyd" in native_helper
    assert "$OwnedProcess.Modules" in native_helper

    residue_helper = script[
        script.index("function Assert-InstallTreeRemoved") : script.index("function Resolve-OwnedInstalledProcess")
    ]
    for expected in (
        "Test-Path -LiteralPath $Path",
        "Get-ChildItem -LiteralPath $Path -Force -Recurse",
        "ConvertTo-Json -Depth 5",
        "Set-Content -LiteralPath $DiagnosticPath",
        "Restinventar: $DiagnosticPath",
        "Uninstall-Log: $UninstallLogPath",
    ):
        assert expected in residue_helper
    assert "Start-Sleep" not in residue_helper
    assert "Remove-Item" not in residue_helper

    uninstall = script.index("Invoke-TestUninstaller -Path $Uninstaller -LogPath $UninstallLog")
    completed = script.index("$UninstallCompleted = $true", uninstall)
    complete_tree = script.index("Assert-InstallTreeRemoved `", completed)
    cleanup = script.index("if ($InstallationStarted -and -not $UninstallCompleted)", complete_tree)
    assert uninstall < completed < complete_tree < cleanup
    assert "Remove-Item $TestRoot -Recurse" not in script


def test_windows_package_test_exercises_packaged_pdf_report() -> None:
    script = (PROJECT_ROOT / "scripts/test_windows_package.ps1").read_text(encoding="utf-8")

    for expected in (
        '"http://127.0.0.1:$($runtime.port)/api/report/pdf"',
        '[System.Text.Encoding]::ASCII.GetString($PdfBytes, 0, 5) -ne "%PDF-"',
        'Content-Disposition:\\s*attachment; filename="E-Rechnungs-Pruefbericht\\.pdf"',
        "X-Einvoice-Analysis-Schema:\\s*2",
        "X-Einvoice-Syntax:\\s*CII",
        "X-Einvoice-Conformity-Status:\\s*not-requested",
        "X-Einvoice-Internal-Status:\\s*attention",
        "X-Einvoice-Processing-Status:\\s*complete",
        "X-Einvoice-Report-Scope:\\s*readable",
        "if ($PdfResponseHeaders -match $ForbiddenHeader)",
        "nicht mehr zulässigen Legacy-Header",
        "Der installierte PDF-Endpunkt veröffentlicht fachliche Daten in Antwort-Headern.",
    ):
        assert expected in script

    assert "(?im)^X-Einvoice-Validation-Status\\s*:" in script
    assert "(?im)^X-Einvoice-Official-Status\\s*:" in script


def test_windows_package_test_uses_only_the_schema_two_analysis_contract() -> None:
    script = (PROJECT_ROOT / "scripts/test_windows_package.ps1").read_text(encoding="utf-8")

    for expected in (
        "$Analysis.schema_version -ne 2",
        '$Analysis.assessment.official.status -ne "not-requested"',
        '$Analysis.assessment.internal.status -ne "attention"',
        '$Analysis.assessment.processing.status -ne "complete"',
        "$Official.schema_version -ne 2",
        "$Official.assessment.official.executed",
        '$Official.assessment.official.status -ne "accepted"',
    ):
        assert expected in script

    assert ".validation.official" not in script


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
