[CmdletBinding()]
param(
    [string]$Setup = "",
    [switch]$RequireSignature,
    [switch]$ConfirmIsolatedEnvironment,
    [switch]$AllowElevatedMigrationTestContext,
    [switch]$PreflightOnly,
    [ValidateSet("None", "Immediate", "LeaveForReboot")]
    [string]$CommitHardKillRecovery = "None"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Wait-ServiceState {
    param([string]$Name, [string]$State, [int]$Seconds = 330)
    $Deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        $Candidate = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($null -eq $Candidate) {
            if ($State -eq "Absent") { return }
        } else {
            $Candidate.Refresh()
            if ([string]::Equals([string]$Candidate.Status, $State, [StringComparison]::OrdinalIgnoreCase)) {
                return
            }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $Deadline)
    throw "Dienst $Name erreichte Zustand $State nicht innerhalb des Zeitlimits."
}

function Invoke-WindowedExecutable {
    param(
        [string]$Path,
        [string[]]$Arguments,
        [int]$TimeoutMilliseconds = 60000
    )
    $StartInfo = [Diagnostics.ProcessStartInfo]::new()
    $StartInfo.FileName = $Path
    $StartInfo.UseShellExecute = $false
    foreach ($Argument in $Arguments) {
        [void]$StartInfo.ArgumentList.Add($Argument)
    }
    $Process = [Diagnostics.Process]::Start($StartInfo)
    if ($null -eq $Process) {
        throw "Der windowed Testprozess konnte nicht gestartet werden: $Path"
    }
    try {
        if (-not $Process.WaitForExit($TimeoutMilliseconds)) {
            $Process.Kill($true)
            $Process.WaitForExit()
            throw "Der windowed Testprozess überschritt das Zeitlimit: $Path"
        }
        return [int]$Process.ExitCode
    } finally {
        $Process.Dispose()
    }
}

function Test-BytePrefix {
    param([byte[]]$Prefix, [byte[]]$Value)
    if ($Value.Length -lt $Prefix.Length) { return $false }
    for ($Index = 0; $Index -lt $Prefix.Length; $Index++) {
        if ($Prefix[$Index] -ne $Value[$Index]) { return $false }
    }
    return $true
}

function Invoke-ServiceInstaller {
    param(
        [string]$Path,
        [string]$LogPath,
        [string]$Tasks = "systemstart",
        [string[]]$ExtraArguments = @()
    )
    $Arguments = @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
        "/TASKS=`"$Tasks`"", "/LOG=`"$LogPath`""
    ) + $ExtraArguments
    if ($AllowElevatedMigrationTestContext) {
        $Arguments += "/ALLOWELEVATEDTESTCONTEXT=1"
    }
    $Process = Start-Process $Path -ArgumentList $Arguments -PassThru
    if (-not $Process.WaitForExit(600000)) {
        throw "Der Dienst-Installer überschritt das Zeitlimit."
    }
    if ($Process.ExitCode -ne 0) {
        $LogTail = if (Test-Path -LiteralPath $LogPath) {
            (Get-Content -LiteralPath $LogPath -Tail 80) -join "`n"
        } else {
            "Der angeforderte Inno-Setup-Log wurde nicht erzeugt: $LogPath"
        }
        throw "Der Dienst-Installer schlug mit Exitcode $($Process.ExitCode) fehl.`n$LogTail"
    }
}

function Get-TreeFingerprint {
    param([string]$Path)
    $Lines = Get-ChildItem -LiteralPath $Path -Recurse -File | Sort-Object FullName | ForEach-Object {
        $Relative = [IO.Path]::GetRelativePath($Path, $_.FullName).Replace('\', '/')
        "$Relative $((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash)"
    }
    $Bytes = [Text.Encoding]::UTF8.GetBytes(($Lines -join "`n"))
    $Sha = [Security.Cryptography.SHA256]::Create()
    try { return [Convert]::ToHexString($Sha.ComputeHash($Bytes)) }
    finally { $Sha.Dispose() }
}

function Invoke-ServiceInstallerExpectedFailure {
    param(
        [string]$Path,
        [string]$LogPath,
        [string[]]$ExtraArguments,
        [ValidateNotNullOrEmpty()]
        [string]$ExpectedLogReason
    )
    $Arguments = @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
        "/TASKS=`"systemstart`"", "/LOG=`"$LogPath`""
    ) + $ExtraArguments
    if ($AllowElevatedMigrationTestContext) {
        $Arguments += "/ALLOWELEVATEDTESTCONTEXT=1"
    }
    $Process = Start-Process $Path -ArgumentList $Arguments -PassThru
    if (-not $Process.WaitForExit(600000)) {
        throw "Der absichtlich fehlschlagende Dienst-Installer überschritt das Zeitlimit."
    }
    if ($Process.ExitCode -eq 0) {
        $LogTail = if (Test-Path -LiteralPath $LogPath) {
            (Get-Content -LiteralPath $LogPath -Tail 80) -join "`n"
        } else {
            "Kein Inno-Setup-Log vorhanden: $LogPath"
        }
        throw "Der erwartete Installer-Fehler wurde nicht ausgelöst: $ExpectedLogReason`n$LogTail"
    }
    if (-not (Test-Path -LiteralPath $LogPath)) {
        throw "Der fehlgeschlagene Dienst-Installer erzeugte keinen Inno-Setup-Log: $LogPath"
    }
    $Log = Get-Content -LiteralPath $LogPath -Raw
    if ($Log.IndexOf($ExpectedLogReason, [StringComparison]::Ordinal) -lt 0) {
        $LogTail = (Get-Content -LiteralPath $LogPath -Tail 80) -join "`n"
        throw "Der Dienst-Installer schlug aus einem unerwarteten Grund fehl. " +
            "Erwartet: $ExpectedLogReason`n$LogTail"
    }
}

function Assert-NoEarlyInstallerState {
    param(
        [string]$Scenario,
        [string[]]$Paths
    )
    foreach ($Path in $Paths) {
        if (Test-Path -LiteralPath $Path) {
            throw "$Scenario ließ unerwarteten Installer- oder Transferzustand zurück: $Path"
        }
    }
}

function Assert-CanonicalJsonElement {
    param([Text.Json.JsonElement]$Element, [string]$Path)
    if ($Element.ValueKind -eq [Text.Json.JsonValueKind]::Object) {
        $Names = [Collections.Generic.List[string]]::new()
        $Seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        foreach ($Property in $Element.EnumerateObject()) {
            if (-not $Seen.Add($Property.Name)) { throw "Doppeltes JSON-Feld in $Path`: $($Property.Name)" }
            $Names.Add($Property.Name)
            Assert-CanonicalJsonElement -Element $Property.Value -Path "$Path.$($Property.Name)"
        }
        $SortedNames = @($Names | Sort-Object)
        if (Compare-Object -ReferenceObject @($Names) -DifferenceObject $SortedNames -SyncWindow 0) {
            throw "JSON-Felder sind nicht kanonisch sortiert: $Path"
        }
    } elseif ($Element.ValueKind -eq [Text.Json.JsonValueKind]::Array) {
        $Index = 0
        foreach ($Value in $Element.EnumerateArray()) {
            Assert-CanonicalJsonElement -Element $Value -Path "$Path[$Index]"
            $Index += 1
        }
    }
}

function Read-StrictJsonMarker {
    param([string]$Path, [string[]]$ExpectedProperties)
    $Item = Get-Item -LiteralPath $Path -Force
    if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $Item.PSIsContainer -or $Item.Length -le 0 -or $Item.Length -gt 65536) {
        throw "Transaktionsmarker ist kein kleines reguläres lokales Dokument: $Path"
    }
    $Bytes = [IO.File]::ReadAllBytes($Path)
    if ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xEF -and $Bytes[1] -eq 0xBB -and $Bytes[2] -eq 0xBF) {
        throw "Transaktionsmarker enthält unerwartet eine UTF-8-BOM: $Path"
    }
    $Utf8 = [Text.UTF8Encoding]::new($false, $true)
    $Text = $Utf8.GetString($Bytes)
    if (-not $Text.EndsWith("`n", [StringComparison]::Ordinal) -or $Text.Contains("`r")) {
        throw "Transaktionsmarker ist nicht im erwarteten kanonischen Zeilenformat: $Path"
    }
    $InString = $false
    $Escaped = $false
    for ($Index = 0; $Index -lt $Text.Length; $Index += 1) {
        $Character = $Text[$Index]
        if ($InString) {
            if ($Escaped) { $Escaped = $false }
            elseif ($Character -eq "\") { $Escaped = $true }
            elseif ($Character -eq '"') { $InString = $false }
            elseif ([int]$Character -gt 127) {
                throw "Transaktionsmarker enthält nicht kanonisch escaptes Nicht-ASCII-JSON: $Path"
            }
        } elseif ($Character -eq '"') {
            $InString = $true
        } elseif ([char]::IsWhiteSpace($Character) -and $Index -ne ($Text.Length - 1)) {
            throw "Transaktionsmarker enthält nicht kanonische JSON-Trennzeichen: $Path"
        }
    }
    $Document = [Text.Json.JsonDocument]::Parse($Text)
    try {
        if ($Document.RootElement.ValueKind -ne [Text.Json.JsonValueKind]::Object) {
            throw "Transaktionsmarker ist kein JSON-Objekt: $Path"
        }
        Assert-CanonicalJsonElement -Element $Document.RootElement -Path '$'
    } finally {
        $Document.Dispose()
    }
    $Record = $Text | ConvertFrom-Json
    $Observed = @($Record.PSObject.Properties.Name | Sort-Object)
    $Expected = @($ExpectedProperties | Sort-Object)
    if (Compare-Object -ReferenceObject $Expected -DifferenceObject $Observed) {
        throw "Transaktionsmarker besitzt unerwartete Felder: $Path"
    }
    return $Record
}

function Assert-AdministrativeCheckpointAcl {
    param([string]$Path)
    $Acl = Get-Acl -LiteralPath $Path
    if (-not $Acl.AreAccessRulesProtected) { throw "Checkpoint-Datei besitzt eine vererbte DACL: $Path" }
    $OwnerSid = ([Security.Principal.NTAccount]$Acl.Owner).Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
    if ($OwnerSid -ne "S-1-5-32-544") { throw "Checkpoint-Datei besitzt einen unerwarteten Besitzer: $Path" }
    $Rules = @($Acl.Access)
    $ObservedSids = @(
        $Rules | ForEach-Object {
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        }
    )
    $ExpectedSids = @("S-1-5-18", "S-1-5-32-544")
    if ($Rules.Count -ne 2 -or (Compare-Object $ExpectedSids $ObservedSids)) {
        throw "Checkpoint-Datei besitzt nicht exakt die administrativen Identitäten: $Path"
    }
    foreach ($Rule in $Rules) {
        if ($Rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $Rule.IsInherited -or
            $Rule.InheritanceFlags -ne [Security.AccessControl.InheritanceFlags]::None -or
            $Rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None -or
            $Rule.FileSystemRights -ne
            [Security.AccessControl.FileSystemRights]::FullControl) {
            throw "Checkpoint-Datei besitzt keine exakt administrative Vollzugriff-DACL: $Path"
        }
    }
}

function Stop-VerifiedSetupProcessTree {
    param([Diagnostics.Process]$Process, [string]$ExpectedPath)
    if ($Process.HasExited) { throw "Setup endete, bevor der Hard-Kill-Checkpoint gesichert werden konnte." }
    $ProcessRecord = Get-CimInstance Win32_Process -Filter "ProcessId = $($Process.Id)"
    if (-not $ProcessRecord -or [string]::IsNullOrWhiteSpace($ProcessRecord.ExecutablePath)) {
        throw "Der zu beendende Setup-Prozess konnte nicht eindeutig inventarisiert werden."
    }
    $Expected = [IO.Path]::GetFullPath($ExpectedPath)
    $Observed = [IO.Path]::GetFullPath([string]$ProcessRecord.ExecutablePath)
    if (-not [string]::Equals($Expected, $Observed, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Der beobachtete Prozess ist nicht der gestartete Testinstaller: $Observed"
    }
    try {
        $Process.Kill($true)
    } catch {
        throw "Der eindeutig identifizierte Setup-Prozessbaum konnte nicht hart beendet werden."
    }
    if (-not $Process.WaitForExit(30000)) {
        throw "Der hart beendete Setup-Prozessbaum blieb unerwartet aktiv."
    }
}

function Invoke-CommitCheckpointHardKill {
    param(
        [string]$Path,
        [string[]]$Arguments,
        [string]$ExpectedServiceExecutable,
        [string]$ExpectedServiceName,
        [string]$ExpectedReaderSid
    )
    $ServiceDirectory = Split-Path $ExpectedServiceExecutable -Parent
    $InstallationDirectory = Split-Path $ServiceDirectory -Parent
    $StateDirectory = Join-Path $InstallationDirectory ".installer-state"
    $PreparedPath = Join-Path $StateDirectory "install-transaction.prepared.json"
    $PhasePath = Join-Path $StateDirectory "install-transaction.phase.json"
    $Process = Start-Process $Path -ArgumentList $Arguments -PassThru
    $Deadline = [DateTime]::UtcNow.AddSeconds(180)
    $TransactionId = ""
    $PreparedLock = $null
    $PhaseLock = $null
    do {
        if ($Process.HasExited) {
            throw "Setup endete, ohne den COMMIT_STARTED-Checkpoint beobachtbar zu hinterlassen."
        }
        if ((Test-Path -LiteralPath $PreparedPath) -and (Test-Path -LiteralPath $PhasePath)) {
            $CandidatePreparedLock = $null
            $CandidateLock = $null
            try {
                $CandidatePreparedLock = [IO.File]::Open(
                    $PreparedPath,
                    [IO.FileMode]::Open,
                    [IO.FileAccess]::Read,
                    [IO.FileShare]::Read
                )
                $CandidateLock = [IO.File]::Open(
                    $PhasePath,
                    [IO.FileMode]::Open,
                    [IO.FileAccess]::Read,
                    [IO.FileShare]::Read
                )
                $Prepared = Read-StrictJsonMarker -Path $PreparedPath -ExpectedProperties @(
                    "desktop_binding", "expected_executable", "machine_before", "schema_version",
                    "service_before", "target", "transaction_id"
                )
                $Phase = Read-StrictJsonMarker -Path $PhasePath -ExpectedProperties @(
                    "phase", "prepared_sha256", "schema_version", "transaction_id"
                )
                if ($Phase.phase -eq "commit_started") {
                    $PreparedHash = (Get-FileHash -LiteralPath $PreparedPath -Algorithm SHA256).Hash.ToLowerInvariant()
                    if ($Prepared.schema_version -ne 1 -or $Phase.schema_version -ne 1 -or
                        $Prepared.transaction_id -notmatch "^[0-9a-f]{32}$" -or
                        $Phase.transaction_id -ne $Prepared.transaction_id -or
                        $Phase.prepared_sha256 -ne $PreparedHash -or
                        $Prepared.desktop_binding.reader_sid -ne $ExpectedReaderSid -or
                        $Prepared.desktop_binding.seal_sha256 -notmatch "^[0-9a-f]{64}$" -or
                        -not $Prepared.service_before.existed -or
                        -not $Prepared.service_before.running -or
                        $null -eq $Prepared.service_before.metadata -or
                        -not $Prepared.machine_before.configuration -or
                        -not $Prepared.machine_before.token -or
                        -not $Prepared.machine_before.logs -or
                        -not $Prepared.target.service_running -or
                        $Prepared.target.token_transfer_consent -or
                        -not [string]::Equals(
                            [string]$Prepared.expected_executable,
                            [IO.Path]::GetFullPath($ExpectedServiceExecutable),
                            [StringComparison]::OrdinalIgnoreCase
                        )) {
                        throw "Der beobachtete COMMIT_STARTED-Marker ist nicht an das erwartete Manifest gebunden."
                    }
                    $Service = Get-CimInstance Win32_Service -Filter "Name='$ExpectedServiceName'"
                    if (-not $Service -or $Service.State -ne "Running" -or
                        -not [string]::Equals(
                            ([string]$Service.PathName).Trim('"'),
                            [IO.Path]::GetFullPath($ExpectedServiceExecutable),
                            [StringComparison]::OrdinalIgnoreCase
                        ) -or
                        -not (Test-Path -LiteralPath $ServiceDirectory) -or
                        (Test-Path -LiteralPath (Join-Path $InstallationDirectory "service.new")) -or
                        (Test-Path -LiteralPath (Join-Path $InstallationDirectory "service.rollback"))) {
                        throw "COMMIT_STARTED wurde nicht zusammen mit dem vollständig aktivierten Dienst beobachtet."
                    }
                    $PreparedLock = $CandidatePreparedLock
                    $CandidatePreparedLock = $null
                    $PhaseLock = $CandidateLock
                    $CandidateLock = $null
                    $TransactionId = [string]$Prepared.transaction_id
                    break
                }
            } catch [IO.IOException] {
                # Final cleanup may win the race. In that case this attempt must not kill another process later.
            } finally {
                if ($null -ne $CandidatePreparedLock) { $CandidatePreparedLock.Dispose() }
                if ($null -ne $CandidateLock) { $CandidateLock.Dispose() }
            }
        }
        Start-Sleep -Milliseconds 10
    } while ([DateTime]::UtcNow -lt $Deadline)
    if (-not $TransactionId) {
        throw "COMMIT_STARTED-Hard-Kill-Checkpoint wurde nicht rechtzeitig und eindeutig beobachtet."
    }

    try {
        Stop-VerifiedSetupProcessTree -Process $Process -ExpectedPath $Path
    } finally {
        if ($null -ne $PreparedLock) { $PreparedLock.Dispose() }
        if ($null -ne $PhaseLock) { $PhaseLock.Dispose() }
    }

    if (-not (Test-Path -LiteralPath $PreparedPath) -or -not (Test-Path -LiteralPath $PhasePath)) {
        throw "Die Abschlussmarker wurden vor dem Hard Kill bereits finalisiert; der Checkpoint gilt als verfehlt."
    }
    Assert-AdministrativeCheckpointAcl -Path $PreparedPath
    Assert-AdministrativeCheckpointAcl -Path $PhasePath
    $PersistedPhase = Read-StrictJsonMarker -Path $PhasePath -ExpectedProperties @(
        "phase", "prepared_sha256", "schema_version", "transaction_id"
    )
    $PersistedPreparedHash = (
        Get-FileHash -LiteralPath $PreparedPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($PersistedPhase.transaction_id -ne $TransactionId -or
        $PersistedPhase.phase -ne "commit_started" -or
        $PersistedPhase.prepared_sha256 -ne $PersistedPreparedHash) {
        throw "COMMIT_STARTED blieb nach dem Hard Kill nicht unverändert erhalten."
    }
    Wait-ServiceState -Name $ExpectedServiceName -State "Running" -Seconds 30
    Write-Host "Dienst-Hard-Kill nach COMMIT_STARTED eindeutig erfasst."
}

function Invoke-ServiceUninstaller {
    param([string]$Path, [string]$LogPath, [switch]$PurgeData)
    $Arguments = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=`"$LogPath`"")
    if ($PurgeData) { $Arguments += "/PURGEDATA=1" }
    $Process = Start-Process $Path -ArgumentList $Arguments -PassThru
    if (-not $Process.WaitForExit(600000)) {
        throw "Der Dienst-Uninstaller überschritt das Zeitlimit."
    }
    if ($Process.ExitCode -ne 0) {
        throw "Der Dienst-Uninstaller schlug mit Exitcode $($Process.ExitCode) fehl."
    }
}

function Assert-ValidSignature {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { throw "Signaturziel fehlt: $Path" }
    $Signature = Get-AuthenticodeSignature $Path
    if ($RequireSignature -and $Signature.Status -ne "Valid") {
        throw "Authenticode-Signatur ist ungültig für $Path`: $($Signature.Status)"
    }
}

function Assert-ProtectedAcl {
    param([string]$Path, [switch]$Token)
    $Acl = Get-Acl -LiteralPath $Path
    if (-not $Acl.AreAccessRulesProtected) { throw "DACL erbt unerwartet: $Path" }
    $OwnerSid = ([Security.Principal.NTAccount]$Acl.Owner).Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
    if ($OwnerSid -ne "S-1-5-32-544") { throw "Unerwarteter Besitzer an $Path`: $OwnerSid" }
    $Rules = @($Acl.Access)
    $Sids = @($Rules | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value })
    $ServiceSid = ([Security.Principal.NTAccount]"NT SERVICE\ERechnungsPrueferService").Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
    foreach ($Required in @("S-1-5-18", "S-1-5-32-544", $ServiceSid)) {
        if ($Required -notin $Sids) { throw "Erforderliche DACL-Identität fehlt an $Path`: $Required" }
    }
    foreach ($Forbidden in @("S-1-1-0", "S-1-5-11", "S-1-5-32-545")) {
        if ($Forbidden -in $Sids) { throw "Zu breite DACL an $Path`: $Forbidden" }
    }
    if (-not $Token -and $Sids.Count -ne 3) { throw "Unerwartete DACL-Identität an $Path" }
    foreach ($Rule in $Rules) {
        $RuleSid = $Rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($Rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            throw "Nicht erlaubter ACE-Typ an $Path`: $RuleSid"
        }
        if ($RuleSid -in @("S-1-5-18", "S-1-5-32-544", $ServiceSid)) {
            $FullControl = [Security.AccessControl.FileSystemRights]::FullControl
            if (($Rule.FileSystemRights -band $FullControl) -ne $FullControl) {
                throw "Erforderliche Identität besitzt keinen Vollzugriff an $Path`: $RuleSid"
            }
        }
    }
}

function Assert-TokenReaderAcl {
    param([string]$Path, [string]$ReaderSid)
    $Rules = @(
        (Get-Acl -LiteralPath $Path).Access | Where-Object {
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $ReaderSid
        }
    )
    if ($Rules.Count -ne 1) { throw "Die konkrete Token-Leseidentität besitzt nicht exakt einen ACE: $ReaderSid" }
    $Rule = $Rules[0]
    $RequiredRead = [Security.AccessControl.FileSystemRights]::ReadData
    $ForbiddenWrite =
        [Security.AccessControl.FileSystemRights]::WriteData -bor
        [Security.AccessControl.FileSystemRights]::AppendData -bor
        [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
        [Security.AccessControl.FileSystemRights]::WriteAttributes -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    if ($Rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
        ($Rule.FileSystemRights -band $RequiredRead) -ne $RequiredRead -or
        ($Rule.FileSystemRights -band $ForbiddenWrite) -ne 0) {
        throw "Die konkrete Token-Leseidentität besitzt nicht ausschließlich Leserechte: $ReaderSid"
    }
}

function Assert-ProtectedLogAcl {
    param([string]$Path, [switch]$Directory)
    $Acl = Get-Acl -LiteralPath $Path
    if (-not $Acl.AreAccessRulesProtected) { throw "Log-DACL erbt unerwartet: $Path" }
    $OwnerSid = ([Security.Principal.NTAccount]$Acl.Owner).Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
    if ($OwnerSid -notin @("S-1-5-18", "S-1-5-19", "S-1-5-32-544")) {
        throw "Unerwarteter Logbesitzer an $Path`: $OwnerSid"
    }
    $Rules = @($Acl.Access)
    $OwnerRights = @(
        $Rules | Where-Object {
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq "S-1-3-4"
        }
    )
    if ($OwnerRights.Count -ne 1 -or
        $OwnerRights[0].AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
        $OwnerRights[0].FileSystemRights -ne [Security.AccessControl.FileSystemRights]::ReadPermissions) {
        throw "Der Logpfad begrenzt die impliziten LocalService-Besitzerrechte nicht exakt: $Path"
    }
    $ExpectedInheritance = if ($Directory) {
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    } else {
        [Security.AccessControl.InheritanceFlags]::None
    }
    if ($OwnerRights[0].InheritanceFlags -ne $ExpectedInheritance -or
        $OwnerRights[0].PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None) {
        throw "Der Owner-Rights-ACE wird am Logverzeichnis nicht vererbt: $Path"
    }
    $ServiceSid = ([Security.Principal.NTAccount]"NT SERVICE\ERechnungsPrueferService").Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
    $ExpectedSids = @("S-1-3-4", "S-1-5-18", "S-1-5-32-544", $ServiceSid)
    $ActualSids = @(
        $Rules | ForEach-Object {
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        }
    )
    if ($ActualSids.Count -ne $ExpectedSids.Count) {
        throw "Der Logpfad besitzt nicht exakt die erforderlichen DACL-Identitäten: $Path"
    }
    foreach ($ExpectedSid in $ExpectedSids) {
        if (@($ActualSids | Where-Object { $_ -eq $ExpectedSid }).Count -ne 1) {
            throw "Der Logpfad besitzt eine fehlende oder duplizierte DACL-Identität: $Path`: $ExpectedSid"
        }
    }
    foreach ($Rule in $Rules) {
        $RuleSid = $Rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($Rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            throw "Der Logpfad besitzt einen nicht erlaubten ACE-Typ: $Path`: $RuleSid"
        }
        if ($RuleSid -ne "S-1-3-4" -and
            $Rule.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl) {
            throw "Eine Dienstidentität besitzt am Logpfad keinen exakten Vollzugriff: $Path`: $RuleSid"
        }
        if ($Rule.InheritanceFlags -ne $ExpectedInheritance -or
            $Rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None) {
            throw "Der Logpfad besitzt unerwartete Vererbungsflags: $Path`: $RuleSid"
        }
    }
}

function Add-ExplorerAdministratorDirectoryAce {
    param(
        [string]$Path,
        [Security.Principal.SecurityIdentifier]$UserSid
    )
    $Acl = Get-Acl -LiteralPath $Path
    $Inheritance =
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    $Rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $UserSid,
        [Security.AccessControl.FileSystemRights]::FullControl,
        $Inheritance,
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $Acl.AddAccessRule($Rule)
    Set-Acl -LiteralPath $Path -AclObject $Acl
    $Matching = @(
        (Get-Acl -LiteralPath $Path).Access | Where-Object {
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq
                $UserSid.Value
        }
    )
    if ($Matching.Count -ne 1 -or $Matching[0].IsInherited -or
        $Matching[0].AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
        $Matching[0].FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or
        $Matching[0].InheritanceFlags -ne $Inheritance -or
        $Matching[0].PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None) {
        throw "Explorer-kompatibler Administrator-ACE besitzt nicht die erwartete Form: $Path"
    }
}

function Wait-ServiceProcessRestart {
    param([string]$Name, [int]$PreviousProcessId, [int]$Seconds = 150)
    $Deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        $Candidate = Get-CimInstance Win32_Service -Filter "Name='$Name'" -ErrorAction SilentlyContinue
        if ($Candidate -and $Candidate.State -eq "Running" -and
            [int]$Candidate.ProcessId -gt 0 -and [int]$Candidate.ProcessId -ne $PreviousProcessId) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $Deadline)
    throw "Die konfigurierte SCM-Recovery hat den Dienst nicht mit einem neuen Prozess gestartet."
}

if (-not $IsWindows) { throw "Der Dienst-Pakettest kann nur unter Windows laufen." }
if (-not $ConfirmIsolatedEnvironment) {
    throw "Nur auf einer sauberen Wegwerf-VM mit -ConfirmIsolatedEnvironment ausführen."
}
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Der Dienst-Pakettest benötigt eine administrative Testidentität."
}
if (-not [Environment]::Is64BitProcess) {
    throw "Der Dienst-Pakettest benötigt einen 64-Bit-PowerShell-Prozess."
}
$AdministratorsSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
$DirectAdministratorUsers = @(
    Get-LocalGroupMember -SID $AdministratorsSid | Where-Object {
        [string]$_.ObjectClass -eq "User"
    } | Sort-Object { $_.SID.Value }
)
if ($DirectAdministratorUsers.Count -eq 0) {
    throw "Die isolierte Testmaschine besitzt keinen direkten Benutzer in der lokalen Administratorgruppe."
}
$CurrentDirectAdministrator = @(
    $DirectAdministratorUsers | Where-Object { $_.SID.Value -eq $Identity.User.Value }
)
$ExplorerAdministratorSid = if ($CurrentDirectAdministrator.Count -eq 1) {
    $Identity.User
} else {
    [Security.Principal.SecurityIdentifier]$DirectAdministratorUsers[0].SID
}
if ($CommitHardKillRecovery -ne "None" -and -not $AllowElevatedMigrationTestContext) {
    throw "Hard-Kill-Recovery darf nur mit dem isolierten Testinstaller und -AllowElevatedMigrationTestContext laufen."
}
if ($PreflightOnly -and $CommitHardKillRecovery -ne "None") {
    throw "-PreflightOnly und -CommitHardKillRecovery schließen einander aus."
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Version = (Get-Content (Join-Path $ProjectRoot "VERSION") -Raw).Trim()
$ProductionSetup = Join-Path $ProjectRoot "dist\E-Rechnungs-Pruefer-$Version-Windows-x64-Dienst-Setup.exe"
if (-not $Setup) {
    $SetupRoot = if ($AllowElevatedMigrationTestContext) {
        Join-Path $ProjectRoot "build\windows\test-installer"
    } else {
        Join-Path $ProjectRoot "dist"
    }
    $Setup = Join-Path $SetupRoot "E-Rechnungs-Pruefer-$Version-Windows-x64-Dienst-Setup.exe"
}
$ServiceName = "ERechnungsPrueferService"
$InstallDir = Join-Path $env:ProgramFiles "E-Rechnungs-Pruefer-Dienst"
$ServiceExe = Join-Path $InstallDir "service\E-Rechnungs-Pruefer-Dienst.exe"
$OpenClient = Join-Path $InstallDir "service\E-Rechnungs-Pruefer-Oeffnen.exe"
$CurrentDesktopExe = Join-Path $ProjectRoot "build\windows\bundle\E-Rechnungs-Pruefer\E-Rechnungs-Pruefer.exe"
$Uninstaller = Join-Path $InstallDir "unins000.exe"
$DataDir = Join-Path $env:ProgramData "E-Rechnungs-Pruefer"
$ConfigFile = Join-Path $DataDir "service.json"
$TokenFile = Join-Path $DataDir "api-token.txt"
$LogDir = Join-Path $DataDir "logs"
$LogFile = Join-Path $LogDir "service.log"
$ServiceRegistryPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"
$UninstallKey = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8824D15C-7F4E-4CB2-B957-FBC26B923363}_is1"
$DesktopMigrationStateDir = Join-Path $env:ProgramData "E-Rechnungs-Pruefer-Installer-State"
$DesktopMigrationTransferRoot = Join-Path $env:ProgramData "E-Rechnungs-Pruefer-Installer-Transfer"
$ServiceInstallerStateDir = Join-Path $InstallDir ".installer-state"
$EarlyInstallerStatePaths = @(
    $DesktopMigrationStateDir,
    $DesktopMigrationTransferRoot,
    $ServiceInstallerStateDir
)

$DesktopDir = Join-Path $env:LOCALAPPDATA "Programs\E-Rechnungs-Pruefer"
$DesktopData = Join-Path $env:LOCALAPPDATA "E-Rechnungs-Pruefer"
$DesktopUninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{D33FD9E5-0C5E-48ED-BF0C-E9D2962A45DF}_is1"
$DesktopRunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$Existing = @()
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) { $Existing += "Dienst $ServiceName" }
foreach ($Path in @($InstallDir, $DataDir, $UninstallKey, $DesktopDir, $DesktopData, $DesktopUninstallKey)) {
    if (Test-Path -LiteralPath $Path) { $Existing += $Path }
}
if (Get-ItemProperty $DesktopRunKey -Name "E-Rechnungs-Pruefer" -ErrorAction SilentlyContinue) {
    $Existing += "$DesktopRunKey\E-Rechnungs-Pruefer"
}
if (Get-Process -Name "E-Rechnungs-Pruefer" -ErrorAction SilentlyContinue) {
    $Existing += "laufender Desktopprozess E-Rechnungs-Pruefer.exe"
}
if ($Existing.Count -gt 0) {
    throw "Vorhandener Produktzustand; Abbruch ohne Änderung:`n$($Existing -join "`n")"
}
if ($PreflightOnly) { return }
if (-not (Test-Path -LiteralPath $Setup)) { throw "Dienst-Installer fehlt: $Setup" }
if ($CommitHardKillRecovery -ne "None") {
    $ExpectedTestSetup = Join-Path $ProjectRoot (
        "build\windows\test-installer\E-Rechnungs-Pruefer-$Version-Windows-x64-Dienst-Setup.exe"
    )
    if (-not [string]::Equals(
        [IO.Path]::GetFullPath($Setup),
        [IO.Path]::GetFullPath($ExpectedTestSetup),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Hard-Kill-Recovery akzeptiert ausschließlich den lokal gebauten isolierten Testinstaller."
    }
    $SetupItem = Get-Item -LiteralPath $Setup -Force
    if (($SetupItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Der isolierte Testinstaller darf kein Reparse-Point sein."
    }
}

$TemporaryRoot = if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
    [IO.Path]::GetTempPath()
} else {
    $env:RUNNER_TEMP
}
$TestRoot = Join-Path $TemporaryRoot "e-rechnungs-pruefer-service-test-$([Guid]::NewGuid().ToString('N'))"
$InstallLog = Join-Path $TestRoot "install.log"
$PortConflictLog = Join-Path $TestRoot "port-conflict.log"
$UpdateLog = Join-Path $TestRoot "update.log"
$StoppedUpdateLog = Join-Path $TestRoot "stopped-update.log"
$AutomaticUpdateLog = Join-Path $TestRoot "automatic-update.log"
$UninstallLog = Join-Path $TestRoot "uninstall-preserve.log"
$ReinstallLog = Join-Path $TestRoot "reinstall.log"
$PurgeLog = Join-Path $TestRoot "uninstall-purge.log"
$FailedUpdateLog = Join-Path $TestRoot "update-rollback.log"
$CommitHardKillLog = Join-Path $TestRoot "update-commit-hard-kill.log"
$CommitRecoveryLog = Join-Path $TestRoot "update-commit-recovery.log"
$PdfOutput = Join-Path $TestRoot "report.pdf"
$XmlOutput = Join-Path $TestRoot "export.xml"
New-Item $TestRoot -ItemType Directory | Out-Null
Assert-ValidSignature $Setup
if ($AllowElevatedMigrationTestContext) {
    if (-not (Test-Path -LiteralPath $ProductionSetup)) {
        throw "Produktiver Dienst-Installer für den Testkontext-Schutz fehlt: $ProductionSetup"
    }
    Assert-ValidSignature $ProductionSetup
    Invoke-ServiceInstallerExpectedFailure -Path $ProductionSetup `
        -LogPath (Join-Path $TestRoot "production-context-guard.log") -ExtraArguments @() `
        -ExpectedLogReason "Die ursprüngliche interaktive Benutzeridentität konnte nicht sicher bestätigt werden."
    if ((Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) -or
        (Test-Path -LiteralPath $InstallDir) -or (Test-Path -LiteralPath $DataDir) -or
        (Test-Path -LiteralPath $UninstallKey)) {
        throw "Der produktive Dienst-Installer akzeptierte den erhöhten internen Testkontext."
    }
    Assert-NoEarlyInstallerState -Scenario "Produktiver Testkontext-Schutz" -Paths $EarlyInstallerStatePaths
}
$JunctionTarget = Join-Path $TestRoot "programdata-junction-target"
$JunctionSentinel = Join-Path $JunctionTarget "sentinel.txt"
New-Item $JunctionTarget -ItemType Directory | Out-Null
Set-Content -LiteralPath $JunctionSentinel -Value "darf nicht verändert werden" -Encoding utf8NoBOM
New-Item -ItemType Junction -Path $DataDir -Target $JunctionTarget | Out-Null
try {
    Invoke-ServiceInstallerExpectedFailure -Path $Setup -LogPath (Join-Path $TestRoot "junction-preflight.log") `
        -ExtraArguments @() `
        -ExpectedLogReason "Der vorhandene Maschinenzustand ist unvollständig, unsicher oder ungültig."
    if ((Get-Content -LiteralPath $JunctionSentinel -Raw).Trim() -ne "darf nicht verändert werden") {
        throw "Unsicherer ProgramData-Junction-Zielinhalt wurde verändert."
    }
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        throw "ProgramData-Preflight legte trotz Junction einen Dienst an."
    }
} finally {
    Remove-Item -LiteralPath $DataDir -Force
}
Assert-NoEarlyInstallerState -Scenario "ProgramData-Junction-Preflight" -Paths $EarlyInstallerStatePaths
$PortBlocker = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 8080)
try {
    $PortBlocker.Server.ExclusiveAddressUse = $true
    $PortBlocker.Start()
    Invoke-ServiceInstallerExpectedFailure -Path $Setup -LogPath $PortConflictLog -ExtraArguments @() `
        -ExpectedLogReason "Der konfigurierte lokale Dienstport ist belegt oder nicht exklusiv reservierbar."
} finally {
    $PortBlocker.Stop()
}
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    throw "Port-Preflight legte trotz Konflikt einen Dienst an."
}
foreach ($Unexpected in @($InstallDir, $DataDir)) {
    if (Test-Path -LiteralPath $Unexpected) {
        throw "Port-Preflight veränderte vor dem Abbruch den Produktzustand: $Unexpected"
    }
}
Assert-NoEarlyInstallerState -Scenario "Port-Preflight" -Paths $EarlyInstallerStatePaths
Invoke-ServiceInstaller -Path $Setup -LogPath $InstallLog
Wait-ServiceState -Name $ServiceName -State "Running"

$CimService = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
if ($CimService.StartName -ne "NT AUTHORITY\LocalService") { throw "Falsches Dienstkonto: $($CimService.StartName)" }
if ($CimService.StartMode -ne "Auto") { throw "Falscher Starttyp: $($CimService.StartMode)" }
if ($CimService.PathName.Trim('"') -ne $ServiceExe) { throw "Falscher ImagePath: $($CimService.PathName)" }
$DelayedAutoStart = Get-ItemPropertyValue $ServiceRegistryPath "DelayedAutoStart"
if ($DelayedAutoStart -ne 1) { throw "Automatic (Delayed Start) wurde nicht eingerichtet." }
$ServiceSidType = Get-ItemPropertyValue $ServiceRegistryPath "ServiceSidType"
if ($ServiceSidType -ne 1) { throw "Dienst-SID-Typ ist nicht UNRESTRICTED: $ServiceSidType" }
$FailureOutput = & sc.exe qfailure $ServiceName | Out-String
$FailureOutput | Write-Host
if ($LASTEXITCODE -ne 0) { throw "Recovery-Konfiguration konnte nicht gelesen werden." }
foreach ($ExpectedDelay in @("60000", "300000")) {
    if (-not $FailureOutput.Contains($ExpectedDelay)) {
        throw "Recovery-Konfiguration enthält die erwartete Verzögerung $ExpectedDelay nicht."
    }
}

foreach ($Path in @($DataDir, $ConfigFile)) { Assert-ProtectedAcl -Path $Path }
Assert-ProtectedAcl -Path $TokenFile -Token
Assert-ProtectedLogAcl -Path $LogDir -Directory
Assert-ProtectedLogAcl -Path $LogFile
foreach ($Path in @($ServiceExe, $OpenClient, $CurrentDesktopExe)) {
    Assert-ValidSignature $Path
}
$InitialServiceProcessId = [int]$CimService.ProcessId
Stop-Process -Id $InitialServiceProcessId -Force
Wait-ServiceProcessRestart -Name $ServiceName -PreviousProcessId $InitialServiceProcessId
Stop-Service $ServiceName
Wait-ServiceState -Name $ServiceName -State "Stopped"
Add-ExplorerAdministratorDirectoryAce -Path $DataDir -UserSid $ExplorerAdministratorSid
Add-ExplorerAdministratorDirectoryAce -Path $LogDir -UserSid $ExplorerAdministratorSid
$VerifyStateExitCode = Invoke-WindowedExecutable -Path $ServiceExe -Arguments @("--verify-state")
if ($VerifyStateExitCode -ne 0) {
    throw "Der reparierbare Explorer-Administrator-ACE wurde bei der Zustandsprüfung abgewiesen."
}
Start-Service $ServiceName
Wait-ServiceState -Name $ServiceName -State "Running"
Assert-ProtectedAcl -Path $DataDir
Assert-ProtectedLogAcl -Path $LogDir -Directory
Assert-ProtectedLogAcl -Path $LogFile
$HadNoDialog = Test-Path Env:EINVOICE_DESKTOP_NO_DIALOG
$PreviousNoDialog = $env:EINVOICE_DESKTOP_NO_DIALOG
try {
    $env:EINVOICE_DESKTOP_NO_DIALOG = "1"
    $ConflictingDesktop = Start-Process $CurrentDesktopExe -ArgumentList "--background" -PassThru
    if (-not $ConflictingDesktop.WaitForExit(30000)) {
        $ConflictingDesktop.Kill($true)
        throw "Der aktuelle Desktopmodus lief unerwartet parallel zum Dienst weiter."
    }
    if ($ConflictingDesktop.ExitCode -eq 0) {
        throw "Der aktuelle Desktopmodus meldete den Global-Mutex-Konflikt nicht als Startfehler."
    }
} finally {
    if ($HadNoDialog) { $env:EINVOICE_DESKTOP_NO_DIALOG = $PreviousNoDialog }
    else { Remove-Item Env:EINVOICE_DESKTOP_NO_DIALOG -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $DesktopData) { Remove-Item -LiteralPath $DesktopData -Recurse -Force }
}
$ProbeExitCode = Invoke-WindowedExecutable -Path $OpenClient -Arguments @("--probe")
if ($ProbeExitCode -ne 0) { throw "Authentifizierte Named-Pipe-/Browserbootstrap-Prüfung fehlgeschlagen." }

$Configuration = Get-Content $ConfigFile -Raw | ConvertFrom-Json
$Port = [int]$Configuration.port
$Listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port)
if ($Listeners.Count -eq 0 -or @($Listeners | Where-Object LocalAddress -ne "127.0.0.1").Count -gt 0) {
    throw "Der Dienst bindet nicht ausschließlich an 127.0.0.1:$Port."
}
$Health = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 5
if ($Health.status -ne "ok") { throw "Dienst-Healthcheck fehlgeschlagen." }
$Token = (Get-Content $TokenFile -Raw).Trim()
$Example = Join-Path $ProjectRoot "app\examples\cii-rechnung-demo.xml"
$RejectedExample = Join-Path $ProjectRoot "tests\fixtures\cii-category-o.xml"

$MissingStatus = & curl.exe --silent --output NUL --write-out "%{http_code}" `
    --form "file=@$Example;type=application/xml" --form "official=false" `
    "http://127.0.0.1:$Port/api/analyze"
$WrongStatus = & curl.exe --silent --output NUL --write-out "%{http_code}" `
    --header "Authorization: Bearer falsch" --form "file=@$Example;type=application/xml" `
    --form "official=false" "http://127.0.0.1:$Port/api/analyze"
if ($MissingStatus -ne "403" -or $WrongStatus -ne "403") { throw "API akzeptierte fehlendes oder falsches Token." }

$DisabledJson = & curl.exe --silent --show-error --fail --header "Authorization: Bearer $Token" `
    --form "file=@$Example;type=application/xml" --form "official=false" `
    "http://127.0.0.1:$Port/api/analyze"
$Disabled = $DisabledJson | ConvertFrom-Json
if ($Disabled.validation.official.executed -ne $false) { throw "official=false hat KoSIT nicht übersprungen." }

& curl.exe --silent --show-error --fail --header "Authorization: Bearer $Token" `
    --form "file=@$Example;type=application/xml" --form "official=false" --output $PdfOutput `
    "http://127.0.0.1:$Port/api/report/pdf"
if ($LASTEXITCODE -ne 0 -or [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($PdfOutput), 0, 5) -ne "%PDF-") {
    throw "PDF-Bericht des Dienstes ist ungültig."
}
& curl.exe --silent --show-error --fail --header "Authorization: Bearer $Token" `
    --form "file=@$Example;type=application/xml" --output $XmlOutput `
    "http://127.0.0.1:$Port/api/xml"
if ((Get-FileHash $Example).Hash -ne (Get-FileHash $XmlOutput).Hash) { throw "XML-Export ist nicht byteidentisch." }

$Accepted = (& curl.exe --silent --show-error --fail --header "Authorization: Bearer $Token" `
    --form "file=@$Example;type=application/xml" --form "official=true" `
    "http://127.0.0.1:$Port/api/analyze") | ConvertFrom-Json
if (-not $Accepted.validation.official.executed -or $Accepted.validation.official.accepted -ne $true) {
    throw "Realer KoSIT-Annahmefall fehlgeschlagen."
}
$Rejected = (& curl.exe --silent --show-error --fail --header "Authorization: Bearer $Token" `
    --form "file=@$RejectedExample;type=application/xml" --form "official=true" `
    "http://127.0.0.1:$Port/api/analyze") | ConvertFrom-Json
if (-not $Rejected.validation.official.executed -or $Rejected.validation.official.accepted -ne $false) {
    throw "Realer KoSIT-Ablehnungsfall fehlgeschlagen."
}

$TokenBeforeRestart = $Token
Stop-Service $ServiceName
Wait-ServiceState -Name $ServiceName -State "Stopped"
Start-Service $ServiceName
Wait-ServiceState -Name $ServiceName -State "Running"
if ((Get-Content $TokenFile -Raw).Trim() -ne $TokenBeforeRestart) { throw "Token änderte sich bei Stop/Start." }

$ReaderSid = $Identity.User.Value
$GrantReaderExitCode = Invoke-WindowedExecutable -Path $ServiceExe `
    -Arguments @("--grant-token-read", $Identity.Name)
if ($GrantReaderExitCode -ne 0) { throw "Konkrete Windows-Testidentität konnte nicht provisioniert werden." }
Assert-TokenReaderAcl -Path $TokenFile -ReaderSid $ReaderSid
$TokenBeforeRotation = (Get-Content $TokenFile -Raw).Trim()
Stop-Service $ServiceName
Wait-ServiceState -Name $ServiceName -State "Stopped"
$RotateTokenExitCode = Invoke-WindowedExecutable -Path $ServiceExe -Arguments @("--rotate-token")
if ($RotateTokenExitCode -ne 0) { throw "Diensttoken konnte nicht kontrolliert rotiert werden." }
$RotatedToken = (Get-Content $TokenFile -Raw).Trim()
if ($RotatedToken -eq $TokenBeforeRotation) { throw "Tokenrotation erzeugte kein neues Token." }
Assert-TokenReaderAcl -Path $TokenFile -ReaderSid $ReaderSid
Start-Service $ServiceName
Wait-ServiceState -Name $ServiceName -State "Running"

$TokenBeforeUpdate = $RotatedToken
$StaleBundleFile = Join-Path $InstallDir "service\stale-from-previous-release.txt"
Set-Content -LiteralPath $StaleBundleFile -Value "muss beim Verzeichnistausch verschwinden" -Encoding utf8NoBOM
Invoke-ServiceInstaller -Path $Setup -LogPath $UpdateLog
Wait-ServiceState -Name $ServiceName -State "Running"
if ((Get-Content $TokenFile -Raw).Trim() -ne $TokenBeforeUpdate) { throw "Token änderte sich beim laufenden Update." }
Assert-TokenReaderAcl -Path $TokenFile -ReaderSid $ReaderSid
if (Test-Path -LiteralPath $StaleBundleFile) { throw "Update ließ eine veraltete Bundledatei zurück." }
foreach ($TransactionDirectory in @("service.new", "service.rollback", "service.obsolete")) {
    if (Test-Path -LiteralPath (Join-Path $InstallDir $TransactionDirectory)) {
        throw "Update ließ das Transaktionsverzeichnis $TransactionDirectory zurück."
    }
}
if ($CommitHardKillRecovery -ne "None") {
    $CommitRecoverySentinel = Join-Path $InstallDir "service\commit-recovery-sentinel.txt"
    Set-Content -LiteralPath $CommitRecoverySentinel `
        -Value "muss durch den committed Bundlewechsel verschwinden" -Encoding utf8NoBOM
    $CommitHardKillArguments = @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
        "/TASKS=`"systemstart`"", "/LOG=`"$CommitHardKillLog`"",
        "/ALLOWELEVATEDTESTCONTEXT=1"
    )
    Invoke-CommitCheckpointHardKill -Path $Setup -Arguments $CommitHardKillArguments `
        -ExpectedServiceExecutable $ServiceExe -ExpectedServiceName $ServiceName `
        -ExpectedReaderSid $ReaderSid
    if ($CommitHardKillRecovery -eq "LeaveForReboot") {
        Write-Warning (
            "Der beweisbare COMMIT_STARTED-Zustand bleibt absichtlich erhalten. VM jetzt hart neu starten; " +
            "danach denselben Testinstaller erneut ausführen und Roll-forward, Dienstzustand und Markerbereinigung " +
            "prüfen. Diese Ausführung meldet bewusst keinen vollständigen Paketerfolg."
        )
        exit 194
    }
    Invoke-ServiceInstaller -Path $Setup -LogPath $CommitRecoveryLog
    Wait-ServiceState -Name $ServiceName -State "Running"
    if ((Get-Content $TokenFile -Raw).Trim() -ne $TokenBeforeUpdate) {
        throw "Roll-forward-Recovery änderte das vorhandene Diensttoken."
    }
    Assert-TokenReaderAcl -Path $TokenFile -ReaderSid $ReaderSid
    if (Test-Path -LiteralPath $CommitRecoverySentinel) {
        throw "Roll-forward-Recovery ließ eine Datei des committed Altbundles zurück."
    }
    foreach ($TransactionDirectory in @("service.new", "service.rollback", "service.obsolete", ".installer-state")) {
        if (Test-Path -LiteralPath (Join-Path $InstallDir $TransactionDirectory)) {
            throw "Roll-forward-Recovery ließ den Transaktionspfad $TransactionDirectory zurück."
        }
    }
}
& sc.exe config $ServiceName start= demand
if ($LASTEXITCODE -ne 0) { throw "Eigener Starttyp für Rollback-Test konnte nicht gesetzt werden." }
Set-ItemProperty -LiteralPath $ServiceRegistryPath -Name "DelayedAutoStart" -Type DWord -Value 0
& sc.exe description $ServiceName "Rollback-Test-Beschreibung"
if ($LASTEXITCODE -ne 0) { throw "Eigene Dienstbeschreibung für Rollback-Test konnte nicht gesetzt werden." }
& sc.exe failure $ServiceName reset= 12345 actions= restart/7000
if ($LASTEXITCODE -ne 0) { throw "Eigene Recovery-Konfiguration für Rollback-Test konnte nicht gesetzt werden." }
& sc.exe failureflag $ServiceName 0
if ($LASTEXITCODE -ne 0) { throw "Eigene Recovery-Option für Rollback-Test konnte nicht gesetzt werden." }
$RollbackSentinel = Join-Path $InstallDir "service\rollback-sentinel.txt"
Set-Content -LiteralPath $RollbackSentinel -Value "muss mit dem vollständigen alten Baum zurückkehren" -Encoding utf8NoBOM
$TreeBeforeFailedUpdate = Get-TreeFingerprint (Join-Path $InstallDir "service")
$HashBeforeFailedUpdate = (Get-FileHash $ServiceExe -Algorithm SHA256).Hash
$ConfigHashBeforeFailedUpdate = (Get-FileHash $ConfigFile -Algorithm SHA256).Hash
$DescriptionBeforeFailedUpdate = Get-ItemPropertyValue $ServiceRegistryPath "Description"
$FailureActionsBeforeFailedUpdate = [Convert]::ToBase64String(
    [byte[]](Get-ItemPropertyValue $ServiceRegistryPath "FailureActions")
)
$FailureFlagBeforeFailedUpdate = Get-ItemPropertyValue $ServiceRegistryPath "FailureActionsOnNonCrashFailures"
$DelayedStartBeforeFailedUpdate = Get-ItemPropertyValue $ServiceRegistryPath "DelayedAutoStart" -ErrorAction SilentlyContinue
$DescriptionScmBeforeFailedUpdate = (& sc.exe qdescription $ServiceName) | Out-String
if ($LASTEXITCODE -ne 0) { throw "Dienstbeschreibung konnte vor dem Rollback-Test nicht gelesen werden." }
$FailureActionsScmBeforeFailedUpdate = (& sc.exe qfailure $ServiceName) | Out-String
if ($LASTEXITCODE -ne 0) { throw "Recovery-Konfiguration konnte vor dem Rollback-Test nicht gelesen werden." }
$FailureFlagScmBeforeFailedUpdate = (& sc.exe qfailureflag $ServiceName) | Out-String
if ($LASTEXITCODE -ne 0) { throw "Recovery-Option konnte vor dem Rollback-Test nicht gelesen werden." }
$ServiceSidScmBeforeFailedUpdate = (& sc.exe qsidtype $ServiceName) | Out-String
if ($LASTEXITCODE -ne 0) { throw "Dienst-SID-Typ konnte vor dem Rollback-Test nicht gelesen werden." }
$ServiceConfigScmBeforeFailedUpdate = (& sc.exe qc $ServiceName) | Out-String
if ($LASTEXITCODE -ne 0) { throw "Dienstkonfiguration konnte vor dem Rollback-Test nicht gelesen werden." }
Invoke-ServiceInstallerExpectedFailure -Path $Setup -LogPath $FailedUpdateLog -ExtraArguments @(
    "/TESTFAILAFTERCONFIG=1"
) -ExpectedLogReason "Absichtlich ausgelöster transaktionaler Installationstest."
Wait-ServiceState -Name $ServiceName -State "Running"
if ((Get-Content $TokenFile -Raw).Trim() -ne $TokenBeforeUpdate) { throw "Fehlgeschlagenes Update änderte das Token." }
if ((Get-FileHash $ServiceExe -Algorithm SHA256).Hash -ne $HashBeforeFailedUpdate) {
    throw "Fehlgeschlagenes Update ließ andere Dienstbinärdateien zurück."
}
if ((Get-TreeFingerprint (Join-Path $InstallDir "service")) -ne $TreeBeforeFailedUpdate -or
    -not (Test-Path -LiteralPath $RollbackSentinel)) {
    throw "Fehlgeschlagenes Update stellte den vollständigen alten Bundlebaum nicht wieder her."
}
foreach ($TransactionDirectory in @("service.new", "service.rollback", "service.obsolete")) {
    if (Test-Path -LiteralPath (Join-Path $InstallDir $TransactionDirectory)) {
        throw "Rollback ließ das Transaktionsverzeichnis $TransactionDirectory zurück."
    }
}
if ((Get-FileHash $ConfigFile -Algorithm SHA256).Hash -ne $ConfigHashBeforeFailedUpdate) {
    throw "Fehlgeschlagenes Update änderte die Maschinenkonfiguration."
}
if ((Get-ItemPropertyValue $ServiceRegistryPath "Description") -ne $DescriptionBeforeFailedUpdate) {
    throw "Fehlgeschlagenes Update stellte die ursprüngliche Dienstbeschreibung nicht wieder her."
}
$FailureActionsAfterFailedUpdate = [Convert]::ToBase64String(
    [byte[]](Get-ItemPropertyValue $ServiceRegistryPath "FailureActions")
)
if ($FailureActionsAfterFailedUpdate -ne $FailureActionsBeforeFailedUpdate) {
    throw "Fehlgeschlagenes Update stellte die ursprüngliche Recovery-Konfiguration nicht wieder her."
}
if ((Get-ItemPropertyValue $ServiceRegistryPath "FailureActionsOnNonCrashFailures") -ne
    $FailureFlagBeforeFailedUpdate) {
    throw "Fehlgeschlagenes Update stellte die ursprüngliche Recovery-Option nicht wieder her."
}
$DelayedStartAfterFailedUpdate = Get-ItemPropertyValue $ServiceRegistryPath "DelayedAutoStart" -ErrorAction SilentlyContinue
if ($DelayedStartAfterFailedUpdate -ne $DelayedStartBeforeFailedUpdate) {
    throw "Fehlgeschlagenes Update stellte den ursprünglichen verzögerten Startzustand nicht wieder her."
}
$DescriptionScmAfterFailedUpdate = (& sc.exe qdescription $ServiceName) | Out-String
if ($LASTEXITCODE -ne 0 -or $DescriptionScmAfterFailedUpdate -ne $DescriptionScmBeforeFailedUpdate) {
    throw "SCM meldet nach fehlgeschlagenem Update nicht die ursprüngliche Dienstbeschreibung."
}
$FailureActionsScmAfterFailedUpdate = (& sc.exe qfailure $ServiceName) | Out-String
if ($LASTEXITCODE -ne 0 -or $FailureActionsScmAfterFailedUpdate -ne $FailureActionsScmBeforeFailedUpdate) {
    throw "SCM meldet nach fehlgeschlagenem Update nicht die ursprüngliche Recovery-Konfiguration."
}
$FailureFlagScmAfterFailedUpdate = (& sc.exe qfailureflag $ServiceName) | Out-String
if ($LASTEXITCODE -ne 0 -or $FailureFlagScmAfterFailedUpdate -ne $FailureFlagScmBeforeFailedUpdate) {
    throw "SCM meldet nach fehlgeschlagenem Update nicht die ursprüngliche Recovery-Option."
}
$ServiceSidScmAfterFailedUpdate = (& sc.exe qsidtype $ServiceName) | Out-String
if ($LASTEXITCODE -ne 0 -or $ServiceSidScmAfterFailedUpdate -ne $ServiceSidScmBeforeFailedUpdate) {
    throw "SCM meldet nach fehlgeschlagenem Update nicht den ursprünglichen Dienst-SID-Typ."
}
$ServiceConfigScmAfterFailedUpdate = (& sc.exe qc $ServiceName) | Out-String
if ($LASTEXITCODE -ne 0 -or $ServiceConfigScmAfterFailedUpdate -ne $ServiceConfigScmBeforeFailedUpdate) {
    throw "SCM meldet nach fehlgeschlagenem Update nicht die ursprüngliche Dienstkonfiguration."
}
Stop-Service $ServiceName
Wait-ServiceState -Name $ServiceName -State "Stopped"
Invoke-ServiceInstaller -Path $Setup -LogPath $StoppedUpdateLog -Tasks ""
Wait-ServiceState -Name $ServiceName -State "Stopped"
$ManualService = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
if ($ManualService.StartMode -ne "Manual") { throw "Abgewählte Systemstart-Option ergab keinen manuellen Dienst." }
if (Test-Path -LiteralPath $ServiceRegistryPath) {
    $DelayedValue = Get-ItemProperty $ServiceRegistryPath -Name "DelayedAutoStart" -ErrorAction SilentlyContinue
    if ($DelayedValue -and $DelayedValue.DelayedAutoStart -ne 0) {
        throw "Manueller Dienst behielt unerwartet DelayedAutoStart."
    }
}
Invoke-ServiceInstaller -Path $Setup -LogPath $AutomaticUpdateLog
Wait-ServiceState -Name $ServiceName -State "Stopped"
$AutomaticService = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
if ($AutomaticService.StartMode -ne "Auto") { throw "Erneut gewählter Systemstart wurde nicht hergestellt." }
Start-Service $ServiceName
Wait-ServiceState -Name $ServiceName -State "Running"

$ConfigurationHashBeforePreserve = (Get-FileHash -LiteralPath $ConfigFile -Algorithm SHA256).Hash
$LogBytesBeforePreserve = [IO.File]::ReadAllBytes($LogFile)
if ($LogBytesBeforePreserve.Length -eq 0) {
    throw "Das technische Protokoll enthält vor dem Preserve-Test keinen Nachweis."
}
Add-ExplorerAdministratorDirectoryAce -Path $DataDir -UserSid $ExplorerAdministratorSid
Add-ExplorerAdministratorDirectoryAce -Path $LogDir -UserSid $ExplorerAdministratorSid
Invoke-ServiceUninstaller -Path $Uninstaller -LogPath $UninstallLog
Wait-ServiceState -Name $ServiceName -State "Absent"
if (Test-Path -LiteralPath (Join-Path $InstallDir ".uninstaller-state")) {
    throw "Standard-Deinstallation ließ den geschützten Deinstallationsbeleg zurück."
}
if (-not (Test-Path $TokenFile)) { throw "Standard-Deinstallation hat das Token unerwartet gelöscht." }
Invoke-ServiceInstaller -Path $Setup -LogPath $ReinstallLog
Wait-ServiceState -Name $ServiceName -State "Running"
if ((Get-Content $TokenFile -Raw).Trim() -ne $TokenBeforeUpdate) { throw "Neuinstallation hat erhaltenes Token ersetzt." }
Assert-TokenReaderAcl -Path $TokenFile -ReaderSid $ReaderSid
if ((Get-FileHash -LiteralPath $ConfigFile -Algorithm SHA256).Hash -ne $ConfigurationHashBeforePreserve) {
    throw "Neuinstallation hat die erhaltene Maschinenkonfiguration verändert."
}
$LogPrefixPreserved = $false
foreach ($Candidate in @($LogFile, "${LogFile}.1", "${LogFile}.2", "${LogFile}.3")) {
    if ((Test-Path -LiteralPath $Candidate -PathType Leaf) -and
        (Test-BytePrefix -Prefix $LogBytesBeforePreserve -Value ([IO.File]::ReadAllBytes($Candidate)))) {
        $LogPrefixPreserved = $true
        break
    }
}
if (-not $LogPrefixPreserved) {
    throw "Neuinstallation hat das erhaltene technische Protokoll nicht bewahrt."
}
Assert-ProtectedAcl -Path $DataDir
Assert-ProtectedLogAcl -Path $LogDir -Directory
Assert-ProtectedLogAcl -Path $LogFile
$ReinstalledService = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
if ($ReinstalledService.StartMode -ne "Auto") {
    throw "Neuinstallation stellte den automatischen Starttyp nicht wieder her."
}
if ((Get-ItemPropertyValue $ServiceRegistryPath "DelayedAutoStart") -ne 1) {
    throw "Neuinstallation stellte den verzögerten automatischen Start nicht wieder her."
}
Invoke-ServiceUninstaller -Path $Uninstaller -LogPath $PurgeLog -PurgeData
Wait-ServiceState -Name $ServiceName -State "Absent"
if (Test-Path $DataDir) { throw "Explizite /PURGEDATA=1-Deinstallation ließ ProgramData zurück." }
if (Test-Path $InstallDir) { throw "Dienst-Binärdateien wurden nicht vollständig entfernt." }

Write-Host "Windows-Dienstpaket erfolgreich geprüft."
