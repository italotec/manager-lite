import time
from flask import (
    Blueprint,
    render_template,
    redirect,
    url_for,
    flash,
    request,
    jsonify,
)
from flask_login import login_required, current_user
from .. import db
from ..services.meta import templates_status_summary

from ..json_store import (
    ensure_user_bms_file,
    load_user_bms,
    save_waba_remarks,
    import_wabas,
    delete_wabas as delete_wabas_store,
)
from ..services.sync_service import (
    start_sync_job,
    get_job as get_sync_job,
    request_stop as sync_request_stop,
)

bp = Blueprint("dashboard", __name__)


@bp.route("/", methods=["GET"])
@login_required
def dashboard():
    ensure_user_bms_file(current_user.id)
    bms = load_user_bms(current_user.id)

    rows = []
    for key, data in (bms or {}).items():
        if not isinstance(data, dict):
            continue

        waba_id = str(data.get("waba_id") or "").strip()
        snap = data.get("snapshot", {}) or {}

        tpl_map = snap.get("template_status_map")
        t_counts = (
            templates_status_summary(list(tpl_map.values()))
            if isinstance(tpl_map, dict) and tpl_map
            else snap.get("template_counts")
        )
        rows.append({
            "waba_id": waba_id,
            "waba_name": snap.get("waba_name") or "—",
            "serial_number": data.get("serial_number") or "",
            "adspower_profile_id": data.get("adspower_profile_id") or "",
            "phone_numbers": snap.get("phone_numbers") or [],
            "t": t_counts or {
                "APPROVED": 0,
                "PENDING": 0,
                "PAUSED": 0,
                "REJECTED": 0,
                "DISABLED": 0,
                "OTHER": 0,
            },
            "last_sync_at": snap.get("last_sync_at") or 0,
            "status_label": snap.get("status_label") or "",
            "last_error": snap.get("last_error") or "",
            "ever_had_erro_generic": snap.get("ever_had_erro_generic", False),
            "disparou": bool(snap.get("disparou_at")) and (time.time() - (snap.get("disparou_at") or 0)) < 86400,
            "ultimo_disparo": snap.get("ultimo_disparo") or "",
            "health_ok": bool(snap.get("health_test_ok_at")) and (time.time() - (snap.get("health_test_ok_at") or 0)) < 86400,
            "card_added": bool(snap.get("card_added_at")),
            "card_last4": snap.get("card_last4") or "",
            "remarks": data.get("remarks") or "",
            "messaging_limit_tier": snap.get("messaging_limit_tier"),
        })

    return render_template("dashboard.html", title="Manager Lite", rows=rows)


@bp.route("/open-profiles")
@login_required
def open_profiles():
    from .agent_ws import get_open_profiles
    return jsonify({"open_profile_ids": list(get_open_profiles(current_user.id))})


@bp.route("/api-settings")
@login_required
def api_page():
    return render_template("api.html", title="API")


@bp.route("/dashboard/analytics", methods=["GET"])
@login_required
def dashboard_analytics():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from flask import current_app
    from ..services.meta import get_waba_analytics

    start_ts = request.args.get("start", type=int)
    end_ts = request.args.get("end", type=int)
    if not start_ts or not end_ts:
        return jsonify({"error": "Parâmetros start e end são obrigatórios."}), 400

    bms = load_user_bms(current_user.id) or {}
    api_version = current_app.config["META_API_VERSION"]

    wabas_disparadas = 0
    total_sent = 0
    total_delivered = 0
    errors = []

    entries = [(wid, data) for wid, data in bms.items() if isinstance(data, dict)]

    for _wid, data in entries:
        snap = data.get("snapshot", {}) or {}
        d_at = snap.get("disparou_at")
        if d_at and start_ts <= d_at <= end_ts:
            wabas_disparadas += 1

    def _fetch(wid, data):
        token = (data.get("token") or "").strip()
        if not token:
            return 0, 0, None
        analytics, err = get_waba_analytics(api_version, token, wid, start_ts, end_ts)
        if err:
            return 0, 0, f"{wid}: {err}"
        points = (analytics or {}).get("data_points") or []
        sent = sum(p.get("sent", 0) for p in points)
        delivered = sum(p.get("delivered", 0) for p in points)
        return sent, delivered, None

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_fetch, wid, data): wid for wid, data in entries}
        for future in as_completed(futures):
            try:
                sent, delivered, err = future.result()
                total_sent += sent
                total_delivered += delivered
                if err:
                    errors.append(err)
            except Exception as exc:
                errors.append(f"{futures[future]}: {exc}")

    return jsonify({
        "wabas_disparadas": wabas_disparadas,
        "total_sent": total_sent,
        "total_delivered": total_delivered,
        "waba_count": len(entries),
        "errors": errors,
    })


@bp.route("/sync-start", methods=["POST"])
@login_required
def sync_start():
    from flask import current_app
    ensure_user_bms_file(current_user.id)
    bms = load_user_bms(current_user.id)
    if not bms:
        return jsonify({"ok": False, "error": "Você não tem WABAs cadastrados."}), 400
    api_version = current_app.config["META_API_VERSION"]
    job_id = start_sync_job(current_user.id, api_version)
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/sync/job/<int:job_id>")
@login_required
def sync_job_status(job_id: int):
    state = get_sync_job(job_id)
    if state is None:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(state)


@bp.route("/sync/job/<int:job_id>/stop", methods=["POST"])
@login_required
def sync_job_stop(job_id: int):
    sync_request_stop(job_id)
    return jsonify({"ok": True})


@bp.route("/export-selected", methods=["POST"])
@login_required
def export_selected():
    """Returns {<bms.json key>: {waba_id, phone_number_id, token, templates: [""]}}
    for the selected WABAs — used by the dashboard's "copy selected" button."""
    ensure_user_bms_file(current_user.id)
    bms = load_user_bms(current_user.id)

    payload = request.get_json(silent=True) or {}
    waba_ids = payload.get("waba_ids") or []
    if not isinstance(waba_ids, list):
        return jsonify({"error": "invalid_payload"}), 400

    out = {}
    for original_key, entry in bms.items():
        if not isinstance(entry, dict):
            continue
        waba_id = str(entry.get("waba_id") or "").strip()
        if not waba_id or waba_id not in waba_ids:
            continue
        out[original_key] = {
            "waba_id": waba_id,
            "phone_number_id": str(entry.get("phone_number_id") or ""),
            "token": str(entry.get("token") or ""),
            "templates": [""],
        }

    return jsonify(out)


@bp.route("/export-wabas-download", methods=["POST"])
@login_required
def export_wabas_download():
    """Download the selected WABAs as a .json file in the full bms.json shape."""
    import json
    from datetime import datetime
    from flask import Response

    ensure_user_bms_file(current_user.id)
    bms = load_user_bms(current_user.id)

    payload = request.get_json(silent=True) or {}
    waba_ids = payload.get("waba_ids") or []
    if not isinstance(waba_ids, list):
        return jsonify({"error": "invalid_payload"}), 400

    wanted = set(str(w) for w in waba_ids)
    out = {}
    for key, entry in bms.items():
        if not isinstance(entry, dict):
            continue
        wid = str(entry.get("waba_id") or key).strip()
        if wid in wanted or str(key) in wanted:
            out[key] = entry

    body = json.dumps(out, indent=4, ensure_ascii=False)
    fn = f"wabas_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


@bp.route("/import-wabas", methods=["POST"])
@login_required
def import_wabas_route():
    """Merge WABAs from an uploaded bms.json-shaped file into the user's store."""
    import json

    f = request.files.get("wabas_file")
    if not f or not f.filename:
        return jsonify({"error": "Nenhum arquivo enviado."}), 400
    if not f.filename.lower().endswith(".json"):
        return jsonify({"error": "Envie um arquivo .json."}), 400

    try:
        incoming = json.load(f.stream)
    except Exception:
        return jsonify({"error": "Arquivo JSON inválido."}), 400

    if not isinstance(incoming, dict):
        return jsonify({"error": "O JSON deve ser um objeto de WABAs (como o bms.json)."}), 400

    ensure_user_bms_file(current_user.id)
    report = import_wabas(current_user.id, incoming)
    return jsonify(report)


@bp.route("/waba/<waba_id>/remarks", methods=["POST"])
@login_required
def save_remarks(waba_id):
    text = (request.get_json(silent=True) or {}).get("text", "")
    save_waba_remarks(current_user.id, waba_id, text)
    return jsonify({"ok": True})


@bp.route("/delete-wabas", methods=["POST"])
@login_required
def delete_wabas():
    payload = request.get_json(silent=True) or {}
    waba_ids = payload.get("waba_ids") or []
    if not isinstance(waba_ids, list) or not waba_ids:
        return jsonify({"error": "invalid_payload"}), 400

    deleted = delete_wabas_store(current_user.id, waba_ids)
    return jsonify({"deleted": deleted})


@bp.route("/regenerate-api-key", methods=["POST"])
@login_required
def regenerate_api_key():
    current_user.generate_api_key()
    db.session.commit()
    flash("Nova chave de API gerada com sucesso.", "success")
    return redirect(url_for("dashboard.api_page"))
