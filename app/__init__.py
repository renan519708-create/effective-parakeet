"""App factory: wires up config, database, login, blueprints, and
starts the background trading engine. `python manage.py runserver` or
a WSGI server (gunicorn "app:create_app()") both go through here."""

from datetime import timezone, timedelta

from flask import Flask
from sqlalchemy import text

from app.config import Config
from app.extensions import db, login_manager

BRASILIA_TZ = timezone(timedelta(hours=-3))


def _brasilia_filter(value):
    """Jinja filter: renders a UTC-aware datetime (every timestamp in
    this app is stored via _utcnow()) in Brasília local time (UTC-3,
    fixed, no DST) instead of the raw UTC value templates were printing
    directly -- confirmed live 2026-09-15: showing raw UTC (+00:00)
    timestamps read as "o horario esta errado" to a Brasília-based
    operator, since it's 3 hours ahead of their own wall clock."""
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M:%S")


def _patch_schema():
    """db.create_all() only creates tables that don't exist yet -- it
    never alters an existing table's columns. Since this app runs
    against a live Postgres DB with real campaign data and has no
    Alembic migrations (deliberate v1 choice, see design doc), a column
    added to a model after the first deploy needs to be added here too,
    or it silently never appears on Render. `ADD COLUMN IF NOT EXISTS`
    is idempotent, so this is safe to run on every startup."""
    statements = [
        "ALTER TABLE campaign_symbols ADD COLUMN IF NOT EXISTS entry_price DOUBLE PRECISION",
        "ALTER TABLE campaign_symbols ADD COLUMN IF NOT EXISTS exit_price DOUBLE PRECISION",
        "ALTER TABLE follower_settings ADD COLUMN IF NOT EXISTS trade_size_usd DOUBLE PRECISION DEFAULT 10.0",
        "ALTER TABLE follower_settings ADD COLUMN IF NOT EXISTS daily_composto_capital_usd DOUBLE PRECISION DEFAULT 0.0",
        "ALTER TABLE autobot_positions ADD COLUMN IF NOT EXISTS leverage INTEGER",
    ]
    for stmt in statements:
        db.session.execute(text(stmt))
    db.session.commit()


def create_app(config_class=Config, start_engine=True):
    app = Flask(__name__)
    app.config.from_object(config_class)

    missing = [name for name in config_class.REQUIRED if not app.config.get(name)]
    if missing:
        raise RuntimeError(
            "Variaveis de ambiente obrigatorias ausentes: " + ", ".join(missing) +
            " -- ver README.md para a lista completa antes de rodar o servidor."
        )

    db.init_app(app)
    login_manager.init_app(app)
    app.jinja_env.filters["brasilia"] = _brasilia_filter

    from app import models  # noqa: F401 -- registers models with SQLAlchemy

    @login_manager.user_loader
    def load_user(user_id):
        return models.User.query.get(int(user_id))

    from app.auth import auth_bp
    from app.operator import operator_bp
    from app.follower import follower_bp
    from app.results import results_bp
    from app.autobot import autobot_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(operator_bp)
    app.register_blueprint(follower_bp)
    app.register_blueprint(results_bp)
    app.register_blueprint(autobot_bp)

    with app.app_context():
        db.create_all()
        _patch_schema()

    if start_engine:
        from app.engine import start_background_engine
        from app.autobot_engine import start_background_engine as start_autobot_engine
        start_background_engine(app)
        start_autobot_engine(app)

    return app
