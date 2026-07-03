from flask import Flask, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user, logout_user
from .config import Config

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login_get"

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)

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

    app.register_blueprint(auth_bp)
    app.register_blueprint(wabas_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(disparar_bp)
    app.register_blueprint(templates_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(docs_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(sync_bp)

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

        # Add new columns to existing DBs (create_all won't add new columns)
        cols = [c["name"] for c in db.inspect(db.engine).get_columns("user")]
        if "source_id" not in cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN source_id INTEGER"))
            db.session.commit()
        db.session.execute(db.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_user_source_id ON user (source_id)"
        ))
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
