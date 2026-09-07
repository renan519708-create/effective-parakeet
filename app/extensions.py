"""Shared extension instances -- created here (not in __init__.py) so
models.py and every blueprint can import `db`/`login_manager` without
a circular import back to the app factory."""

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
