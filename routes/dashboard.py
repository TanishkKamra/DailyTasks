"""
routes/dashboard.py, Dashboard & Landing routes with live multi-service request logging feed
and recent job history API.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request

import config
from core.aocr_core import OCR_ENGINE as AADHAAR_ENGINE
from core.job_manager import job_manager
from core.pan_ocr import OCR_ENGINE as PAN_ENGINE

dashboard_bp = Blueprint("dashboard", __name__)

REQUEST_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| "
    r"req=(?P<request_id>[^|]+) \| ip=(?P<ip>[^|]+) \| host=(?P<host>[^|]+) \| "
    r"method=(?P<method>[^|]+) \| path=(?P<path>[^|]+) \| url=(?P<url>[^|]+) \| "
    r"public_url=(?P<public_url>[^|]+) \| svc=(?P<service>[^|]+) \| status=(?P<status>\d+) \| "
    r"ms=(?P<duration>[^|]+) \| (?P<details>.+)$"
)


def _parse_request_line(line: str) -> Optional[Dict[str, Any]]:
    m = REQUEST_PATTERN.match(line.strip())
    if not m:
        return None
    data = m.groupdict()
    try:
        status_code = int(data["status"])
    except ValueError:
        status_code = 0
    try:
        duration_ms = float(data["duration"])
    except ValueError:
        duration_ms = 0.0

    raw_details = data["details"].strip()
    details_dict = {}
    for part in raw_details.split("|"):
        part = part.strip()
        kv = re.match(r"^(\w+)=(.*)$", part)
        if kv:
            details_dict[kv.group(1)] = kv.group(2).strip().strip("'\"")

    return {
        "timestamp": data["timestamp"],
        "request_id": data["request_id"].strip(),
        "ip": data["ip"].strip(),
        "service": data["service"].strip(),
        "method": data["method"].strip(),
        "path": data["path"].strip(),
        "status": status_code,
        "duration": duration_ms,
        "details_dict": details_dict or {"info": raw_details},
    }


def _read_recent_logs(limit: int = 50) -> List[Dict[str, Any]]:
    entries = []
    log_files = [
        config.LOG_DIR / "api_requests.log",
        config.PAN_LOG_DIR / "requests.log",
        config.AADHAAR_LOG_DIR / "requests.log",
        config.LOG_DIR / "policy_requests.log",
    ]
    seen_keys = set()
    for log_path in log_files:
        if not log_path.exists():
            continue
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                parsed = _parse_request_line(line)
                if parsed:
                    key = (parsed["timestamp"], parsed["request_id"], parsed["path"])
                    if key not in seen_keys:
                        seen_keys.add(key)
                        entries.append(parsed)
        except Exception:
            pass

    entries.sort(key=lambda x: x["timestamp"], reverse=True)
    return entries[:limit]


@dashboard_bp.route("/")
@dashboard_bp.route("/dashboard")
def index():
    return render_template(
        "dashboard.html",
        active_page="dashboard",
        lan_ip=config.LAN_IP,
        port=config.PORT,
        pan_engine=PAN_ENGINE,
        aadhar_engine=AADHAAR_ENGINE,
    )


@dashboard_bp.route("/api/logs")
def api_logs():
    entries = _read_recent_logs(50)
    return jsonify({"entries": entries})


@dashboard_bp.route("/api/recent_jobs")
def api_recent_jobs():
    jobs = job_manager.list_jobs(limit=25)
    return jsonify({"jobs": jobs})


@dashboard_bp.route("/api/stats")
def api_stats():
    entries = _read_recent_logs(200)
    total_reqs = len(entries)
    pan_reqs = sum(1 for e in entries if "PAN" in e["service"].upper())
    aadhar_reqs = sum(1 for e in entries if "AADHAAR" in e["service"].upper())
    policy_reqs = sum(1 for e in entries if "POLICY" in e["service"].upper())
    lp_reqs = sum(1 for e in entries if "LP" in e["service"].upper())
    err_count = sum(1 for e in entries if e["status"] >= 400)

    return jsonify({
        "total_requests": total_reqs,
        "pan_requests": pan_reqs,
        "aadhar_requests": aadhar_reqs,
        "policy_requests": policy_reqs,
        "lp_requests": lp_reqs,
        "error_count": err_count,
        "success_rate": round(((total_reqs - err_count) / max(1, total_reqs)) * 100, 1),
    })
