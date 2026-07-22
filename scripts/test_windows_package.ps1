[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$Setup = "",
    [switch]$RequireSignature,
    [switch]$ConfirmIsolatedEnvironment,
    [switch]$PreflightOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Stop-OwnedProcess {
    param(
        [Parameter(Mandatory = $false)]
        [AllowNull()]
        [System.Diagnostics.Process]$OwnedProcess
    )

    if ($null -eq $OwnedProcess) {
        return
    }
    $OwnedProcess.Refresh()
    if ($OwnedProcess.HasExited) {
        return
    }

    $OwnedProcess.Kill($true)
    if (-not $OwnedProcess.WaitForExit(10000)) {
        throw "Der vom Pakettest gestartete Prozessbaum $($OwnedProcess.Id) wurde nicht innerhalb von 10 Sekunden beendet."
    }
}

function Resolve-OwnedInstalledProcess {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedExecutable
    )

    $Candidate = Get-Process -Id $ProcessId -ErrorAction Stop
    $ActualExecutable = $Candidate.Path
    if (-not [string]::Equals(
        [System.IO.Path]::GetFullPath($ActualExecutable),
        [System.IO.Path]::GetFullPath($ExpectedExecutable),
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Der automatisch gestartete Prozess $ProcessId gehört nicht zur Testinstallation: $ActualExecutable"
    }
    return $Candidate
}

function Restore-ProcessEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$HadNoDialog,
        [Parameter(Mandatory = $false)]
        [AllowNull()]
        $NoDialogValue,
        [Parameter(Mandatory = $true)]
        [bool]$HadPort,
        [Parameter(Mandatory = $false)]
        [AllowNull()]
        $PortValue
    )

    $Target = [System.EnvironmentVariableTarget]::Process
    if ($HadNoDialog) {
        [System.Environment]::SetEnvironmentVariable("EINVOICE_DESKTOP_NO_DIALOG", $NoDialogValue, $Target)
    } else {
        Remove-Item Env:EINVOICE_DESKTOP_NO_DIALOG -ErrorAction SilentlyContinue
    }
    if ($HadPort) {
        [System.Environment]::SetEnvironmentVariable("PORT", $PortValue, $Target)
    } else {
        Remove-Item Env:PORT -ErrorAction SilentlyContinue
    }
}

function Invoke-TestUninstaller {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$LogPath
    )

    $UninstallerProcess = Start-Process $Path -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/LOG=`"$LogPath`""
    ) -PassThru
    if (-not $UninstallerProcess.WaitForExit(120000)) {
        try {
            Stop-OwnedProcess -OwnedProcess $UninstallerProcess
        } catch {
            Write-Warning "Der hängende Test-Uninstaller konnte nicht beendet werden: $_"
        }
        throw "Die Deinstallation überschritt das Zeitlimit von 120 Sekunden."
    }
    if ($UninstallerProcess.ExitCode -ne 0) {
        throw "Deinstallation fehlgeschlagen (Exitcode $($UninstallerProcess.ExitCode))."
    }
}

function Invoke-TestInstaller {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TargetDirectory,
        [Parameter(Mandatory = $true)]
        [string]$LogPath
    )

    $InstallerProcess = Start-Process $Path -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/TASKS=`"autostart`"",
        "/DIR=`"$TargetDirectory`"",
        "/LOG=`"$LogPath`""
    ) -PassThru
    if (-not $InstallerProcess.WaitForExit(300000)) {
        try {
            Stop-OwnedProcess -OwnedProcess $InstallerProcess
        } catch {
            Write-Warning "Der hängende Test-Installer konnte nicht beendet werden: $_"
        }
        throw "Die Installation überschritt das Zeitlimit von 300 Sekunden."
    }
    if ($InstallerProcess.ExitCode -ne 0) {
        throw "Installation fehlgeschlagen (Exitcode $($InstallerProcess.ExitCode))."
    }
}

function Get-OptionalRegistryValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $MissingValue = [PSCustomObject]@{
        Exists = $false
        Kind = $null
        Value = $null
    }
    if (-not (Test-Path -LiteralPath $Path -ErrorAction Stop)) {
        return $MissingValue
    }

    $RegistryKey = Get-Item -LiteralPath $Path -ErrorAction Stop
    try {
        $ExistingName = $RegistryKey.GetValueNames() |
            Where-Object { [string]::Equals($_, $Name, [System.StringComparison]::OrdinalIgnoreCase) } |
            Select-Object -First 1
        if ($null -eq $ExistingName) {
            return $MissingValue
        }
        return [PSCustomObject]@{
            Exists = $true
            Kind = $RegistryKey.GetValueKind($ExistingName)
            Value = $RegistryKey.GetValue(
                $ExistingName,
                $null,
                [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
            )
        }
    } finally {
        $RegistryKey.Dispose()
    }
}

function Test-ExpectedStringRegistryValue {
    param(
        [Parameter(Mandatory = $true)]
        $State,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedValue
    )

    return (
        $State.Exists -and
        $State.Kind -eq [Microsoft.Win32.RegistryValueKind]::String -and
        $State.Value -is [string] -and
        [string]::Equals([string]$State.Value, $ExpectedValue, [System.StringComparison]::Ordinal)
    )
}

if (-not $IsWindows) {
    throw "Der Installer-Test kann nur unter Windows laufen."
}
if (-not $ConfirmIsolatedEnvironment) {
    throw @"
Dieser Pakettest installiert und deinstalliert dieselbe Produkt-ID wie die reguläre Anwendung.
Er darf nur auf einer sauberen, entbehrlichen Windows-VM oder Testidentität laufen.
Starten Sie ihn dort erneut mit -ConfirmIsolatedEnvironment.
"@
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Version = (Get-Content (Join-Path $ProjectRoot "VERSION") -Raw).Trim()
if (-not $Setup) {
    $Setup = Join-Path $ProjectRoot "dist\E-Rechnungs-Pruefer-$Version-Windows-x64-Setup.exe"
}

$TemporaryRoot = if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
    [System.IO.Path]::GetTempPath()
} else {
    $env:RUNNER_TEMP
}
$TestRoot = Join-Path $TemporaryRoot "e-rechnungs-pruefer-package-test-$([Guid]::NewGuid().ToString('N'))"
$InstallDir = Join-Path $TestRoot "Installation mit Leerzeichen"
$Executable = Join-Path $InstallDir "E-Rechnungs-Pruefer.exe"
$Uninstaller = Join-Path $InstallDir "unins000.exe"
$CookieFile = Join-Path $TestRoot "cookies.txt"
$XmlOutput = Join-Path $TestRoot "export.xml"
$PdfOutput = Join-Path $TestRoot "report.pdf"
$PdfHeaders = Join-Path $TestRoot "report-headers.txt"
$InstallLog = Join-Path $TestRoot "install.log"
$UpdateLog = Join-Path $TestRoot "update.log"
$UninstallLog = Join-Path $TestRoot "uninstall.log"
$DataDirectory = Join-Path $env:LOCALAPPDATA "E-Rechnungs-Pruefer"
$RuntimeFile = Join-Path $DataDirectory "runtime.json"
$ApiTokenFile = Join-Path $DataDirectory "api-token.txt"
$StartupErrorFile = Join-Path $DataDirectory "startup-error.log"
$DefaultInstallDir = Join-Path $env:LOCALAPPDATA "Programs\E-Rechnungs-Pruefer"
$StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\E-Rechnungs-Prüfer"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunValueName = "E-Rechnungs-Pruefer"
$ExpectedAutostartCommand = "`"$Executable`" --background"
$UninstallKeyName = "{D33FD9E5-0C5E-48ED-BF0C-E9D2962A45DF}_is1"
$UninstallKeys = @(
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$UninstallKeyName",
    "HKCU:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\$UninstallKeyName",
    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$UninstallKeyName",
    "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\$UninstallKeyName"
)

$HadNoDialog = Test-Path Env:EINVOICE_DESKTOP_NO_DIALOG
$OriginalNoDialog = [System.Environment]::GetEnvironmentVariable(
    "EINVOICE_DESKTOP_NO_DIALOG",
    [System.EnvironmentVariableTarget]::Process
)
$HadPort = Test-Path Env:PORT
$OriginalPort = [System.Environment]::GetEnvironmentVariable(
    "PORT",
    [System.EnvironmentVariableTarget]::Process
)
$CurrentUserSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ([string]::IsNullOrWhiteSpace($CurrentUserSid)) {
    throw "Die Windows-Benutzeridentität für die Pakettest-Sperre konnte nicht bestimmt werden."
}
$PackageTestMutexName = "Global\E-Rechnungs-Pruefer-Package-Test-$CurrentUserSid"
$PackageTestMutex = [System.Threading.Mutex]::new($false, $PackageTestMutexName)
$PackageTestMutexAcquired = $false
$InstallationStarted = $false
$UninstallCompleted = $false
$process = $null
$restartedProcess = $null
$runtime = $null
$health = $null

try {
    try {
        $PackageTestMutexAcquired = $PackageTestMutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $PackageTestMutexAcquired = $true
    }
    if (-not $PackageTestMutexAcquired) {
        throw "Ein anderer Windows-Pakettest läuft bereits für diese Benutzeridentität."
    }

    $ExistingState = [System.Collections.Generic.List[string]]::new()
    if (Test-Path -LiteralPath $TestRoot) {
        [void]$ExistingState.Add("Testverzeichnis: $TestRoot")
    }
    if (Test-Path -LiteralPath $DefaultInstallDir) {
        [void]$ExistingState.Add("Standardinstallation: $DefaultInstallDir")
    }
    if (Test-Path -LiteralPath $StartMenuDir) {
        [void]$ExistingState.Add("Startmenüeintrag: $StartMenuDir")
    }

    $KnownDataStateFound = $false
    foreach ($StateFile in @($RuntimeFile, $ApiTokenFile, $StartupErrorFile)) {
        if (Test-Path -LiteralPath $StateFile) {
            $KnownDataStateFound = $true
            [void]$ExistingState.Add("Anwendungszustand: $StateFile")
        }
    }
    if (-not $KnownDataStateFound -and (Test-Path -LiteralPath $DataDirectory)) {
        [void]$ExistingState.Add("Anwendungsdatenverzeichnis: $DataDirectory")
    }

    $ExistingAutostartState = Get-OptionalRegistryValue -Path $RunKey -Name $RunValueName
    if ($ExistingAutostartState.Exists) {
        [void]$ExistingState.Add("Autostart-Eintrag: $RunKey\$RunValueName")
    }
    foreach ($UninstallKey in $UninstallKeys) {
        if (Test-Path -LiteralPath $UninstallKey -ErrorAction SilentlyContinue) {
            [void]$ExistingState.Add("Installationsregistrierung: $UninstallKey")
        }
    }

    $ExistingProcesses = @(Get-Process -Name "E-Rechnungs-Pruefer" -ErrorAction SilentlyContinue)
    if ($ExistingProcesses.Count -gt 0) {
        $ProcessIds = ($ExistingProcesses.Id -join ", ")
        [void]$ExistingState.Add("laufender E-Rechnungs-Pruefer-Prozess (PID $ProcessIds)")
    }

    if ($ExistingState.Count -gt 0) {
        $ConflictList = $ExistingState -join "`n- "
        throw @"
Der Windows-Pakettest wurde vor der Installation abgebrochen, weil bereits Anwendungszustand vorhanden ist:
- $ConflictList

Verwenden Sie eine saubere, entbehrliche Windows-VM oder Testidentität. Bestehende Installationen und Daten werden nicht verändert.
"@
    }

    if ($PreflightOnly) {
        Write-Host "Windows-Pakettest-Vorabprüfung erfolgreich: kein bestehender Anwendungszustand erkannt."
        return
    }
    if (-not (Test-Path -LiteralPath $Setup)) {
        throw "Installer nicht gefunden: $Setup"
    }
    $Signature = Get-AuthenticodeSignature $Setup
    if ($RequireSignature -and $Signature.Status -ne "Valid") {
        throw "Der Release-Installer besitzt keine gültige Authenticode-Signatur: $($Signature.Status)"
    }

    New-Item $TestRoot -ItemType Directory | Out-Null
    $InstallationStarted = $true
    Invoke-TestInstaller -Path $Setup -TargetDirectory $InstallDir -LogPath $InstallLog

    if (-not (Test-Path -LiteralPath $Executable)) {
        throw "Installierte Anwendung nicht gefunden: $Executable"
    }
    $AutostartState = Get-OptionalRegistryValue -Path $RunKey -Name $RunValueName
    if (-not (Test-ExpectedStringRegistryValue -State $AutostartState -ExpectedValue $ExpectedAutostartCommand)) {
        throw "Der optionale Autostart wurde nicht korrekt eingerichtet: $($AutostartState.Value)"
    }

    try {
        $env:EINVOICE_DESKTOP_NO_DIALOG = "1"
        $env:PORT = "18080"
        $process = Start-Process $Executable -ArgumentList "--background" -PassThru
    } finally {
        Restore-ProcessEnvironment `
            -HadNoDialog $HadNoDialog `
            -NoDialogValue $OriginalNoDialog `
            -HadPort $HadPort `
            -PortValue $OriginalPort
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
        if (Test-Path -LiteralPath $RuntimeFile) {
            try {
                $runtime = Get-Content $RuntimeFile -Raw | ConvertFrom-Json
                $health = Invoke-RestMethod "http://127.0.0.1:$($runtime.port)/api/health" -TimeoutSec 2
            } catch {
                $runtime = $null
                $health = $null
            }
        }
    } until (($runtime -and $health.status -eq "ok") -or [DateTime]::UtcNow -ge $deadline -or $process.HasExited)

    if (-not $runtime -or -not $health -or $health.status -ne "ok") {
        $StartupError = if (Test-Path -LiteralPath $StartupErrorFile) {
            (Get-Content $StartupErrorFile -Raw).Trim()
        } else {
            "Keine Startdiagnose wurde geschrieben."
        }
        throw "Die installierte Anwendung wurde nicht betriebsbereit.`n$StartupError"
    }
    if (-not $health.kosit.configured) {
        throw "Das Windows-Paket enthält keine betriebsbereite KoSIT-Konfiguration."
    }
    if ($runtime.port -ne 18080) {
        throw "Die installierte Anwendung verwendet nicht den konfigurierten festen Port."
    }
    if (-not (Test-Path -LiteralPath $ApiTokenFile)) {
        throw "Das persistente API-Zugriffstoken wurde nicht angelegt."
    }
    $ApiToken = (Get-Content $ApiTokenFile -Raw).Trim()
    if ($ApiToken.Length -lt 32) {
        throw "Das persistente API-Zugriffstoken ist ungültig."
    }

    $Bootstrap = "http://127.0.0.1:$($runtime.port)/desktop/bootstrap?token=$($runtime.token)"
    & curl.exe --silent --show-error --fail --location --cookie-jar $CookieFile $Bootstrap | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Desktop-Sitzung konnte nicht initialisiert werden."
    }

    $Example = Join-Path $ProjectRoot "app\examples\cii-rechnung-demo.xml"
    $AnalysisJson = & curl.exe --silent --show-error --fail `
        --header "Authorization: Bearer $ApiToken" `
        --form "file=@$Example;type=application/xml" `
        --form "official=false" `
        "http://127.0.0.1:$($runtime.port)/api/analyze"
    if ($LASTEXITCODE -ne 0) {
        throw "Analyse über das installierte Paket ist fehlgeschlagen."
    }
    $Analysis = $AnalysisJson | ConvertFrom-Json
    if ($Analysis.document.id -ne "CII-DEMO-1") {
        throw "Das Paket lieferte ein unerwartetes Analyseergebnis."
    }

    $OfficialJson = & curl.exe --silent --show-error --fail `
        --cookie $CookieFile `
        --form "file=@$Example;type=application/xml" `
        --form "official=true" `
        "http://127.0.0.1:$($runtime.port)/api/analyze"
    if ($LASTEXITCODE -ne 0) {
        throw "KoSIT-Prüfung über das installierte Paket ist fehlgeschlagen."
    }
    $Official = $OfficialJson | ConvertFrom-Json
    if (-not $Official.validation.official.executed) {
        throw "KoSIT wurde im installierten Paket nicht ausgeführt: $($Official.validation.official.summary)"
    }

    & curl.exe --silent --show-error --fail `
        --cookie $CookieFile `
        --form "file=@$Example;type=application/xml" `
        --output $XmlOutput `
        "http://127.0.0.1:$($runtime.port)/api/xml"
    if ($LASTEXITCODE -ne 0) {
        throw "XML-Export über das installierte Paket ist fehlgeschlagen."
    }
    if ((Get-FileHash $Example -Algorithm SHA256).Hash -ne (Get-FileHash $XmlOutput -Algorithm SHA256).Hash) {
        throw "Der XML-Export ist nicht byteidentisch."
    }

    & curl.exe --silent --show-error --fail `
        --header "Authorization: Bearer $ApiToken" `
        --form "file=@$Example;type=application/xml" `
        --form "official=false" `
        --dump-header $PdfHeaders `
        --output $PdfOutput `
        "http://127.0.0.1:$($runtime.port)/api/report/pdf"
    if ($LASTEXITCODE -ne 0) {
        throw "PDF-Bericht über das installierte Paket ist fehlgeschlagen."
    }
    $PdfBytes = [System.IO.File]::ReadAllBytes($PdfOutput)
    if ($PdfBytes.Length -lt 5 -or
        [System.Text.Encoding]::ASCII.GetString($PdfBytes, 0, 5) -ne "%PDF-") {
        throw "Der installierte PDF-Endpunkt lieferte keine gültige PDF-Signatur."
    }
    $PdfResponseHeaders = Get-Content $PdfHeaders -Raw
    foreach ($ExpectedHeader in @(
        '(?im)^Content-Type:\s*application/pdf\s*\r?$',
        '(?im)^Content-Disposition:\s*attachment; filename="E-Rechnungs-Pruefbericht\.pdf"\s*\r?$',
        '(?im)^X-Einvoice-Syntax:\s*CII\s*\r?$',
        '(?im)^X-Einvoice-Validation-Status:\s*warning\s*\r?$',
        '(?im)^X-Einvoice-Official-Status:\s*not-requested\s*\r?$'
    )) {
        if ($PdfResponseHeaders -notmatch $ExpectedHeader) {
            throw "Dem installierten PDF-Endpunkt fehlt ein erwarteter Antwort-Header: $ExpectedHeader"
        }
    }
    if ($PdfResponseHeaders -match 'CII-DEMO-1' -or $PdfResponseHeaders -match [regex]::Escape((Split-Path $Example -Leaf))) {
        throw "Der installierte PDF-Endpunkt veröffentlicht fachliche Daten in Antwort-Headern."
    }

    $OriginalProcessId = $process.Id
    try {
        $env:EINVOICE_DESKTOP_NO_DIALOG = "1"
        $env:PORT = "18080"
        Invoke-TestInstaller -Path $Setup -TargetDirectory $InstallDir -LogPath $UpdateLog
    } finally {
        Restore-ProcessEnvironment `
            -HadNoDialog $HadNoDialog `
            -NoDialogValue $OriginalNoDialog `
            -HadPort $HadPort `
            -PortValue $OriginalPort
    }

    if (-not $process.WaitForExit(10000)) {
        throw "Die laufende Anwendung wurde beim Update nicht kontrolliert beendet."
    }

    $runtime = $null
    $health = $null
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
        if (Test-Path -LiteralPath $RuntimeFile) {
            try {
                $runtime = Get-Content $RuntimeFile -Raw | ConvertFrom-Json
                if ([int]$runtime.pid -ne $OriginalProcessId) {
                    $health = Invoke-RestMethod "http://127.0.0.1:$($runtime.port)/api/health" -TimeoutSec 2
                }
            } catch {
                $runtime = $null
                $health = $null
            }
        }
    } until (($runtime -and [int]$runtime.pid -ne $OriginalProcessId -and $health.status -eq "ok") -or
        [DateTime]::UtcNow -ge $deadline)

    if (-not $runtime -or [int]$runtime.pid -eq $OriginalProcessId -or -not $health -or $health.status -ne "ok") {
        throw "Die zuvor laufende Autostart-Anwendung wurde nach dem Update nicht erneut betriebsbereit gestartet."
    }
    if ([int]$runtime.port -ne 18080) {
        throw "Die nach dem Update neu gestartete Anwendung verwendet nicht den konfigurierten festen Port."
    }
    $restartedProcess = Resolve-OwnedInstalledProcess `
        -ProcessId ([int]$runtime.pid) `
        -ExpectedExecutable $Executable
    $UpdatedApiToken = (Get-Content $ApiTokenFile -Raw).Trim()
    if ($UpdatedApiToken -ne $ApiToken) {
        throw "Das persistente API-Zugriffstoken wurde beim Update unerwartet geändert."
    }

    if (-not (Test-Path -LiteralPath $Uninstaller)) {
        throw "Deinstallationsprogramm wurde nicht gefunden."
    }
    Invoke-TestUninstaller -Path $Uninstaller -LogPath $UninstallLog
    $UninstallCompleted = $true

    if (-not $restartedProcess.WaitForExit(10000)) {
        throw "Die laufende Anwendung wurde bei der Deinstallation nicht kontrolliert beendet."
    }

    if (Test-Path -LiteralPath $Executable) {
        throw "Die Anwendung blieb nach der Deinstallation zurück."
    }
    if (Test-Path -LiteralPath $RuntimeFile) {
        throw "Die Laufzeitdatei blieb nach der Deinstallation zurück."
    }
    if (Test-Path -LiteralPath $ApiTokenFile) {
        throw "Das API-Zugriffstoken blieb nach der Deinstallation zurück."
    }
    $RemainingAutostartState = Get-OptionalRegistryValue -Path $RunKey -Name $RunValueName
    if ($RemainingAutostartState.Exists) {
        throw "Der Autostart-Eintrag blieb nach der Deinstallation zurück."
    }
} finally {
    Restore-ProcessEnvironment `
        -HadNoDialog $HadNoDialog `
        -NoDialogValue $OriginalNoDialog `
        -HadPort $HadPort `
        -PortValue $OriginalPort

    $OwnedProcessStopped = $true
    try {
        Stop-OwnedProcess -OwnedProcess $process
        Stop-OwnedProcess -OwnedProcess $restartedProcess
    } catch {
        $OwnedProcessStopped = $false
        Write-Warning "Der vom Pakettest gestartete Prozess konnte nicht bereinigt werden: $_"
    }

    if ($InstallationStarted -and -not $UninstallCompleted) {
        if (-not $OwnedProcessStopped) {
            Write-Warning "Die Deinstallation wird ausgelassen, solange der test-eigene Prozess nicht sicher beendet ist."
        } elseif (Test-Path -LiteralPath $Uninstaller) {
            try {
                Invoke-TestUninstaller -Path $Uninstaller -LogPath $UninstallLog
                $UninstallCompleted = $true
            } catch {
                Write-Warning "Die fehlgeschlagene Testinstallation konnte nicht vollständig deinstalliert werden: $_"
            }
        } else {
            Write-Warning "Für die fehlgeschlagene Testinstallation wurde kein eigener Uninstaller gefunden; mögliche Reste bleiben in der Testumgebung erhalten."
        }
    }

    try {
        if ($InstallationStarted) {
            $CurrentAutostartState = Get-OptionalRegistryValue -Path $RunKey -Name $RunValueName
            if (Test-ExpectedStringRegistryValue -State $CurrentAutostartState -ExpectedValue $ExpectedAutostartCommand) {
                Remove-ItemProperty -Path $RunKey -Name $RunValueName -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {
        Write-Warning "Der test-eigene Autostart-Eintrag konnte nicht sicher geprüft oder bereinigt werden: $_"
    }
    try {
        if ($PackageTestMutexAcquired) {
            $PackageTestMutex.ReleaseMutex()
        }
    } catch {
        Write-Warning "Die Pakettest-Sperre konnte nicht ordnungsgemäß freigegeben werden: $_"
    } finally {
        $PackageTestMutex.Dispose()
    }
}

Write-Host "Windows-Installer erfolgreich geprüft: $Setup"
