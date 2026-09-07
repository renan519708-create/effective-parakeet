"""Signup (invite-code gated), login, logout. Small trusted-group
platform -- no public open registration (see design spec)."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required, login_user, logout_user, current_user
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models import FollowerSettings, InviteCode, User

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/")
def index():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    if current_user.can_operate:
        return redirect(url_for("operator.dashboard"))
    return redirect(url_for("follower.dashboard"))


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("follower.dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        code_value = request.form.get("invite_code", "").strip()

        invite = InviteCode.query.filter_by(code=code_value).first()
        if not invite or invite.is_used:
            flash("Codigo de convite invalido ou ja utilizado.", "error")
            return render_template("signup.html")
        if not email or "@" not in email:
            flash("Informe um email valido.", "error")
            return render_template("signup.html")
        if len(password) < 8:
            flash("A senha precisa ter pelo menos 8 caracteres.", "error")
            return render_template("signup.html")
        if User.query.filter_by(email=email).first():
            flash("Ja existe uma conta com esse email.", "error")
            return render_template("signup.html")

        user = User(email=email, password_hash=generate_password_hash(password), role="follower")
        db.session.add(user)
        db.session.flush()  # assigns user.id before the FK rows below
        db.session.add(FollowerSettings(user_id=user.id))
        invite.used_by_id = user.id
        from datetime import datetime, timezone
        invite.used_at = datetime.now(timezone.utc)
        db.session.commit()

        login_user(user)
        return redirect(url_for("follower.dashboard"))

    return render_template("signup.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("follower.dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash("Email ou senha incorretos.", "error")
            return render_template("login.html")
        login_user(user)
        if user.can_operate:
            return redirect(url_for("operator.dashboard"))
        return redirect(url_for("follower.dashboard"))

    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
