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
