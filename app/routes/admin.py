"""Blueprint: Admin — user management (create, ban, promote, reset, delete)."""
from __future__ import annotations
import shutil

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import current_user

from .. import db
from ..models import User, LoginLog, DisparoJob, TemplateModel
from ..json_store import ensure_user_bms_file, load_user_bms, user_dir

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _is_admin() -> bool:
    return current_user.is_authenticated and bool(getattr(current_user, "is_admin", False))


@bp.before_request
def guard():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login_get"))
    if not _is_admin():
        flash("Acesso restrito a administradores.", "error")
        return redirect(url_for("dashboard.dashboard"))


def _admin_count() -> int:
    return User.query.filter_by(is_admin=True).count()


@bp.route("/users")
def admin_users():
    users = User.query.order_by(User.is_admin.desc(), User.username).all()
    stats = {}
    for u in users:
        stats[u.id] = {
            "wabas": len(load_user_bms(u.id)),
            "disparos": DisparoJob.query.filter_by(user_id=u.id).count(),
        }
    return render_template("admin_users.html", users=users, stats=stats)


@bp.route("/users/create", methods=["POST"])
def admin_create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    if not username or not password:
        flash("Usuário e senha são obrigatórios.", "error")
        return redirect(url_for("admin.admin_users"))

    if User.query.filter_by(username=username).first():
        flash("Já existe um usuário com esse nome.", "error")
        return redirect(url_for("admin.admin_users"))

    u = User(username=username, is_admin=False, is_banned=False)
    u.set_password(password)
    u.generate_api_key()
    db.session.add(u)
    db.session.commit()

    ensure_user_bms_file(u.id)

    flash(f"Usuário '{username}' criado com sucesso.", "ok")
    return redirect(url_for("admin.admin_users"))


@bp.route("/users/<int:user_id>")
def admin_user_detail(user_id: int):
    u = User.query.get_or_404(user_id)
    wabas = load_user_bms(user_id)
    logs = (
        LoginLog.query
        .filter_by(user_id=user_id)
        .order_by(LoginLog.created_at.desc())
        .limit(50)
        .all()
    )
    return render_template("admin_user_detail.html", u=u, wabas=wabas, logs=logs)


@bp.route("/users/<int:user_id>/toggle-ban", methods=["POST"])
def admin_toggle_ban(user_id: int):
    u = User.query.get_or_404(user_id)

    if u.is_admin:
        flash("Não é permitido banir um administrador.", "error")
        return redirect(url_for("admin.admin_users"))

    u.is_banned = not u.is_banned
    db.session.commit()
    flash(f"Usuário '{u.username}' {'banido' if u.is_banned else 'desbanido'}.", "ok")
    return redirect(request.referrer or url_for("admin.admin_users"))


@bp.route("/users/<int:user_id>/toggle-admin", methods=["POST"])
def admin_toggle_admin(user_id: int):
    u = User.query.get_or_404(user_id)

    if u.id == current_user.id:
        flash("Você não pode alterar seu próprio nível de admin.", "error")
        return redirect(request.referrer or url_for("admin.admin_users"))

    if u.is_admin and _admin_count() <= 1:
        flash("Não é possível remover o último administrador.", "error")
        return redirect(request.referrer or url_for("admin.admin_users"))

    u.is_admin = not u.is_admin
    db.session.commit()
    flash(f"Usuário '{u.username}' {'promovido a admin' if u.is_admin else 'rebaixado para usuário comum'}.", "ok")
    return redirect(request.referrer or url_for("admin.admin_users"))


@bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
def admin_reset_password(user_id: int):
    u = User.query.get_or_404(user_id)
    new_password = request.form.get("new_password", "").strip()

    if not new_password:
        flash("Informe a nova senha.", "error")
        return redirect(url_for("admin.admin_user_detail", user_id=user_id))

    u.set_password(new_password)
    db.session.commit()
    flash(f"Senha de '{u.username}' redefinida.", "ok")
    return redirect(url_for("admin.admin_user_detail", user_id=user_id))


@bp.route("/users/<int:user_id>/regenerate-key", methods=["POST"])
def admin_regenerate_key(user_id: int):
    u = User.query.get_or_404(user_id)
    u.generate_api_key()
    db.session.commit()
    flash(f"Chave de API de '{u.username}' regenerada.", "ok")
    return redirect(url_for("admin.admin_user_detail", user_id=user_id))


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
def admin_delete_user(user_id: int):
    u = User.query.get_or_404(user_id)

    if u.id == current_user.id:
        flash("Você não pode excluir a si mesmo.", "error")
        return redirect(url_for("admin.admin_users"))

    if u.is_admin and _admin_count() <= 1:
        flash("Não é possível excluir o último administrador.", "error")
        return redirect(url_for("admin.admin_users"))

    LoginLog.query.filter_by(user_id=user_id).delete()
    DisparoJob.query.filter_by(user_id=user_id).delete()
    TemplateModel.query.filter_by(user_id=user_id).delete()

    username = u.username
    db.session.delete(u)
    db.session.commit()

    shutil.rmtree(user_dir(user_id), ignore_errors=True)

    flash(f"Usuário '{username}' excluído.", "ok")
    return redirect(url_for("admin.admin_users"))
