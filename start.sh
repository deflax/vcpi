#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
PYTHON_BIN="$VENV_DIR/bin/python"
PIP_BIN="$VENV_DIR/bin/pip"

if [ ! -x "$PYTHON_BIN" ]; then
  python3 -m venv "$VENV_DIR"
  "$PIP_BIN" install --upgrade pip
  "$PIP_BIN" install -r "$ROOT_DIR/requirements.txt"
fi

exec "$PYTHON_BIN" "$ROOT_DIR/vst_host.py" "$@"
