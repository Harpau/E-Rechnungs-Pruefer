[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$Setup = "",
    [switch]$RequireSignature
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $IsWindows) {
    throw "Der Installer-Test kann nur unter Windows laufen."
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Version = (Get-Content (Join-Path $ProjectRoot "VERSION") -Raw).Trim()
if (-not $Setup) {
    $Setup = Join-Path $ProjectRoot "dist\E-Rechnungs-Pruefer-$Version-Windows-x64-Setup.exe"
}
if (-not (Test-Path $Setup)) {
    throw "Installer nicht gefunden: $Setup"
}

$Signature = Get-AuthenticodeSignature $Setup
if ($RequireSignature -and $Signature.Status -ne "Valid") {
    throw "Der Release-Installer besitzt keine gültige Authenticode-Signatur: $($Signature.Status)"
}

$TestRoot = Join-Path $env:RUNNER_TEMP "e-rechnungs-pruefer-package-test"
$InstallDir = Join-Path $TestRoot "Installation mit Leerzeichen"
$CookieFile = Join-Path $TestRoot "cookies.txt"
$XmlOutput = Join-Path $TestRoot "export.xml"
$InstallLog = Join-Path $TestRoot "install.log"
$UninstallLog = Join-Path $TestRoot "uninstall.log"
$RuntimeFile = Join-Path $env:LOCALAPPDATA "E-Rechnungs-Pruefer\runtime.json"
$StartupErrorFile = Join-Path $env:LOCALAPPDATA "E-Rechnungs-Pruefer\startup-error.log"

Remove-Item $TestRoot -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $RuntimeFile -Force -ErrorAction SilentlyContinue
Remove-Item $StartupErrorFile -Force -ErrorAction SilentlyContinue
New-Item $TestRoot -ItemType Directory -Force | Out-Null
$runtime = $null
$health = $null

try {
    $installer = Start-Process $Setup -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/DIR=`"$InstallDir`"",
        "/LOG=`"$InstallLog`""
    ) -PassThru -Wait
    if ($installer.ExitCode -ne 0) {
        throw "Installation fehlgeschlagen (Exitcode $($installer.ExitCode))."
    }

    $Executable = Join-Path $InstallDir "E-Rechnungs-Pruefer.exe"
    if (-not (Test-Path $Executable)) {
        throw "Installierte Anwendung nicht gefunden: $Executable"
    }

    $env:EINVOICE_DESKTOP_NO_DIALOG = "1"
    $process = Start-Process $Executable -PassThru
    Remove-Item Env:EINVOICE_DESKTOP_NO_DIALOG
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
        if (Test-Path $RuntimeFile) {
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
        $StartupError = if (Test-Path $StartupErrorFile) {
            (Get-Content $StartupErrorFile -Raw).Trim()
        } else {
            "Keine Startdiagnose wurde geschrieben."
        }
        throw "Die installierte Anwendung wurde nicht betriebsbereit.`n$StartupError"
    }
    if (-not $health.kosit.configured) {
        throw "Das Windows-Paket enthält keine betriebsbereite KoSIT-Konfiguration."
    }

    $Bootstrap = "http://127.0.0.1:$($runtime.port)/desktop/bootstrap?token=$($runtime.token)"
    & curl.exe --silent --show-error --fail --location --cookie-jar $CookieFile $Bootstrap | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Desktop-Sitzung konnte nicht initialisiert werden."
    }

    $Example = Join-Path $ProjectRoot "app\examples\cii-rechnung-demo.xml"
    $AnalysisJson = & curl.exe --silent --show-error --fail `
        --cookie $CookieFile `
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

    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    $process.WaitForExit()

    $Uninstaller = Join-Path $InstallDir "unins000.exe"
    if (-not (Test-Path $Uninstaller)) {
        throw "Deinstallationsprogramm wurde nicht gefunden."
    }
    $uninstallerProcess = Start-Process $Uninstaller -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/LOG=`"$UninstallLog`""
    ) -PassThru -Wait
    if ($uninstallerProcess.ExitCode -ne 0) {
        throw "Deinstallation fehlgeschlagen (Exitcode $($uninstallerProcess.ExitCode))."
    }
    if (Test-Path $Executable) {
        throw "Die Anwendung blieb nach der Deinstallation zurück."
    }
    if (Test-Path $RuntimeFile) {
        throw "Die Laufzeitdatei blieb nach der Deinstallation zurück."
    }
} finally {
    Remove-Item Env:EINVOICE_DESKTOP_NO_DIALOG -ErrorAction SilentlyContinue
    Get-Process "E-Rechnungs-Pruefer" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}

Write-Host "Windows-Installer erfolgreich geprüft: $Setup"
