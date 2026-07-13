from flask import Blueprint, render_template, request, jsonify, abort
from flask_login import login_required, current_user
from ..json_store import load_user_bms

bp = Blueprint("waba_detail", __name__)


def _get_waba_or_404(user_id: int, waba_id: str):
    bms = load_user_bms(user_id)
    entry = bms.get(str(waba_id))
    if not entry or not isinstance(entry, dict):
        abort(404)
    return entry, bms


@bp.route("/waba/<waba_id>")
@login_required
def detail(waba_id):
    entry, _ = _get_waba_or_404(current_user.id, waba_id)
    snap = entry.get("snapshot", {}) or {}
    phone_numbers = snap.get("phone_numbers") or []
    return render_template(
        "waba_detail.html",
        title=snap.get("waba_name") or waba_id,
        waba_id=waba_id,
        waba_name=snap.get("waba_name") or waba_id,
        phone_numbers=phone_numbers,
    )


@bp.route("/waba/<waba_id>/conversations")
@login_required
def conversations(waba_id):
    _get_waba_or_404(current_user.id, waba_id)
    phone_number_id = request.args.get("phone_number_id", "").strip()
    if not phone_number_id:
        return jsonify({"error": "phone_number_id required"}), 400
    from ..services.chat_service import get_conversations
    return jsonify({"conversations": get_conversations(waba_id, phone_number_id)})


@bp.route("/waba/<waba_id>/messages/<contact_wa_id>")
@login_required
def message_history(waba_id, contact_wa_id):
    _get_waba_or_404(current_user.id, waba_id)
    phone_number_id = request.args.get("phone_number_id", "").strip()
    if not phone_number_id:
        return jsonify({"error": "phone_number_id required"}), 400
    before_id = request.args.get("before_id", type=int)
    from ..services.chat_service import get_message_history
    return jsonify({"messages": get_message_history(
        waba_id, phone_number_id, contact_wa_id, before_id=before_id
    )})


@bp.route("/waba/<waba_id>/messages/send", methods=["POST"])
@login_required
def send_message(waba_id):
    entry, _ = _get_waba_or_404(current_user.id, waba_id)
    token = entry.get("token", "")
    if not token:
        return jsonify({"error": "Token não encontrado."}), 400

    data            = request.get_json(silent=True) or {}
    phone_number_id = (data.get("phone_number_id") or "").strip()
    to_wa_id        = (data.get("to") or "").strip()
    msg_type        = (data.get("type") or "text").strip()
    body            = (data.get("body") or "").strip()
    image_url       = (data.get("image_url") or "").strip()
    caption         = (data.get("caption") or "").strip()

    if not phone_number_id or not to_wa_id:
        return jsonify({"error": "phone_number_id e to são obrigatórios."}), 400

    from ..services.chat_service import send_text_message, send_image_message, save_message
    from ..models import ChatMessage

    if msg_type == "image":
        if not image_url:
            return jsonify({"error": "image_url obrigatória para tipo image."}), 400
        success, result = send_image_message(token, phone_number_id, to_wa_id, image_url, caption)
        save_body = caption or "[imagem]"
    else:
        if not body:
            return jsonify({"error": "body obrigatório para tipo text."}), 400
        success, result = send_text_message(token, phone_number_id, to_wa_id, body)
        save_body = body

    if not success:
        return jsonify({"error": result}), 502

    existing = ChatMessage.query.filter_by(
        waba_id=waba_id, phone_number_id=phone_number_id, contact_wa_id=to_wa_id,
    ).first()
    contact_name = existing.contact_name if existing else to_wa_id

    save_message(
        waba_id=waba_id, phone_number_id=phone_number_id, contact_wa_id=to_wa_id,
        contact_name=contact_name, direction="out", msg_type=msg_type, body=save_body,
        media_url=image_url if msg_type == "image" else "", wamid=result, status="sent",
    )
    return jsonify({"ok": True, "wamid": result})
