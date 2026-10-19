#!/usr/bin/env bash
# run_all.sh - Launch the unified OCR Hub Flask Server on Linux / macOS.
#
# Usage:
#   chmod +x run_all.sh
#   ./run_all.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

# Try the standard .venv location first, then fall back to system python
if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
elif command -v python3 &>/dev/null; then
    PY="python3"
elif command -v python &>/dev/null; then
    PY="python"
else
    echo "ERROR: Could not find Python. Create a venv first:"
    echo "  python3 -m venv .venv"
    echo "  source .venv/bin/activate"
    echo "  pip install -r requirements.txt"
    exit 1
fi

echo "Starting OCR Hub on http://0.0.0.0:5000 ..."
exec "$PY" "$ROOT/app.py"
