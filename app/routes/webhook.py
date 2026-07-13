import queue
import threading
from flask import Blueprint, request, current_app

from ..services.chat_service import save_message, update_message_status

bp = Blueprint("webhook", __name__)

_WORKER_COUNT = 2
_QUEUE_MAXSIZE = 20000
_work_queue: "queue.Queue" = queue.Queue(maxsize=_QUEUE_MAXSIZE)
_workers_started = False
_workers_lock = threading.Lock()
_dropped = 0


def _worker_loop(app):
    while True:
        payload = _work_queue.get()
        try:
            with app.app_context():
                process_webhook_payload(payload)
        except Exception as e:
            app.logger.exception("webhook worker error: %s", e)
        finally:
            _work_queue.task_done()


def _ensure_workers(app):
    global _workers_started
    if _workers_started:
        return
    with _workers_lock:
        if _workers_started:
            return
        for _ in range(_WORKER_COUNT):
            threading.Thread(target=_worker_loop, args=(app,), daemon=True).start()
        _workers_started = True


@bp.route("/webhook", methods=["GET"])
def verify():
    mode      = request.args.get("hub.mode", "")
    token     = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    verify_token = current_app.config.get("WEBHOOK_VERIFY_TOKEN", "")
    if mode == "subscribe" and token == verify_token:
        return challenge, 200
    return "Forbidden", 403


@bp.route("/webhook", methods=["POST"])
def receive():
    global _dropped
    payload = request.get_json(silent=True)
    if not payload:
        return "OK", 200
    _ensure_workers(current_app._get_current_object())
    try:
        _work_queue.put_nowait(payload)
    except queue.Full:
        _dropped += 1
    return "OK", 200


def process_webhook_payload(payload):
    for entry in (payload.get("entry") or []):
        waba_id = str(entry.get("id") or "")
        if not waba_id:
            continue
        for change in (entry.get("changes") or []):
            field = change.get("field") or ""
            if field != "messages":
                continue
            value = change.get("value") or {}

            metadata        = value.get("metadata") or {}
            phone_number_id = metadata.get("phone_number_id", "")

            contact_map: dict = {}
            for c in (value.get("contacts") or []):
                wa_id = c.get("wa_id", "")
                name  = (c.get("profile") or {}).get("name", "")
                if wa_id:
                    contact_map[wa_id] = name

            for msg in (value.get("messages") or []):
                from_wa   = msg.get("from", "")
                wamid     = msg.get("id", "")
                msg_type  = msg.get("type", "text")
                cname     = contact_map.get(from_wa, "")
                body      = ""
                media_url = ""
                if msg_type == "text":
                    body = (msg.get("text") or {}).get("body", "")
                elif msg_type == "image":
                    img       = msg.get("image") or {}
                    body      = img.get("caption", "")
                    media_url = img.get("id", "")
                elif msg_type == "button":
                    body = (msg.get("button") or {}).get("text", "")
                elif msg_type == "interactive":
                    inter = msg.get("interactive") or {}
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    body  = reply.get("title", f"[{msg_type}]")
                else:
                    body = f"[{msg_type}]"

                save_message(
                    waba_id=waba_id, phone_number_id=phone_number_id,
                    contact_wa_id=from_wa, contact_name=cname,
                    direction="in", msg_type=msg_type, body=body,
                    media_url=media_url, wamid=wamid, status="received",
                )

            for status_obj in (value.get("statuses") or []):
                wamid    = status_obj.get("id", "")
                status_v = status_obj.get("status", "")
                if wamid and status_v in ("sent", "delivered", "read"):
                    update_message_status(wamid, status_v)
