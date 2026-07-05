from flask import Blueprint, request, redirect, url_for, flash, jsonify, abort
from flask_login import login_required, current_user
from ..json_store import upsert_waba, update_waba, ensure_user_bms_file, load_user_bms
from ..services.meta import subscribe_waba_webhook
from ..services.sync_service import start_sync_job
from ..config import Config

bp = Blueprint("wabas", __name__, url_prefix="/wabas")

@bp.route("/add", methods=["POST"])
@login_required
def add():
    waba_id = (request.form.get("waba_id") or "").strip()
    token = (request.form.get("token") or "").strip()

    if not waba_id or not token:
        flash("Informe WABA ID e Token.", "error")
        return redirect(url_for("dashboard.dashboard"))

    ensure_user_bms_file(current_user.id)

    adspower_profile_id = (request.form.get("adspower_profile_id") or "").strip()
    serial_number = (request.form.get("serial_number") or "").strip()

    # Write/update in user's bms.json
    upsert_waba(current_user.id, waba_id=waba_id, token=token,
                adspower_profile_id=adspower_profile_id,
                serial_number=serial_number)

    # Subscribe app to webhook events for this WABA (best-effort)
    subscribe_waba_webhook(Config.META_API_VERSION, token, waba_id)

    # Kick a sync so phone numbers / templates / tier populate right away
    start_sync_job(current_user.id, Config.META_API_VERSION)

    flash("WABA adicionado — sincronizando dados da Meta.", "success")
    return redirect(url_for("dashboard.dashboard"))


@bp.route("/<waba_id>/data")
@login_required
def data(waba_id):
    bms = load_user_bms(current_user.id)
    entry = bms.get(str(waba_id))
    if not entry or not isinstance(entry, dict):
        abort(404)
    return jsonify({
        "waba_id": entry.get("waba_id", ""),
        "token": entry.get("token", ""),
        "adspower_profile_id": entry.get("adspower_profile_id", ""),
        "serial_number": entry.get("serial_number", ""),
    })


@bp.route("/<waba_id>/open-adspower", methods=["POST"])
@login_required
def open_adspower(waba_id):
    entry = load_user_bms(current_user.id).get(str(waba_id))
    if not isinstance(entry, dict):
        return jsonify({"ok": False, "error": "WABA não encontrada."}), 404

    profile_id = (entry.get("adspower_profile_id") or "").strip()
    if not profile_id:
        return jsonify({"ok": False, "error": "Esta WABA não tem um perfil AdsPower vinculado."}), 400

    from .agent_ws import is_agent_connected, push_to_agent
    if not is_agent_connected(current_user.id):
        return jsonify({"ok": False, "error": "Agente não conectado. Abra o cliente local primeiro."}), 400

    push_to_agent(current_user.id, {"type": "open_browser", "profile_id": profile_id, "cmd_id": None})
    return jsonify({"ok": True})


@bp.route("/edit", methods=["POST"])
@login_required
def edit():
    original_waba_id = (request.form.get("original_waba_id") or "").strip()
    waba_id = (request.form.get("waba_id") or "").strip()
    token = (request.form.get("token") or "").strip()

    if not original_waba_id or not waba_id or not token:
        flash("Informe WABA ID e Token.", "error")
        return redirect(url_for("dashboard.dashboard"))

    adspower_profile_id = (request.form.get("adspower_profile_id") or "").strip()
    serial_number = (request.form.get("serial_number") or "").strip()

    ok, err = update_waba(current_user.id, original_waba_id, waba_id, token,
                          adspower_profile_id=adspower_profile_id,
                          serial_number=serial_number)
    if not ok:
        flash(err, "error")
        return redirect(url_for("dashboard.dashboard"))

    subscribe_waba_webhook(Config.META_API_VERSION, token, waba_id)
    start_sync_job(current_user.id, Config.META_API_VERSION)

    flash("WABA atualizado — sincronizando dados da Meta.", "success")
    return redirect(url_for("dashboard.dashboard"))
