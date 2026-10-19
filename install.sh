#!/usr/bin/env bash
# install.sh - All-in-One Automated Installer for Linux / macOS
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "===================================================================="
echo "               OCR HUB - ALL-IN-ONE INSTALLER "
echo "===================================================================="
echo

# 1. Detect Python
echo "[1/5] Detecting Python installation..."
if command -v python3 &>/dev/null; then
    SYS_PY="python3"
elif command -v python &>/dev/null; then
    SYS_PY="python"
else
    echo "[ERROR] Python 3.10+ is required but not found."
    exit 1
fi
echo "  Found Python: $("$SYS_PY" --version)"

# 2. Setup Virtual Environment
echo
echo "[2/5] Setting up virtual environment (.venv)..."
if [ ! -d "$ROOT/.venv" ]; then
    "$SYS_PY" -m venv "$ROOT/.venv"
    echo "  Created virtual environment at $ROOT/.venv"
else
    echo "  Existing virtual environment detected."
fi

VENV_PY="$ROOT/.venv/bin/python"
VENV_PIP="$ROOT/.venv/bin/pip"

# 3. Install Dependencies
echo
echo "[3/5] Installing dependencies from requirements.txt..."
"$VENV_PY" -m pip install --upgrade pip setuptools wheel
"$VENV_PIP" install -r "$ROOT/requirements.txt"

# 4. Create Runtime Folders
echo
echo "[4/5] Creating runtime directories..."
mkdir -p "$ROOT/uploads" "$ROOT/outputs" "$ROOT/log/PANLOGS" "$ROOT/log/AADHARLOGS"

# 5. Verify Installation
echo
echo "[5/5] Verifying installation..."
"$VENV_PY" -c "import flask, cv2, rapidocr_onnxruntime, pymupdf, pandas, openpyxl, requests; print('  [OK] All core packages verified successfully!')"

echo
echo "===================================================================="
echo "        🎉 OCR HUB INSTALLATION COMPLETED SUCCESSFULLY! 🎉"
echo "===================================================================="
echo
echo "To run OCR Hub, execute:"
echo "  ./start.sh   (or python app.py with .venv activated)"
echo
echo "Server will run on http://0.0.0.0:5000"
echo
