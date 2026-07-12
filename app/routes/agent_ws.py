"""WebSocket endpoint for the local card-adding agent.

The agent (a Tkinter client running next to AdsPower on the operator's PC)
connects here and authenticates with the user's existing Lite API key. The
server pushes add_card commands and correlates replies by cmd_id. It also
receives async pushes (profiles_push, link_start/done/summary) that have no
cmd_id and are handled by dedicated handlers instead of the reply-queue path.
"""
import json
import queue
import threading
import time
import uuid
from datetime import datetime
from dataclasses import dataclass, field

from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user

from ..models import User, VerificarProfile
from .. import db

bp = Blueprint("agent_ws", __name__, url_prefix="/agent")


# ── Per-user agent registry ───────────────────────────────────────────────────

@dataclass
class AgentSession:
    user_id:       int
    username:      str
    ws:            object
    send_queue:    queue.Queue = field(default_factory=queue.Queue)
    sender_thread: object = None


_registry_lock = threading.Lock()
_agents: dict[int, AgentSession] = {}

_open_browsers: dict[int, set[str]] = {}
_open_browsers_lock = threading.Lock()

# ── Command/result correlation (request-response over the WS) ─────────────────

_pending: dict[str, queue.Queue] = {}
_pending_lock = threading.Lock()


def send_command_and_wait(
    user_id: int, msg: dict, timeout: float = 120.0, stop_event: threading.Event = None
) -> dict:
    """Push a command to the agent and block until it replies (or times out).

    The caller must set msg["type"]; this function injects a unique cmd_id and
    registers a reply queue before sending so no race with the receive loop.

    If stop_event is given, the wait is polled in short slices so a caller
    (e.g. the scan job loop) can be interrupted well before the full timeout
    elapses instead of being stuck for it — see scan_service.request_stop.
    """
    if not is_agent_connected(user_id):
        return {"ok": False, "error": "agente não conectado"}

    cmd_id = str(uuid.uuid4())
    msg = {**msg, "cmd_id": cmd_id}
    reply_q: queue.Queue = queue.Queue()

    with _pending_lock:
        _pending[cmd_id] = reply_q

    try:
        if not push_to_agent(user_id, msg):
            return {"ok": False, "error": "agente desconectou antes do envio"}
        if stop_event is None:
            try:
                return reply_q.get(timeout=timeout)
            except queue.Empty:
                return {"ok": False, "error": "timeout — agente não respondeu"}

        deadline = time.monotonic() + timeout
        slice_s = 0.5
        while True:
            if stop_event.is_set():
                return {"ok": False, "stopped": True}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"ok": False, "error": "timeout — agente não respondeu"}
            try:
                return reply_q.get(timeout=min(slice_s, remaining))
            except queue.Empty:
                continue
    finally:
        with _pending_lock:
            _pending.pop(cmd_id, None)


def get_open_profiles(user_id: int) -> set[str]:
    with _open_browsers_lock:
        return set(_open_browsers.get(user_id, ()))


def is_agent_connected(user_id: int) -> bool:
    return user_id in _agents


def push_to_agent(user_id: int, msg: dict) -> bool:
    session = _agents.get(user_id)
    if not session:
        return False
    session.send_queue.put(json.dumps(msg))
    return True


# ── Auth ───────────────────────────────────────────────────────────────────────

def _auth_user() -> User | None:
    token = request.args.get("token", "").strip() or request.args.get("key", "").strip()
    if not token:
        return None
    user = User.query.filter_by(api_key=token).first()
    if user and user.is_banned:
        return None
    return user


# ── Incoming message handlers ─────────────────────────────────────────────────

def _handle_browser_status(user_id: int, open_profile_ids: list):
    with _open_browsers_lock:
        _open_browsers[user_id] = set(open_profile_ids)


def _handle_profiles_push(app, user_id: int, profiles: list):
    with app.app_context():
        incoming_ids = {p["profile_id"] for p in profiles}

        for p in profiles:
            profile = db.session.get(VerificarProfile, p["profile_id"])
            if profile is None:
                profile = VerificarProfile(profile_id=p["profile_id"], user_id=user_id)
                db.session.add(profile)
            profile.user_id    = user_id
            profile.name       = p.get("name", "")
            profile.group_name = p.get("group_name", "")
            profile.remark     = p.get("remark", "")
            profile.synced_at  = datetime.utcnow()

        existing = VerificarProfile.query.filter_by(user_id=user_id).all()
        stale = [p for p in existing if p.profile_id not in incoming_ids]
        for profile in stale:
            db.session.delete(profile)

        db.session.commit()
        print(f"[AGENT WS] user_id={user_id}: {len(profiles)} perfis Verificar sincronizados, {len(stale)} removidos")


def _handle_link_start(app, msg: dict):
    print(f"[AGENT WS] link_start profile={msg.get('profile_id')}")


def _apply_link_result(profile: VerificarProfile, msg: dict):
    status = msg.get("status")
    if msg.get("waba_id"):
        profile.waba_id = msg["waba_id"]
    if msg.get("business_id"):
        profile.business_id = msg["business_id"]

    if status == "ok":
        if msg.get("shared"):
            profile.shared_to_partner_at = datetime.utcnow()
        if msg.get("registered"):
            profile.registered_with_manager_at = datetime.utcnow()
        profile.last_error = ""
    elif status == "restrita":
        profile.last_error = "BM restrito"
        profile.error_count += 1
    elif status == "error":
        profile.last_error = msg.get("message") or "Erro desconhecido"
        profile.error_count += 1
    profile.linking_at = None


def _handle_link_done(app, msg: dict):
    with app.app_context():
        profile = db.session.get(VerificarProfile, msg.get("profile_id"))
        if not profile:
            return
        _apply_link_result(profile, msg)
        db.session.commit()


def _handle_link_summary(app, msg: dict):
    with app.app_context():
        profile = db.session.get(VerificarProfile, msg.get("profile_id"))
        if not profile:
            return
        profile.linking_at = None
        if msg.get("status") == "error":
            profile.last_error = f"Falhou {msg.get('failed', 0)}/{msg.get('total', 0)} BM(s)"
            profile.error_count += 1
        db.session.commit()


def _handle_agent_message(app, user_id: int, data: str):
    try:
        msg = json.loads(data)
    except Exception:
        return

    msg_type = msg.get("type")
    if msg_type == "browser_status":
        _handle_browser_status(user_id, msg.get("open_profile_ids", []))
        return
    if msg_type == "profiles_push":
        _handle_profiles_push(app, user_id, msg.get("profiles", []))
        return
    if msg_type == "link_start":
        _handle_link_start(app, msg)
        return
    if msg_type == "link_done":
        _handle_link_done(app, msg)
        return
    if msg_type == "link_summary":
        _handle_link_summary(app, msg)
        return

    cmd_id = msg.get("cmd_id")
    if cmd_id:
        with _pending_lock:
            q = _pending.get(cmd_id)
        if q:
            q.put(msg)
    # ping / unknown (no cmd_id) → silently ignored


# ── WebSocket handler (registered via sock.route in __init__.py) ──────────────

def handle_ws(ws):
    app = current_app._get_current_object()
    user = _auth_user()
    if not user:
        db.session.remove()
        print("[AGENT WS] Auth failed — invalid or missing token")
        ws.close()
        return

    user_id  = user.id
    username = user.username
    db.session.remove()  # release connection immediately — handler holds no DB connection for its lifetime
    print(f"[AGENT WS] Auth OK — user='{username}' id={user_id}")

    session = AgentSession(user_id=user_id, username=username, ws=ws)

    with _registry_lock:
        old = _agents.get(user_id)
        if old:
            print(f"[AGENT WS] Closing previous session for '{username}'")
            try:
                old.ws.close()
            except Exception:
                pass
            old.send_queue.put(None)

        while not session.send_queue.empty():
            try:
                session.send_queue.get_nowait()
            except queue.Empty:
                break

        _agents[user_id] = session

    print(f"[AGENT WS] '{username}' registered")

    # ── Sender thread ─────────────────────────────────────────────────────────
    def _sender():
        while True:
            try:
                data = session.send_queue.get(timeout=1)
                if data is None:
                    break
                ws.send(data)
            except queue.Empty:
                if user_id not in _agents:
                    break
            except Exception as e:
                print(f"[AGENT WS] sender error: {type(e).__name__}: {e}")
                break

    session.sender_thread = threading.Thread(target=_sender, daemon=True)
    session.sender_thread.start()

    # ── Receive loop ──────────────────────────────────────────────────────────
    try:
        while True:
            try:
                data = ws.receive(timeout=120)
            except Exception as e:
                print(f"[AGENT WS] ws.receive() raised {type(e).__name__}: {e}")
                break
            if data is None:
                # timeout — send keepalive ping
                try:
                    ws.send(json.dumps({"type": "ping"}))
                except Exception as e:
                    print(f"[AGENT WS] ping send failed: {type(e).__name__}: {e}")
                    break
                continue
            try:
                _handle_agent_message(app, user_id, data)
            except Exception as e:
                print(f"[AGENT WS] message handling error: {type(e).__name__}: {e}")
    finally:
        with _registry_lock:
            if _agents.get(user_id) is session:
                del _agents[user_id]
        with _open_browsers_lock:
            _open_browsers.pop(user_id, None)
        session.send_queue.put(None)
        print(f"[AGENT WS] '{username}' disconnected")


# ── Status endpoint ───────────────────────────────────────────────────────────

@bp.route("/status")
@login_required
def agent_status():
    return jsonify({"online": is_agent_connected(current_user.id)})
