#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ ! -d .venv ]; then
  "$PYTHON_BIN" -m venv .venv
fi
. .venv/bin/activate
if ! python -c "import fastapi, lxml, pypdf, uvicorn" >/dev/null 2>&1; then
  python -m pip install -r requirements.txt
fi
python -m app --open "$@"
