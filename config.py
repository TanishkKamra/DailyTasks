"""
config.py, Central configuration for OCR Hub
"""

import os
import socket
from pathlib import Path


def safe_print(*args, **kwargs):
    """print() that never raises if stdout is unavailable.

    Under IIS (httpPlatformHandler with stdoutLogEnabled), the pipe backing
    stdout can be transiently unwritable (e.g. while IIS recycles the
    captured log). A plain print() at that moment raises
    OSError: [Errno 22] Invalid argument, and if that happens inside a
    request handler (every route file has status prints like
    "[PAN OCR] ... -> PAN: ..." after each processed file/job), it turns an
    otherwise-successful request into an unhandled 500. Every route module
    that prints per-request status should import this instead of using the
    builtin print() directly. Everything important is also written to the
    real log files (api_requests.log, PANLOGS/, AADHARLOGS/, etc.), so it's
    safe to just drop the console line if stdout isn't writable right now.
    """
    try:
        print(*args, **kwargs)
    except OSError:
        pass


def detect_local_ip() -> str:
    """Auto-detect the machine's LAN IP address.

    Falls back to 127.0.0.1 if there's no outbound network route (e.g. an
    isolated IIS box with no internet access). Previously this failed
    completely silently, so BASE_URL/LAN_IP would quietly become
    "127.0.0.1" with no indication anything was wrong. We now print a
    one-line warning when the fallback triggers (via safe_print, since
    stdout can be transiently unwritable under IIS) and allow the LAN_IP
    env var to override detection entirely.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        safe_print(
            "[WARNING] Could not auto-detect a LAN IP (no outbound network "
            "route found) - falling back to 127.0.0.1. If this server "
            "should be reachable from other machines, set the LAN_IP "
            "environment variable explicitly.",
            flush=True,
        )
        return "127.0.0.1"


BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from .env into os.environ (real env vars win)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
LOG_DIR = BASE_DIR / "log"
PAN_LOG_DIR = LOG_DIR / "PANLOGS"
AADHAAR_LOG_DIR = LOG_DIR / "AADHARLOGS"

# Ensure directories exist
for directory in (UPLOAD_DIR, OUTPUT_DIR, LOG_DIR, PAN_LOG_DIR, AADHAAR_LOG_DIR):
    directory.mkdir(parents=True, exist_ok=True)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))
LAN_IP = os.getenv("LAN_IP") or detect_local_ip()
BASE_URL = f"http://{LAN_IP}:{PORT}"

# Upload limits (50 MB)
MAX_CONTENT_LENGTH = 50 * 1024 * 1024

# Demo columns workbook for Policy Reader
DEMO_COLUMNS_WORKBOOK = BASE_DIR / "DemoColumns.xlsx"
DEFAULT_POLICY_OUTPUT = BASE_DIR / "policy_reading_outputs.xlsx"

# Azure AI default credentials (key comes from .env / environment only)
AZURE_AI_ENDPOINT = os.getenv("AZURE_AI_ENDPOINT", "")
AZURE_AI_API_KEY = os.getenv("AZURE_AI_API_KEY", "")
if not AZURE_AI_API_KEY:
    safe_print(
        "[WARNING] AZURE_AI_API_KEY is not set (add it and AZURE_AI_ENDPOINT to .env). The Policy Reader "
        "will reject requests that rely on the server key.",
        flush=True,
    )
AZURE_AI_MODEL = os.getenv("AZURE_AI_MODEL", "gpt-5-mini")
AZURE_AI_API_VERSION = os.getenv("AZURE_AI_API_VERSION", "2025-04-01-preview")


# Your own brokerage's email domains (comma-separated), so the Policy Reader never mistakes them for the customer's
BROKER_EMAIL_DOMAINS = {d.strip().lower() for d in os.getenv("BROKER_EMAIL_DOMAINS", "").split(",") if d.strip()}
