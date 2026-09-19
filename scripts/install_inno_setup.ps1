[CmdletBinding()]
param(
    [string]$InstallDirectory = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$InnoSetupVersion = "7.1.0"
$InstallerFileName = "innosetup-$InnoSetupVersion-x64.exe"
$InstallerUrl = "https://github.com/jrsoftware/issrc/releases/download/is-7_1_0/$InstallerFileName"
$ExpectedInstallerSha256 = "0362a383ed217d4c4239b5933866dd96d3eb2102737da92f80f6057a4b40df2f"
$ExpectedCompilerSha256 = "d06ebd38f38e3cee60a3c50cc45bd449d77e0bc6a5cabc607ea9886808e4de1a"

if (-not $IsWindows) {
    throw "Inno Setup kann nur unter Windows installiert werden."
}

if (-not $InstallDirectory) {
    if ($env:EINVOICE_INNO_SETUP_COMPILER) {
        $InstallDirectory = Split-Path -Parent $env:EINVOICE_INNO_SETUP_COMPILER
    } elseif ($env:RUNNER_TEMP) {
        $InstallDirectory = Join-Path $env:RUNNER_TEMP "inno-setup-$InnoSetupVersion"
    } else {
        $InstallDirectory = Join-Path $ProjectRoot ".cache\windows-build-tools\inno-setup-$InnoSetupVersion"
    }
}

$InstallDirectory = [System.IO.Path]::GetFullPath($InstallDirectory)
$CompilerPath = Join-Path $InstallDirectory "ISCC.exe"
if ($env:EINVOICE_INNO_SETUP_COMPILER) {
    $ConfiguredCompilerPath = [System.IO.Path]::GetFullPath($env:EINVOICE_INNO_SETUP_COMPILER)
    if (-not $CompilerPath.Equals($ConfiguredCompilerPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Installationsziel und EINVOICE_INNO_SETUP_COMPILER bezeichnen nicht denselben Compiler."
    }
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-CompilerHash {
    if (-not (Test-Path -LiteralPath $CompilerPath -PathType Leaf)) {
        throw "Der erwartete Inno-Setup-Compiler fehlt: $CompilerPath"
    }
    $ActualCompilerSha256 = Get-Sha256 $CompilerPath
    if ($ActualCompilerSha256 -ne $ExpectedCompilerSha256) {
        throw "Der Inno-Setup-Compiler entspricht nicht dem festgeschriebenen Inno Setup $InnoSetupVersion x64."
    }
}

if (Test-Path -LiteralPath $CompilerPath -PathType Leaf) {
    Assert-CompilerHash
    Write-Host "Festgeschriebener Inno-Setup-Compiler bereits vorhanden: $CompilerPath"
    Write-Output $CompilerPath
    return
}
if (Test-Path -LiteralPath $InstallDirectory) {
    throw "Das Inno-Setup-Installationsziel ist nicht frisch: $InstallDirectory"
}

$DownloadRoot = if ($env:RUNNER_TEMP) {
    $env:RUNNER_TEMP
} else {
    Join-Path $ProjectRoot ".cache\windows-build-tools"
}
New-Item -Path $DownloadRoot -ItemType Directory -Force | Out-Null
$InstallerPath = Join-Path $DownloadRoot $InstallerFileName

if (Test-Path -LiteralPath $InstallerPath -PathType Leaf) {
    if ((Get-Sha256 $InstallerPath) -ne $ExpectedInstallerSha256) {
        Remove-Item -LiteralPath $InstallerPath -Force
    }
}
if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
    Invoke-WebRequest -Uri $InstallerUrl -OutFile $InstallerPath
}
$ActualInstallerSha256 = Get-Sha256 $InstallerPath
if ($ActualInstallerSha256 -ne $ExpectedInstallerSha256) {
    throw "SHA-256-Prüfung für $InstallerFileName fehlgeschlagen."
}

$InstallProcess = Start-Process -FilePath $InstallerPath -ArgumentList @(
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/CURRENTUSER",
    "/DIR=`"$InstallDirectory`""
) -Wait -PassThru
if ($InstallProcess.ExitCode -ne 0) {
    throw "Inno Setup $InnoSetupVersion konnte nicht installiert werden (Exitcode $($InstallProcess.ExitCode))."
}

Assert-CompilerHash
Write-Host "Inno Setup $InnoSetupVersion x64 installiert: $CompilerPath"
Write-Output $CompilerPath
