$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$Python = if (Test-Path ".venv\Scripts\python.exe") {
    (Resolve-Path ".venv\Scripts\python.exe").Path
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    "py"
} else {
    "python"
}

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    if ($Python -eq "py") {
        & py -3 @Arguments
    } else {
        & $Python @Arguments
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Invoke-Python scripts\verify_version.py
Invoke-Python -m ruff check app tests scripts
Invoke-Python -m ruff format --check app tests scripts
Invoke-Python -m mypy
Invoke-Python -m pytest --cov=app --cov-report=term-missing
