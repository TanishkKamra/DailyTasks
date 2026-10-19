"""
routes/policy.py: Insurance Policy Reader routes with parallel multi-threaded AI processing,
auto-insurer detection, clean single-company workbook generation,
company-specific worksheet routing, background job management, and unified JSON OCR API.
"""

import concurrent.futures
import io
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Blueprint, g, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import config
from core.job_manager import Job, job_manager
from core.policy_engine import (
    DEFAULT_AZURE_API_VERSION,
    PROVIDERS,
    append_result_to_excel,
    detect_insurer_from_text,
    extract_pdf_text,
    load_policy_fields,
    run_inference_api,
)
from core.excel_utils import _ensure_policy_template_copy

policy_bp = Blueprint("policy", __name__)

COMPANY_MODES = ["ZUNO", "HDFC ERGO", "TATA AIG", "GO DIGIT", "ICICI Lombard"]

# Default API configs from Azure AI (key, endpoint and model always come as one set;
# the Azure OpenAI env pair is handled separately in _server_api_key)
_POLICY_DEFAULT_API_KEY = config.AZURE_AI_API_KEY
_POLICY_DEFAULT_ENDPOINT = config.AZURE_AI_ENDPOINT
_POLICY_DEFAULT_MODEL = config.AZURE_AI_MODEL
_MISSING_KEY_ERROR = "No API key. Enter one, or set AZURE_AI_ENDPOINT and AZURE_AI_API_KEY in the server's .env file."
_BACKUP_DIR = config.BASE_DIR / "backups"
_BACKUPS_KEPT = 5

_excel_lock = threading.Lock()


def _norm_url(url: Optional[str]) -> str:
    return (url or "").strip().rstrip("/").lower()


def _server_api_key(base_url: str, preset: Optional[dict] = None) -> str:
    """Server-held key for base_url, only when base_url is the endpoint that key belongs to.

    Callers may override the endpoint; they must then bring their own key, so the
    server key is never sent to a caller-chosen host.
    """
    target = _norm_url(base_url)
    if not target:
        return ""
    if target == _norm_url(_POLICY_DEFAULT_ENDPOINT):
        return _POLICY_DEFAULT_API_KEY.strip()
    if preset and preset.get("key_env") and target == _norm_url(preset.get("base_url")):
        return os.getenv(preset["key_env"], "").strip()
    if target == _norm_url(os.getenv("AZURE_OPENAI_ENDPOINT")):
        return os.getenv("AZURE_OPENAI_API_KEY", "").strip()
    return ""


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


pol_req_log = _make_logger("policy.requests", config.LOG_DIR / "policy_requests.log")
pol_err_log = _make_logger("policy.errors", config.LOG_DIR / "policy_errors.log")


def _log_event(logger: logging.Logger, service: str, status: int, details: str) -> None:
    req_id = getattr(g, "request_id", "-")
    client_ip = request.remote_addr or "unknown"
    elapsed = (time.perf_counter() - getattr(g, "request_started_at", time.perf_counter())) * 1000.0
    meta = (
        f"req={req_id} | ip={client_ip} | host={request.host} | method={request.method} "
        f"| path={request.path} | url={request.base_url} | public_url={config.BASE_URL}{request.path}"
    )
    logger.info(f"{meta} | svc={service} | status={status} | ms={elapsed:.1f} | {details}")


def determine_policy_sheet_name(company_mode: Optional[str] = None, extracted_data: Optional[dict] = None) -> str:
    """
    Route extracted policy to its respective company worksheet.
    Checks extracted insurer first, then falls back to selected company_mode.
    """
    if extracted_data and isinstance(extracted_data, dict):
        ins = str(extracted_data.get("insurer") or "").strip().upper()
        if "TATA" in ins:
            return "TATAPolicies"
        if "HDFC" in ins:
            return "HDFCPolicies"
        if "ZUNO" in ins or "EDELWEISS" in ins:
            return "ZUNOPolicies"
        if "ICICI" in ins:
            return "ICICIPolicies"
        if "DIGIT" in ins or "GO DIGIT" in ins:
            return "GoDigitPolicies"
        if "BAJAJ" in ins:
            return "BajajPolicies"
        if "STAR" in ins:
            return "StarPolicies"
        if "CARE" in ins or "RELIGARE" in ins:
            return "CarePolicies"
        if "NIVA" in ins or "MAX BUPA" in ins:
            return "NivaPolicies"
        if "NATIONAL" in ins:
            return "NationalPolicies"
        if "NEW INDIA" in ins:
            return "NewIndiaPolicies"
        if "ORIENTAL" in ins:
            return "OrientalPolicies"
        if "UNITED" in ins:
            return "UnitedPolicies"

    mode = str(company_mode or "").strip().upper()
    if "HDFC" in mode:
        return "HDFCPolicies"
    if "TATA" in mode:
        return "TATAPolicies"
    if "ZUNO" in mode or "EDELWEISS" in mode:
        return "ZUNOPolicies"
    if "ICICI" in mode:
        return "ICICIPolicies"
    if "DIGIT" in mode or "GO DIGIT" in mode:
        return "GoDigitPolicies"
    if "BAJAJ" in mode:
        return "BajajPolicies"
    if "STAR" in mode:
        return "StarPolicies"
    if "CARE" in mode:
        return "CarePolicies"
    if "NIVA" in mode:
        return "NivaPolicies"

    return "Policies"


@policy_bp.route("/policy", methods=["GET"])
@policy_bp.route("/policy/", methods=["GET"])
@policy_bp.route("/gi_policy", methods=["GET"])
@policy_bp.route("/gi_policy/", methods=["GET"])
def index():
    return render_template(
        "policy.html",
        active_page="policy",
        lan_ip=config.LAN_IP,
        port=config.PORT,
        providers=PROVIDERS,
        company_modes=COMPANY_MODES,
    )


@policy_bp.route("/policy/api/fields", methods=["GET"])
def api_fields():
    fields = load_policy_fields()
    return jsonify({"fields": [{"key": k, "label": l} for k, l in fields]})


@policy_bp.route("/policy/api/providers", methods=["GET"])
def api_providers():
    return jsonify({"providers": PROVIDERS})


@policy_bp.route("/policy/api/process", methods=["POST"])
def api_process():
    uploaded_files = request.files.getlist("files")
    if not uploaded_files or not uploaded_files[0].filename:
        uploaded_files = request.files.getlist("file") or request.files.getlist("policy") or request.files.getlist("policies")

    if not uploaded_files or not uploaded_files[0].filename:
        return jsonify({"error": "No files uploaded"}), 400

    provider_name = request.form.get("provider", "").strip() or "AzureAI"
    preset = PROVIDERS.get(provider_name, PROVIDERS["AzureAI"])
    api_key = request.form.get("api_key", "").strip()

    company_mode = request.form.get("company_mode", "").strip() or "ZUNO"
    auto_detect = request.form.get("auto_detect", "false").lower() in ("true", "1", "on")
    fresh_workbook = request.form.get("fresh_workbook", "false").lower() in ("true", "1", "on")

    max_pages_raw = request.form.get("max_pages", "0")
    try:
        max_pages = int(max_pages_raw)
    except ValueError:
        max_pages = 0

    api_base_url = preset.get("base_url", "")
    model_name = preset.get("model", "")
    azure_api_version = preset.get("api_version", DEFAULT_AZURE_API_VERSION)

    if provider_name in ("AzureAI", "Azure OpenAI"):
        endpoint_override = request.form.get("azure_endpoint", "").strip()
        deployment_override = request.form.get("azure_deployment", "").strip()
        version_override = request.form.get("azure_version", "").strip()
        if endpoint_override:
            api_base_url = endpoint_override
        if deployment_override:
            model_name = deployment_override
        if version_override:
            azure_api_version = version_override
    elif provider_name == "Custom / Other":
        api_base_url = request.form.get("custom_base_url", "").strip()
        model_name = request.form.get("custom_model_name", "").strip()

    if not api_key:
        api_key = _server_api_key(api_base_url, preset)
    if not api_key and provider_name != "Custom / Other":  # local endpoints may need no key
        return jsonify({"error": _MISSING_KEY_ERROR}), 400

    # Read files into memory for worker thread
    file_data_list = []
    for f in uploaded_files:
        if f and f.filename:
            file_data_list.append((f.filename, f.read()))

    if not file_data_list:
        return jsonify({"error": "No valid PDF files"}), 400

    job = job_manager.create_job("policy_reader")
    print(f"[POLICY READER] Job {job.id} started: {len(file_data_list)} file(s), provider={provider_name}, auto_detect={auto_detect}, fresh_wb={fresh_workbook}", flush=True)

    def _worker(job_obj: Job):
        total = len(file_data_list)
        extracted_policies = [None] * total
        saved_sheets = set()
        completed_count = 0

        job_obj.add_log(f"Starting parallel extraction for {total} document(s) using {provider_name} ({model_name}).")
        if auto_detect:
            job_obj.add_log("Auto-detect insurer mode enabled.")
        if fresh_workbook:
            job_obj.add_log("Single-company workbook creation enabled.")

        # Determine target workbook path
        custom_target_path = None
        if fresh_workbook:
            clean_name = f"{company_mode.replace(' ', '_')}Policies.xlsx"
            custom_target_path = config.DATA_DIR / clean_name
            # Fresh copy of template; keep the previous workbook instead of overwriting it
            # Copy (not rename) the old file first, so a failed template copy leaves it in place
            try:
                with _excel_lock:
                    if custom_target_path.exists():
                        _BACKUP_DIR.mkdir(exist_ok=True)
                        stem = custom_target_path.stem
                        backup = _BACKUP_DIR / f"{stem}.{time.strftime('%Y%m%d-%H%M%S')}.bak.xlsx"
                        shutil.copy2(custom_target_path, backup)
                        for old in sorted(_BACKUP_DIR.glob(f"{stem}.*.bak.xlsx"))[:-_BACKUPS_KEPT]:
                            old.unlink(missing_ok=True)
                        job_obj.add_log(f"Previous workbook kept as backups/{backup.name}")
                    if config.DEMO_COLUMNS_WORKBOOK.exists():
                        shutil.copy2(config.DEMO_COLUMNS_WORKBOOK, custom_target_path)
            except Exception as exc:
                job_obj.add_log(f"Template init note: {exc}")

        def process_single(idx: int, filename: str, raw_bytes: bytes):
            nonlocal completed_count
            if job_obj.stop_requested:
                return None

            job_obj.add_log(f"Extracting text layer from '{filename}'...")
            eff_pages = None if max_pages == 0 else max_pages
            raw_text, page_count, pdf_warnings = extract_pdf_text(raw_bytes, max_pages=eff_pages)

            if pdf_warnings:
                for w in pdf_warnings:
                    job_obj.add_log(f"{w}")

            if not raw_text.strip():
                job_obj.add_log(f"'{filename}' has no extractable text layer.")
                res = {
                    "file_name": filename,
                    "error": "No extractable text found in PDF (scanned image or empty).",
                }
                extracted_policies[idx] = res
                with _excel_lock:
                    completed_count += 1
                    job_obj.update_progress(completed_count / total, f"Processed {completed_count}/{total} files.")
                return res

            eff_mode = company_mode
            if auto_detect:
                detected = detect_insurer_from_text(raw_text)
                eff_mode = detected
                job_obj.add_log(f"[Auto-Detect] '{filename}' -> Detected Insurer: {detected}")

            t_start = time.perf_counter()
            inference_result = run_inference_api(
                api_key=api_key,
                api_base_url=api_base_url,
                model_name=model_name,
                policy_text=raw_text,
                max_chars=None,
                log_placeholder=lambda msg: job_obj.add_log(f"  [{filename}] {msg}"),
                azure_api_version=azure_api_version,
                company_mode=eff_mode,
            )
            elapsed = time.perf_counter() - t_start
            inference_result["file_name"] = filename
            inference_result["raw_text"] = raw_text
            inference_result["detected_insurer"] = eff_mode

            sheet_name = determine_policy_sheet_name(eff_mode, inference_result)
            with _excel_lock:
                saved_sheets.add(sheet_name)
                try:
                    out_path = append_result_to_excel(
                        filename,
                        inference_result,
                        output_excel=custom_target_path or config.DEFAULT_POLICY_OUTPUT,
                        sheet_name=sheet_name
                    )
                    job_obj.add_log(f"Saved '{filename}' to {out_path.name} [{sheet_name}]")
                except Exception as exc:
                    job_obj.add_log(f"Could not save '{filename}' to Excel: {exc}")

                completed_count += 1
                job_obj.update_progress(completed_count / total, f"Finished {filename} ({completed_count}/{total}).")

            print(f"[POLICY READER] Processed '{filename}' in {elapsed:.1f}s ({eff_mode}) -> Saved to {sheet_name}", flush=True)
            extracted_policies[idx] = inference_result
            return inference_result

        # Execute parallel processing
        max_workers = min(4, total)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(process_single, i, fn, b)
                for i, (fn, b) in enumerate(file_data_list)
            ]
            concurrent.futures.wait(futures)

        valid_policies = [p for p in extracted_policies if p is not None]
        final_wb_name = custom_target_path.name if custom_target_path else config.DEFAULT_POLICY_OUTPUT.name

        job_obj.result = {
            "policies": valid_policies,
            "saved_workbook": final_wb_name,
            "download_url": f"/policy/api/download_excel?file={final_wb_name}",
            "saved_sheets": list(saved_sheets),
            "total": len(valid_policies),
        }
        job_obj.update_progress(1.0, "All documents processed.")
        job_obj.add_log(f"Parallel batch complete: {len(valid_policies)} policy document(s) processed in {final_wb_name}.")
        print(f"[POLICY READER] Completed job {job_obj.id} ({len(valid_policies)} policies processed)", flush=True)

    job_manager.start_job_thread(job, _worker)
    return jsonify({"job_id": job.id, "status": "started"})


@policy_bp.route("/policy/api/ocr", methods=["GET", "POST", "OPTIONS"])
@policy_bp.route("/gi_policy/api/ocr", methods=["GET", "POST", "OPTIONS"])
@policy_bp.route("/api/policy/ocr", methods=["GET", "POST", "OPTIONS"])
@policy_bp.route("/api/policy", methods=["GET", "POST", "OPTIONS"])
def api_policy_ocr():
    """
    JSON API for Insurance Policy Extraction matching unified_server.py contract.
    """
    if request.method == "OPTIONS":
        resp = jsonify({"status": "ok"})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    if request.method == "GET":
        resp = jsonify({
            "status": "online",
            "endpoint": "POST /policy/api/ocr",
            "message": (
                "Policy extraction API. POST multipart/form-data with one or more PDFs. "
                "All optional fields match the Streamlit / Web UI controls."
            ),
            "file_fields": {
                "files / policies": "One or more policy PDFs (preferred batch field names)",
                "file / policy / card": "Accepted single-file aliases",
            },
            "optional_fields": {
                "company / company_mode": "'ZUNO', 'HDFC ERGO', 'TATA AIG', 'GO DIGIT', or 'ICICI Lombard' (default: auto-detect)",
                "api_key": "LLM provider API key (default: server Azure AI key, only with the default endpoint)",
                "api_base_url": "Provider base URL (default: Azure AI endpoint)",
                "model_name": "Model identifier, e.g. 'gpt-5-mini', 'gpt-4o-mini' (default: AzureAI model)",
                "azure_api_version": "Azure REST API version string (Azure endpoints only)",
                "max_pages": "Pages to read per PDF (0 means all pages, default: 0)",
            },
        })
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    uploaded_files = (
        request.files.getlist("files")
        or request.files.getlist("policies")
        or request.files.getlist("file")
        or request.files.getlist("policy")
        or request.files.getlist("card")
    )
    uploaded_files = [f for f in uploaded_files if f and f.filename]
    if not uploaded_files:
        _log_event(pol_err_log, "POLICY_API", 400, "file=(none) | error=No file uploaded")
        resp = jsonify({
            "error": "No file uploaded. Send PDF(s) as field name 'files' (batch) or 'file' / 'policy' / 'card' (single)."
        })
        resp.status_code = 400
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    _VALID_COMPANY_MODES = {"ZUNO", "HDFC ERGO", "TATA AIG", "GO DIGIT", "ICICI Lombard"}
    raw_company = (request.form.get("company") or request.form.get("company_mode") or "").strip()
    if raw_company:
        company_mode = raw_company.upper().replace("_", " ")
        if company_mode in {"HDFCERGO", "HDFC-ERGO"}:
            company_mode = "HDFC ERGO"
        elif company_mode in {"TATA", "TATAAIG", "TATA-AIG"}:
            company_mode = "TATA AIG"
        elif company_mode in {"GODIGIT", "GO-DIGIT"}:
            company_mode = "GO DIGIT"
        elif company_mode in {"ICICI", "ICICILOMBARD", "ICICI-LOMBARD", "ICLB"}:
            company_mode = "ICICI Lombard"
        if company_mode not in _VALID_COMPANY_MODES:
            resp = jsonify({
                "error": f"Invalid 'company' value {raw_company!r}. Accepted: {sorted(_VALID_COMPANY_MODES)}. Omit to auto-detect."
            })
            resp.status_code = 400
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp
    else:
        company_mode = None

    api_base_url = (request.form.get("api_base_url") or "").strip() or _POLICY_DEFAULT_ENDPOINT
    api_key = (request.form.get("api_key") or "").strip() or _server_api_key(api_base_url)
    if not api_key and _norm_url(api_base_url) == _norm_url(_POLICY_DEFAULT_ENDPOINT):
        resp = jsonify({"error": _MISSING_KEY_ERROR})
        resp.status_code = 400
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    model_name = (request.form.get("model_name") or "").strip() or _POLICY_DEFAULT_MODEL
    azure_api_version = (request.form.get("azure_api_version") or "").strip() or DEFAULT_AZURE_API_VERSION

    try:
        max_pages_raw = int(request.form.get("max_pages") or 0)
        effective_max_pages = None if max_pages_raw <= 0 else max_pages_raw
    except (ValueError, TypeError):
        resp = jsonify({"error": "'max_pages' must be an integer (0 = all pages)."})
        resp.status_code = 400
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    results = []
    for uploaded in uploaded_files:
        entry: dict = {"filename": uploaded.filename}
        try:
            raw_bytes = uploaded.read()
            policy_text, page_count, warnings = extract_pdf_text(raw_bytes, max_pages=effective_max_pages)
            entry["page_count"] = page_count

            eff_mode = company_mode
            if not eff_mode:
                eff_mode = detect_insurer_from_text(policy_text)

            entry["company_mode"] = eff_mode
            entry["warnings"] = warnings

            if not policy_text.strip():
                _log_event(pol_err_log, "POLICY_API", 400, f"file={uploaded.filename!r} | error=No extractable text found")
                entry["status"] = "error"
                entry["error"] = "No extractable text found in this PDF."
                results.append(entry)
                continue

            parsed = run_inference_api(
                api_key=api_key,
                api_base_url=api_base_url,
                model_name=model_name,
                policy_text=policy_text,
                max_chars=None,
                log_placeholder=None,
                azure_api_version=azure_api_version,
                company_mode=eff_mode,
            )

            entry["status"] = "success"
            entry["needs_review"] = parsed.get("needs_review", False) if isinstance(parsed, dict) else False
            entry["parsed"] = parsed

            # Flatten parsed keys onto entry for direct root access
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if k not in entry:
                        entry[k] = v

            sheet_name = determine_policy_sheet_name(eff_mode, parsed)
            try:
                with _excel_lock:
                    append_result_to_excel(uploaded.filename, parsed, sheet_name=sheet_name)
            except Exception as exc:
                entry["excel_error"] = f"Could not save to Excel: {exc}"
                _log_event(pol_err_log, "POLICY_API", 500, f"file={uploaded.filename!r} | excel_error={exc}")

            _log_event(
                pol_req_log, "POLICY_API", 200,
                f"file={uploaded.filename!r} | policy={parsed.get('policy_number')!r} | needs_review={entry['needs_review']}"
            )
            print(f"[POLICY OCR API] '{uploaded.filename}' -> Policy: {parsed.get('policy_number') or '(none)'} | Insurer: {parsed.get('insurer') or '(none)'}", flush=True)
            results.append(entry)
        except Exception as exc:
            _log_event(pol_err_log, "POLICY_API", 500, f"file={uploaded.filename!r} | error={exc}")
            entry["status"] = "error"
            entry["error"] = str(exc)
            results.append(entry)

    resp = jsonify(results)
    resp.status_code = 200
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@policy_bp.route("/policy/api/status/<job_id>", methods=["GET"])
def api_status(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    data = job.to_dict()
    if job.status == "completed" and job.result:
        data["result"] = job.result
    return jsonify(data)


@policy_bp.route("/policy/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id: str):
    ok = job_manager.cancel_job(job_id)
    return jsonify({"cancelled": ok})


@policy_bp.route("/policy/api/download_excel", methods=["GET"])
def api_download_excel():
    custom_filename = request.args.get("file", "").strip()
    if custom_filename:
        safe_name = secure_filename(custom_filename)
        if not safe_name:
            return jsonify({"error": "Invalid filename requested."}), 400
        
        # Check output directory and base directory
        candidate_output = config.OUTPUT_DIR / safe_name
        candidate_base = config.BASE_DIR / safe_name

        if candidate_output.exists():
            target_path = candidate_output
        elif candidate_base.exists():
            target_path = candidate_base
        else:
            target_path = config.DEFAULT_POLICY_OUTPUT
    else:
        target_path = config.DEFAULT_POLICY_OUTPUT

    if not target_path.exists():
        return jsonify({"error": f"Workbook '{target_path.name}' not found."}), 404

    return send_file(
        str(target_path),
        as_attachment=True,
        download_name=target_path.name,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
