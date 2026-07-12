"""Scanner page — WABA health scan across every AdsPower profile.

Opens each profile one at a time via the local agent, assesses every WABA it
can reach (approved/appealable/in_review/permanent/restricted), and
optionally appeals appealable ones automatically. Real-time per-profile save
+ resume: a restarted scan skips profiles already recorded.
"""
from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user

from .. import db
from ..models import ScanWaba
from ..services import scan_service
from .agent_ws import is_agent_connected, push_to_agent, send_command_and_wait

bp = Blueprint("scanner", __name__, url_prefix="/scanner")


@bp.route("")
@login_required
def page():
    return render_template("scanner.html", title="Scanner")


@bp.route("/rows")
@login_required
def rows():
    wabas = ScanWaba.query.filter_by(user_id=current_user.id).order_by(ScanWaba.scanned_at.desc()).all()
    return jsonify({"rows": [w.to_dict() for w in wabas]})


@bp.route("/scan-start", methods=["POST"])
@login_required
def scan_start():
    if not is_agent_connected(current_user.id):
        return jsonify({"ok": False, "error": "Agente não conectado."}), 400
    body = request.get_json(silent=True) or {}
    auto_appeal = bool(body.get("auto_appeal", True))
    rescan = bool(body.get("rescan", False))
    job_id = scan_service.start_scan_job(current_user.id, auto_appeal=auto_appeal, rescan=rescan)
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/job/<int:job_id>")
@login_required
def job_status(job_id):
    state = scan_service.get_job(job_id)
    if not state:
        return jsonify({"ok": False, "error": "Job não encontrado."}), 404
    public_state = {k: v for k, v in state.items() if k != "stop_event"}
    return jsonify({"ok": True, **public_state})


@bp.route("/job/<int:job_id>/stop", methods=["POST"])
@login_required
def job_stop(job_id):
    found = scan_service.request_stop(job_id)
    return jsonify({"ok": True, "found": found})


@bp.route("/clear-history", methods=["POST"])
@login_required
def clear_history():
    if scan_service.has_active_job(current_user.id):
        return jsonify({"ok": False, "error": "Pare o scan atual antes de limpar o histórico."}), 400
    counts = scan_service.clear_scan_history(current_user.id)
    return jsonify({"ok": True, **counts})


@bp.route("/open/<profile_id>", methods=["POST"])
@login_required
def open_adspower(profile_id):
    if not is_agent_connected(current_user.id):
        return jsonify({"ok": False, "error": "Agente não conectado. Abra o cliente local primeiro."}), 400
    push_to_agent(current_user.id, {"type": "open_browser", "profile_id": profile_id, "cmd_id": None})
    return jsonify({"ok": True})


@bp.route("/appeal/<waba_id>", methods=["POST"])
@login_required
def appeal(waba_id):
    if not is_agent_connected(current_user.id):
        return jsonify({"ok": False, "error": "Agente não conectado."}), 400
    sw = ScanWaba.query.filter_by(user_id=current_user.id, waba_id=waba_id).first()
    if not sw:
        return jsonify({"ok": False, "error": "WABA não encontrada no scanner."}), 404

    res = send_command_and_wait(current_user.id, {
        "type": "appeal_waba",
        "profile_id": sw.profile_id,
        "business_id": sw.business_id,
        "waba_id": sw.waba_id,
    }, timeout=90.0)

    if res.get("ok") and res.get("appeal_sent"):
        sw.state = "in_review"
        sw.appeal_sent = True
        db.session.commit()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": res.get("error") or "Falha ao enviar apelação."})
