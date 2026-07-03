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
