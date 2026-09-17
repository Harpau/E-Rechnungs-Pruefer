# Dot-sourced only by the already bound disposable package controllers.
# No installation, global process search/kill, or recovery after a failed case.
function Invoke-BoundProcessingPackageTests {
    [CmdletBinding()]
    param(
        [ValidateSet("desktop", "service")][string]$Mode,
        [System.Diagnostics.Process]$ParentProcess,
        [string]$Executable,
        [string]$ExpectedExecutable,
        [int]$Port,
        [string]$TokenFile,
        [string]$OwnerSid,
        [string]$ServiceSid = "",
        [scriptblock]$StartBackend,
        [scriptblock]$StopBackend
    )
    if ($env:GITHUB_ACTIONS -ne "true" -or
        [string]::IsNullOrWhiteSpace($env:EINVOICE_ACCEPTANCE_ROOT) -or
        [string]::IsNullOrWhiteSpace($env:EINVOICE_ACCEPTANCE_CONTROLLER)) {
        throw "Native Paketproben benötigen den bereits konsumierten isolierten CI-Kontext."
    }
    $Root = Split-Path -Parent $PSScriptRoot
    $Harness = Join-Path $PSScriptRoot "test_processing_package.py"
    $HarnessHash = (Get-FileHash -LiteralPath $Harness -Algorithm SHA256).Hash.ToLowerInvariant()
    $ExpectedHash = (Get-FileHash -LiteralPath $ExpectedExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
    if ((Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ExpectedHash) {
        throw "Installierte EXE weicht vom gebundenen Buildbundle ab."
    }
    $EvidenceRoot = Join-Path $Root ".cache\acceptance-logs\processing-package"
    New-Item -ItemType Directory -Force $EvidenceRoot | Out-Null
    $Current = $ParentProcess
    $VerifyBinding = {
        param([Diagnostics.Process]$BoundProcess, [switch]$ContextOnly)
        $CheckCreated = $Created
        if (-not $ContextOnly) {
            $BoundProcess.Refresh()
            if ($BoundProcess.HasExited) { throw "Der zu prüfende gebundene Prozess ist beendet." }
            $CheckCreated = $BoundProcess.StartTime.ToUniversalTime().ToFileTimeUtc()
        }
        $CheckArguments = @(
            $Harness, "--confirm-isolated-environment", "--mode", $Mode, "--case", $Case,
            "--parent-pid", [string]$BoundProcess.Id, "--parent-created", [string]$CheckCreated,
            "--executable", $Executable, "--executable-sha256", $ExpectedHash,
            "--owner-sid", $OwnerSid, "--port", [string]$Port, "--token-file", $TokenFile,
            "--output-directory", $Evidence, "--verify-only", $(if ($ContextOnly) { "context" } else { "parent" })
        )
        if ($ServiceSid) { $CheckArguments += @("--service-sid", $ServiceSid) }
        $CheckRaw = & python @CheckArguments
        if ($LASTEXITCODE -ne 0) { throw "Aktuelle Kontext-/TTL-/Prozessprüfung fehlgeschlagen; keine Folgeaktion." }
        $Check = $CheckRaw | ConvertFrom-Json
        if ($Check.status -ne "PASS" -or $Check.controller.harness.sha256 -ne $HarnessHash) {
            throw "Read-only-Bindungsprüfung liefert keinen gültigen Beleg."
        }
        Write-Host "Processing binding $Mode/$Case $CheckRaw"
        return $Check
    }
    foreach ($Case in @("health", "worker-death", "supervisor-death", "parent-death", "controlled-stop", "xml25")) {
        $Current.Refresh()
        if ($Current.HasExited) { throw "Der gebundene Backendprozess endete vor der Probe." }
        $Created = $Current.StartTime.ToUniversalTime().ToFileTimeUtc()
        $Evidence = Join-Path $EvidenceRoot "$Mode-$Case"
        foreach ($Target in @($Evidence, "$Evidence.stdout.log", "$Evidence.stderr.log")) {
            if (Test-Path -LiteralPath $Target) { throw "Paketproben-Evidence wird nicht wiederverwendet." }
        }
        $Arguments = @(
            $Harness, "--confirm-isolated-environment", "--mode", $Mode, "--case", $Case,
            "--parent-pid", [string]$Current.Id, "--parent-created", [string]$Created,
            "--executable", $Executable, "--executable-sha256", $ExpectedHash,
            "--owner-sid", $OwnerSid, "--port", [string]$Port, "--token-file", $TokenFile,
            "--output-directory", $Evidence
        )
        if ($ServiceSid) { $Arguments += @("--service-sid", $ServiceSid) }
        $Info = [Diagnostics.ProcessStartInfo]::new()
        $Info.FileName = (Get-Command python -CommandType Application).Source
        $Info.UseShellExecute = $false
        $Info.RedirectStandardOutput = $true
        $Info.RedirectStandardError = $true
        foreach ($Argument in $Arguments) { $Info.ArgumentList.Add($Argument) }
        $Helper = [Diagnostics.Process]::Start($Info)
        $Stdout = $Helper.StandardOutput.ReadToEndAsync()
        $Stderr = $Helper.StandardError.ReadToEndAsync()
        try {
            if ($Case -eq "controlled-stop") {
                $ReadyFile = Join-Path $Evidence "ready.json"
                $Ready = $null
                $ReadyDeadline = [DateTime]::UtcNow.AddSeconds(30)
                while ($null -eq $Ready -and -not $Helper.HasExited -and [DateTime]::UtcNow -lt $ReadyDeadline) {
                    if (Test-Path -LiteralPath $ReadyFile) {
                        try { $Ready = Get-Content -LiteralPath $ReadyFile -Raw | ConvertFrom-Json }
                        catch { $Ready = $null }
                    }
                    if ($null -eq $Ready) { Start-Sleep -Milliseconds 20 }
                }
                $Plan = Get-Content (Join-Path $env:EINVOICE_ACCEPTANCE_ROOT "acceptance-plan.json") -Raw | ConvertFrom-Json
                $Running = @($Plan.contexts.PSObject.Properties.Value | Where-Object status -eq "RUNNING")
                $Current.Refresh()
                $Helper.Refresh()
                if ($null -eq $Ready -or $Ready.status -ne "READY" -or $Ready.case -ne $Case -or
                    $Ready.expected_controller_action -ne "stop-bound-parent" -or
                    $Ready.package.parent_pid -ne $Current.Id -or $Ready.package.parent_created -ne $Created -or
                    $Ready.package.executable_sha256 -ne $ExpectedHash -or $Ready.package.mode -ne $Mode -or
                    $Ready.controller.harness.sha256 -ne $HarnessHash -or
                    $Ready.nonce -notmatch '^[0-9a-f]{32}$' -or $Running.Count -ne 1 -or
                    $Running[0].id -ne $Ready.controller.context_id -or $Plan.blocked -or
                    $Plan.controller -ne $env:EINVOICE_ACCEPTANCE_CONTROLLER -or $Current.HasExited -or $Helper.HasExited -or
                    $Current.StartTime.ToUniversalTime().ToFileTimeUtc() -ne $Created) {
                    throw "Kein aktuelles exakt gebundenes Stop-READY; keine Stopaktion ausgeführt."
                }
                $StopGuard = & $VerifyBinding -BoundProcess $Current
                if ($StopGuard.controller.context_id -ne $Ready.controller.context_id -or $Helper.HasExited) {
                    throw "Stop-READY und aktuell geprüfter Kontext stimmen nicht überein."
                }
                # QPC is the Windows system monotonic counter used by the native
                # observer. Start before the actual stop: conservative deadline.
                $StopReceipt = [ordered]@{
                    schema_version = 1; action = "stop-bound-parent"; nonce = $Ready.nonce
                    package = $Ready.package; controller = $Ready.controller
                    ready_sha256 = (Get-FileHash -LiteralPath $ReadyFile -Algorithm SHA256).Hash.ToLowerInvariant()
                    qpc_ticks = [Diagnostics.Stopwatch]::GetTimestamp()
                    qpc_frequency = [Diagnostics.Stopwatch]::Frequency
                }
                $StopPath = Join-Path $Evidence "stop-action.json"
                $StopTemporaryPath = Join-Path $Evidence "stop-action.tmp"
                $StopStream = [IO.File]::Open($StopTemporaryPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try {
                    $StopBytes = [Text.UTF8Encoding]::new($false).GetBytes(($StopReceipt | ConvertTo-Json -Depth 20 -Compress))
                    $StopStream.Write($StopBytes, 0, $StopBytes.Length)
                    $StopStream.Flush($true)
                } finally { $StopStream.Dispose() }
                [IO.File]::Move($StopTemporaryPath, $StopPath)
                $AssertStopAllowed = {
                    # Called after the callback's last potentially blocking
                    # lookup, immediately at the real event/SCM stop action.
                    $Current.Refresh()
                    $Helper.Refresh()
                    $StopElapsed = ([Diagnostics.Stopwatch]::GetTimestamp() - $StopReceipt.qpc_ticks) / [double]$StopReceipt.qpc_frequency
                    if ($Helper.HasExited -or (Test-Path -LiteralPath (Join-Path $Evidence "result.json")) -or
                        $Current.HasExited -or $Current.StartTime.ToUniversalTime().ToFileTimeUtc() -ne $Created -or
                        [Diagnostics.Stopwatch]::Frequency -ne $StopReceipt.qpc_frequency -or $StopElapsed -lt 0 -or $StopElapsed -ge 1) {
                        throw "Stopfreigabe ist nicht mehr aktiv oder hat keine ausreichende Fristreserve; keine Stopaktion."
                    }
                }
                & $StopBackend $Current $AssertStopAllowed
            }
            if (-not $Helper.WaitForExit(95000)) {
                # Only the process object returned by Start is terminated. This is
                # failed evidence, never a substitute for product-tree cleanup.
                $Helper.Kill()
                [void]$Helper.WaitForExit(5000)
                throw "Der eigene Paketproben-Harness überschritt die äußere Frist."
            }
            $ExitCode = $Helper.ExitCode
            $ResultFile = Join-Path $Evidence "result.json"
            if (-not (Test-Path -LiteralPath $ResultFile)) { throw "Gebundener Ergebnisbeleg fehlt." }
            $Result = Get-Content -LiteralPath $ResultFile -Raw | ConvertFrom-Json
            if ($ExitCode -ne 0 -or $Result.status -ne "PASS" -or $Result.case -ne $Case -or
                $Result.package.parent_pid -ne $Current.Id -or $Result.package.parent_created -ne $Created -or
                $Result.package.executable_sha256 -ne $ExpectedHash -or $Result.package.mode -ne $Mode -or
                $Result.controller.harness.sha256 -ne $HarnessHash -or -not $Result.bound_role_exit_confirmed) {
                throw "Native Paketprobe $Mode/$Case ist kein gebundener PASS; Zustand bleibt erhalten."
            }
            if ($Case -in @("parent-death", "controlled-stop")) {
                $ReadyFile = Join-Path $Evidence "ready.json"
                if ($Result.ready_sha256 -ne (Get-FileHash -LiteralPath $ReadyFile -Algorithm SHA256).Hash.ToLowerInvariant() -or
                    -not $Current.WaitForExit(1000)) {
                    throw "Parentende oder READY-Ergebnisbindung ist unbestätigt."
                }
                if ($Case -eq "controlled-stop" -and (
                    $Result.stop_action_sha256 -ne (Get-FileHash -LiteralPath (Join-Path $Evidence "stop-action.json") -Algorithm SHA256).Hash.ToLowerInvariant() -or
                    $Result.role_exit_after_stop_seconds -gt 5 -or $Result.parent_exit_after_stop_seconds -gt 10)) {
                    throw "Stopaktion und getrennte Rollen-/Parentfristen sind nicht bestätigt."
                }
                $PreviousId = $Current.Id
                $StartGuard = & $VerifyBinding -BoundProcess $Current -ContextOnly
                if ($StartGuard.controller.context_id -ne $Result.controller.context_id) {
                    throw "Neustartkontext stimmt nicht mehr mit dem Prüfkontext überein."
                }
                $Next = & $StartBackend $PreviousId $Created
                if ($Next -isnot [Diagnostics.Process] -or $Next.Id -eq $PreviousId -or $Next.HasExited -or
                    $Next.StartTime.ToUniversalTime().ToFileTimeUtc() -le $Created) {
                    throw "Der kontrollierte Neustart liefert keine neue gebundene Prozessidentität."
                }
                $RestartGuard = & $VerifyBinding -BoundProcess $Next
                if ($RestartGuard.controller.context_id -ne $Result.controller.context_id) {
                    throw "Neue native Prozessbindung gehört nicht zum aktuellen Prüfkontext."
                }
                $Current.Dispose()
                $Current = $Next
            }
        } finally {
            # Preserve the own helper output even on failure. Never terminate or
            # uninstall the backend here after an unclassified native finding.
            if (-not $Helper.HasExited) {
                $Helper.Kill()
                [void]$Helper.WaitForExit(5000)
            }
            [IO.File]::WriteAllText("$Evidence.stdout.log", $Stdout.GetAwaiter().GetResult())
            [IO.File]::WriteAllText("$Evidence.stderr.log", $Stderr.GetAwaiter().GetResult())
            foreach ($EvidenceFile in @("$Evidence\ready.json", "$Evidence\stop-action.json", "$Evidence\result.json", "$Evidence.stdout.log", "$Evidence.stderr.log")) {
                if (Test-Path -LiteralPath $EvidenceFile) {
                    $Digest = (Get-FileHash -LiteralPath $EvidenceFile -Algorithm SHA256).Hash.ToLowerInvariant()
                    Write-Host "Processing evidence $Mode/$Case $EvidenceFile sha256=$Digest"
                }
            }
            $Helper.Dispose()
        }
    }
    return $Current
}
