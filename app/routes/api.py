from flask import Blueprint, request, jsonify
from ..models import User, PartnerCredential, VerificarProfile
from ..json_store import ensure_user_bms_file, upsert_waba, patch_snapshot
from ..services.meta import subscribe_waba_webhook
from ..config import Config

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def register_waba_for_user(
    user, waba_id: str, token: str, *,
    adspower_profile_id: str = "", business_manager_id: str = "",
    payment_account_id: str = "", serial_number: str = "", messaging_limit_tier: str = "",
) -> dict:
    """Persist a WABA for *user* and subscribe its webhook. Shared by the
    /api/v1/business-managers endpoint (agent callback) and the Minha Conta
    "pending partner" auto-complete sweep (server-side, no agent involved).

    Returns the same shape POSTed back to /api/v1/business-managers callers.
    """
    # A caller that knows the AdsPower profile but not its serial (e.g. the browser extension, whose
    # AdsPower local-API detection can fail) still gets it recovered here from the same synced
    # VerificarProfile row the "Vincular ao Manager" flow already populates.
    if adspower_profile_id and not serial_number:
        profile = VerificarProfile.query.filter_by(
            profile_id=adspower_profile_id, user_id=user.id,
        ).first()
        if profile and profile.serial_number:
            serial_number = profile.serial_number

    ensure_user_bms_file(user.id)
    upsert_waba(user.id, waba_id=waba_id, token=token,
                adspower_profile_id=adspower_profile_id,
                business_manager_id=business_manager_id,
                payment_account_id=payment_account_id,
                serial_number=serial_number)

    # Same TIER_50…TIER_UNLIMITED vocabulary the dashboard already renders from
    # snapshot.messaging_limit_tier (app/services/sync_service.py, via the official Graph API field) —
    # the browser extension (extension/) reads it from WhatsApp Manager's own internal GraphQL, which
    # can be fresher than the next background sync. patch_snapshot leaves last_sync_at untouched.
    if messaging_limit_tier:
        patch_snapshot(user.id, waba_id, messaging_limit_tier=messaging_limit_tier)

    webhook_ok, webhook_err = subscribe_waba_webhook(Config.META_API_VERSION, token, waba_id)

    return {
        "ok": True,
        "waba_id": waba_id,
        "adspower_profile_id": adspower_profile_id or None,
        "business_manager_id": business_manager_id or None,
        "payment_account_id": payment_account_id or None,
        "serial_number": serial_number or None,
        "webhook_subscribed": webhook_ok,
        "webhook_error": webhook_err,
    }


def _authenticate():
    """Extract and validate the API key from request headers.

    Returns (user, None) on success or (None, (response, status_code)) on failure —
    the caller can `return err` directly since Flask accepts (body, status) tuples.
    Accepts: Authorization: Bearer <key>  OR  X-API-Key: <key>
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        key = auth_header[7:].strip()
    else:
        key = request.headers.get("X-API-Key", "").strip()

    if not key:
        return None, (jsonify({"ok": False, "error": "Missing API key. Send Authorization: Bearer <key> or X-API-Key: <key>."}), 401)

    user = User.query.filter_by(api_key=key).first()
    if not user:
        return None, (jsonify({"ok": False, "error": "Invalid API key."}), 401)

    if user.is_banned:
        return None, (jsonify({"ok": False, "error": "Account banned."}), 403)

    return user, None


@bp.route("/business-managers", methods=["POST"])
def add_business_manager():
    """Register a Business Manager (WABA) for the authenticated user.

    Body (JSON): { "waba_id": "...", "token": "..." }
    Returns 201 on success.
    """
    user, err = _authenticate()
    if err:
        return err

    body = request.get_json(silent=True)
    if not body:
        return jsonify({"ok": False, "error": "Request body must be valid JSON with Content-Type: application/json."}), 400

    waba_id = str(body.get("waba_id") or "").strip()
    token = str(body.get("token") or "").strip()
    adspower_profile_id = str(body.get("adspower_profile_id") or "").strip()
    business_manager_id = str(body.get("business_manager_id") or "").strip()
    payment_account_id = str(body.get("payment_account_id") or "").strip()
    serial_number = str(body.get("serial_number") or "").strip()
    messaging_limit_tier = str(body.get("messaging_limit_tier") or "").strip()

    if not waba_id:
        return jsonify({"ok": False, "error": "waba_id is required."}), 400
    if not token:
        return jsonify({"ok": False, "error": "token is required."}), 400

    result = register_waba_for_user(
        user, waba_id, token,
        adspower_profile_id=adspower_profile_id,
        business_manager_id=business_manager_id,
        payment_account_id=payment_account_id,
        serial_number=serial_number,
        messaging_limit_tier=messaging_limit_tier,
    )
    return jsonify(result), 201


@bp.route("/me/extension-config", methods=["GET"])
def extension_config():
    """Config the browser extension (extension/) needs to check BM restriction and add a
    partner+register a WABA on its own: the same partner BM id + Meta token the "Vincular ao Manager"
    (Verificar) flow already sends to the local agent (see app/routes/verificar.py:link). Read live on
    every popup open so changing it in Minha Conta propagates to every profile without re-downloading
    the extension ZIP.
    """
    user, err = _authenticate()
    if err:
        return err

    partner_business_id = (user.share_partner_business_id or "").strip()
    meta_token = (user.share_meta_token or "").strip()
    if not partner_business_id or not meta_token:
        return jsonify({
            "ok": False,
            "error": "Configure o BM parceiro e o token Meta em Minha Conta antes de usar a extensão.",
        }), 400

    known_partners = [{"business_id": partner_business_id, "token": meta_token}]
    for p in PartnerCredential.query.filter_by(user_id=user.id).all():
        known_partners.append({"business_id": p.business_id, "token": p.token})

    return jsonify({
        "ok": True,
        "username": user.username,
        "partner_business_id": partner_business_id,
        "meta_token": meta_token,
        "known_partners": known_partners,
    })
