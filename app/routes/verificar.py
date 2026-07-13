"""Verificar page — "Vincular ao Manager" (Conectar) feature.

Lists AdsPower profiles from the "Verificar" group (synced by the local
agent) and lets the user bulk-connect them: share each WABA to a partner
Business Manager on Facebook, then register it with Manager Lite's own
/api/v1/business-managers endpoint.
"""
from datetime import datetime

from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user

from .. import db
from ..models import VerificarProfile, PartnerCredential, get_bsp_names
from .agent_ws import is_agent_connected, push_to_agent
from ..config import Config

bp = Blueprint("verificar", __name__, url_prefix="/verificar")


@bp.route("")
@login_required
def page():
    return render_template("verificar.html", title="Verificar")


@bp.route("/rows")
@login_required
def rows():
    profiles = VerificarProfile.query.filter_by(user_id=current_user.id).order_by(VerificarProfile.name).all()
    return jsonify({"rows": [p.to_dict() for p in profiles]})


@bp.route("/link", methods=["POST"])
@login_required
def link():
    body = request.get_json(silent=True) or {}
    profile_ids = body.get("profile_ids") or []
    if not profile_ids:
        return jsonify({"ok": False, "error": "Nenhum perfil selecionado."}), 400

    partner_business_id = (current_user.share_partner_business_id or "").strip()
    meta_token = (current_user.share_meta_token or "").strip()
    if not partner_business_id or not meta_token:
        return jsonify({"ok": False, "error": "Configure o BM parceiro e o token Meta em Minha Conta."}), 400

    if not is_agent_connected(current_user.id):
        return jsonify({"ok": False, "error": "Agente não conectado."}), 400

    manager_base_url = Config.MANAGER_BASE_URL or request.host_url.rstrip("/")

    # All partners this user already has a token for — the main one (default
    # share target) plus every secondary (match-only) — so the agent can
    # recognize a WABA already shared to any of them and skip re-sharing.
    known_partners = [{"business_id": partner_business_id, "token": meta_token}]
    for p in PartnerCredential.query.filter_by(user_id=current_user.id).all():
        known_partners.append({"business_id": p.business_id, "token": p.token})
    bsp_names = get_bsp_names()

    enqueued = 0
    skipped = 0
    for pid in profile_ids:
        profile = VerificarProfile.query.filter_by(profile_id=pid, user_id=current_user.id).first()
        if not profile:
            continue
        if profile.shared_to_partner_at and profile.registered_with_manager_at:
            skipped += 1
            continue

        profile.linking_at = datetime.utcnow()
        db.session.commit()

        push_to_agent(current_user.id, {
            "type": "link_waba",
            "profile_id": profile.profile_id,
            "business_id": profile.business_id or "",
            "waba_id": profile.waba_id or "",
            "waba_name": profile.waba_name or "",
            "partner_business_id": partner_business_id,
            "meta_token": meta_token,
            "known_partners": known_partners,
            "bsp_names": bsp_names,
            "manager_api_key": current_user.api_key,
            "manager_base_url": manager_base_url,
        })
        enqueued += 1

    return jsonify({"ok": True, "enqueued": enqueued, "skipped": skipped})
