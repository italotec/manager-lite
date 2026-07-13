from flask import Flask, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user, logout_user
from flask_sock import Sock
from .config import Config

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login_get"
sock = Sock()

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)
    sock.init_app(app)

    # Blueprints
    from .routes.auth import bp as auth_bp
    from .routes.wabas import bp as wabas_bp
    from .routes.dashboard import bp as dashboard_bp
    from .routes.disparar import bp as disparar_bp
    from .routes.templates_bp import bp as templates_bp
    from .routes.api import bp as api_bp
    from .routes.docs import bp as docs_bp
    from .routes.admin import bp as admin_bp
    from .routes.sync import bp as sync_bp
    from .routes.account import bp as account_bp
    from .routes.photos_bp import bp as photos_bp
    from .routes.cartoes import bp as cartoes_bp
    from .routes.verificar import bp as verificar_bp
    from .routes.scanner import bp as scanner_bp
    from .routes.agent_ws import bp as agent_ws_bp, handle_ws as agent_handle_ws
    from .routes.waba_detail import bp as waba_detail_bp
    from .routes.webhook import bp as webhook_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(wabas_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(disparar_bp)
    app.register_blueprint(templates_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(docs_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(sync_bp)
    app.register_blueprint(account_bp)
    app.register_blueprint(photos_bp)
    app.register_blueprint(cartoes_bp)
    app.register_blueprint(verificar_bp)
    app.register_blueprint(scanner_bp)
    app.register_blueprint(agent_ws_bp)
    app.register_blueprint(waba_detail_bp)
    app.register_blueprint(webhook_bp)

    sock.route("/agent/ws")(agent_handle_ws)

    # Block banned users everywhere (force logout)
    @app.before_request
    def block_banned():
        if current_user.is_authenticated and getattr(current_user, "is_banned", False):
            logout_user()
            flash("Sua conta está banida. Fale com o suporte.", "error")
            return redirect(url_for("auth.login_get"))

    with app.app_context():
        from . import models  # noqa
        db.create_all()

        # Enable WAL mode — allows concurrent reads while writing
        db.session.execute(db.text("PRAGMA journal_mode=WAL"))
        db.session.commit()

        db.session.execute(db.text(
            "CREATE INDEX IF NOT EXISTS ix_chat_message_timestamp ON chat_message (timestamp)"
        ))
        db.session.commit()

        # Add new columns to existing DBs (create_all won't add new columns)
        cols = [c["name"] for c in db.inspect(db.engine).get_columns("user")]
        if "source_id" not in cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN source_id INTEGER"))
            db.session.commit()
        db.session.execute(db.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_user_source_id ON user (source_id)"
        ))
        db.session.commit()

        if "sync_enabled" not in cols:
            db.session.execute(db.text(
                "ALTER TABLE user ADD COLUMN sync_enabled BOOLEAN NOT NULL DEFAULT 1"))
            db.session.commit()

        if "share_partner_business_id" not in cols:
            db.session.execute(db.text(
                "ALTER TABLE user ADD COLUMN share_partner_business_id VARCHAR(64)"))
            db.session.commit()
        if "share_meta_token" not in cols:
            db.session.execute(db.text(
                "ALTER TABLE user ADD COLUMN share_meta_token TEXT"))
            db.session.commit()

        vp_cols = [c["name"] for c in db.inspect(db.engine).get_columns("verificar_profile")]
        if "pending_partner_business_id" not in vp_cols:
            db.session.execute(db.text(
                "ALTER TABLE verificar_profile ADD COLUMN pending_partner_business_id VARCHAR(64)"))
            db.session.commit()
        if "pending_partner_name" not in vp_cols:
            db.session.execute(db.text(
                "ALTER TABLE verificar_profile ADD COLUMN pending_partner_name VARCHAR(255)"))
            db.session.commit()

        # Recover DisparoJobs left "running"/"queued" by a previous restart. The
        # Disparou stamp is only written by _finish() at job end, so a job whose
        # process died mid-send never stamped it — recover it here from the send log.
        from .models import DisparoJob
        from .services.disparar_service import stamp_disparo_events, count_sent_from_log
        stuck = DisparoJob.query.filter(DisparoJob.status.in_(["running", "queued"])).all()
        for j in stuck:
            try:
                if getattr(j, "waba_id", "") and not getattr(j, "skip_log", False):
                    recovered = count_sent_from_log(j.user_id, j.id)
                    if recovered > 0:
                        stamp_disparo_events(j.user_id, j.waba_id, recovered)
                        j.sent = recovered
            except Exception:
                pass
            j.status = "stopped"
            j.last_message = "Interrompido: servidor reiniciou."
        if stuck:
            db.session.commit()

        # Seed a single admin login on first boot
        from .models import User
        if User.query.count() == 0:
            admin = User(username=Config.ADMIN_USERNAME, is_admin=True, is_banned=False)
            admin.set_password(Config.ADMIN_PASSWORD)
            admin.generate_api_key()
            db.session.add(admin)
            db.session.commit()

        # Backfill api_key for any user that doesn't have one yet
        users_without_key = User.query.filter(
            (User.api_key.is_(None)) | (User.api_key == "")
        ).all()
        for u in users_without_key:
            u.generate_api_key()
        if users_without_key:
            db.session.commit()

    return app
