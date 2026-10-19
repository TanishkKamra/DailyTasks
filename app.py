#!/usr/bin/env python3
"""
app.py, Unified OCR Hub Flask Server.
Hosts PAN OCR, Aadhaar OCR, Policy Reader, LP_Delete, LP_DateWiseData,
and Dashboard on a single server, binding to 0.0.0.0:5000.
"""

import argparse
import logging
import sys
import time
import uuid
from pathlib import Path

# Ensure UTF-8 output on Windows consoles to prevent charmap UnicodeEncodeErrors
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from flask import Flask, g, render_template, request

import config
from config import safe_print
from core.aocr_core import OCR_ENGINE as AADHAAR_ENGINE
from core.pan_ocr import OCR_ENGINE as PAN_ENGINE
from routes.aadhar import aadhar_bp
from routes.dashboard import dashboard_bp
from routes.lp_datewise import lp_datewise_bp
from routes.lp_delete import lp_delete_bp
from routes.pan import pan_bp
from routes.policy import policy_bp
from routes.filesharing import filesharing_bp

# Disable default Werkzeug HTTP access log spam
logging.getLogger("werkzeug").setLevel(logging.ERROR)

api_logger = logging.getLogger("unified.api_requests")
if not api_logger.handlers:
    api_logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(config.LOG_DIR / "api_requests.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    api_logger.addHandler(file_handler)
    api_logger.propagate = False


def _wants_json_error(path: str) -> bool:
    """True if this path is a JSON/AJAX API endpoint that should get a JSON
    error body on failure, rather than the HTML dashboard page.

    The old check only matched "/policy/api", "/pan/api", "/aadhar/api" and
    a bare "/api/" prefix. That silently missed several real API endpoints:
      - /lp_delete/api/... and /lp_datewise/api/...  (prefix is the blueprint
        name, not literally "/api/")
      - /filesharing/... (files, upload, download, delete, rename, move,
        stream) which never contain "/api/" in the path at all.
    Those requests are made via fetch()/JS expecting JSON, and previously
    got the full HTML dashboard page back on error, which the front-end code
    can't parse as JSON, turning a real backend error into a confusing
    "Unexpected token '<'" in the browser console.
    """
    if "/api/" in path or path.startswith(("/policy/api", "/pan/api", "/aadhar/api")):
        return True
    # Everything under /filesharing/ is a JSON endpoint except the two page
    # routes that render the file-sharing UI itself.
    if path.startswith("/filesharing/") or path == "/filesharing":
        return path not in ("/filesharing", "/filesharing/")
    return False


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH

    @app.before_request
    def _mark_request_start():
        g.request_id = uuid.uuid4().hex[:8]
        g.request_started_at = time.perf_counter()

    @app.after_request
    def _attach_request_headers_and_log(response):
        req_id = getattr(g, "request_id", None)
        if req_id:
            response.headers["X-Request-ID"] = req_id

        # Standard defensive security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        path = request.path
        ignored_prefixes = ("/api/logs", "/api/stats", "/api/recent_jobs", "/static", "/favicon.ico")
        if not any(path.startswith(prefix) for prefix in ignored_prefixes):
            elapsed = (time.perf_counter() - getattr(g, "request_started_at", time.perf_counter())) * 1000.0
            status = response.status_code
            status_text = "OK" if status < 400 else f"STATUS {status}"
            tag = "REQUEST" if status < 400 else "ERROR"
            now_str = time.strftime("%Y-%m-%d %H:%M:%S")
            client_ip = request.remote_addr or "127.0.0.1"

            # Determine service name
            service_name = "CORE"
            if "/pan" in path:
                service_name = "PAN OCR"
            elif "/aadhar" in path:
                service_name = "Aadhaar OCR"
            elif "/policy" in path or "/gi_policy" in path:
                service_name = "Policy Reader"
            elif "/lp-delete" in path or "/lp_delete" in path or "/excelkeyword" in path:
                service_name = "LP_Delete"
            elif "/lp-datewise" in path or "/lp_datewise" in path or "/lp_datewisedata" in path:
                service_name = "LP_DateWiseData"
            elif "/filesharing" in path:
                service_name = "File Share"

            # Print to stdout for desktop GUI streaming
            safe_print(f"[{now_str}] [{service_name}] {request.method} {path} -> {status} {status_text} ({elapsed:.0f}ms)", flush=True)

            # Write formatted line to unified api_requests.log
            meta = (
                f"req={req_id} | ip={client_ip} | host={request.host} | method={request.method} "
                f"| path={path} | url={request.base_url} | public_url={config.BASE_URL}{path}"
            )
            api_logger.info(f"{meta} | svc={service_name} | status={status} | ms={elapsed:.1f} | endpoint={path}")

        return response

    @app.context_processor
    def _inject_globals():
        return {
            "lan_ip": config.LAN_IP,
            "port": config.PORT,
            "base_url": config.BASE_URL,
            "pan_engine": PAN_ENGINE,
            "aadhar_engine": AADHAAR_ENGINE,
        }

    @app.errorhandler(404)
    def _page_not_found(e):
        if _wants_json_error(request.path):
            return {"error": f"Endpoint not found: {request.path}"}, 404
        return render_template("dashboard.html", active_page="dashboard"), 404

    @app.errorhandler(400)
    def _bad_request(e):
        # Silently swallow TLS-probe connections (client accidentally sent
        # https:// to our plain-HTTP port).  Werkzeug logs these as:
        #   code 400, message Bad request version ('\x13\x01...')
        # We return a plain 400 so the socket closes cleanly rather than
        # letting the error propagate into a misleading 500.
        msg = str(e)
        if _wants_json_error(request.path):
            return {"error": "Bad Request", "details": msg}, 400
        return render_template("dashboard.html", active_page="dashboard"), 400

    @app.errorhandler(413)
    def _file_too_large(e):
        return {"error": "Uploaded file is too large (max 50 MB allowed)."}, 413

    @app.errorhandler(500)
    def _internal_server_error(e):
        if _wants_json_error(request.path):
            return {"error": "Internal Server Error"}, 500
        return render_template("dashboard.html", active_page="dashboard"), 500

    @app.errorhandler(Exception)
    def _unhandled_exception(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):  # 405 etc. keep their own status
            if _wants_json_error(request.path):
                return {"error": e.description or e.name}, e.code or 500
            return e
        import traceback
        tb = traceback.format_exc()
        # Always print the real traceback to stdout so it's visible in the console
        safe_print(f"[ERROR] Unhandled exception on {request.method} {request.path}:\n{tb}", flush=True)
        logging.getLogger("unified.api_requests").error(
            f"Unhandled Exception on {request.path}: {e}\n{tb}"
        )
        if _wants_json_error(request.path):
            # Details stay in the server log; clients get a generic message.
            return {"error": "Internal Server Error"}, 500
        return render_template("dashboard.html", active_page="dashboard"), 500

    # Register Blueprints
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(pan_bp)
    app.register_blueprint(aadhar_bp)
    app.register_blueprint(policy_bp)
    app.register_blueprint(lp_delete_bp)
    app.register_blueprint(lp_datewise_bp)
    app.register_blueprint(filesharing_bp)

    return app


app = create_app()


def print_banner(host: str, port: int) -> None:
    lan_url = f"http://{config.LAN_IP}:{port}"
    safe_print()
    safe_print("=" * 64)
    safe_print("           [DAILY TASKS] Unified Flask Server")
    safe_print("=" * 64)
    safe_print(f"  Listening on      :  http://{host}:{port}")
    safe_print(f"  LAN Access URL    :  {lan_url}")
    safe_print(f"  PAN OCR Engine    :  {PAN_ENGINE}")
    safe_print(f"  Aadhaar Engine    :  {AADHAAR_ENGINE}")
    safe_print("-" * 64)
    safe_print(f"  Dashboard         :  {lan_url}/dashboard")
    safe_print(f"  PAN OCR           :  {lan_url}/pan")
    safe_print(f"  Aadhaar OCR       :  {lan_url}/aadhar")
    safe_print(f"  Policy Reader     :  {lan_url}/policy")
    safe_print(f"  LP_Delete         :  {lan_url}/lp-delete")
    safe_print(f"  LP_DateWiseData   :  {lan_url}/lp-datewise")
    safe_print(f"  File Share        :  {lan_url}/filesharing")
    safe_print("=" * 64)
    safe_print()


def main():
    parser = argparse.ArgumentParser(description="Daily Tasks Unified Flask Server")
    parser.add_argument("--port", type=int, default=config.PORT, help="Port to bind (default: 5000)")
    parser.add_argument("--host", type=str, default=config.HOST, help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    config.PORT = args.port
    config.HOST = args.host

    print_banner(args.host, args.port)

    if args.debug:
        # Keep Flask's dev server only for local debugging (auto-reload, debugger).
        app.run(host=args.host, port=args.port, debug=True, threaded=True)
    else:
        # Werkzeug's dev server does not support chunked Transfer-Encoding
        # requests (common with .NET HttpClient/StreamContent uploads), which
        # causes a generic 500 before the request ever reaches Flask/our own
        # error handlers. Waitress is a production-grade WSGI server that
        # handles this correctly and is far more stable under concurrent API
        # load.
        try:
            from waitress import serve
        except ImportError:
            safe_print(
                "[WARNING] 'waitress' is not installed. Falling back to Flask's "
                "dev server, which does NOT support chunked-encoded API requests "
                "(e.g. some .NET HttpClient uploads) and may return generic 500 "
                "errors for those callers. Install it with:\n"
                "    pip install waitress\n",
                flush=True,
            )
            app.run(host=args.host, port=args.port, debug=False, threaded=True)
            return

        serve(app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
