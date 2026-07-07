from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from .. import db

bp = Blueprint("account", __name__)


@bp.route("/conta")
@login_required
def account_page():
    return render_template("account.html", title="Minha Conta")


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
