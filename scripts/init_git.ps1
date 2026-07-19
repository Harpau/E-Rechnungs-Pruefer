param(
    [string]$RemoteUrl = ""
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (Test-Path ".git") {
    throw "Dieses Verzeichnis ist bereits ein Git-Repository."
}

git init -b main
git add .
git diff --cached --check

$UserName = git config user.name
$UserEmail = git config user.email
if ([string]::IsNullOrWhiteSpace($UserName) -or [string]::IsNullOrWhiteSpace($UserEmail)) {
    Write-Host @"
Git wurde initialisiert und alle Dateien wurden vorgemerkt.
Vor dem ersten Commit bitte die Identität konfigurieren:
  git config user.name "Ihr Name"
  git config user.email "ihre-adresse@example.com"
  git commit -m "Initial import of E-Rechnungs-Pruefer 1.1.0"
"@
} else {
    git commit -m "Initial import of E-Rechnungs-Pruefer 1.1.0"
}

if (-not [string]::IsNullOrWhiteSpace($RemoteUrl)) {
    git remote add origin $RemoteUrl
    Write-Host "Remote 'origin' wurde gesetzt. Push nach Prüfung mit: git push -u origin main"
} else {
    Write-Host "Remote später setzen mit: git remote add origin <GitHub-URL>"
}
