[CmdletBinding()]
param(
    [string]$DesktopSetup = "",
    [string]$ServiceSetup = "",
    [switch]$RequireSignature,
    [switch]$ConfirmIsolatedEnvironment
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Wait-SetupUninstallMutexReleased {
    param(
        [string]$Name = "Global\E-Rechnungs-Pruefer-Setup-Uninstall",
        [ValidateRange(1, 600)]
        [int]$Seconds = 60
    )
    $Mutex = $null
    try {
        try {
            $Mutex = [Threading.Mutex]::OpenExisting($Name)
        } catch [Threading.WaitHandleCannotBeOpenedException] {
            return
        }

        $Owned = $false
        $Abandoned = $false
        try {
            try {
                $Owned = $Mutex.WaitOne($Seconds * 1000)
            } catch [Threading.AbandonedMutexException] {
                $Owned = $true
                $Abandoned = $true
            }
            if (-not $Owned) {
                throw "Die systemweite Installations-/Deinstallationssperre wurde nicht rechtzeitig freigegeben."
            }
            if ($Abandoned) {
                throw "Der Installer oder Uninstaller wurde während seiner zweiten Phase unerwartet beendet."
            }
        } finally {
            if ($Owned) {
                $Mutex.ReleaseMutex()
            }
        }
    } finally {
        if ($null -ne $Mutex) {
            $Mutex.Dispose()
        }
    }
}

function Get-DiagnosticPathMasks {
    $RepositoryRoot = Split-Path -Parent $PSScriptRoot
    $Candidates = @(
        [PSCustomObject]@{ Path = $RepositoryRoot; Mask = "<REPOSITORY>" }
        [PSCustomObject]@{
            Path = [Environment]::GetEnvironmentVariable("LOCALAPPDATA")
            Mask = "<LOCALAPPDATA>"
        }
        [PSCustomObject]@{
            Path = [Environment]::GetEnvironmentVariable("RUNNER_TEMP")
            Mask = "<TEMP>"
        }
        [PSCustomObject]@{
            Path = [Environment]::GetEnvironmentVariable("TEMP")
            Mask = "<TEMP>"
        }
        [PSCustomObject]@{
            Path = [Environment]::GetEnvironmentVariable("TMP")
            Mask = "<TEMP>"
        }
        [PSCustomObject]@{ Path = [IO.Path]::GetTempPath(); Mask = "<TEMP>" }
        [PSCustomObject]@{
            Path = [Environment]::GetEnvironmentVariable("USERPROFILE")
            Mask = "<USERPROFILE>"
        }
    )
    return @(
        $Candidates |
            Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_.Path) } |
            Sort-Object @{ Expression = { ([string]$_.Path).Length }; Descending = $true },
                @{ Expression = { [string]$_.Mask }; Descending = $false }
    )
}

function Protect-DiagnosticText {
    param(
        [Parameter(Mandatory = $false)]
        [AllowEmptyString()]
        [string]$Text
    )
    if ($Text -match "(?i)token") {
        return "<TOKEN-RELATED-LINE-REDACTED>"
    }
    $Protected = $Text
    foreach ($PathMask in Get-DiagnosticPathMasks) {
        $Protected = [regex]::Replace(
            $Protected,
            [regex]::Escape([string]$PathMask.Path),
            [string]$PathMask.Mask,
            [Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
    }
    $Protected = [regex]::Replace(
        $Protected,
        "(?i)\bS-\d+(?:-\d+){2,}\b",
        "<SID>"
    )
    if ($Protected -match "(?i)(?:[a-z]:[\\/]|\\\\)") {
        return "<PATH-RELATED-LINE-REDACTED>"
    }
    $Protected = [regex]::Replace(
        $Protected,
        "(?i)\b(?:[0-9a-f]{32,}|[a-z0-9_-]{40,})\b",
        "<SECRET>"
    )
    if ($Protected.Length -gt 500) {
        return $Protected.Substring(0, 500) + "<TRUNCATED>"
    }
    return $Protected
}

function Get-SanitizedInnoLogTail {
    param(
        [AllowEmptyString()]
        [string]$LogPath,
        [ValidateRange(1, 80)]
        [int]$MaximumLines = 60
    )
    if ([string]::IsNullOrWhiteSpace($LogPath)) {
        return "Kein Inno-Setup-Logpfad wurde übergeben."
    }
    try {
        if (-not (Test-Path -LiteralPath $LogPath -PathType Leaf -ErrorAction Stop)) {
            return "Kein Inno-Setup-Log vorhanden."
        }
        $Lines = @(Get-Content -LiteralPath $LogPath -Tail $MaximumLines -ErrorAction Stop)
    } catch {
        return "Der begrenzte Inno-Setup-Logauszug konnte nicht sicher gelesen werden."
    }
    if ($Lines.Count -eq 0) {
        return "Das Inno-Setup-Log ist leer."
    }
    return (@($Lines | ForEach-Object { Protect-DiagnosticText -Text ([string]$_) }) -join "`n")
}

function Get-InnoLogPath {
    param([string[]]$Arguments)
    foreach ($Argument in $Arguments) {
        if ($Argument.StartsWith("/LOG=", [StringComparison]::OrdinalIgnoreCase)) {
            return $Argument.Substring(5).Trim().Trim('"')
        }
    }
    return ""
}

function Invoke-Setup {
    param(
        [string]$Path,
        [string[]]$Arguments,
        [string]$Scenario
    )
    $Process = Start-Process $Path -ArgumentList $Arguments -PassThru
    if (-not $Process.WaitForExit(600000)) {
        try {
            $Process.Kill($true)
            $Process.WaitForExit()
        } catch {
            Write-Warning "Der hängende Setup-Prozess konnte nicht beendet werden: $_"
        }
        throw "$Scenario überschritt das Zeitlimit."
    }
    $ExitCode = [int]$Process.ExitCode
    Wait-SetupUninstallMutexReleased
    if ($ExitCode -ne 0) {
        $LogTail = Get-SanitizedInnoLogTail -LogPath (Get-InnoLogPath -Arguments $Arguments)
        throw "$Scenario schlug mit Exitcode $ExitCode fehl.`n" +
            "Begrenzter, maskierter Inno-Logauszug:`n$LogTail"
    }
}

function Invoke-SetupExpectedFailure {
    param(
        [string]$Path,
        [string[]]$Arguments,
        [string]$LogPath,
        [string]$Scenario
    )
    $Process = Start-Process $Path -ArgumentList $Arguments -PassThru
    if (-not $Process.WaitForExit(600000)) {
        try {
            $Process.Kill($true)
            $Process.WaitForExit()
        } catch {
            Write-Warning "Der hängende Setup-Prozess konnte nicht beendet werden: $_"
        }
        throw "$Scenario überschritt das Zeitlimit."
    }
    $ExitCode = [int]$Process.ExitCode
    Wait-SetupUninstallMutexReleased
    if ($ExitCode -eq 0) {
        $LogTail = Get-SanitizedInnoLogTail -LogPath $LogPath
        throw "$Scenario wurde unerwartet akzeptiert.`n$LogTail"
    }
    if (-not (Test-Path -LiteralPath $LogPath -PathType Leaf)) {
        throw "$Scenario erzeugte trotz Exitcode $ExitCode keinen Setup-Log."
    }
}

function Resolve-DiagnosticProfilePath {
    param(
        [Parameter(Mandatory = $false)]
        [AllowNull()]
        [object]$Value,
        [Microsoft.Win32.RegistryValueKind]$Kind
    )
    if ($Kind -notin @(
        [Microsoft.Win32.RegistryValueKind]::String,
        [Microsoft.Win32.RegistryValueKind]::ExpandString
    ) -or
        $Value -isnot [string] -or
        [string]::IsNullOrWhiteSpace([string]$Value) -or
        ([string]$Value).Contains([char]0)) {
        return $null
    }
    try {
        $Expanded = [Environment]::ExpandEnvironmentVariables([string]$Value)
        $FullPath = [IO.Path]::GetFullPath($Expanded)
        $Root = [IO.Path]::GetPathRoot($FullPath)
        if ($Root -notmatch "^[a-zA-Z]:\\$") {
            return $null
        }
        if (Test-Path -LiteralPath $FullPath -ErrorAction Stop) {
            $Item = Get-Item -LiteralPath $FullPath -Force -ErrorAction Stop
            if (-not $Item.PSIsContainer -or
                ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                return $null
            }
        }
        return $FullPath
    } catch {
        return $null
    }
}

function Get-DiagnosticHiveFileState {
    param([string]$Path)
    try {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf -ErrorAction Stop)) {
            return "missing"
        }
        $Item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if ($Item -isnot [IO.FileInfo] -or
            ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            return "unsafe"
        }
        return "present"
    } catch {
        return "unsafe"
    }
}

function Write-ProfileHiveCategoryDiagnostic {
    $Counts = [ordered]@{
        loaded = 0
        "offline DAT" = 0
        "offline MAN" = 0
        missing = 0
        ambiguous = 0
        unsafe = 0
    }
    $MachineRoot = $null
    $UsersRoot = $null
    $ProfileList = $null
    try {
        $MachineRoot = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
            [Microsoft.Win32.RegistryHive]::LocalMachine,
            [Microsoft.Win32.RegistryView]::Registry64
        )
        $UsersRoot = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
            [Microsoft.Win32.RegistryHive]::Users,
            [Microsoft.Win32.RegistryView]::Registry64
        )
        $ProfileList = $MachineRoot.OpenSubKey(
            "SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList",
            $false
        )
        if ($null -eq $ProfileList) {
            $Counts.unsafe++
        } else {
            foreach ($ProfileKeyName in $ProfileList.GetSubKeyNames()) {
                if ($ProfileKeyName -in @("S-1-5-18", "S-1-5-19", "S-1-5-20")) {
                    continue
                }
                $Category = "unsafe"
                $ProfileKey = $null
                $LoadedHive = $null
                try {
                    $ProfileKey = $ProfileList.OpenSubKey($ProfileKeyName, $false)
                    if ($null -ne $ProfileKey) {
                        $Kind = $ProfileKey.GetValueKind("ProfileImagePath")
                        $Value = $ProfileKey.GetValue(
                            "ProfileImagePath",
                            $null,
                            [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
                        )
                        $ProfilePath = Resolve-DiagnosticProfilePath -Value $Value -Kind $Kind
                        if ($null -ne $ProfilePath) {
                            $LoadedHive = $UsersRoot.OpenSubKey($ProfileKeyName, $false)
                            if ($null -ne $LoadedHive) {
                                $Category = "loaded"
                            } else {
                                $DatState = Get-DiagnosticHiveFileState -Path (
                                    Join-Path $ProfilePath "NTUSER.DAT"
                                )
                                $ManState = Get-DiagnosticHiveFileState -Path (
                                    Join-Path $ProfilePath "NTUSER.MAN"
                                )
                                if ($DatState -eq "unsafe" -or $ManState -eq "unsafe") {
                                    $Category = "unsafe"
                                } elseif ($DatState -eq "present" -and $ManState -eq "present") {
                                    $Category = "ambiguous"
                                } elseif ($DatState -eq "present") {
                                    $Category = "offline DAT"
                                } elseif ($ManState -eq "present") {
                                    $Category = "offline MAN"
                                } else {
                                    $Category = "missing"
                                }
                            }
                        }
                    }
                } catch {
                    $Category = "unsafe"
                } finally {
                    if ($null -ne $LoadedHive) {
                        $LoadedHive.Dispose()
                    }
                    if ($null -ne $ProfileKey) {
                        $ProfileKey.Dispose()
                    }
                }
                $Counts[$Category]++
            }
        }
    } catch {
        $Counts.unsafe++
    } finally {
        if ($null -ne $ProfileList) {
            $ProfileList.Dispose()
        }
        if ($null -ne $UsersRoot) {
            $UsersRoot.Dispose()
        }
        if ($null -ne $MachineRoot) {
            $MachineRoot.Dispose()
        }
    }
    Write-Host (
        "ProfileList/Hive-Diagnose (nur Kategorien): " +
        "loaded=$($Counts.loaded); offline DAT=$($Counts['offline DAT']); " +
        "offline MAN=$($Counts['offline MAN']); missing=$($Counts.missing); " +
        "ambiguous=$($Counts.ambiguous); unsafe=$($Counts.unsafe)"
    )
}

function Write-LoopbackPortCategoryDiagnostic {
    $Counts = [ordered]@{
        listen = 0
        timeWait = 0
        closeWait = 0
        other = 0
        inventoryError = 0
    }
    try {
        $Connections = @(
            Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort 8765 `
                -ErrorAction SilentlyContinue
        )
        foreach ($Connection in $Connections) {
            switch ([string]$Connection.State) {
                "Listen" { $Counts.listen++; break }
                "TimeWait" { $Counts.timeWait++; break }
                "CloseWait" { $Counts.closeWait++; break }
                default { $Counts.other++ }
            }
        }
    } catch {
        $Counts.inventoryError++
    }
    Write-Host (
        "TCP-Portdiagnose (nur Zähler): listen=$($Counts.listen); " +
        "timeWait=$($Counts.timeWait); closeWait=$($Counts.closeWait); " +
        "other=$($Counts.other); inventoryError=$($Counts.inventoryError)"
    )
}

function Wait-ServiceState {
    param(
        [string]$Name,
        [string]$State,
        [ValidateRange(1, 600)]
        [int]$Seconds = 330
    )
    $Deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        $Candidate = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($null -eq $Candidate) {
            if ($State -eq "Absent") {
                return
            }
        } else {
            $Candidate.Refresh()
            if ([string]::Equals(
                [string]$Candidate.Status,
                $State,
                [StringComparison]::OrdinalIgnoreCase
            )) {
                return
            }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $Deadline)
    throw "Dienst $Name erreichte Zustand $State nicht innerhalb des Zeitlimits."
}

function Assert-ValidSignature {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Installer fehlt: $Path"
    }
    if ($RequireSignature) {
        $Signature = Get-AuthenticodeSignature $Path
        if ($Signature.Status -ne "Valid" -or $null -eq $Signature.TimeStamperCertificate) {
            throw "Installer besitzt keine gültige Authenticode-Signatur mit Zeitstempel: $Path"
        }
    }
}

function Get-OptionalRegistryValue {
    param(
        [string]$Path,
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
            Where-Object {
                [string]::Equals($_, $Name, [StringComparison]::OrdinalIgnoreCase)
            } |
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

function Assert-RegistryValueUnchanged {
    param(
        $Before,
        $After,
        [string]$Description
    )
    if ($Before.Exists -ne $After.Exists -or
        $Before.Kind -ne $After.Kind -or
        -not [object]::Equals($Before.Value, $After.Value)) {
        throw "$Description wurde durch den abgewiesenen Installer verändert."
    }
}

function Get-TreeFingerprint {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Fingerprint-Ziel fehlt: $Path"
    }
    $Root = (Get-Item -LiteralPath $Path -Force).FullName
    $Entries = @(
        Get-Item -LiteralPath $Root -Force
        Get-ChildItem -LiteralPath $Root -Recurse -Force
    )
    $SecuritySections =
        [Security.AccessControl.AccessControlSections]::Owner -bor
        [Security.AccessControl.AccessControlSections]::Access
    $Lines = [Collections.Generic.List[string]]::new()
    foreach ($Entry in $Entries) {
        if (($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Fingerprint-Ziel enthält einen unerwarteten Reparse-Point: $($Entry.FullName)"
        }
        $Relative = if ([string]::Equals(
            $Entry.FullName,
            $Root,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            "."
        } else {
            [IO.Path]::GetRelativePath($Root, $Entry.FullName).Replace('\', '/')
        }
        $EncodedRelative = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Relative))
        $Acl = Get-Acl -LiteralPath $Entry.FullName
        $SecurityDescriptor = $Acl.GetSecurityDescriptorSddlForm($SecuritySections)
        $Attributes = ([int64]$Entry.Attributes).ToString(
            [Globalization.CultureInfo]::InvariantCulture
        )
        if ($Entry.PSIsContainer) {
            $Kind = "D"
            $Length = "-"
            $ContentHash = "-"
        } elseif ($Entry -is [IO.FileInfo]) {
            $Kind = "F"
            $Length = ([int64]$Entry.Length).ToString(
                [Globalization.CultureInfo]::InvariantCulture
            )
            $ContentHash = (Get-FileHash -LiteralPath $Entry.FullName -Algorithm SHA256).Hash
        } else {
            throw "Fingerprint-Ziel enthält einen unbekannten Dateisystemeintrag: $($Entry.FullName)"
        }
        $Lines.Add(
            "$EncodedRelative|$Kind|$Attributes|$Length|$SecurityDescriptor|$ContentHash"
        )
    }
    $Lines.Sort([StringComparer]::Ordinal)
    $Bytes = [Text.Encoding]::UTF8.GetBytes(($Lines -join "`n"))
    $Sha = [Security.Cryptography.SHA256]::Create()
    try {
        return [Convert]::ToHexString($Sha.ComputeHash($Bytes))
    } finally {
        $Sha.Dispose()
    }
}

function Convert-RegistryValueToStableText {
    param(
        [Parameter(Mandatory = $false)]
        [AllowNull()]
        [object]$Value
    )
    if ($null -eq $Value) {
        return "<null>"
    }
    $TypeName = $Value.GetType().FullName
    if ($Value -is [byte[]]) {
        return "${TypeName}:$([Convert]::ToHexString([byte[]]$Value))"
    }
    if ($Value -is [string[]]) {
        $Encoded = @(
            [string[]]$Value | ForEach-Object {
                [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($_))
            }
        )
        return "${TypeName}:$($Encoded -join ',')"
    }
    if ($Value -is [IFormattable]) {
        return "${TypeName}:$($Value.ToString($null, [Globalization.CultureInfo]::InvariantCulture))"
    }
    $EncodedValue = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes([string]$Value)
    )
    return "${TypeName}:$EncodedValue"
}

function Get-RegistryValueFingerprint {
    param(
        [string]$Path,
        [string]$Name
    )
    $State = Get-OptionalRegistryValue -Path $Path -Name $Name
    if (-not $State.Exists) {
        return "missing"
    }
    $Kind = ([int]$State.Kind).ToString([Globalization.CultureInfo]::InvariantCulture)
    $Value = Convert-RegistryValueToStableText -Value $State.Value
    return "present|$Kind|$Value"
}

function Get-ServiceSnapshot {
    param(
        [string]$Name,
        [string]$RegistryPath
    )
    $Services = @(Get-CimInstance Win32_Service -Filter "Name='$Name'" -ErrorAction Stop)
    if ($Services.Count -ne 1) {
        throw "Dienstzustand konnte nicht eindeutig gelesen werden: $Name"
    }
    $Service = $Services[0]
    return [PSCustomObject]@{
        Name = [string]$Service.Name
        DisplayName = [string]$Service.DisplayName
        Description = [string]$Service.Description
        PathName = [string]$Service.PathName
        StartMode = [string]$Service.StartMode
        StartName = [string]$Service.StartName
        State = [string]$Service.State
        RegistryImagePath = Get-RegistryValueFingerprint -Path $RegistryPath -Name "ImagePath"
        RegistryDisplayName = Get-RegistryValueFingerprint -Path $RegistryPath -Name "DisplayName"
        RegistryDescription = Get-RegistryValueFingerprint -Path $RegistryPath -Name "Description"
        RegistryObjectName = Get-RegistryValueFingerprint -Path $RegistryPath -Name "ObjectName"
        RegistryStart = Get-RegistryValueFingerprint -Path $RegistryPath -Name "Start"
        RegistryType = Get-RegistryValueFingerprint -Path $RegistryPath -Name "Type"
        RegistryErrorControl = Get-RegistryValueFingerprint -Path $RegistryPath -Name "ErrorControl"
        DelayedAutoStart = Get-RegistryValueFingerprint -Path $RegistryPath -Name "DelayedAutoStart"
        ServiceSidType = Get-RegistryValueFingerprint -Path $RegistryPath -Name "ServiceSidType"
        FailureActions = Get-RegistryValueFingerprint -Path $RegistryPath -Name "FailureActions"
        FailureActionsOnNonCrashFailures = Get-RegistryValueFingerprint `
            -Path $RegistryPath -Name "FailureActionsOnNonCrashFailures"
    }
}

function Assert-ServiceSnapshotUnchanged {
    param(
        $Before,
        $After
    )
    foreach ($Property in @(
        "Name",
        "DisplayName",
        "Description",
        "PathName",
        "StartMode",
        "StartName",
        "State",
        "RegistryImagePath",
        "RegistryDisplayName",
        "RegistryDescription",
        "RegistryObjectName",
        "RegistryStart",
        "RegistryType",
        "RegistryErrorControl",
        "DelayedAutoStart",
        "ServiceSidType",
        "FailureActions",
        "FailureActionsOnNonCrashFailures"
    )) {
        if (-not [string]::Equals(
            [string]$Before.$Property,
            [string]$After.$Property,
            [StringComparison]::Ordinal
        )) {
            throw "Der Desktop-Installer veränderte die Diensteigenschaft $Property."
        }
    }
}

if (-not $IsWindows) {
    throw "Der Windows-Modusausschlusstest kann nur unter Windows laufen."
}
if (-not $ConfirmIsolatedEnvironment) {
    throw "Der Modusausschlusstest darf nur auf einer sauberen Wegwerf-VM mit -ConfirmIsolatedEnvironment laufen."
}
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Der Modusausschlusstest benötigt eine administrative Testidentität."
}
if (-not [Environment]::Is64BitProcess) {
    throw "Der Modusausschlusstest benötigt einen 64-Bit-PowerShell-Prozess."
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Version = (Get-Content (Join-Path $ProjectRoot "VERSION") -Raw).Trim()
if (-not $DesktopSetup) {
    $DesktopSetup = Join-Path $ProjectRoot "dist\E-Rechnungs-Pruefer-$Version-Windows-x64-Setup.exe"
}
if (-not $ServiceSetup) {
    $ServiceSetup = Join-Path (Join-Path $ProjectRoot "dist") (
        "E-Rechnungs-Pruefer-$Version-Windows-x64-Dienst-Setup.exe"
    )
}
Assert-ValidSignature $DesktopSetup
Assert-ValidSignature $ServiceSetup

$ServiceName = "ERechnungsPrueferService"
$ServiceRegistryPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"
$DesktopDir = Join-Path $env:LOCALAPPDATA "Programs\E-Rechnungs-Pruefer"
$DesktopExe = Join-Path $DesktopDir "E-Rechnungs-Pruefer.exe"
$DesktopUninstaller = Join-Path $DesktopDir "unins000.exe"
$DesktopData = Join-Path $env:LOCALAPPDATA "E-Rechnungs-Pruefer"
$DesktopToken = Join-Path $DesktopData "api-token.txt"
$DesktopRuntime = Join-Path $DesktopData "runtime.json"
$DesktopUninstallKey = (
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\" +
    "{D33FD9E5-0C5E-48ED-BF0C-E9D2962A45DF}_is1"
)
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunName = "E-Rechnungs-Pruefer"
$ExpectedAutostart = "`"$DesktopExe`" --background"
$ServiceDir = Join-Path $env:ProgramFiles "E-Rechnungs-Pruefer-Dienst"
$ServiceUninstaller = Join-Path $ServiceDir "unins000.exe"
$ServiceData = Join-Path $env:ProgramData "E-Rechnungs-Pruefer"
$ServiceToken = Join-Path $ServiceData "api-token.txt"
$ServiceUninstallKey = (
    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\" +
    "{8824D15C-7F4E-4CB2-B957-FBC26B923363}_is1"
)
$UnsupportedLegacyState = Join-Path $env:ProgramData "E-Rechnungs-Pruefer-Installer-State"
$UnsupportedLegacyTransfer = Join-Path $env:ProgramData "E-Rechnungs-Pruefer-Installer-Transfer"

$Conflicts = [Collections.Generic.List[string]]::new()
foreach ($Path in @(
    $DesktopDir,
    $DesktopData,
    $DesktopUninstallKey,
    $ServiceDir,
    $ServiceData,
    $ServiceUninstallKey,
    $UnsupportedLegacyState,
    $UnsupportedLegacyTransfer
)) {
    if (Test-Path -LiteralPath $Path) {
        $Conflicts.Add($Path)
    }
}
if ((Get-OptionalRegistryValue -Path $RunKey -Name $RunName).Exists) {
    $Conflicts.Add("$RunKey\$RunName")
}
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    $Conflicts.Add("Dienst $ServiceName")
}
if (@(Get-Process -Name "E-Rechnungs-Pruefer" -ErrorAction SilentlyContinue).Count -gt 0) {
    $Conflicts.Add("laufender Desktopprozess E-Rechnungs-Pruefer.exe")
}
if ($Conflicts.Count -gt 0) {
    throw "Vorhandener Produkt- oder nicht unterstützter v1.4.0-Altzustand; Abbruch ohne Änderung:`n" +
        ($Conflicts -join "`n")
}

$TemporaryRoot = if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
    [IO.Path]::GetTempPath()
} else {
    $env:RUNNER_TEMP
}
$TestRoot = Join-Path $TemporaryRoot (
    "e-rechnungs-pruefer-mode-exclusion-$([Guid]::NewGuid().ToString('N'))"
)
New-Item -Path $TestRoot -ItemType Directory | Out-Null
$DesktopInstallLog = Join-Path $TestRoot "desktop-install.log"
$ServiceBlockedLog = Join-Path $TestRoot "service-blocked-by-desktop.log"
$DesktopUninstallLog = Join-Path $TestRoot "desktop-uninstall.log"
$ServiceInstallLog = Join-Path $TestRoot "service-install.log"
$DesktopBlockedLog = Join-Path $TestRoot "desktop-blocked-by-service.log"
$ServicePreserveUninstallLog = Join-Path $TestRoot "service-uninstall-preserve.log"
$DesktopPreservedDataInstallLog = Join-Path $TestRoot "desktop-install-with-preserved-service-data.log"
$DesktopPreservedDataUninstallLog = Join-Path $TestRoot "desktop-uninstall-with-preserved-service-data.log"
$ServiceReinstallLog = Join-Path $TestRoot "service-reinstall-with-preserved-data.log"
$ServicePurgeUninstallLog = Join-Path $TestRoot "service-uninstall-purge.log"
$DesktopProcess = $null

try {
    Invoke-Setup -Path $DesktopSetup -Scenario "Desktopinstallation" -Arguments @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/TASKS=`"autostart`"",
        "/LOG=`"$DesktopInstallLog`""
    )
    if (-not (Test-Path -LiteralPath $DesktopExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $DesktopUninstaller -PathType Leaf)) {
        throw "Der Desktopmodus wurde nicht vollständig installiert."
    }
    $AutostartBefore = Get-OptionalRegistryValue -Path $RunKey -Name $RunName
    if (-not $AutostartBefore.Exists -or
        -not [string]::Equals(
            [string]$AutostartBefore.Value,
            $ExpectedAutostart,
            [StringComparison]::Ordinal
        )) {
        throw "Der Desktop-Autostart wurde nicht wie erwartet eingerichtet."
    }

    $HadNoDialog = Test-Path Env:EINVOICE_DESKTOP_NO_DIALOG
    $PreviousNoDialog = $env:EINVOICE_DESKTOP_NO_DIALOG
    try {
        $env:EINVOICE_DESKTOP_NO_DIALOG = "1"
        $DesktopProcess = Start-Process $DesktopExe -ArgumentList "--background" -PassThru
    } finally {
        if ($HadNoDialog) {
            $env:EINVOICE_DESKTOP_NO_DIALOG = $PreviousNoDialog
        } else {
            Remove-Item Env:EINVOICE_DESKTOP_NO_DIALOG -ErrorAction SilentlyContinue
        }
    }
    $DesktopDeadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
    } while (
        (-not (Test-Path -LiteralPath $DesktopToken -PathType Leaf) -or
            -not (Test-Path -LiteralPath $DesktopRuntime -PathType Leaf)) -and
        -not $DesktopProcess.HasExited -and
        [DateTime]::UtcNow -lt $DesktopDeadline
    )
    if ($DesktopProcess.HasExited -or
        -not (Test-Path -LiteralPath $DesktopToken -PathType Leaf)) {
        throw "Der Desktopmodus wurde für den Ausschlusstest nicht betriebsbereit."
    }

    $DesktopExeHashBefore = (Get-FileHash -LiteralPath $DesktopExe -Algorithm SHA256).Hash
    $DesktopTokenHashBefore = (Get-FileHash -LiteralPath $DesktopToken -Algorithm SHA256).Hash
    $DesktopTreeBefore = Get-TreeFingerprint -Path $DesktopDir
    $DesktopDataBefore = Get-TreeFingerprint -Path $DesktopData
    $DesktopProcessIdBefore = [int]$DesktopProcess.Id
    $BlockedServiceArguments = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/TASKS=`"systemstart`"",
        "/LOG=`"$ServiceBlockedLog`""
    )
    Invoke-SetupExpectedFailure -Path $ServiceSetup -LogPath $ServiceBlockedLog `
        -Scenario "Dienstinstallation bei vorhandenem Desktopmodus" `
        -Arguments $BlockedServiceArguments

    $DesktopProcess.Refresh()
    if ($DesktopProcess.HasExited -or $DesktopProcess.Id -ne $DesktopProcessIdBefore) {
        throw "Der abgewiesene Dienst-Installer beendete oder ersetzte den laufenden Desktopprozess."
    }
    if ((Get-FileHash -LiteralPath $DesktopExe -Algorithm SHA256).Hash -ne $DesktopExeHashBefore) {
        throw "Der abgewiesene Dienst-Installer veränderte die Desktopinstallation."
    }
    if ((Get-TreeFingerprint -Path $DesktopDir) -ne $DesktopTreeBefore -or
        (Get-TreeFingerprint -Path $DesktopData) -ne $DesktopDataBefore) {
        throw "Der abgewiesene Dienst-Installer veränderte den Desktopdateibaum."
    }
    if ((Get-FileHash -LiteralPath $DesktopToken -Algorithm SHA256).Hash -ne $DesktopTokenHashBefore) {
        throw "Der abgewiesene Dienst-Installer veränderte das Desktop-API-Token."
    }
    Assert-RegistryValueUnchanged -Before $AutostartBefore `
        -After (Get-OptionalRegistryValue -Path $RunKey -Name $RunName) `
        -Description "Der Desktop-Autostart"
    if ((Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) -or
        (Test-Path -LiteralPath $ServiceDir) -or
        (Test-Path -LiteralPath $ServiceData) -or
        (Test-Path -LiteralPath $ServiceUninstallKey)) {
        throw "Der abgewiesene Dienst-Installer hinterließ Dienst- oder Maschinenzustand."
    }

    Invoke-Setup -Path $DesktopUninstaller -Scenario "Desktopdeinstallation" -Arguments @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/LOG=`"$DesktopUninstallLog`""
    )
    if (-not $DesktopProcess.WaitForExit(30000)) {
        throw "Die Desktopdeinstallation beendete den laufenden Desktopprozess nicht."
    }
    $DesktopProcess = $null
    if ((Test-Path -LiteralPath $DesktopDir) -or
        (Test-Path -LiteralPath $DesktopData) -or
        (Test-Path -LiteralPath $DesktopUninstallKey) -or
        (Get-OptionalRegistryValue -Path $RunKey -Name $RunName).Exists) {
        throw "Die Desktopdeinstallation hinterließ einen Konflikt für den Dienstmodus."
    }

    $ServiceInstallArguments = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/TASKS=`"systemstart`"",
        "/LOG=`"$ServiceInstallLog`""
    )
    Write-ProfileHiveCategoryDiagnostic
    Write-LoopbackPortCategoryDiagnostic
    Invoke-Setup -Path $ServiceSetup -Scenario "Dienstinstallation nach Desktopdeinstallation" `
        -Arguments $ServiceInstallArguments
    Wait-ServiceState -Name $ServiceName -State "Running"
    if (-not (Test-Path -LiteralPath $ServiceUninstaller -PathType Leaf) -or
        -not (Test-Path -LiteralPath $ServiceToken -PathType Leaf)) {
        throw "Der Dienstmodus wurde nach Entfernung des Desktopmodus nicht vollständig installiert."
    }

    Stop-Service $ServiceName
    Wait-ServiceState -Name $ServiceName -State "Stopped"
    $ServiceSnapshotBefore = Get-ServiceSnapshot -Name $ServiceName `
        -RegistryPath $ServiceRegistryPath
    $ServiceTreeBefore = Get-TreeFingerprint -Path $ServiceDir
    $ServiceDataBefore = Get-TreeFingerprint -Path $ServiceData
    $ServiceTokenHashBefore = (Get-FileHash -LiteralPath $ServiceToken -Algorithm SHA256).Hash

    Invoke-SetupExpectedFailure -Path $DesktopSetup -LogPath $DesktopBlockedLog `
        -Scenario "Desktopinstallation bei vorhandenem Dienstmodus" -Arguments @(
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/TASKS=`"autostart`"",
            "/LOG=`"$DesktopBlockedLog`""
        )

    Assert-ServiceSnapshotUnchanged -Before $ServiceSnapshotBefore `
        -After (Get-ServiceSnapshot -Name $ServiceName -RegistryPath $ServiceRegistryPath)
    if ((Get-TreeFingerprint -Path $ServiceDir) -ne $ServiceTreeBefore) {
        throw "Der abgewiesene Desktop-Installer veränderte das installierte Dienstbundle."
    }
    if ((Get-TreeFingerprint -Path $ServiceData) -ne $ServiceDataBefore -or
        (Get-FileHash -LiteralPath $ServiceToken -Algorithm SHA256).Hash -ne
        $ServiceTokenHashBefore) {
        throw "Der abgewiesene Desktop-Installer veränderte ProgramData oder das Diensttoken."
    }
    if ((Test-Path -LiteralPath $DesktopDir) -or
        (Test-Path -LiteralPath $DesktopData) -or
        (Test-Path -LiteralPath $DesktopUninstallKey) -or
        (Get-OptionalRegistryValue -Path $RunKey -Name $RunName).Exists) {
        throw "Der abgewiesene Desktop-Installer hinterließ Desktopzustand."
    }

    Invoke-Setup -Path $ServiceUninstaller -Scenario "Dienstdeinstallation mit erhaltenem ProgramData" `
        -Arguments @(
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/LOG=`"$ServicePreserveUninstallLog`""
        )
    Wait-ServiceState -Name $ServiceName -State "Absent"
    if ((Test-Path -LiteralPath $ServiceDir) -or
        (Test-Path -LiteralPath $ServiceUninstallKey)) {
        throw "Die Dienstdeinstallation mit Datenerhalt hinterließ Binär- oder Registrierungszustand."
    }
    if (-not (Test-Path -LiteralPath $ServiceData -PathType Container) -or
        (Get-TreeFingerprint -Path $ServiceData) -ne $ServiceDataBefore -or
        (Get-FileHash -LiteralPath $ServiceToken -Algorithm SHA256).Hash -ne
        $ServiceTokenHashBefore) {
        throw "Die Dienstdeinstallation mit Datenerhalt veränderte ProgramData oder das Diensttoken."
    }

    Invoke-Setup -Path $DesktopSetup -Scenario "Desktopinstallation bei reinem erhaltenem ProgramData" `
        -Arguments @(
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/TASKS=`"autostart`"",
            "/LOG=`"$DesktopPreservedDataInstallLog`""
        )
    if (-not (Test-Path -LiteralPath $DesktopExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $DesktopUninstaller -PathType Leaf)) {
        throw "Erhaltenes Dienst-ProgramData blockierte die Desktopinstallation."
    }
    if ((Get-TreeFingerprint -Path $ServiceData) -ne $ServiceDataBefore -or
        (Get-FileHash -LiteralPath $ServiceToken -Algorithm SHA256).Hash -ne
        $ServiceTokenHashBefore) {
        throw "Die Desktopinstallation veränderte erhaltenes Dienst-ProgramData."
    }

    Invoke-Setup -Path $DesktopUninstaller `
        -Scenario "Desktopdeinstallation nach ProgramData-Ausschlusstest" -Arguments @(
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/LOG=`"$DesktopPreservedDataUninstallLog`""
        )
    if ((Test-Path -LiteralPath $DesktopDir) -or
        (Test-Path -LiteralPath $DesktopData) -or
        (Test-Path -LiteralPath $DesktopUninstallKey) -or
        (Get-OptionalRegistryValue -Path $RunKey -Name $RunName).Exists) {
        throw "Die zweite Desktopdeinstallation hinterließ einen Gegenmodus-Footprint."
    }
    if ((Get-TreeFingerprint -Path $ServiceData) -ne $ServiceDataBefore -or
        (Get-FileHash -LiteralPath $ServiceToken -Algorithm SHA256).Hash -ne
        $ServiceTokenHashBefore) {
        throw "Die Desktopdeinstallation veränderte erhaltenes Dienst-ProgramData."
    }

    $ServiceReinstallArguments = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/TASKS=`"systemstart`"",
        "/LOG=`"$ServiceReinstallLog`""
    )
    Invoke-Setup -Path $ServiceSetup -Scenario "Dienstneuinstallation mit erhaltenem ProgramData" `
        -Arguments $ServiceReinstallArguments
    Wait-ServiceState -Name $ServiceName -State "Running"
    if ((Get-FileHash -LiteralPath $ServiceToken -Algorithm SHA256).Hash -ne
        $ServiceTokenHashBefore) {
        throw "Die Dienstneuinstallation ersetzte das erhaltene Diensttoken."
    }

    Invoke-Setup -Path $ServiceUninstaller -Scenario "Dienstdeinstallation mit Datenlöschung" -Arguments @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/PURGEDATA=1",
        "/LOG=`"$ServicePurgeUninstallLog`""
    )
    Wait-ServiceState -Name $ServiceName -State "Absent"
    if ((Test-Path -LiteralPath $ServiceDir) -or
        (Test-Path -LiteralPath $ServiceData) -or
        (Test-Path -LiteralPath $ServiceUninstallKey)) {
        throw "Der Modusausschlusstest hinterließ Dienstzustand."
    }
} finally {
    if ($null -ne $DesktopProcess) {
        try {
            $DesktopProcess.Refresh()
            if (-not $DesktopProcess.HasExited) {
                $DesktopProcess.Kill($true)
                $DesktopProcess.WaitForExit()
            }
        } catch {
            Write-Warning "Der eigene Desktop-Testprozess konnte nicht bereinigt werden: $_"
        }
    }
    if (Test-Path -LiteralPath $DesktopUninstaller -PathType Leaf) {
        try {
            Invoke-Setup -Path $DesktopUninstaller -Scenario "Desktopbereinigung" -Arguments @(
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NORESTART"
            )
        } catch {
            Write-Warning "Die eigene Desktop-Testinstallation konnte nicht bereinigt werden: $_"
        }
    }
    if (Test-Path -LiteralPath $ServiceUninstaller -PathType Leaf) {
        try {
            Invoke-Setup -Path $ServiceUninstaller -Scenario "Dienstbereinigung" -Arguments @(
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NORESTART",
                "/PURGEDATA=1"
            )
            Wait-ServiceState -Name $ServiceName -State "Absent"
        } catch {
            Write-Warning "Die eigene Dienst-Testinstallation konnte nicht bereinigt werden: $_"
        }
    }
}

Write-Host "Gegenseitiger Ausschluss von Desktop- und Dienstmodus erfolgreich geprüft."
