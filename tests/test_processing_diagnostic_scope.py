"""Bounded PowerShell diagnostic selection; synthetic calls, never an installation."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "scripts/test_processing_package.ps1"
DESKTOP = ROOT / "scripts/test_windows_package.ps1"
FULL_CASES = [
    "held-responses",
    "health",
    "worker-death",
    "supervisor-death",
    "parent-death",
    "controlled-stop",
    "xml25",
]


def quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def powershell(tmp_path: Path, source: str) -> dict[str, object]:
    executable = shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell is required for synthetic diagnostic-scope regressions")
    script = tmp_path / "synthetic-scope.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n$WarningPreference = 'SilentlyContinue'\n" + source, encoding="utf-8"
    )
    completed = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert isinstance(result, dict)
    return result


@pytest.mark.parametrize("mode,diagnostic", [("desktop", False), ("service", False), ("desktop", True)])
def test_fixed_case_selection_retains_default_and_historical_diagnostic_order(
    tmp_path: Path, mode: str, diagnostic: bool
) -> None:
    result = powershell(
        tmp_path,
        f". {quote(COMMON)}\n"
        "$null = Get-Command Get-BoundProcessingPackageCases -ErrorAction Stop\n"
        f"$Cases = @(Get-BoundProcessingPackageCases -Mode {mode} -ProcessingDiagnosticOnly:${str(diagnostic).lower()})\n"
        "@{cases=$Cases} | ConvertTo-Json -Compress\n",
    )
    assert result["cases"] == (["held-responses", "health"] if diagnostic else FULL_CASES)


def test_diagnostic_selection_rejects_service_mode(tmp_path: Path) -> None:
    result = powershell(
        tmp_path,
        f". {quote(COMMON)}\n"
        "try { Get-BoundProcessingPackageCases -Mode service -ProcessingDiagnosticOnly; throw 'unexpected pass' }\n"
        "catch { @{rejected=($_.Exception.Message -ne 'unexpected pass')} | ConvertTo-Json -Compress }\n",
    )
    assert result == {"rejected": True}


@pytest.mark.parametrize("bad_field", [None, "status", "scope", "cases", "exit"])
def test_diagnostic_scope_requires_exact_success_and_fixed_cases(tmp_path: Path, bad_field: str | None) -> None:
    receipt: dict[str, object] = {
        "status": "PASS",
        "scope": "processing-diagnostic-only",
        "cases": ["held-responses", "health"],
    }
    if bad_field in {"status", "scope"}:
        receipt[bad_field] = "wrong"
    if bad_field == "cases":
        receipt["cases"] = ["health", "held-responses"]
    code = 2 if bad_field == "exit" else 0
    result = powershell(
        tmp_path,
        f". {quote(COMMON)}\n"
        "$null = Get-Command Assert-BoundProcessingDiagnosticScope -ErrorAction Stop\n"
        "$env:GITHUB_ACTIONS='true'; $env:EINVOICE_ACCEPTANCE_ROOT='synthetic-root'; "
        "$env:EINVOICE_ACCEPTANCE_CONTROLLER='synthetic-controller'\n"
        "function Resolve-BoundProcessingPython { 'Invoke-SyntheticGuard' }\n"
        "function Invoke-SyntheticGuard { $script:GuardArguments=@($args); "
        f"$global:LASTEXITCODE={code}; {quote(json.dumps(receipt))} }}\n"
        f"try {{ Assert-BoundProcessingDiagnosticScope -Setup {quote(tmp_path / 'synthetic-setup.exe')}; "
        "$Status='accepted' } catch { $Status='rejected' }\n"
        "@{status=$Status; arguments=$script:GuardArguments} | ConvertTo-Json -Compress\n",
    )
    assert result["status"] == ("accepted" if bad_field is None else "rejected")
    assert result["arguments"] == [
        str(ROOT / "scripts/windows_package_diagnostic.py"),
        "verify-scope",
        "--setup",
        str(tmp_path / "synthetic-setup.exe"),
    ]


@pytest.mark.parametrize("missing", ["GITHUB_ACTIONS", "EINVOICE_ACCEPTANCE_ROOT", "EINVOICE_ACCEPTANCE_CONTROLLER"])
def test_diagnostic_scope_rejects_missing_ci_context_before_guard_launch(tmp_path: Path, missing: str) -> None:
    result = powershell(
        tmp_path,
        f". {quote(COMMON)}\n"
        "$env:GITHUB_ACTIONS='true'; $env:EINVOICE_ACCEPTANCE_ROOT='synthetic-root'; "
        "$env:EINVOICE_ACCEPTANCE_CONTROLLER='synthetic-controller'\n"
        f"Remove-Item Env:{missing}\n"
        "$script:Resolved=$false\n"
        "function Resolve-BoundProcessingPython { $script:Resolved=$true; throw 'must not run' }\n"
        "try { Assert-BoundProcessingDiagnosticScope -Setup 'synthetic.exe'; $Rejected=$false } "
        "catch { $Rejected=$true }\n"
        "@{rejected=$Rejected; resolved=$script:Resolved} | ConvertTo-Json -Compress\n",
    )
    assert result == {"rejected": True, "resolved": False}


def test_diagnostic_guard_and_parameter_flow_precede_product_mutations() -> None:
    desktop = DESKTOP.read_text(encoding="utf-8")
    common = COMMON.read_text(encoding="utf-8")
    assert "[switch]$ProcessingDiagnosticOnly" in desktop
    assert desktop.index("Assert-BoundProcessingDiagnosticScope -Setup $Setup") < desktop.index("$PackageTestMutex =")
    assert "if ($PreflightOnly)" in desktop.split("Assert-BoundProcessingDiagnosticScope -Setup $Setup")[0]
    assert "-ProcessingDiagnosticOnly:$ProcessingDiagnosticOnly -Setup $Setup" in desktop
    function = common.split("function Invoke-BoundProcessingPackageTests", 1)[1]
    assert function.index("Assert-BoundProcessingDiagnosticScope -Setup $Setup") < function.index(
        "New-Item -ItemType Directory"
    )
    assert "foreach ($Case in $Cases)" in function
    assert "Get-BoundProcessingPackageCases -Mode $Mode -ProcessingDiagnosticOnly:$ProcessingDiagnosticOnly" in function


def test_diagnostic_skips_other_cases_and_update_but_reaches_normal_uninstall(tmp_path: Path) -> None:
    result = powershell(
        tmp_path,
        f"$Ast = [Management.Automation.Language.Parser]::ParseFile({quote(DESKTOP)}, [ref]$null, [ref]$null)\n"
        "$Branches = @($Ast.FindAll({param($Node) $Node -is [Management.Automation.Language.IfStatementAst] "
        "-and $Node.Clauses[0].Item1.Extent.Text -eq '-not $ProcessingDiagnosticOnly' "
        "-and $Node.Extent.Text.Contains('$Bootstrap =')}, $true))\n"
        "if ($Branches.Count -ne 1) { throw 'Missing bounded full-only branch' }\n"
        "$Block=$Branches[0]; $ProcessingDiagnosticOnly=$true\n"
        "& ([scriptblock]::Create($Block.Extent.Text))\n"
        "$Uninstall=@($Ast.FindAll({param($Node) $Node -is [Management.Automation.Language.CommandAst] "
        "-and $Node.Extent.Text -eq 'Invoke-TestUninstaller -Path $Uninstaller -LogPath $UninstallLog'}, $true))\n"
        "@{skipped=$true; update_inside=$Block.Extent.Text.Contains('-LogPath $UpdateLog'); "
        "no_early_return=(@($Block.FindAll({param($Node) $Node -is [Management.Automation.Language.ReturnStatementAst]}, $true)).Count -eq 0); "
        "uninstall_after=(@($Uninstall | Where-Object {$_.Extent.StartOffset -gt $Block.Extent.EndOffset}).Count -eq 2)} "
        "| ConvertTo-Json -Compress\n",
    )
    assert result == {"skipped": True, "update_inside": True, "no_early_return": True, "uninstall_after": True}


@pytest.mark.parametrize("failed", [True, False])
def test_existing_finally_preserves_failed_diagnosis_and_only_cleans_owned_success(
    tmp_path: Path, failed: bool
) -> None:
    uninstaller = tmp_path / "synthetic-uninstaller.exe"
    uninstaller.write_bytes(b"never executed")
    result = powershell(
        tmp_path,
        f"$Ast = [Management.Automation.Language.Parser]::ParseFile({quote(DESKTOP)}, [ref]$null, [ref]$null)\n"
        "$Final = @($Ast.EndBlock.Statements | Where-Object { $_ -is [Management.Automation.Language.TryStatementAst] })[-1].Finally\n"
        "$script:Actions=[Collections.Generic.List[string]]::new()\n"
        "function Restore-ProcessEnvironment {}\n"
        "function Stop-OwnedProcess { $script:Actions.Add('stop-owned') }\n"
        "function Invoke-TestUninstaller { $script:Actions.Add('uninstall-owned') }\n"
        "function Get-OptionalRegistryValue { $null }\n"
        "function Test-ExpectedStringRegistryValue { $false }\n"
        "function Remove-ItemProperty { throw 'Unexpected registry mutation' }\n"
        f"$NativeProcessingProbeFailed=${str(failed).lower()}; $InstallationStarted=$true; $UninstallCompleted=$false\n"
        f"$Uninstaller={quote(uninstaller)}; $PackageTestMutexAcquired=$false; $PackageTestMutex=[IO.MemoryStream]::new()\n"
        "& ([scriptblock]::Create($Final.Extent.Text.Trim().Substring(1).TrimEnd().TrimEnd('}')))\n"
        "@{actions=@($script:Actions)} | ConvertTo-Json -Compress\n",
    )
    assert result["actions"] == ([] if failed else ["stop-owned", "stop-owned", "uninstall-owned"])


def test_diagnostic_preservation_lasts_until_normal_uninstall_checks_complete() -> None:
    source = DESKTOP.read_text(encoding="utf-8")
    assert "$NativeProcessingProbeFailed = [bool]$ProcessingDiagnosticOnly" in source
    start = source.index("$process = Invoke-BoundProcessingPackageTests")
    after_probe = source[start : source.index("$Bootstrap =", start)]
    assert "if (-not $ProcessingDiagnosticOnly) { $NativeProcessingProbeFailed = $false }" in after_probe
    last_check = source.index('throw "Der Autostart-Eintrag blieb nach der Deinstallation zurück."')
    assert last_check < source.index("$NativeProcessingProbeFailed = $false", last_check)
    assert "Windows-Verarbeitungsdiagnose erfolgreich" in source
    assert "keine vollständige Paketabnahme" in source


def test_scope_source_contract_is_independent_of_windows_default_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    read_text = Path.read_text

    def windows_read_text(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        return read_text(path, encoding=encoding or "cp1252", errors=errors)

    monkeypatch.setattr(Path, "read_text", windows_read_text)
    test_diagnostic_guard_and_parameter_flow_precede_product_mutations()
    test_diagnostic_preservation_lasts_until_normal_uninstall_checks_complete()
    test_diagnostic_cleanup_requires_fresh_scope_before_uninstaller()


def test_diagnostic_cleanup_requires_fresh_scope_before_uninstaller() -> None:
    source = DESKTOP.read_text(encoding="utf-8")
    invocation = source.index("Invoke-TestUninstaller -Path $Uninstaller -LogPath $UninstallLog")
    before = source[:invocation]
    scope = before.rindex("Assert-BoundProcessingDiagnosticScope -Setup $Setup")
    assert scope > before.index("$process = Invoke-BoundProcessingPackageTests")
    assert "if ($ProcessingDiagnosticOnly) {" in before[scope - 45 : scope]


def test_installer_evidence_retains_only_three_named_technical_files(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-installer-state"
    source.mkdir()
    (source / "install.log").write_bytes(b"synthetic install transcript\r\n")
    (source / "uninstall.log").write_bytes(b"synthetic uninstall transcript\r\n")
    for name in ("api-token.txt", "runtime.json", "cookies.txt", "unrelated.log"):
        (source / name).write_bytes(b"must not be read or copied")
    destination = tmp_path / "exclusive-evidence"
    result = powershell(
        tmp_path,
        f". {quote(COMMON)}\n"
        f"Save-BoundDiagnosticInstallerEvidence -SourceDirectory {quote(source)} "
        f"-EvidenceDirectory {quote(destination)} -RequireInstallLog -RequireUninstallLog 6>$null\n"
        f"Get-Content -LiteralPath {quote(destination / 'retention.json')} -Raw\n",
    )
    assert result["status"] == "PASS"
    assert {p.name for p in destination.iterdir()} == {"install.log", "uninstall.log", "retention.json"}
    retained = result["files"]
    assert isinstance(retained, list)
    records = {record["name"]: record for record in retained}

    for name in ("install.log", "uninstall.log"):
        assert (destination / name).read_bytes() == (source / name).read_bytes()
        assert records[name]["status"] == "retained"
        assert records[name]["sha256"] == hashlib.sha256((source / name).read_bytes()).hexdigest()
        assert records[name]["size"] == (source / name).stat().st_size
    assert records["uninstall-diagnostic.json"]["status"] == "missing"
    assert not (destination / "retention.json").read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("required_missing", [False, True])
def test_missing_technical_evidence_is_explicit_and_required_files_fail(tmp_path: Path, required_missing: bool) -> None:
    source = tmp_path / "not-created"
    destination = tmp_path / "missing-evidence"
    result = powershell(
        tmp_path,
        f". {quote(COMMON)}\n"
        f"try {{ Save-BoundDiagnosticInstallerEvidence -SourceDirectory {quote(source)} "
        f"-EvidenceDirectory {quote(destination)} -RequireInstallLog:${str(required_missing).lower()} 6>$null; "
        "$Failed=$false } catch { $Failed=$true }\n"
        f"$Receipt=Get-Content -LiteralPath {quote(destination / 'retention.json')} -Raw | ConvertFrom-Json\n"
        "@{failed=$Failed; receipt=$Receipt} | ConvertTo-Json -Depth 8 -Compress\n",
    )
    assert result["failed"] is required_missing
    receipt = result["receipt"]
    assert isinstance(receipt, dict)
    assert receipt["status"] == ("FAIL" if required_missing else "PASS")
    assert [r["name"] for r in receipt["files"]] == ["install.log", "uninstall.log", "uninstall-diagnostic.json"]
    assert all(r["status"] == "missing" for r in receipt["files"])


def test_installer_evidence_destination_is_exclusive_and_source_directories_rejected(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-input"
    source.mkdir()
    (source / "install.log").mkdir()
    (source / "uninstall.log").write_bytes(b"still retain this valid technical file")
    destination = tmp_path / "evidence"
    result = powershell(
        tmp_path,
        f". {quote(COMMON)}\n"
        f"try {{ Save-BoundDiagnosticInstallerEvidence -SourceDirectory {quote(source)} "
        f"-EvidenceDirectory {quote(destination)} 6>$null; $FirstFailed=$false }} catch {{ $FirstFailed=$true }}\n"
        f"$Before=(Get-FileHash -LiteralPath {quote(destination / 'retention.json')}).Hash\n"
        f"try {{ Save-BoundDiagnosticInstallerEvidence -SourceDirectory {quote(source)} "
        f"-EvidenceDirectory {quote(destination)} 6>$null; $SecondFailed=$false }} catch {{ $SecondFailed=$true }}\n"
        f"$After=(Get-FileHash -LiteralPath {quote(destination / 'retention.json')}).Hash\n"
        "@{first_failed=$FirstFailed; second_failed=$SecondFailed; unchanged=($Before -eq $After)} | ConvertTo-Json -Compress\n",
    )
    assert result == {"first_failed": True, "second_failed": True, "unchanged": True}
    assert (destination / "uninstall.log").read_bytes() == (source / "uninstall.log").read_bytes()
    receipt = json.loads((destination / "retention.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "FAIL" and receipt["files"][0]["status"] == "error"


@pytest.mark.parametrize("original_failure", [False, True])
def test_evidence_failure_preserves_original_exception_and_fails_otherwise_successful_run(
    tmp_path: Path, original_failure: bool
) -> None:
    result = powershell(
        tmp_path,
        f"$Ast=[Management.Automation.Language.Parser]::ParseFile({quote(DESKTOP)},[ref]$null,[ref]$null)\n"
        "$Outer=@($Ast.EndBlock.Statements | Where-Object {$_ -is [Management.Automation.Language.TryStatementAst]})[-1]\n"
        "if ($Outer.CatchClauses.Count -ne 1) { throw 'Missing original-error retention' }\n"
        "$PackageFailure=$null; $DiagnosticEvidenceFailure=$null; $ProcessingDiagnosticOnly=$true\n"
        "$NativeProcessingProbeFailed=$true; $PackageTestMutexAcquired=$false; $PackageTestMutex=[IO.MemoryStream]::new()\n"
        "function Restore-ProcessEnvironment {}\n"
        "function Save-BoundDiagnosticInstallerEvidence { throw 'synthetic-evidence-failure' }\n"
        "function Stop-OwnedProcess { throw 'Forbidden product cleanup' }\n"
        "$ProjectRoot='synthetic'; $TestRoot='synthetic'; $InstallationStarted=$false; $UninstallCompleted=$false\n"
        "$Code="
        + quote("try { " + ("throw 'original-package-finding'" if original_failure else "$null") + " } catch ")
        + " + $Outer.CatchClauses[0].Body.Extent.Text + ' finally ' + $Outer.Finally.Extent.Text\n"
        "try { & ([scriptblock]::Create($Code)) 3>$null; $Message='unexpected-success' } "
        "catch { $Message=$_.Exception.Message }\n"
        "@{message=$Message; mutex_disposed=(-not $PackageTestMutex.CanRead)} | ConvertTo-Json -Compress\n",
    )
    assert result == {
        "message": "original-package-finding" if original_failure else "synthetic-evidence-failure",
        "mutex_disposed": True,
    }
