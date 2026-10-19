"""
routes/lp_datewise.py, LP_DateWiseData routes for stratified random sampling.
"""

import io
from flask import Blueprint, jsonify, render_template, request, send_file

import config
from core.job_manager import Job, job_manager
from core.lp_datewise import get_columns, get_sheet_names, process_lp_datewise

lp_datewise_bp = Blueprint("lp_datewise", __name__)


@lp_datewise_bp.route("/lp-datewise", methods=["GET"])
@lp_datewise_bp.route("/lp_datewise", methods=["GET"])
@lp_datewise_bp.route("/lp_datewise/", methods=["GET"])
@lp_datewise_bp.route("/LP_DateWiseData", methods=["GET"])
@lp_datewise_bp.route("/LP_DateWiseData/", methods=["GET"])
@lp_datewise_bp.route("/lp_datewisedata", methods=["GET"])
@lp_datewise_bp.route("/lp_datewisedata/", methods=["GET"])
def index():
    return render_template(
        "lp_datewise.html",
        active_page="lp_datewise",
        lan_ip=config.LAN_IP,
        port=config.PORT,
    )


@lp_datewise_bp.route("/lp_datewise/api/inspect", methods=["POST"])
def api_inspect():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        raw_bytes = file.read()
        sheets = get_sheet_names(raw_bytes, file.filename)
        first_sheet = sheets[0] if sheets else None
        columns = get_columns(raw_bytes, file.filename, first_sheet)
        return jsonify({"sheets": sheets, "columns": columns})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@lp_datewise_bp.route("/lp_datewise/api/columns", methods=["POST"])
def api_columns():
    file = request.files.get("file")
    sheet_name = request.form.get("sheet_name")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        raw_bytes = file.read()
        columns = get_columns(raw_bytes, file.filename, sheet_name)
        return jsonify({"columns": columns})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@lp_datewise_bp.route("/lp_datewise/api/process", methods=["POST"])
def api_process():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded. Send workbook under field 'file'."}), 400

    file_bytes = file.read()
    filename = file.filename
    sheet_name = request.form.get("sheet_name")
    datetime_col = request.form.get("datetime_col")
    target_total_raw = request.form.get("target_total", "10000")
    result_column_name = request.form.get("result_column_name", "Selected").strip() or "Selected"
    seed = request.form.get("seed", "").strip()

    if seed:
        try:
            int(seed)
        except ValueError:
            return jsonify({"error": "Seed must be an integer"}), 400

    if not datetime_col:
        cols = get_columns(file_bytes, filename, sheet_name)
        lower_cols = [c.lower() for c in cols]
        date_keywords = ("date", "time", "timestamp", "datetime", "created", "call", "txn", "trans")
        best_idx = next((i for i, c in enumerate(lower_cols) if any(kw in c for kw in date_keywords)), 0)
        datetime_col = cols[best_idx] if cols else "Date"

    try:
        target_total = int(target_total_raw)
    except ValueError:
        target_total = 10000

    job = job_manager.create_job("lp_datewise")
    print(f"[LP-DATEWISE] Starting sampling job {job.id} for '{filename}' (Target: {target_total:,}, Column: {datetime_col})", flush=True)

    def _worker(job_obj: Job):
        job_obj.add_log(f"Starting stratified sampling for '{filename}'...")
        res = process_lp_datewise(
            file_bytes=file_bytes,
            filename=filename,
            datetime_col=datetime_col,
            target_total=target_total,
            result_column_name=result_column_name,
            seed_input=seed,
            sheet_name=sheet_name,
            progress_callback=lambda p, text: job_obj.update_progress(p, text),
            log_callback=lambda msg: job_obj.add_log(msg),
            is_cancelled=lambda: job_obj.stop_requested,
        )
        if res.get("cancelled"):
            job_obj.status = "cancelled"
            job_obj.add_log("Sampling cancelled by user.")
            print(f"[LP-DATEWISE] Job {job_obj.id} cancelled.", flush=True)
            return

        job_obj.output_data = res.pop("output_bytes")
        job_obj.output_filename = f"sampled_{filename}" if not filename.lower().endswith(".csv") else filename.replace(".csv", "_sampled.xlsx")
        job_obj.result = res
        job_obj.add_log(f"Sampling finished: {res['total_selected']:,} records marked.")
        print(f"[LP-DATEWISE] Completed job {job_obj.id}: {res['total_selected']:,} records selected.", flush=True)

    job_manager.start_job_thread(job, _worker)
    return jsonify({"job_id": job.id, "status": "started", "datetime_col": datetime_col, "target_total": target_total})


@lp_datewise_bp.route("/lp_datewise/api/status/<job_id>", methods=["GET"])
def api_status(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job.to_dict())


@lp_datewise_bp.route("/lp_datewise/api/result/<job_id>", methods=["GET"])
def api_result(job_id: str):
    job = job_manager.get_job(job_id)
    if not job or not job.result:
        return jsonify({"error": "Result not found"}), 404
    return jsonify(job.result)


@lp_datewise_bp.route("/lp_datewise/api/download/<job_id>", methods=["GET"])
def api_download(job_id: str):
    job = job_manager.get_job(job_id)
    if not job or not job.output_data:
        return "File not ready or expired", 404
    return send_file(
        io.BytesIO(job.output_data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=job.output_filename or "sampled_data.xlsx",
    )
