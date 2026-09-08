"""'Minha Conta': each follower manages their own Binance API key, risk
settings, and sees only their own positions/history. Never renders a
decrypted secret back to the browser -- once saved, the form shows a
masked placeholder, not the real value (see app/crypto.py). Also where
the Owner generates invite codes (moved here from the operator
dashboard -- it's account/access management, not campaign control)."""

import secrets
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.binance_broker import BinanceBroker
from app.crypto import encrypt_secret
from app.extensions import db
from app.models import ApiCredential, FollowerAllocation, InviteCode, OrderLog, Position
from app.operator import owner_required

follower_bp = Blueprint("follower", __name__, url_prefix="/conta")


@follower_bp.route("/")
@login_required
def dashboard():
    open_positions = Position.query.filter_by(user_id=current_user.id, status="open").order_by(Position.opened_at.desc()).all()
    closed_positions = Position.query.filter_by(user_id=current_user.id, status="closed").order_by(Position.closed_at.desc()).limit(100).all()
    has_key = current_user.api_credential is not None
    is_owner = current_user.role == "owner"
    invites = InviteCode.query.order_by(InviteCode.created_at.desc()).limit(20).all() if is_owner else []
    return render_template(
        "follower_dashboard.html",
        settings=current_user.settings,
        has_key=has_key,
        key_valid=current_user.api_credential.is_valid if has_key else None,
        open_positions=open_positions,
        closed_positions=closed_positions,
        is_owner=is_owner,
        invites=invites,
    )


@follower_bp.route("/convites/gerar", methods=["POST"])
@owner_required
def create_invite():
    code = secrets.token_urlsafe(9)
    db.session.add(InviteCode(code=code, created_by_id=current_user.id))
    db.session.commit()
    flash(f"Codigo de convite gerado: {code}", "success")
    return redirect(url_for("follower.dashboard"))


@follower_bp.route("/chave", methods=["POST"])
@login_required
def save_api_key(reveal=None):
    api_key = request.form.get("api_key", "").strip()
    api_secret = request.form.get("api_secret", "").strip()
    if not api_key or not api_secret:
        flash("Preencha a API key e a API secret.", "error")
        return redirect(url_for("follower.dashboard"))

    testnet = current_app.config["BINANCE_TESTNET"]
    broker = BinanceBroker(api_key, api_secret, testnet=testnet)
    balance, err = broker.get_account_balance()
    if err:
        flash(f"Nao foi possivel validar a chave (a chave NAO foi salva): {err}", "error")
        return redirect(url_for("follower.dashboard"))

    encryption_key = current_app.config["ENCRYPTION_KEY"]
    encrypted_key = encrypt_secret(api_key, encryption_key)
    encrypted_secret = encrypt_secret(api_secret, encryption_key)

    cred = current_user.api_credential
    if cred is None:
        cred = ApiCredential(user_id=current_user.id)
        db.session.add(cred)
    cred.encrypted_api_key = encrypted_key
    cred.encrypted_api_secret = encrypted_secret
    cred.is_valid = True
    cred.last_validated_at = datetime.now(timezone.utc)
    db.session.commit()

    flash(f"Chave validada e salva -- saldo disponivel: ${balance:.2f}", "success")
    return redirect(url_for("follower.dashboard"))


@follower_bp.route("/configuracoes", methods=["POST"])
@login_required
def save_settings():
    settings = current_user.settings
    try:
        settings.risk_pct = max(0.1, min(100.0, float(request.form.get("risk_pct", settings.risk_pct))))
        settings.leverage = max(1, int(request.form.get("leverage", settings.leverage)))
        settings.max_drawdown_pct = max(1.0, min(99.0, float(request.form.get("max_drawdown_pct", settings.max_drawdown_pct))))
    except ValueError:
        flash("Valores invalidos.", "error")
        return redirect(url_for("follower.dashboard"))
    settings.max_drawdown_enabled = request.form.get("max_drawdown_enabled") == "on"
    db.session.commit()
    flash("Configuracoes salvas.", "success")
    return redirect(url_for("follower.dashboard"))


@follower_bp.route("/seguir", methods=["POST"])
@login_required
def toggle_following():
    if not current_user.api_credential or not current_user.api_credential.is_valid:
        flash("Cadastre uma chave de API valida antes de seguir operacoes.", "error")
        return redirect(url_for("follower.dashboard"))
    settings = current_user.settings
    settings.following_enabled = not settings.following_enabled
    db.session.commit()
    flash("Seguindo operacoes." if settings.following_enabled else "Parou de seguir operacoes -- posicoes abertas serao encerradas no proximo ciclo.", "success")
    return redirect(url_for("follower.dashboard"))
