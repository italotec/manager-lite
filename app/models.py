import secrets
from datetime import datetime
from zoneinfo import ZoneInfo
from flask_login import UserMixin

_SP = ZoneInfo("America/Sao_Paulo")


def _now_sp():
    return datetime.now(_SP)


from werkzeug.security import generate_password_hash, check_password_hash
from . import db, login_manager


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)

    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_banned = db.Column(db.Boolean, default=False, nullable=False)

    api_key = db.Column(db.String(64), unique=True, nullable=True, index=True)

    # Manager (GERENCIADOR DE BMS) user id, for users replicated from Manager.
    # NULL for Lite-native users.
    source_id = db.Column(db.Integer, unique=True, nullable=True, index=True)

    # When False, this user's WABAs are frozen: Manager sync neither writes
    # nor reads them (both /sync/users and /sync/pending-wabas skip them).
    sync_enabled = db.Column(db.Boolean, default=True, nullable=False)

    # Partner Business Manager + Meta token used by the "Vincular ao Manager"
    # (Verificar) flow to share a WABA to a partner BM before registering it.
    share_partner_business_id = db.Column(db.String(64), nullable=True)
    share_meta_token = db.Column(db.Text, nullable=True)

    def generate_api_key(self):
        self.api_key = secrets.token_urlsafe(32)

    def set_password(self, pw: str):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


class LoginLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    ip_address = db.Column(db.String(64), default="", nullable=False)
    user_agent = db.Column(db.String(512), default="", nullable=False)
    created_at = db.Column(db.DateTime, default=_now_sp, nullable=False)


class DisparoJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    status = db.Column(db.String(32), default="queued", nullable=False)  # queued/running/done/error/stopped

    waba_id = db.Column(db.String(64), default="", nullable=False)
    skip_log = db.Column(db.Boolean, default=False, nullable=False)

    total = db.Column(db.Integer, default=0, nullable=False)
    sent = db.Column(db.Integer, default=0, nullable=False)
    failed = db.Column(db.Integer, default=0, nullable=False)
    skipped = db.Column(db.Integer, default=0, nullable=False)

    last_message = db.Column(db.Text, default="", nullable=False)
    stop_requested = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(db.DateTime, default=_now_sp, nullable=False)


class TemplateModel(db.Model):
    """Reusable WhatsApp message-template definition (model library)."""
    id           = db.Column(db.Integer,     primary_key=True)
    user_id      = db.Column(db.Integer,     db.ForeignKey("user.id"), nullable=False, index=True)
    name         = db.Column(db.String(128), nullable=False)   # base name, e.g. "template"
    category     = db.Column(db.String(32),  nullable=False, default="UTILITY")
    language     = db.Column(db.String(16),  nullable=False, default="pt_BR")
    payload_json = db.Column(db.Text,        nullable=False, default="{}")
    created_at   = db.Column(db.DateTime,    default=_now_sp, nullable=False)

    def to_dict(self):
        return {
            "id":         self.id,
            "name":       self.name,
            "category":   self.category,
            "language":   self.language,
            "created_at": self.created_at.strftime("%d/%m/%Y") if self.created_at else "",
        }


class Card(db.Model):
    """Credit/debit card stored per user for bulk WABA billing attachment."""
    id            = db.Column(db.Integer,     primary_key=True)
    user_id       = db.Column(db.Integer,     db.ForeignKey("user.id"), nullable=False, index=True)
    number        = db.Column(db.String(20),  nullable=False)   # full PAN, plaintext
    exp_month     = db.Column(db.String(2),   nullable=False)   # "6"
    exp_year      = db.Column(db.String(4),   nullable=False)   # "2032"
    csc           = db.Column(db.String(4),   nullable=False)
    holder_name   = db.Column(db.String(128), nullable=False, default="")
    brand         = db.Column(db.String(16),  nullable=False, default="unknown")
    bin           = db.Column(db.String(8),   nullable=False, default="")
    last4         = db.Column(db.String(4),   nullable=False, default="")
    # JSON list of waba_id strings (distinct WABAs this card has been attached to)
    used_waba_ids = db.Column(db.Text,        nullable=False, default="[]")
    status        = db.Column(db.String(16),  nullable=False, default="active")  # active|overused|invalid
    last_error    = db.Column(db.Text,        nullable=False, default="")
    created_at    = db.Column(db.DateTime,    default=_now_sp, nullable=False)

    @property
    def usage_count(self):
        import json
        try:
            return len(json.loads(self.used_waba_ids or "[]"))
        except Exception:
            return 0

    @property
    def remaining(self):
        return max(0, 10 - self.usage_count)

    @property
    def is_available(self):
        return self.status == "active" and self.remaining > 0

    def mark_used(self, waba_id: str):
        import json
        try:
            ids = json.loads(self.used_waba_ids or "[]")
        except Exception:
            ids = []
        if waba_id not in ids:
            ids.append(waba_id)
        self.used_waba_ids = json.dumps(ids)

    def to_dict(self):
        return {
            "id": self.id,
            "brand": self.brand,
            "last4": self.last4,
            "bin": self.bin,
            "exp_month": self.exp_month,
            "exp_year": self.exp_year,
            "holder_name": self.holder_name,
            "usage_count": self.usage_count,
            "remaining": self.remaining,
            "status": self.status,
            "last_error": self.last_error,
            "created_at": self.created_at.strftime("%d/%m/%Y") if self.created_at else "",
        }


class VerificarProfile(db.Model):
    """AdsPower profile in the "Verificar" group, synced by the local agent.

    Tracks the "Vincular ao Manager" (Conectar) link state: share the WABA to
    a partner Business Manager on Facebook, then register it with Manager
    Lite's own /api/v1/business-managers endpoint.
    """
    profile_id = db.Column(db.String(64), primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name       = db.Column(db.String(255), default="", nullable=False)
    group_name = db.Column(db.String(64),  default="", nullable=False)
    remark     = db.Column(db.Text,        default="", nullable=False)
    synced_at  = db.Column(db.DateTime,    default=_now_sp, nullable=False)

    business_id = db.Column(db.String(64),  nullable=True)
    waba_id     = db.Column(db.String(64),  nullable=True)
    waba_name   = db.Column(db.String(255), nullable=True)

    linking_at                 = db.Column(db.DateTime,   nullable=True)
    shared_to_partner_at       = db.Column(db.DateTime,   nullable=True)
    shared_partner_business_id = db.Column(db.String(64), nullable=True)
    registered_with_manager_at = db.Column(db.DateTime,   nullable=True)

    # Set when the agent detects the WABA already shared to a partner BM we
    # have no saved token for — the row is "Aguardando token" until a
    # matching PartnerCredential is added, which auto-completes it.
    pending_partner_business_id = db.Column(db.String(64),  nullable=True)
    pending_partner_name        = db.Column(db.String(255), nullable=True)

    last_error  = db.Column(db.Text,    default="", nullable=False)
    error_count = db.Column(db.Integer, default=0,  nullable=False)

    def to_dict(self):
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "group_name": self.group_name,
            "waba_id": self.waba_id,
            "waba_name": self.waba_name,
            "business_id": self.business_id,
            "linking_at": bool(self.linking_at),
            "shared_to_partner_at": bool(self.shared_to_partner_at),
            "registered_with_manager_at": bool(self.registered_with_manager_at),
            "pending_partner_business_id": self.pending_partner_business_id or "",
            "pending_partner_name": self.pending_partner_name or "",
            "last_error": self.last_error or "",
        }


class PartnerCredential(db.Model):
    """A secondary partner Business Manager + Meta token the user already knows
    about. Used only to *recognize* a WABA already shared to this partner (by
    business_id) and pick the right token to register it — never a share
    target on its own; the "Vincular ao Manager" main partner in Minha Conta
    (User.share_partner_business_id) remains the only share target.
    """
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    label       = db.Column(db.String(120), default="", nullable=False)
    business_id = db.Column(db.String(64), nullable=False)
    token       = db.Column(db.Text, nullable=False)
    created_at  = db.Column(db.DateTime, default=_now_sp, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "label": self.label or "",
            "business_id": self.business_id,
            "token_masked": ("•" * 8 + self.token[-4:]) if self.token and len(self.token) > 4 else "••••",
        }


class BspPartner(db.Model):
    """Admin-managed name added to the built-in BSP ignore list (Manychat,
    Gupshup, Callbell, ...) so the "Vincular ao Manager" partner detection
    never mistakes a messaging BSP for the user's own partner BM. Global,
    not per-user.
    """
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=_now_sp, nullable=False)


# Built-in BSPs ignored by "Vincular ao Manager" partner detection — never
# mistaken for the user's own partner BM. Admins can add more via /admin/bsp.
DEFAULT_BSP_NAMES = [
    "Manychat", "Gupshup", "Callbell", "360dialog", "Twilio", "Zenvia",
    "WATI", "Take Blip", "Blip", "Infobip", "MessageBird", "Sinch",
]


def get_bsp_names() -> list[str]:
    """Combined hardcoded + admin-managed BSP ignore list, sent to the agent
    as bsp_names on every link_waba dispatch."""
    extra = [b.name for b in BspPartner.query.all()]
    seen = set()
    combined = []
    for name in DEFAULT_BSP_NAMES + extra:
        key = name.strip().lower()
        if key and key not in seen:
            seen.add(key)
            combined.append(name.strip())
    return combined


class ScanProfile(db.Model):
    """One AdsPower profile scanned by the WABA health scanner.

    Row existence (keyed by profile_id) is what drives resume: a restarted
    scan skips any profile already present here.
    """
    profile_id   = db.Column(db.String(64), primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    profile_name = db.Column(db.String(255), default="", nullable=False)
    outcome      = db.Column(db.String(32), default="", nullable=False)  # ok|checkpoint|not_logged_in|error
    detail       = db.Column(db.Text, default="", nullable=False)
    scanned_at   = db.Column(db.DateTime, default=_now_sp, nullable=False)

    def to_dict(self):
        return {
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "outcome": self.outcome,
            "detail": self.detail,
            "scanned_at": self.scanned_at.strftime("%d/%m/%Y %H:%M") if self.scanned_at else "",
        }


class ScanWaba(db.Model):
    """One WABA found by the health scanner — one row per WABA per profile."""
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    waba_id      = db.Column(db.String(64), nullable=False, index=True)
    waba_name    = db.Column(db.String(255), default="", nullable=False)
    business_id  = db.Column(db.String(64), default="", nullable=False)
    profile_id   = db.Column(db.String(64), nullable=False, index=True)
    profile_name = db.Column(db.String(255), default="", nullable=False)
    # approved|appealable|in_review|permanent|restricted|error
    state        = db.Column(db.String(32), default="", nullable=False)
    appeal_sent  = db.Column(db.Boolean, default=False, nullable=False)
    scanned_at   = db.Column(db.DateTime, default=_now_sp, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "waba_id", name="uq_scanwaba_user_waba"),
    )

    def to_dict(self):
        return {
            "waba_id": self.waba_id,
            "waba_name": self.waba_name,
            "business_id": self.business_id,
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "state": self.state,
            "appeal_sent": self.appeal_sent,
            "scanned_at": self.scanned_at.strftime("%d/%m/%Y %H:%M") if self.scanned_at else "",
        }


class ScanProfileArchive(db.Model):
    """Snapshot of a ScanProfile row taken right before a "rescan all" wipes it.

    ScanProfile.profile_id is a primary key that gets reused on every scan, so
    a soft-delete flag on that same row can't preserve history across more
    than one rescan — rows are copied here first instead, grouped by batch_at
    so each rescan's snapshot stays distinguishable.
    """
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    batch_at     = db.Column(db.DateTime, nullable=False, index=True)
    profile_id   = db.Column(db.String(64), nullable=False)
    profile_name = db.Column(db.String(255), default="", nullable=False)
    outcome      = db.Column(db.String(32), default="", nullable=False)
    detail       = db.Column(db.Text, default="", nullable=False)
    scanned_at   = db.Column(db.DateTime)


class ScanWabaArchive(db.Model):
    """Snapshot of a ScanWaba row taken right before a "rescan all" wipes it."""
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    batch_at     = db.Column(db.DateTime, nullable=False, index=True)
    waba_id      = db.Column(db.String(64), nullable=False)
    waba_name    = db.Column(db.String(255), default="", nullable=False)
    business_id  = db.Column(db.String(64), default="", nullable=False)
    profile_id   = db.Column(db.String(64), nullable=False)
    profile_name = db.Column(db.String(255), default="", nullable=False)
    state        = db.Column(db.String(32), default="", nullable=False)
    appeal_sent  = db.Column(db.Boolean, default=False, nullable=False)
    scanned_at   = db.Column(db.DateTime)


class ChatMessage(db.Model):
    id              = db.Column(db.Integer,     primary_key=True)
    waba_id         = db.Column(db.String(64),  nullable=False, index=True)
    phone_number_id = db.Column(db.String(64),  nullable=False, index=True)
    contact_wa_id   = db.Column(db.String(32),  nullable=False, index=True)
    contact_name    = db.Column(db.String(255), default="", nullable=False)
    direction       = db.Column(db.String(4),   nullable=False)            # "in" or "out"
    msg_type        = db.Column(db.String(32),  default="text", nullable=False)
    body            = db.Column(db.Text,        default="", nullable=False)
    media_url       = db.Column(db.Text,        default="", nullable=False)
    wamid           = db.Column(db.String(128), default="", nullable=False, index=True)
    status          = db.Column(db.String(16),  default="sent", nullable=False)
    timestamp       = db.Column(db.DateTime,    default=_now_sp,   nullable=False, index=True)
    __table_args__  = (db.Index("ix_chat_conv", "waba_id", "phone_number_id", "contact_wa_id"),)


class PhotoModel(db.Model):
    """Saved profile picture — reusable across WABAs."""
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name       = db.Column(db.String(128), nullable=False)
    filename   = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=_now_sp, nullable=False)

    def to_dict(self):
        return {
            "id":         self.id,
            "name":       self.name,
            "url":        f"/photos/{self.id}/file",
            "created_at": self.created_at.strftime("%d/%m/%Y %H:%M") if self.created_at else "",
        }
