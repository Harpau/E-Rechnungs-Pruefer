[CmdletBinding()]
param(
    [string]$DesktopSetup130 = "",
    [string]$ServiceSetup = "",
    [switch]$RequireSignature,
    [switch]$ConfirmIsolatedEnvironment,
    [switch]$AllowElevatedMigrationTestContext,
    [ValidateSet("None", "Immediate", "LeaveForReboot")]
    [string]$DesktopHardKillRecovery = "None"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-Setup {
    param([string]$Path, [string[]]$Arguments)
    $Process = Start-Process $Path -ArgumentList $Arguments -PassThru
    if (-not $Process.WaitForExit(600000)) { throw "Setup-Zeitlimit überschritten: $Path" }
    if ($Process.ExitCode -ne 0) { throw "Setup fehlgeschlagen ($($Process.ExitCode)): $Path" }
}

function Invoke-SetupExpectedFailure {
    param([string]$Path, [string[]]$Arguments)
    $Process = Start-Process $Path -ArgumentList $Arguments -PassThru
    if (-not $Process.WaitForExit(600000)) { throw "Erwarteter Setupfehler überschritt das Zeitlimit: $Path" }
    if ($Process.ExitCode -eq 0) { throw "Der angeforderte transaktionale Setupfehler blieb aus: $Path" }
}

function Get-TokenScryptVerifier {
    param([string]$Path, [string]$TransactionId)
    $Verifier = & python -c (
        "import hashlib,pathlib,sys; " +
        "print(hashlib.scrypt(pathlib.Path(sys.argv[1]).read_bytes(), " +
        "salt=bytes.fromhex(sys.argv[2]), n=2**14, r=8, p=1, dklen=32).hex())"
    ) $Path $TransactionId
    if ($LASTEXITCODE -ne 0 -or $Verifier -notmatch "^[0-9a-f]{64}$") {
        throw "Der scrypt-Token-Verifier konnte nicht unabhängig berechnet werden."
    }
    return [string]$Verifier
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

function Assert-CheckpointFileAcl {
    param([string]$Path, [AllowEmptyString()][string]$ReaderSid = "")
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
    if ($ReaderSid) { $ExpectedSids += $ReaderSid }
    if ($Rules.Count -ne $ExpectedSids.Count -or (Compare-Object $ExpectedSids $ObservedSids)) {
        throw "Checkpoint-Datei besitzt nicht die exakt erwarteten Identitäten: $Path"
    }
    foreach ($Rule in $Rules) {
        $Sid = $Rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($Rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $Rule.IsInherited -or
            $Rule.InheritanceFlags -ne [Security.AccessControl.InheritanceFlags]::None -or
            $Rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None) {
            throw "Checkpoint-Datei enthält einen nicht erlaubten ACE-Typ: $Path"
        }
        if ($ReaderSid -and $Sid -eq $ReaderSid) {
            $ExpectedRead =
                [Security.AccessControl.FileSystemRights]::Read -bor
                [Security.AccessControl.FileSystemRights]::Synchronize
            if ($Rule.FileSystemRights -ne $ExpectedRead) {
                throw "Checkpoint-Datei gewährt der gebundenen Identität nicht ausschließlich Leserechte: $Path"
            }
        } elseif ($Rule.FileSystemRights -ne
            [Security.AccessControl.FileSystemRights]::FullControl) {
            throw "Checkpoint-Datei gewährt Administratoren oder SYSTEM keinen Vollzugriff: $Path"
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

function Invoke-DesktopCheckpointHardKill {
    param(
        [string]$Path,
        [string[]]$Arguments,
        [string]$StateDirectory,
        [string]$LegacyExecutable,
        [string]$ExpectedServiceExecutable,
        [string]$ExpectedServiceName,
        [string]$ExpectedReaderSid
    )
    $SealPath = Join-Path $StateDirectory "desktop-migration-receipt.json"
    $PhasePath = Join-Path $StateDirectory "desktop-migration-phase.json"
    $ProtectedTokenPath = Join-Path $StateDirectory "desktop-api-token.txt"
    $ServicePreparedPath = Join-Path (
        Split-Path (Split-Path $ExpectedServiceExecutable -Parent) -Parent
    ) ".installer-state\install-transaction.prepared.json"
    $QuarantinedExecutable = "$LegacyExecutable.service-mode-disabled"
    $Process = Start-Process $Path -ArgumentList $Arguments -PassThru
    $Deadline = [DateTime]::UtcNow.AddSeconds(180)
    $TransactionId = ""
    do {
        if ($Process.HasExited) {
            throw "Setup endete, ohne den Desktop-Hard-Kill-Checkpoint beobachtbar zu hinterlassen."
        }
        if ((Test-Path -LiteralPath $SealPath) -and
            (Test-Path -LiteralPath $PhasePath) -and
            (Test-Path -LiteralPath $QuarantinedExecutable) -and
            -not (Test-Path -LiteralPath $LegacyExecutable) -and
            -not (Test-Path -LiteralPath $ServicePreparedPath) -and
            -not (Get-Service -Name $ExpectedServiceName -ErrorAction SilentlyContinue)) {
            $Seal = Read-StrictJsonMarker -Path $SealPath -ExpectedProperties @(
                "reader_sid", "receipt", "schema_version", "token_scrypt", "transaction_id"
            )
            $Phase = Read-StrictJsonMarker -Path $PhasePath -ExpectedProperties @(
                "generation", "phase", "schema_version", "transaction_id"
            )
            $ObservedTokenScrypt = if (Test-Path -LiteralPath $ProtectedTokenPath) {
                Get-TokenScryptVerifier -Path $ProtectedTokenPath -TransactionId ([string]$Seal.transaction_id)
            } else {
                ""
            }
            if ($Seal.schema_version -ne 2 -or $Phase.schema_version -ne 1 -or
                $Phase.generation -ne 0 -or $Phase.phase -ne "rollbackable" -or
                $Seal.reader_sid -ne $ExpectedReaderSid -or
                $Seal.transaction_id -notmatch "^[0-9a-f]{32}$" -or
                $Phase.transaction_id -ne $Seal.transaction_id -or
                $Seal.token_scrypt -notmatch "^[0-9a-f]{64}$" -or
                -not (Test-Path -LiteralPath $ProtectedTokenPath) -or
                $ObservedTokenScrypt -ne $Seal.token_scrypt -or
                -not $Seal.receipt.was_running -or
                -not [string]::Equals(
                    [string]$Seal.receipt.executable,
                    [IO.Path]::GetFullPath($LegacyExecutable),
                    [StringComparison]::OrdinalIgnoreCase
                ) -or
                -not [string]::Equals(
                    [string]$Seal.receipt.disabled_executable,
                    [IO.Path]::GetFullPath($QuarantinedExecutable),
                    [StringComparison]::OrdinalIgnoreCase
                )) {
                throw "Der beobachtete Desktop-Checkpoint ist nicht der erwartete gebundene Rollbackzustand."
            }
            $TransactionId = [string]$Seal.transaction_id
            break
        }
        Start-Sleep -Milliseconds 10
    } while ([DateTime]::UtcNow -lt $Deadline)
    if (-not $TransactionId) {
        throw "Desktop-Hard-Kill-Checkpoint wurde nicht rechtzeitig und eindeutig beobachtet."
    }

    Stop-VerifiedSetupProcessTree -Process $Process -ExpectedPath $Path

    if ((Test-Path -LiteralPath $ServicePreparedPath) -or
        (Get-Service -Name $ExpectedServiceName -ErrorAction SilentlyContinue)) {
        throw "Die SCM-Transaktion begann bereits vor dem Hard Kill; der angeforderte Checkpoint gilt als verfehlt."
    }
    if (-not (Test-Path -LiteralPath $QuarantinedExecutable) -or
        (Test-Path -LiteralPath $LegacyExecutable)) {
        throw "Der hart beendete Desktop-Checkpoint blieb nicht eindeutig quarantänisiert erhalten."
    }
    Assert-CheckpointFileAcl -Path $SealPath -ReaderSid $ExpectedReaderSid
    Assert-CheckpointFileAcl -Path $PhasePath -ReaderSid $ExpectedReaderSid
    Assert-CheckpointFileAcl -Path $ProtectedTokenPath
    $PersistedPhase = Read-StrictJsonMarker -Path $PhasePath -ExpectedProperties @(
        "generation", "phase", "schema_version", "transaction_id"
    )
    if ($PersistedPhase.transaction_id -ne $TransactionId -or $PersistedPhase.phase -ne "rollbackable") {
        throw "Der Desktop-Checkpoint blieb nach dem Hard Kill nicht unverändert erhalten."
    }
    Write-Host "Desktop-Hard-Kill nach Seal/Apply und vor der SCM-Mutation eindeutig erfasst."
}

function Assert-MigratedTokenAcl {
    param([string]$Path)
    $Acl = Get-Acl -LiteralPath $Path
    if (-not $Acl.AreAccessRulesProtected) { throw "Migriertes Token besitzt eine vererbte DACL." }
    $ServiceSid = ([Security.Principal.NTAccount]"NT SERVICE\ERechnungsPrueferService").Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
    $Rules = @($Acl.Access)
    $Sids = @($Rules | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value })
    foreach ($Required in @("S-1-5-18", "S-1-5-32-544", $ServiceSid)) {
        if ($Required -notin $Sids) { throw "Migrierter Token-DACL fehlt $Required." }
    }
    foreach ($Forbidden in @("S-1-1-0", "S-1-5-4", "S-1-5-11", "S-1-5-32-545")) {
        if ($Forbidden -in $Sids) { throw "Migrierter Token-DACL enthält breite Identität $Forbidden." }
    }
    foreach ($Rule in $Rules) {
        if ($Rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            throw "Migrierter Token-DACL enthält einen nicht erlaubten ACE-Typ."
        }
    }
}

function Wait-ServiceRunning {
    param([string]$Name)
    $Deadline = [DateTime]::UtcNow.AddSeconds(330)
    do {
        $Service = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($Service) {
            $Service.Refresh()
            if ($Service.Status -eq "Running") { return }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $Deadline)
    throw "Migrierter Dienst wurde nicht betriebsbereit."
}

if (-not $IsWindows) { throw "Der Migrationstest kann nur unter Windows laufen." }
if (-not $ConfirmIsolatedEnvironment) {
    throw "Migrationstest nur auf einer sauberen Wegwerf-VM mit -ConfirmIsolatedEnvironment ausführen."
}
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Der Migrationstest benötigt eine administrative Testidentität."
}
if ($DesktopHardKillRecovery -ne "None" -and -not $AllowElevatedMigrationTestContext) {
    throw "Hard-Kill-Recovery darf nur mit dem isolierten Testinstaller und -AllowElevatedMigrationTestContext laufen."
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Version = (Get-Content (Join-Path $ProjectRoot "VERSION") -Raw).Trim()
$Release130 = "https://github.com/Harpau/E-Rechnungs-Pruefer/releases/download/v1.3.0/E-Rechnungs-Pruefer-1.3.0-Windows-x64-Setup.exe"
$TemporaryRoot = if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
    [IO.Path]::GetTempPath()
} else {
    $env:RUNNER_TEMP
}
if (-not $DesktopSetup130) {
    $DesktopSetup130 = Join-Path $TemporaryRoot "E-Rechnungs-Pruefer-1.3.0-Windows-x64-Setup.exe"
    Invoke-WebRequest $Release130 -OutFile $DesktopSetup130
}
if (-not $ServiceSetup) {
    $ServiceSetupRoot = if ($AllowElevatedMigrationTestContext) {
        Join-Path $ProjectRoot "build\windows\test-installer"
    } else {
        Join-Path $ProjectRoot "dist"
    }
    $ServiceSetup = Join-Path $ServiceSetupRoot "E-Rechnungs-Pruefer-$Version-Windows-x64-Dienst-Setup.exe"
}
foreach ($Setup in @($DesktopSetup130, $ServiceSetup)) {
    if (-not (Test-Path -LiteralPath $Setup)) { throw "Installer fehlt: $Setup" }
    if (($Setup -eq $DesktopSetup130 -or $RequireSignature) -and
        (Get-AuthenticodeSignature $Setup).Status -ne "Valid") {
        throw "Installer-Signatur ist ungültig: $Setup"
    }
}
if ($DesktopHardKillRecovery -ne "None") {
    $ExpectedTestSetup = Join-Path $ProjectRoot (
        "build\windows\test-installer\E-Rechnungs-Pruefer-$Version-Windows-x64-Dienst-Setup.exe"
    )
    if (-not [string]::Equals(
        [IO.Path]::GetFullPath($ServiceSetup),
        [IO.Path]::GetFullPath($ExpectedTestSetup),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Hard-Kill-Recovery akzeptiert ausschließlich den lokal gebauten isolierten Testinstaller."
    }
    $SetupItem = Get-Item -LiteralPath $ServiceSetup -Force
    if (($SetupItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Der isolierte Testinstaller darf kein Reparse-Point sein."
    }
}

$ServiceName = "ERechnungsPrueferService"
$DesktopDir = Join-Path $env:LOCALAPPDATA "Programs\E-Rechnungs-Pruefer"
$DesktopExe = Join-Path $DesktopDir "E-Rechnungs-Pruefer.exe"
$DesktopUninstaller = Join-Path $DesktopDir "unins000.exe"
$DesktopData = Join-Path $env:LOCALAPPDATA "E-Rechnungs-Pruefer"
$DesktopToken = Join-Path $DesktopData "api-token.txt"
$RuntimeFile = Join-Path $DesktopData "runtime.json"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunName = "E-Rechnungs-Pruefer"
$ServiceDir = Join-Path $env:ProgramFiles "E-Rechnungs-Pruefer-Dienst"
$ServiceExe = Join-Path $ServiceDir "service\E-Rechnungs-Pruefer-Dienst.exe"
$ServiceUninstaller = Join-Path $ServiceDir "unins000.exe"
$ServiceToken = Join-Path $env:ProgramData "E-Rechnungs-Pruefer\api-token.txt"
$MigrationState = Join-Path $env:ProgramData "E-Rechnungs-Pruefer-Installer-State"

$Conflicts = @()
foreach ($Path in @($DesktopDir, $DesktopData, $ServiceDir, (Split-Path $ServiceToken -Parent), $MigrationState)) {
    if (Test-Path $Path) { $Conflicts += $Path }
}
if (Get-Service $ServiceName -ErrorAction SilentlyContinue) { $Conflicts += $ServiceName }
if (Get-ItemProperty $RunKey -Name $RunName -ErrorAction SilentlyContinue) { $Conflicts += "$RunKey\$RunName" }
if ($Conflicts.Count -gt 0) { throw "Vorhandener Produktzustand; Migrationstest bricht ab:`n$($Conflicts -join "`n")" }

Invoke-Setup $DesktopSetup130 @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/TASKS=`"autostart`""
)
if (-not (Test-Path $DesktopExe)) { throw "Desktop v1.3.0 wurde nicht installiert." }
$env:EINVOICE_DESKTOP_NO_DIALOG = "1"
$DesktopProcess = Start-Process $DesktopExe -ArgumentList "--background" -PassThru
$Deadline = [DateTime]::UtcNow.AddSeconds(30)
do {
    Start-Sleep -Milliseconds 250
} while (-not (Test-Path $DesktopToken) -and [DateTime]::UtcNow -lt $Deadline -and -not $DesktopProcess.HasExited)
if (-not (Test-Path $DesktopToken) -or -not (Test-Path $RuntimeFile)) {
    throw "Desktop v1.3.0 wurde für die Migration nicht betriebsbereit."
}
$OriginalToken = (Get-Content $DesktopToken -Raw).Trim()
$ExpectedRun = "`"$DesktopExe`" --background"
if ((Get-ItemPropertyValue $RunKey $RunName) -ne $ExpectedRun) { throw "v1.3.0-HKCU-Autostart fehlt." }

$FailedMigrationArguments = @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/TASKS=`"systemstart`"",
    "/MIGRATEDESKTOPTOKEN=1", "/TESTFAILAFTERCONFIG=1"
)
if ($AllowElevatedMigrationTestContext) {
    $FailedMigrationArguments += "/ALLOWELEVATEDTESTCONTEXT=1"
}
if ($DesktopHardKillRecovery -ne "None") {
    $HardKillArguments = @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/TASKS=`"systemstart`"",
        "/MIGRATEDESKTOPTOKEN=1", "/ALLOWELEVATEDTESTCONTEXT=1"
    )
    Invoke-DesktopCheckpointHardKill -Path $ServiceSetup -Arguments $HardKillArguments `
        -StateDirectory $MigrationState -LegacyExecutable $DesktopExe `
        -ExpectedServiceExecutable $ServiceExe -ExpectedServiceName $ServiceName `
        -ExpectedReaderSid $Identity.User.Value
    if ($DesktopHardKillRecovery -eq "LeaveForReboot") {
        Write-Warning (
            "Der beweisbare Rollbackzustand bleibt absichtlich erhalten. VM jetzt hart neu starten; " +
            "danach denselben Testinstaller mit denselben Migrationsparametern erneut ausführen. " +
            "Diese Ausführung nimmt keine automatische Bereinigung mehr vor und meldet bewusst " +
            "keinen vollständigen Migrationserfolg."
        )
        exit 194
    }
}
Invoke-SetupExpectedFailure $ServiceSetup $FailedMigrationArguments
if (-not $DesktopProcess.WaitForExit(30000)) {
    throw "Desktop v1.3.0 wurde für den Rollback-Test nicht kontrolliert beendet."
}
$RollbackDeadline = [DateTime]::UtcNow.AddSeconds(30)
$RestoredDesktopProcessId = 0
do {
    Start-Sleep -Milliseconds 250
    try {
        $Runtime = Get-Content $RuntimeFile -Raw | ConvertFrom-Json
        $Candidate = Get-Process -Id ([int]$Runtime.pid) -ErrorAction SilentlyContinue
        if ($Candidate -and [int]$Runtime.pid -ne $DesktopProcess.Id) {
            $RestoredDesktopProcessId = [int]$Runtime.pid
        }
    } catch {
        $RestoredDesktopProcessId = 0
    }
} while ($RestoredDesktopProcessId -eq 0 -and [DateTime]::UtcNow -lt $RollbackDeadline)
if ($RestoredDesktopProcessId -eq 0) { throw "Fehlgeschlagene Migration startete den vorher laufenden Desktop nicht neu." }
if ((Get-ItemPropertyValue $RunKey $RunName) -ne $ExpectedRun) {
    throw "Fehlgeschlagene Migration stellte den HKCU-Autostart nicht wieder her."
}
if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
    throw "Fehlgeschlagene Migration ließ einen SCM-Dienst zurück."
}
if (Test-Path $ServiceToken) { throw "Fehlgeschlagene Migration ließ ein Diensttoken zurück." }
if (Test-Path $ServiceDir) { throw "Fehlgeschlagene Migration ließ Dienstbinärdateien zurück." }
if (Test-Path (Split-Path $ServiceToken -Parent)) {
    throw "Fehlgeschlagene Migration ließ unerwarteten Maschinenzustand zurück."
}
if (Test-Path $MigrationState) { throw "Fehlgeschlagene Migration ließ den geschützten Phasenbeleg zurück." }

$MigrationArguments = @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/TASKS=`"systemstart`"",
    "/MIGRATEDESKTOPTOKEN=1"
)
if ($AllowElevatedMigrationTestContext) {
    $MigrationArguments += "/ALLOWELEVATEDTESTCONTEXT=1"
}
Invoke-Setup $ServiceSetup $MigrationArguments
Wait-ServiceRunning $ServiceName
$RestoredDesktopProcess = Get-Process -Id $RestoredDesktopProcessId -ErrorAction SilentlyContinue
if ($RestoredDesktopProcess -and -not $RestoredDesktopProcess.WaitForExit(30000)) {
    throw "Der nach Rollback wiederhergestellte Desktop wurde nicht kontrolliert beendet."
}
if (Get-ItemProperty $RunKey -Name $RunName -ErrorAction SilentlyContinue) {
    throw "HKCU-Autostart wurde beim Moduswechsel nicht entfernt."
}
if ((Get-Content $ServiceToken -Raw).Trim() -ne $OriginalToken) {
    throw "Ausdrücklich freigegebenes API-Token wurde nicht erhalten."
}
Assert-MigratedTokenAcl $ServiceToken

if (Test-Path -LiteralPath $DesktopExe) {
    throw "Die alte Desktop-EXE blieb nach dem erfolgreichen Moduswechsel startbar."
}
$QuarantinedDesktop = "$DesktopExe.service-mode-disabled"
if (Test-Path -LiteralPath $QuarantinedDesktop) {
    throw "Die quarantänisierte Desktop-EXE wurde nach dem erfolgreichen Moduswechsel nicht entfernt."
}
if (Test-Path $MigrationState) { throw "Erfolgreiche Migration ließ den geschützten Phasenbeleg zurück." }

$EINVOICE_API_TOKEN = $OriginalToken
$Config = Get-Content (Join-Path (Split-Path $ServiceToken -Parent) "service.json") -Raw | ConvertFrom-Json
$Listeners = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$Config.port))
if ($Listeners.Count -ne 1 -or $Listeners[0].LocalAddress -ne "127.0.0.1") {
    throw "Nach dem portunabhängigen Deaktivieren des alten Backends existiert nicht genau ein Loopback-Listener."
}
$Status = & curl.exe --silent --output NUL --write-out "%{http_code}" `
    --header "Authorization: Bearer $EINVOICE_API_TOKEN" `
    --form "file=@$(Join-Path $ProjectRoot 'app\examples\cii-rechnung-demo.xml');type=application/xml" `
    --form "official=false" "http://127.0.0.1:$($Config.port)/api/analyze"
if ($Status -ne "200") { throw "Migriertes Node-RED-Bearer-Token funktioniert nicht." }

Invoke-Setup $ServiceUninstaller @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/PURGEDATA=1")
Invoke-Setup $DesktopUninstaller @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
if (Get-Service $ServiceName -ErrorAction SilentlyContinue) { throw "Dienst blieb nach Migrationstest registriert." }

Write-Host "Migration von v1.3.0 zum Dienstmodus erfolgreich geprüft."
