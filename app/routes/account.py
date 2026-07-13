from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from .. import db
from ..models import PartnerCredential, VerificarProfile
from .api import register_waba_for_user

bp = Blueprint("account", __name__)


@bp.route("/conta")
@login_required
def account_page():
    partners = PartnerCredential.query.filter_by(user_id=current_user.id) \
        .order_by(PartnerCredential.created_at).all()
    return render_template("account.html", title="Minha Conta", partners=partners)


@bp.route("/conta/sync", methods=["POST"])
@login_required
def save_sync():
    current_user.sync_enabled = request.form.get("sync_enabled") == "on"
    db.session.commit()
    flash("Preferência de sincronização salva.", "success")
    return redirect(url_for("account.account_page"))


@bp.route("/conta/vincular", methods=["POST"])
@login_required
def save_vincular():
    current_user.share_partner_business_id = (request.form.get("share_partner_business_id") or "").strip()
    current_user.share_meta_token = (request.form.get("share_meta_token") or "").strip()
    db.session.commit()
    flash("Configuração de vínculo salva.", "success")
    return redirect(url_for("account.account_page"))


def _sweep_pending_wabas(user, business_id: str, token: str) -> int:
    """Auto-complete any VerificarProfile rows already waiting on *business_id*'s
    token — pure server-side registration, no agent/browser re-run needed since
    waba_id/profile_id were already captured live during the original link attempt."""
    pending = VerificarProfile.query.filter_by(
        user_id=user.id, pending_partner_business_id=business_id,
    ).filter(VerificarProfile.registered_with_manager_at.is_(None)).all()

    completed = 0
    for profile in pending:
        if not profile.waba_id:
            continue
        result = register_waba_for_user(
            user, profile.waba_id, token,
            adspower_profile_id=profile.profile_id,
        )
        if result.get("ok"):
            profile.registered_with_manager_at = datetime.utcnow()
            profile.pending_partner_business_id = None
            profile.pending_partner_name = None
            profile.last_error = ""
            completed += 1
    if completed:
        db.session.commit()
    return completed


@bp.route("/conta/parceiros", methods=["POST"])
@login_required
def add_partner():
    label = (request.form.get("label") or "").strip()
    business_id = (request.form.get("business_id") or "").strip()
    token = (request.form.get("token") or "").strip()

    if not business_id or not token:
        flash("ID do BM parceiro e token são obrigatórios.", "error")
        return redirect(url_for("account.account_page"))

    partner = PartnerCredential(
        user_id=current_user.id, label=label, business_id=business_id, token=token,
    )
    db.session.add(partner)
    db.session.commit()

    completed = _sweep_pending_wabas(current_user, business_id, token)

    msg = "Parceiro adicionado."
    if completed:
        msg += f" {completed} WABA(s) concluída(s) automaticamente."
    flash(msg, "success")
    return redirect(url_for("account.account_page"))


@bp.route("/conta/parceiros/<int:partner_id>/excluir", methods=["POST"])
@login_required
def delete_partner(partner_id: int):
    partner = PartnerCredential.query.filter_by(id=partner_id, user_id=current_user.id).first()
    if partner:
        db.session.delete(partner)
        db.session.commit()
        flash("Parceiro removido.", "success")
    return redirect(url_for("account.account_page"))
