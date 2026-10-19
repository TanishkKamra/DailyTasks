"""
routes/lp_delete.py, LP_Delete web UI and asynchronous workbook processing endpoints.
"""

import io
from flask import Blueprint, jsonify, render_template, request, send_file

import config
from core.job_manager import Job, job_manager
from core.lp_delete import get_columns, get_row_count, get_sheet_names, process_lp_delete

lp_delete_bp = Blueprint("lp_delete", __name__)


@lp_delete_bp.route("/lp-delete", methods=["GET"])
@lp_delete_bp.route("/LP_Delete", methods=["GET"])
@lp_delete_bp.route("/LP_Delete/", methods=["GET"])
@lp_delete_bp.route("/excelkeyword", methods=["GET"])
@lp_delete_bp.route("/excelkeyword/", methods=["GET"])
def index():
    return render_template(
        "lp_delete.html",
        active_page="lp_delete",
        lan_ip=config.LAN_IP,
        port=config.PORT,
    )


@lp_delete_bp.route("/lp_delete/api/inspect", methods=["POST"])
def api_inspect():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        raw_bytes = file.read()
        sheets = get_sheet_names(raw_bytes)
        return jsonify({"sheets": sheets})
    except Exception as exc:
        return jsonify({"error": f"Could not read workbook: {exc}"}), 400


@lp_delete_bp.route("/lp_delete/api/columns", methods=["POST"])
def api_columns():
    file = request.files.get("file")
    sheet_name = request.form.get("sheet_name")
    if not file or not sheet_name:
        return jsonify({"error": "Missing file or sheet_name"}), 400
    try:
        raw_bytes = file.read()
        columns = get_columns(raw_bytes, sheet_name)
        row_count = get_row_count(raw_bytes, sheet_name)
        return jsonify({"columns": columns, "row_count": row_count})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@lp_delete_bp.route("/lp_delete/api/process", methods=["POST"])
def api_process():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded. Send workbook under field 'file'."}), 400

    file_bytes = file.read()
    filename = file.filename

    sheets = get_sheet_names(file_bytes)
    sheet_edit = request.form.get("sheet_edit")
    sheet_keywords = request.form.get("sheet_keywords")
    target_col = request.form.get("target_col")
    new_col_name = request.form.get("new_col_name", "Contains_Keyword").strip() or "Contains_Keyword"
    whole_word = request.form.get("whole_word", "true").lower() == "true"

    if not sheet_edit:
        sheet_edit = sheets[0] if sheets else "Sheet1"
    if not sheet_keywords:
        lower_sheets = [s.lower() for s in sheets]
        kw_idx = next((i for i, s in enumerate(lower_sheets) if any(k in s for k in ("keyword", "key", "filter", "search"))), -1)
        if kw_idx >= 0:
            sheet_keywords = sheets[kw_idx]
        elif len(sheets) > 1:
            sheet_keywords = sheets[1]
        else:
            sheet_keywords = sheet_edit

    if not target_col:
        cols = get_columns(file_bytes, sheet_edit)
        lower_cols = [c.lower() for c in cols]
        best_col_idx = next((i for i, c in enumerate(lower_cols) if any(k in c for k in ("text", "narration", "desc", "detail", "name", "remark", "comment"))), 0)
        target_col = cols[best_col_idx] if cols else "Text"

    job = job_manager.create_job("lp_delete")
    print(f"[LP-DELETE] Starting job {job.id} for '{filename}' (Sheet: {sheet_edit}, Target: {target_col})", flush=True)

    def _worker(job_obj: Job):
        job_obj.add_log(f"Started LP_Delete job for '{filename}'")
        res = process_lp_delete(
            file_bytes=file_bytes,
            sheet_edit=sheet_edit,
            sheet_keywords=sheet_keywords,
            target_col=target_col,
            new_col_name=new_col_name,
            whole_word=whole_word,
            progress_callback=lambda p, text: job_obj.update_progress(p, text),
            log_callback=lambda msg: job_obj.add_log(msg),
            is_cancelled=lambda: job_obj.stop_requested,
        )
        if res.get("cancelled"):
            job_obj.status = "cancelled"
            job_obj.add_log("Job stopped.")
            print(f"[LP-DELETE] Job {job_obj.id} stopped.", flush=True)
            return

        job_obj.output_data = res.pop("output_bytes")
        job_obj.output_filename = f"updated_{filename}"
        job_obj.result = res
        job_obj.add_log(f"Completed: {res['match_count']:,} matches found.")
        print(f"[LP-DELETE] Completed job {job_obj.id}: {res['match_count']:,} matches found.", flush=True)

    job_manager.start_job_thread(job, _worker)
    return jsonify({"job_id": job.id, "status": "started", "sheet_edit": sheet_edit, "sheet_keywords": sheet_keywords, "target_col": target_col})


@lp_delete_bp.route("/lp_delete/api/status/<job_id>", methods=["GET"])
def api_status(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job.to_dict())


@lp_delete_bp.route("/lp_delete/api/result/<job_id>", methods=["GET"])
def api_result(job_id: str):
    job = job_manager.get_job(job_id)
    if not job or not job.result:
        return jsonify({"error": "Result not found"}), 404
    return jsonify(job.result)


@lp_delete_bp.route("/lp_delete/api/download/<job_id>", methods=["GET"])
def api_download(job_id: str):
    job = job_manager.get_job(job_id)
    if not job or not job.output_data:
        return "File not ready or expired", 404
    return send_file(
        io.BytesIO(job.output_data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=job.output_filename or "updated_workbook.xlsx",
    )


@lp_delete_bp.route("/lp_delete/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id: str):
    ok = job_manager.cancel_job(job_id)
    return jsonify({"cancelled": ok})
