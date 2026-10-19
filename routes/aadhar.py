import base64
import csv
import io
import json
import logging
import re
import time
from pathlib import Path
from typing import List, Tuple

from flask import Blueprint, Response, g, jsonify, render_template, request

import config
from core.aocr_core import OCR_ENGINE, mask_aadhaar, mask_name, process_card_bytes

aadhar_bp = Blueprint("aadhar", __name__)


def _make_logger(name: str, log_path: Path) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger


adh_req_log = _make_logger("aadhaar.requests", config.AADHAAR_LOG_DIR / "requests.log")
adh_err_log = _make_logger("aadhaar.errors", config.AADHAAR_LOG_DIR / "errors.log")


def _log_event(logger: logging.Logger, service: str, status: int, details: str) -> None:
    req_id = getattr(g, "request_id", "-")
    client_ip = request.remote_addr or "unknown"
    elapsed = (time.perf_counter() - getattr(g, "request_started_at", time.perf_counter())) * 1000.0
    meta = (
        f"req={req_id} | ip={client_ip} | host={request.host} | method={request.method} "
        f"| path={request.path} | url={request.base_url} | public_url={config.BASE_URL}{request.path}"
    )
    logger.info(f"{meta} | svc={service} | status={status} | ms={elapsed:.1f} | {details}")


def _clean_filename(name: str) -> str:
    return re.sub(r"[\r\n\t\x00-\x1f]", "_", name)[:200]


def _extract_input_items(req) -> Tuple[List[Tuple[str, bytes]], bool]:
    """
    Extracts (filename, bytes) list from multipart form, JSON payload (base64), or raw binary.
    Returns (items, is_single_hint).
    """
    items = []
    # 1. Multipart files
    files = (
        req.files.getlist("cards")
        or req.files.getlist("card")
        or req.files.getlist("files")
        or req.files.getlist("file")
        or req.files.getlist("images")
        or req.files.getlist("image")
    )
    valid_files = [f for f in files if f and f.filename]
    if valid_files:
        is_single = len(valid_files) == 1 and ("card" in req.files or "file" in req.files or "image" in req.files)
        for f in valid_files:
            items.append((_clean_filename(f.filename), f.read()))
        return items, is_single

    # 2. JSON Base64 payload
    if req.is_json:
        data = req.get_json(silent=True) or {}
        single_b64 = (
            data.get("image")
            or data.get("card")
            or data.get("file")
            or data.get("base64")
            or data.get("image_base64")
            or data.get("pdf_base64")
        )
        if single_b64 and isinstance(single_b64, str):
            fn = data.get("filename") or "card.jpg"
            b64_str = single_b64.split(",")[-1] if "," in single_b64 else single_b64
            try:
                items.append((_clean_filename(fn), base64.b64decode(b64_str)))
                return items, True
            except Exception:
                pass

        batch_list = data.get("cards") or data.get("images") or data.get("files")
        if isinstance(batch_list, list):
            for idx, entry in enumerate(batch_list):
                if isinstance(entry, str):
                    b64_str = entry.split(",")[-1] if "," in entry else entry
                    try:
                        items.append((_clean_filename(f"card_{idx+1}.jpg"), base64.b64decode(b64_str)))
                    except Exception:
                        pass
                elif isinstance(entry, dict):
                    b64_val = entry.get("image") or entry.get("base64") or entry.get("file")
                    fn = entry.get("filename") or f"card_{idx+1}.jpg"
                    if b64_val and isinstance(b64_val, str):
                        b64_str = b64_val.split(",")[-1] if "," in b64_val else b64_val
                        try:
                            items.append((_clean_filename(fn), base64.b64decode(b64_str)))
                        except Exception:
                            pass
            if items:
                return items, False

    # 3. Raw Binary Body
    raw_body = req.get_data()
    if raw_body and len(raw_body) > 10:
        fn = req.headers.get("X-Filename") or ("upload.pdf" if raw_body.startswith(b"%PDF") else "upload.jpg")
        items.append((_clean_filename(fn), raw_body))
        return items, True

    return [], False


@aadhar_bp.route("/aadhar", methods=["GET", "POST"])
@aadhar_bp.route("/aadhar/", methods=["GET", "POST"])
@aadhar_bp.route("/aadharocr/", methods=["GET", "POST"])
@aadhar_bp.route("/aadharocr", methods=["GET", "POST"])
def index():
    results = []
    if request.method == "POST":
        items, _ = _extract_input_items(request)
        for filename, raw in items:
            try:
                res = process_card_bytes(raw, label=filename)
                mapped = res["mapped"]

                _log_event(
                    adh_req_log,
                    "AADHAAR_FORM",
                    200,
                    f"file={filename!r} | aadhaar={mask_aadhaar(mapped['txtaadharno'])!r} "
                    f"| name={mask_name(mapped['txtaname'])!r} | needs_review={res['needs_review']}"
                )
                print(f"[AADHAAR OCR] '{filename}' -> UID: {mask_aadhaar(mapped['txtaadharno']) or '(not found)'} | Name: {mask_name(mapped['txtaname']) or '(not found)'}", flush=True)

                results.append({
                    "filename": filename,
                    "fields": res["fields"],
                    "needs_review": res["needs_review"],
                    "mapped_json": json.dumps(mapped, indent=2),
                })
            except Exception as exc:
                _log_event(adh_err_log, "AADHAAR_FORM", 500, f"file={filename!r} | error={exc}")
                print(f"[AADHAAR OCR ERROR] '{filename}': {exc}", flush=True)
                results.append({"filename": filename, "error": str(exc)})
    else:
        _log_event(adh_req_log, "AADHAAR_FORM", 200, "rendered upload form")

    return render_template(
        "aadhar.html",
        active_page="aadhar",
        lan_ip=config.LAN_IP,
        port=config.PORT,
        engine=OCR_ENGINE,
        results=results,
    )


@aadhar_bp.route("/aadhar/api/ocr", methods=["GET", "POST", "OPTIONS"])
@aadhar_bp.route("/aadharocr/api/ocr", methods=["GET", "POST", "OPTIONS"])
@aadhar_bp.route("/api/aadhar", methods=["GET", "POST", "OPTIONS"])
def api_ocr():
    """
    JSON API endpoint for Aadhaar OCR. Supports multipart/form-data, JSON Base64, raw binary, and ?format=csv export.
    """
    if request.method == "OPTIONS":
        resp = jsonify({"status": "ok"})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Filename"
        return resp

    if request.method == "GET":
        resp = jsonify({
            "status": "online",
            "endpoint": "POST /aadhar/api/ocr",
            "message": "Aadhaar Card OCR API. Send images or PDFs as multipart/form-data, base64 JSON, or raw binary body.",
            "supported_inputs": [
                "multipart/form-data (field 'cards' or 'card')",
                "application/json (keys 'image' or 'images' with base64 string)",
                "raw binary body (Content-Type: image/jpeg or application/pdf, header X-Filename)"
            ],
            "options": {
                "format": "json (default) or csv (?format=csv)"
            },
            "sample_response": {
                "filename": "aadhar.jpg",
                "txtaadharno": "1234 5678 9012",
                "txtaname": "SAMPLE NAME",
                "needs_review": [],
                "fields": {
                    "aadhaar": {"value": "1234 5678 9012", "confidence": 0.95, "valid": True},
                    "name": {"value": "SAMPLE NAME", "confidence": 0.90, "valid": True}
                }
            }
        })
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    items, single_mode = _extract_input_items(request)

    if not items:
        _log_event(adh_err_log, "AADHAAR_API", 400, "file=(none) | error=No file uploaded")
        print("[AADHAAR OCR ERROR] No file uploaded in API request", flush=True)
        resp = jsonify({"error": "No file uploaded. Send image/PDF as multipart field 'card', JSON base64, or raw body."})
        resp.status_code = 400
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    results = []
    for filename, raw in items:
        try:
            res = process_card_bytes(raw, label=filename)
            payload = {
                "filename": filename,
                "txtaadharno": res["mapped"]["txtaadharno"],
                "txtaname": res["mapped"]["txtaname"],
                "needs_review": res["needs_review"],
                "fields": res["fields"],
            }
            _log_event(
                adh_req_log,
                "AADHAAR_API",
                200,
                f"file={filename!r} | aadhaar={mask_aadhaar(payload['txtaadharno'])!r} "
                f"| name={mask_name(payload['txtaname'])!r} | needs_review={payload['needs_review']}"
            )
            print(f"[AADHAAR OCR API] '{filename}' -> UID: {mask_aadhaar(payload['txtaadharno']) or '(not found)'} | Name: {mask_name(payload['txtaname']) or '(not found)'}", flush=True)
            results.append(payload)
        except Exception as exc:
            _log_event(adh_err_log, "AADHAAR_API", 400, f"file={filename!r} | error={exc}")
            print(f"[AADHAAR OCR API ERROR] '{filename}': {exc}", flush=True)
            results.append({"filename": filename, "error": str(exc)})

    # Direct CSV Export for API callers
    if request.args.get("format", "").lower() == "csv":
        si = io.StringIO()
        cw = csv.writer(si)
        cw.writerow(["filename", "aadhaar_number", "cardholder_name", "status", "confidence"])
        for r in results:
            if "error" in r:
                cw.writerow([r.get("filename", ""), "", "", "error", ""])
            else:
                uid_val = r.get("txtaadharno", "")
                name_val = r.get("txtaname", "")
                status_val = "needs_review" if r.get("needs_review") else "validated"
                conf_val = r.get("fields", {}).get("aadhaar", {}).get("confidence", "")
                cw.writerow([r.get("filename", ""), uid_val, name_val, status_val, conf_val])
        resp = Response(si.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = "attachment; filename=aadhaar_ocr_results.csv"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    if single_mode:
        resp = jsonify(results[0])
    else:
        resp = jsonify(results)

    resp.status_code = 200
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp
