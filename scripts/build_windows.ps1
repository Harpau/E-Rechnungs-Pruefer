[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$SignTool = "",
    [string]$CertificateSha1 = $env:EINVOICE_SIGN_CERT_SHA1,
    [string]$AzureSignTool = $env:EINVOICE_AZURE_SIGN_TOOL,
    [string]$AzureKeyVaultUrl = $env:EINVOICE_AZURE_KEY_VAULT_URL,
    [string]$AzureKeyVaultCertificate = $env:EINVOICE_AZURE_KEY_VAULT_CERTIFICATE,
    [string]$TimestampUrl = "http://timestamp.acs.microsoft.com",
    [switch]$WithoutOfficialValidation,
    [switch]$BuildElevatedMigrationTestInstaller
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Version = (Get-Content (Join-Path $ProjectRoot "VERSION") -Raw).Trim()
$DesktopSpecFile = Join-Path $ProjectRoot "packaging\windows\e_rechnungs_pruefer.spec"
$ServiceSpecFile = Join-Path $ProjectRoot "packaging\windows\e_rechnungs_pruefer_service.spec"
$OpenClientSpecFile = Join-Path $ProjectRoot "packaging\windows\e_rechnungs_pruefer_open_client.spec"
$DesktopInstallerFile = Join-Path $ProjectRoot "packaging\windows\installer.iss"
$ServiceInstallerFile = Join-Path $ProjectRoot "packaging\windows\service_installer.iss"
$BuildRoot = Join-Path $ProjectRoot "build\windows"
$BundleRoot = Join-Path $BuildRoot "bundle"
$WorkRoot = Join-Path $BuildRoot "pyinstaller"
$DesktopWorkRoot = Join-Path $WorkRoot "desktop"
$ServiceWorkRoot = Join-Path $WorkRoot "service"
$OpenClientWorkRoot = Join-Path $WorkRoot "open-client"
$DesktopBundle = Join-Path $BundleRoot "E-Rechnungs-Pruefer"
$ServiceBundle = Join-Path $BundleRoot "E-Rechnungs-Pruefer-Dienst"
$OpenClient = Join-Path $BundleRoot "E-Rechnungs-Pruefer-Oeffnen.exe"
$DistRoot = Join-Path $ProjectRoot "dist"
$PublishBundleRoot = Join-Path $BuildRoot "publish-bundle"
$TestInstallerRoot = Join-Path $BuildRoot "test-installer"

if (-not $IsWindows) {
    throw "Das Windows-Paket kann nur unter Windows gebaut werden."
}
if (-not [Environment]::Is64BitProcess) {
    throw "Der Build muss mit einem x64-Python-Prozess laufen."
}

$AzureSigningValues = @(
    @($AzureSignTool, $AzureKeyVaultUrl, $AzureKeyVaultCertificate) |
        Where-Object { $_ }
)
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
    --workpath $DesktopWorkRoot `
    $DesktopSpecFile
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller ist für den Desktopmodus fehlgeschlagen."
}

& $Python -m PyInstaller `
    --clean `
    --noconfirm `
    --distpath $BundleRoot `
    --workpath $ServiceWorkRoot `
    $ServiceSpecFile
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller ist für den Dienstmodus fehlgeschlagen."
}

& $Python -m PyInstaller `
    --clean `
    --noconfirm `
    --distpath $BundleRoot `
    --workpath $OpenClientWorkRoot `
    $OpenClientSpecFile
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller ist für den Öffnen-Client fehlgeschlagen."
}

$DesktopExecutable = Join-Path $DesktopBundle "E-Rechnungs-Pruefer.exe"
$ServiceExecutable = Join-Path $ServiceBundle "E-Rechnungs-Pruefer-Dienst.exe"
foreach ($ExpectedExecutable in @($DesktopExecutable, $ServiceExecutable, $OpenClient)) {
    if (-not (Test-Path -LiteralPath $ExpectedExecutable)) {
        throw "Das erwartete PyInstaller-Artefakt wurde nicht erzeugt: $ExpectedExecutable"
    }
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
    & $VerificationTool verify /pa /all /tw $Path
    if ($LASTEXITCODE -ne 0) {
        throw "Die Signaturprüfung ist fehlgeschlagen: $Path"
    }
    $Signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($Signature.Status -ne "Valid" -or $null -eq $Signature.TimeStamperCertificate) {
        throw "Die Authenticode-Signatur oder ihr RFC-3161-Zeitstempel ist ungültig: $Path"
    }
}

function Test-PublishedWindowsArtifacts {
    param(
        [string]$Archive,
        [string]$DesktopInstaller,
        [string]$ServiceInstaller,
        [string]$Manifest,
        [string[]]$ExpectedPaths,
        [string]$VerificationRoot
    )

    if ($ExpectedPaths.Count -ne 6) {
        throw "Die Veröffentlichungsprüfung erwartet genau sechs Artefaktpfade."
    }

    $ExpectedPathSet = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($ExpectedPath in $ExpectedPaths) {
        if (-not $ExpectedPathSet.Add($ExpectedPath)) {
            throw "Die Veröffentlichungsprüfung enthält einen doppelten erwarteten Pfad: $ExpectedPath"
        }
    }

    if (Test-Path -LiteralPath $VerificationRoot) {
        throw "Der temporäre Veröffentlichungsprüfpfad ist nicht frisch: $VerificationRoot"
    }

    New-Item $VerificationRoot -ItemType Directory | Out-Null
    try {
        Expand-Archive -LiteralPath $Archive -DestinationPath $VerificationRoot
        Copy-Item -LiteralPath $DesktopInstaller -Destination $VerificationRoot
        Copy-Item -LiteralPath $ServiceInstaller -Destination $VerificationRoot
        Copy-Item -LiteralPath $Archive -Destination $VerificationRoot

        $ManifestLines = @([System.IO.File]::ReadAllLines($Manifest))
        if ($ManifestLines.Count -ne 6) {
            throw "Das SHA256-Manifest muss genau sechs Zeilen enthalten."
        }

        $VerifiedPathSet = [System.Collections.Generic.HashSet[string]]::new(
            [System.StringComparer]::Ordinal
        )
        $VerificationRootFullPath = [System.IO.Path]::GetFullPath($VerificationRoot)
        $VerificationRootPrefix = $VerificationRootFullPath + [System.IO.Path]::DirectorySeparatorChar
        foreach ($ManifestLine in $ManifestLines) {
            if ($ManifestLine -notmatch '^(?<Digest>[0-9A-Fa-f]{64})  (?<RelativePath>[^\s]+)$') {
                throw "Ungültige Zeile im SHA256-Manifest: $ManifestLine"
            }

            $ExpectedDigest = $Matches.Digest
            $RelativePath = $Matches.RelativePath
            if ([System.IO.Path]::IsPathRooted($RelativePath) -or $RelativePath.Contains('\')) {
                throw "Unzulässiger Pfad im SHA256-Manifest: $RelativePath"
            }

            $PathSegments = @($RelativePath.Split('/'))
            if ($PathSegments.Count -eq 0 -or @($PathSegments | Where-Object { $_ -in @('', '.', '..') }).Count -gt 0) {
                throw "Traversaler oder leerer Pfad im SHA256-Manifest: $RelativePath"
            }
            if (-not $ExpectedPathSet.Contains($RelativePath)) {
                throw "Unbekannter Pfad im SHA256-Manifest: $RelativePath"
            }
            if (-not $VerifiedPathSet.Add($RelativePath)) {
                throw "Doppelter Pfad im SHA256-Manifest: $RelativePath"
            }

            $PlatformRelativePath = [string]::Join(
                [string][System.IO.Path]::DirectorySeparatorChar,
                [string[]]$PathSegments
            )
            $ArtifactPath = [System.IO.Path]::GetFullPath(
                (Join-Path $VerificationRootFullPath $PlatformRelativePath)
            )
            if (-not $ArtifactPath.StartsWith(
                    $VerificationRootPrefix,
                    [System.StringComparison]::OrdinalIgnoreCase
                )) {
                throw "Der Manifestpfad verlässt den Veröffentlichungsprüfpfad: $RelativePath"
            }
            if (-not (Test-Path -LiteralPath $ArtifactPath -PathType Leaf)) {
                throw "Das im SHA256-Manifest genannte Artefakt fehlt: $RelativePath"
            }

            $ActualDigest = (Get-FileHash -LiteralPath $ArtifactPath -Algorithm SHA256).Hash
            if (-not [string]::Equals(
                    $ActualDigest,
                    $ExpectedDigest,
                    [System.StringComparison]::OrdinalIgnoreCase
                )) {
                throw "SHA256-Prüfung fehlgeschlagen: $RelativePath"
            }
        }

        foreach ($ExpectedPath in $ExpectedPathSet) {
            if (-not $VerifiedPathSet.Contains($ExpectedPath)) {
                throw "Erwarteter Pfad fehlt im SHA256-Manifest: $ExpectedPath"
            }
        }
    } finally {
        Remove-Item -LiteralPath $VerificationRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($SigningEnabled) {
    Sign-File (Join-Path $DesktopBundle "E-Rechnungs-Pruefer.exe")
    Sign-File (Join-Path $ServiceBundle "E-Rechnungs-Pruefer-Dienst.exe")
    Sign-File $OpenClient
} else {
    Write-Warning "Keine Signierkonfiguration gesetzt; die Pakete werden für Tests unsigniert gebaut."
}

$IsccCandidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 7\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe"
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
    "/DSourceDir=$DesktopBundle" `
    "/DOutputDir=$DistRoot" `
    "/DProjectRoot=$ProjectRoot" `
    $DesktopInstallerFile
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup ist für den Desktopinstaller fehlgeschlagen."
}

& $Iscc `
    "/DAppVersion=$Version" `
    "/DServiceSourceDir=$ServiceBundle" `
    "/DOpenClientFile=$OpenClient" `
    "/DOutputDir=$DistRoot" `
    "/DProjectRoot=$ProjectRoot" `
    $ServiceInstallerFile
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup ist für den Dienstinstaller fehlgeschlagen."
}

if ($BuildElevatedMigrationTestInstaller) {
    New-Item $TestInstallerRoot -ItemType Directory -Force | Out-Null
    & $Iscc `
        "/DAppVersion=$Version" `
        "/DServiceSourceDir=$ServiceBundle" `
        "/DOpenClientFile=$OpenClient" `
        "/DOutputDir=$TestInstallerRoot" `
        "/DProjectRoot=$ProjectRoot" `
        "/DAllowElevatedMigrationTestContext=1" `
        $ServiceInstallerFile
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup ist für den ausschließlich internen Dienst-Testinstaller fehlgeschlagen."
    }
}

$DesktopSetup = Join-Path $DistRoot "E-Rechnungs-Pruefer-$Version-Windows-x64-Setup.exe"
$ServiceSetup = Join-Path $DistRoot "E-Rechnungs-Pruefer-$Version-Windows-x64-Dienst-Setup.exe"
foreach ($ExpectedSetup in @($DesktopSetup, $ServiceSetup)) {
    if (-not (Test-Path -LiteralPath $ExpectedSetup)) {
        throw "Der erwartete Installer wurde nicht erzeugt: $ExpectedSetup"
    }
}
Sign-File $DesktopSetup
Sign-File $ServiceSetup
if ($BuildElevatedMigrationTestInstaller) {
    $ElevatedMigrationTestSetup = Join-Path $TestInstallerRoot "E-Rechnungs-Pruefer-$Version-Windows-x64-Dienst-Setup.exe"
    if (-not (Test-Path -LiteralPath $ElevatedMigrationTestSetup)) {
        throw "Der ausschließlich interne Dienst-Testinstaller wurde nicht erzeugt: $ElevatedMigrationTestSetup"
    }
    Sign-File $ElevatedMigrationTestSetup
}

$PublishedBundleDirectory = Join-Path $PublishBundleRoot "bundle"
$PublishedDesktopDirectory = Join-Path $PublishedBundleDirectory "desktop"
$PublishedServiceDirectory = Join-Path $PublishedBundleDirectory "service"
New-Item $PublishedDesktopDirectory -ItemType Directory -Force | Out-Null
New-Item $PublishedServiceDirectory -ItemType Directory -Force | Out-Null
Copy-Item (Join-Path $DesktopBundle "*") $PublishedDesktopDirectory -Recurse -Force
Copy-Item (Join-Path $ServiceBundle "*") $PublishedServiceDirectory -Recurse -Force
Copy-Item $OpenClient $PublishedBundleDirectory -Force
$BundleArchive = Join-Path $DistRoot "E-Rechnungs-Pruefer-$Version-Windows-x64-Binaries.zip"
Remove-Item $BundleArchive -Force -ErrorAction SilentlyContinue
Compress-Archive -Path $PublishedBundleDirectory -DestinationPath $BundleArchive -CompressionLevel Optimal
if (-not (Test-Path -LiteralPath $BundleArchive)) {
    throw "Das veröffentlichbare Binärbundle wurde nicht erzeugt: $BundleArchive"
}

$OwnedFiles = @(
    [PSCustomObject]@{ Path = $DesktopExecutable; Name = "bundle/desktop/E-Rechnungs-Pruefer.exe" },
    [PSCustomObject]@{ Path = $ServiceExecutable; Name = "bundle/service/E-Rechnungs-Pruefer-Dienst.exe" },
    [PSCustomObject]@{ Path = $OpenClient; Name = "bundle/E-Rechnungs-Pruefer-Oeffnen.exe" },
    [PSCustomObject]@{ Path = $DesktopSetup; Name = (Split-Path -Leaf $DesktopSetup) },
    [PSCustomObject]@{ Path = $ServiceSetup; Name = (Split-Path -Leaf $ServiceSetup) },
    [PSCustomObject]@{ Path = $BundleArchive; Name = (Split-Path -Leaf $BundleArchive) }
)
$ChecksumLines = foreach ($OwnedFile in $OwnedFiles) {
    $Digest = (Get-FileHash $OwnedFile.Path -Algorithm SHA256).Hash.ToLowerInvariant()
    "$Digest  $($OwnedFile.Name)"
}
$ChecksumFile = Join-Path $DistRoot "E-Rechnungs-Pruefer-$Version-Windows-x64-SHA256SUMS.txt"
Set-Content $ChecksumFile $ChecksumLines -Encoding utf8NoBOM

$ExpectedPublishedPaths = @(
    "bundle/desktop/E-Rechnungs-Pruefer.exe",
    "bundle/service/E-Rechnungs-Pruefer-Dienst.exe",
    "bundle/E-Rechnungs-Pruefer-Oeffnen.exe",
    "E-Rechnungs-Pruefer-$Version-Windows-x64-Setup.exe",
    "E-Rechnungs-Pruefer-$Version-Windows-x64-Dienst-Setup.exe",
    "E-Rechnungs-Pruefer-$Version-Windows-x64-Binaries.zip"
)
$VerificationRoot = Join-Path $BuildRoot "publish-verification-$([guid]::NewGuid().ToString('N'))"
Test-PublishedWindowsArtifacts `
    -Archive $BundleArchive `
    -DesktopInstaller $DesktopSetup `
    -ServiceInstaller $ServiceSetup `
    -Manifest $ChecksumFile `
    -ExpectedPaths $ExpectedPublishedPaths `
    -VerificationRoot $VerificationRoot

Write-Host "Windows-Pakete erzeugt:"
Write-Host "- $DesktopSetup"
Write-Host "- $ServiceSetup"
Write-Host "- $BundleArchive"
Write-Host "- $ChecksumFile"
if ($BuildElevatedMigrationTestInstaller) {
    Write-Host "Interner, nicht veröffentlichbarer VM-Testinstaller:"
    Write-Host "- $ElevatedMigrationTestSetup"
}
