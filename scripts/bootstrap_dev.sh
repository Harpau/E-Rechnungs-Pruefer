#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ ! -d .venv ]; then
  "$PYTHON_BIN" -m venv .venv
fi
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pre_commit install
python scripts/verify_version.py
printf '\nEntwicklungsumgebung ist bereit. Start: .venv/bin/python -m app --reload\n'
