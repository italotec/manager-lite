import os
from datetime import timedelta

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///app.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_size": 50,
        "max_overflow": 100,
        "pool_timeout": 30,
        "pool_recycle": 1800,
        "pool_pre_ping": True,
        # Wait up to 30s for a SQLite write lock instead of failing instantly with
        # "database is locked" when many disparo jobs commit in parallel.
        "connect_args": {"timeout": 30},
    }

    # Session / login persistence — keeps the user logged in across
    # browser restarts and dev-server reloads.
    PERMANENT_SESSION_LIFETIME = timedelta(days=30)
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SAMESITE = "Lax"
    # Cookies must work over plain http on localhost, so don't force Secure.
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_SECURE = False

    # META
    META_API_VERSION = os.getenv("META_API_VERSION", "v18.0")
    META_UPLOAD_API_VERSION = os.getenv("META_UPLOAD_API_VERSION", "v21.0")
    META_APP_ID = os.getenv("META_APP_ID", "")
    META_REGISTER_PIN = os.getenv("META_REGISTER_PIN", "123456")

    # Seed admin login (created on first boot if no users exist)
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

    # Shared secret for the inbound sync endpoint (/api/v1/sync/users). Must match
    # LITE_SYNC_TOKEN in GERENCIADOR DE BMS. Empty => endpoint rejects everything.
    LITE_SYNC_TOKEN = os.getenv("LITE_SYNC_TOKEN", "85USGKdojLVSVNRCYjJNY3HkKRg9q281hKb6ZU3Rnc")

    # Public base URL the local agent uses to call back into this app's own
    # /api/v1/business-managers endpoint during the "Vincular ao Manager" flow.
    # Falls back to request.host_url at dispatch time when unset.
    MANAGER_BASE_URL = os.getenv("MANAGER_BASE_URL", "")
