#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."

if [ -x .venv/bin/python ]; then
  PYTHON_BIN=.venv/bin/python
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

"$PYTHON_BIN" scripts/verify_version.py
"$PYTHON_BIN" -m ruff check app tests scripts
"$PYTHON_BIN" -m ruff format --check app tests scripts
"$PYTHON_BIN" -m mypy
"$PYTHON_BIN" -m pytest --cov=app --cov-report=term-missing
