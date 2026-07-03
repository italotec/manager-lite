from flask import Blueprint, request, redirect, url_for, flash
from flask_login import login_required, current_user
from ..json_store import upsert_waba, ensure_user_bms_file
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

    # Write/update in user's bms.json
    upsert_waba(current_user.id, waba_id=waba_id, token=token,
                adspower_profile_id=adspower_profile_id)

    # Subscribe app to webhook events for this WABA (best-effort)
    subscribe_waba_webhook(Config.META_API_VERSION, token, waba_id)

    # Kick a sync so phone numbers / templates / tier populate right away
    start_sync_job(current_user.id, Config.META_API_VERSION)

    flash("WABA adicionado — sincronizando dados da Meta.", "success")
    return redirect(url_for("dashboard.dashboard"))
