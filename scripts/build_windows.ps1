[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$SignTool = "",
    [string]$CertificateSha1 = $env:EINVOICE_SIGN_CERT_SHA1,
    [string]$AzureSignTool = $env:EINVOICE_AZURE_SIGN_TOOL,
    [string]$AzureKeyVaultUrl = $env:EINVOICE_AZURE_KEY_VAULT_URL,
    [string]$AzureKeyVaultCertificate = $env:EINVOICE_AZURE_KEY_VAULT_CERTIFICATE,
    [string]$TimestampUrl = "http://timestamp.acs.microsoft.com",
    [switch]$WithoutOfficialValidation
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Version = (Get-Content (Join-Path $ProjectRoot "VERSION") -Raw).Trim()
$SpecFile = Join-Path $ProjectRoot "packaging\windows\e_rechnungs_pruefer.spec"
$InstallerFile = Join-Path $ProjectRoot "packaging\windows\installer.iss"
$BuildRoot = Join-Path $ProjectRoot "build\windows"
$BundleRoot = Join-Path $BuildRoot "bundle"
$WorkRoot = Join-Path $BuildRoot "pyinstaller"
$AppBundle = Join-Path $BundleRoot "E-Rechnungs-Pruefer"
$DistRoot = Join-Path $ProjectRoot "dist"

if (-not $IsWindows) {
    throw "Das Windows-Paket kann nur unter Windows gebaut werden."
}
if (-not [Environment]::Is64BitProcess) {
    throw "Der Build muss mit einem x64-Python-Prozess laufen."
}

$AzureSigningValues = @($AzureSignTool, $AzureKeyVaultUrl, $AzureKeyVaultCertificate) |
    Where-Object { $_ }
$UseAzureSigning = $AzureSigningValues.Count -gt 0
if ($UseAzureSigning -and $AzureSigningValues.Count -ne 3) {
    throw "Für Azure-Signierung müssen EINVOICE_AZURE_SIGN_TOOL, EINVOICE_AZURE_KEY_VAULT_URL und EINVOICE_AZURE_KEY_VAULT_CERTIFICATE gemeinsam gesetzt sein."
}
if ($UseAzureSigning -and $CertificateSha1) {
    throw "Azure-Signierung und lokales Zertifikat dürfen nicht gleichzeitig konfiguriert sein."
}
$SigningEnabled = $UseAzureSigning -or [bool]$CertificateSha1

$BundledJava = Join-Path $ProjectRoot "runtime\java\bin\java.exe"
$BundledValidator = Get-ChildItem (Join-Path $ProjectRoot "vendor\kosit\validator\*-standalone.jar") -ErrorAction SilentlyContinue |
    Select-Object -First 1
$BundledScenarios = Get-ChildItem (Join-Path $ProjectRoot "vendor\kosit\xrechnung") -Filter "scenarios.xml" -Recurse -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $WithoutOfficialValidation -and (-not (Test-Path $BundledJava) -or -not $BundledValidator -or -not $BundledScenarios)) {
    throw "Java/KoSIT sind nicht vollständig vorbereitet. Zuerst 'python scripts/prepare_windows_components.py' ausführen."
}

Remove-Item $BuildRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item $BundleRoot -ItemType Directory -Force | Out-Null
New-Item $WorkRoot -ItemType Directory -Force | Out-Null
New-Item $DistRoot -ItemType Directory -Force | Out-Null

& $Python -m PyInstaller `
    --clean `
    --noconfirm `
    --distpath $BundleRoot `
    --workpath $WorkRoot `
    $SpecFile
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller ist fehlgeschlagen."
}
if (-not (Test-Path (Join-Path $AppBundle "E-Rechnungs-Pruefer.exe"))) {
    throw "Das erwartete PyInstaller-Artefakt wurde nicht erzeugt."
}

function Resolve-SignTool {
    if ($SignTool) {
        return $SignTool
    }
    $command = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $kits = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending
    $firstKit = $kits | Select-Object -First 1
    return $(if ($firstKit) { $firstKit.FullName } else { "" })
}

function Resolve-AzureSignTool {
    $command = Get-Command $AzureSignTool -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "AzureSignTool wurde nicht gefunden: $AzureSignTool"
    }
    return $command.Source
}

function Sign-File([string]$Path) {
    if (-not $SigningEnabled) {
        return
    }

    if ($UseAzureSigning) {
        $ResolvedAzureSignTool = Resolve-AzureSignTool
        & $ResolvedAzureSignTool sign `
            --azure-key-vault-url $AzureKeyVaultUrl `
            --azure-key-vault-certificate $AzureKeyVaultCertificate `
            --azure-key-vault-managed-identity `
            --file-digest sha256 `
            --timestamp-rfc3161 $TimestampUrl `
            --timestamp-digest sha256 `
            --description "E-Rechnungs-Prüfer" `
            --description-url "https://github.com/Harpau/E-Rechnungs-Pruefer" `
            --verbose `
            $Path
        if ($LASTEXITCODE -ne 0) {
            throw "Die Azure-Key-Vault-Signierung ist fehlgeschlagen: $Path"
        }
    } else {
        $ResolvedSignTool = Resolve-SignTool
        if (-not $ResolvedSignTool) {
            throw "SignTool wurde nicht gefunden."
        }
        & $ResolvedSignTool sign /sha1 $CertificateSha1 /fd SHA256 /tr $TimestampUrl /td SHA256 $Path
        if ($LASTEXITCODE -ne 0) {
            throw "Die Signierung ist fehlgeschlagen: $Path"
        }
    }

    $VerificationTool = Resolve-SignTool
    if (-not $VerificationTool) {
        throw "SignTool für die Signaturprüfung wurde nicht gefunden."
    }
    & $VerificationTool verify /pa /all $Path
    if ($LASTEXITCODE -ne 0) {
        throw "Die Signaturprüfung ist fehlgeschlagen: $Path"
    }
}

if ($SigningEnabled) {
    Sign-File (Join-Path $AppBundle "E-Rechnungs-Pruefer.exe")
} else {
    Write-Warning "Keine Signierkonfiguration gesetzt; das Paket wird für Tests unsigniert gebaut."
}

$IsccCandidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 7\ISCC.exe"
)
$IsccCommand = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
if ($IsccCommand) {
    $Iscc = $IsccCommand.Source
} else {
    $Iscc = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc) {
    throw "ISCC.exe wurde nicht gefunden. Bitte Inno Setup 6 oder 7 installieren."
}

& $Iscc `
    "/DAppVersion=$Version" `
    "/DSourceDir=$AppBundle" `
    "/DOutputDir=$DistRoot" `
    "/DProjectRoot=$ProjectRoot" `
    $InstallerFile
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup ist fehlgeschlagen."
}

$Setup = Join-Path $DistRoot "E-Rechnungs-Pruefer-$Version-Windows-x64-Setup.exe"
if (-not (Test-Path $Setup)) {
    throw "Der erwartete Installer wurde nicht erzeugt."
}
Sign-File $Setup

$Digest = (Get-FileHash $Setup -Algorithm SHA256).Hash.ToLowerInvariant()
$ChecksumFile = Join-Path $DistRoot "E-Rechnungs-Pruefer-$Version-Windows-x64-SHA256.txt"
Set-Content $ChecksumFile "$Digest  $(Split-Path -Leaf $Setup)" -Encoding utf8NoBOM

Write-Host "Windows-Paket erzeugt:"
Write-Host "- $Setup"
Write-Host "- $ChecksumFile"
