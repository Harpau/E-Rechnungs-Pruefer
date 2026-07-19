$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } else {
        & python -m venv .venv
    }
}

$Python = (Resolve-Path ".venv\Scripts\python.exe").Path
& $Python -m pip install --upgrade pip
& $Python -m pip install -e ".[dev]"
& $Python -m pre_commit install
& $Python scripts\verify_version.py
Write-Host "`nEntwicklungsumgebung ist bereit. Start: .venv\Scripts\python.exe -m app --reload"
