from flask import Blueprint, request, jsonify, current_app
from .. import db
from ..models import User
from ..json_store import ensure_user_bms_file, upsert_waba_full, replace_user_wabas, get_lite_origin_wabas

bp = Blueprint("sync", __name__, url_prefix="/api/v1/sync")


def _authenticate_sync():
    """Shared-secret auth for the Manager -> Lite replication feed.

    Distinct from the per-user api_key auth in api.py: this endpoint represents
    Manager itself, not a single user, so it uses one shared token from config.
    """
    token = request.headers.get("X-Sync-Token", "")
    expected = current_app.config.get("LITE_SYNC_TOKEN") or ""
    if not expected or token != expected:
        return jsonify({"ok": False, "error": "Invalid or missing sync token."}), 401
    return None


def _apply_user(u: dict) -> dict:
    source_id = u.get("source_id")
    username = str(u.get("username") or "").strip()
    if source_id is None or not username:
        return {"ok": False, "error": "source_id and username are required", "source_id": source_id}

    source_id = int(source_id)

    # Match by source_id first (already-linked replica), else adopt an existing
    # Lite-native account with the same username, else create a new one.
    user = User.query.filter_by(source_id=source_id).first()
    if user is None:
        user = User.query.filter_by(username=username).first()
    if user is None:
        user = User(username=username, is_admin=False, is_banned=False)
        db.session.add(user)

    user.source_id = source_id
    user.username = username
    if u.get("password_hash"):
        user.password_hash = u["password_hash"]  # Manager wins
    user.is_banned = bool(u.get("is_banned", False))
    db.session.flush()  # ensure user.id is populated for a new row
    if not user.api_key:
        user.generate_api_key()
    db.session.commit()

    ensure_user_bms_file(user.id)

    if not user.sync_enabled:
        # Sync disabled by the user in "Minha Conta": leave this user's
        # bms.json untouched — no adds, no prunes, in either direction.
        return {"ok": True, "source_id": source_id, "lite_id": user.id,
                "wabas": 0, "sync_enabled": False}

    keep = set()
    for w in u.get("wabas", []) or []:
        waba_id = str(w.get("waba_id") or "").strip()
        if not waba_id:
            continue
        upsert_waba_full(user.id, w)
        keep.add(waba_id)
    replace_user_wabas(user.id, keep)

    return {"ok": True, "source_id": source_id, "lite_id": user.id,
            "wabas": len(keep), "sync_enabled": True}


@bp.route("/users", methods=["POST"])
def sync_users():
    """Bulk idempotent upsert of users + WABAs replicated from Manager.

    Body: { "users": [ { source_id, username, password_hash, is_banned, wabas: [...] } ] }
    """
    auth_err = _authenticate_sync()
    if auth_err:
        return auth_err

    body = request.get_json(silent=True) or {}
    users = body.get("users")
    if not isinstance(users, list):
        return jsonify({"ok": False, "error": "'users' must be a list."}), 400

    results = []
    for u in users:
        try:
            results.append(_apply_user(u))
        except Exception as exc:
            db.session.rollback()
            results.append({"ok": False, "error": str(exc), "source_id": u.get("source_id")})

    return jsonify({"ok": True, "results": results}), 200


@bp.route("/pending-wabas", methods=["GET"])
def pending_wabas():
    """WABAs added directly in Lite, not yet adopted by Manager.

    Manager polls this to discover and adopt them, then echoes them back in
    the next /sync/users payload (which flips their origin to "manager").
    Users with sync disabled are excluded entirely.
    """
    auth_err = _authenticate_sync()
    if auth_err:
        return auth_err

    out = []
    for user in User.query.all():
        if not user.sync_enabled:
            continue
        wabas = get_lite_origin_wabas(user.id)
        if wabas:
            out.append({
                "source_id": user.source_id,
                "username": user.username,
                "lite_id": user.id,
                "wabas": wabas,
            })

    return jsonify({"ok": True, "users": out}), 200
