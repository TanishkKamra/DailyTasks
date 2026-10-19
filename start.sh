#!/usr/bin/env bash
# start.sh - Launch OCR Hub Server on Linux / macOS
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ ! -x "$ROOT/.venv/bin/python" ]; then
    echo "Virtual environment not found! Running installer first..."
    bash "$ROOT/install.sh"
fi

echo "Starting OCR Hub on http://0.0.0.0:5000 ..."
exec "$ROOT/.venv/bin/python" "$ROOT/app.py"
