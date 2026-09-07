"""App factory: wires up config, database, login, blueprints, and
starts the background trading engine. `python manage.py runserver` or
a WSGI server (gunicorn "app:create_app()") both go through here."""

from flask import Flask

from app.config import Config
from app.extensions import db, login_manager


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

    from app import models  # noqa: F401 -- registers models with SQLAlchemy

    @login_manager.user_loader
    def load_user(user_id):
        return models.User.query.get(int(user_id))

    from app.auth import auth_bp
    from app.operator import operator_bp
    from app.follower import follower_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(operator_bp)
    app.register_blueprint(follower_bp)

    with app.app_context():
        db.create_all()

    if start_engine:
        from app.engine import start_background_engine
        start_background_engine(app)

    return app
