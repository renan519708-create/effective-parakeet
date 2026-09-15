"""'Auto-bot': each user runs the Kairi strategy on their OWN account,
independently -- no operator, no campaign, nothing replicated between
accounts (see app/autobot_engine.py). This blueprint is just account
management (credential, capital/leverage, on/off, manual close); every
real order is placed by the engine's own background loop, same
separation of concerns as operator.py/follower.py vs engine.py."""

from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.autobot_engine import KAIRI_LOWER, KAIRI_STOP_PCT, KAIRI_UPPER, NUM_SLOTS, _build_autobot_broker, _place_live_exit
from app.binance_broker import BinanceBroker
from app.crypto import encrypt_secret
from app.engine import price_roi_pct
from app.extensions import db
from app.models import AutoBotCredential, AutoBotPosition, AutoBotSettings

autobot_bp = Blueprint("autobot", __name__, url_prefix="/autobot")


def _get_or_create_settings():
    settings = AutoBotSettings.query.get(current_user.id)
    if settings is None:
        settings = AutoBotSettings(user_id=current_user.id)
        db.session.add(settings)
        db.session.commit()
    return settings


@autobot_bp.route("/")
@login_required
def dashboard():
    settings = _get_or_create_settings()
    cred = AutoBotCredential.query.filter_by(user_id=current_user.id).first()
    open_positions = AutoBotPosition.query.filter_by(user_id=current_user.id, status="open").order_by(AutoBotPosition.opened_at.desc()).all()
    closed_positions = AutoBotPosition.query.filter_by(user_id=current_user.id).filter(AutoBotPosition.status != "open").order_by(AutoBotPosition.closed_at.desc()).limit(50).all()
    return render_template(
        "autobot_dashboard.html",
        settings=settings,
        has_key=cred is not None,
        key_valid=cred.is_valid if cred else None,
        open_positions=open_positions,
        closed_positions=closed_positions,
        num_slots=NUM_SLOTS,
        stop_pct=KAIRI_STOP_PCT,
        kairi_upper=KAIRI_UPPER,
        kairi_lower=KAIRI_LOWER,
        testnet=current_app.config["AUTOBOT_TESTNET"],
    )


@autobot_bp.route("/chave", methods=["POST"])
@login_required
def save_credential():
    api_key = request.form.get("api_key", "").strip()
    api_secret = request.form.get("api_secret", "").strip()
    if not api_key or not api_secret:
        flash("Preencha a API key e a API secret do Auto-bot.", "error")
        return redirect(url_for("autobot.dashboard"))

    testnet = current_app.config["AUTOBOT_TESTNET"]
    broker = BinanceBroker(api_key, api_secret, testnet=testnet)
    balance, err = broker.get_account_balance()
    if err:
        flash(f"Nao foi possivel validar a chave do Auto-bot (NAO foi salva): {err}", "error")
        return redirect(url_for("autobot.dashboard"))

    encryption_key = current_app.config["ENCRYPTION_KEY"]
    encrypted_key = encrypt_secret(api_key, encryption_key)
    encrypted_secret = encrypt_secret(api_secret, encryption_key)

    cred = AutoBotCredential.query.filter_by(user_id=current_user.id).first()
    if cred is None:
        cred = AutoBotCredential(user_id=current_user.id)
        db.session.add(cred)
    cred.encrypted_api_key = encrypted_key
    cred.encrypted_api_secret = encrypted_secret
    cred.is_valid = True
    cred.last_validated_at = datetime.now(timezone.utc)
    db.session.commit()

    ambiente = "Demo Trading (testnet)" if testnet else "Binance real (mainnet)"
    flash(f"Chave do Auto-bot validada e salva -- saldo disponivel em {ambiente}: ${balance:.2f}", "success")
    return redirect(url_for("autobot.dashboard"))


@autobot_bp.route("/configuracoes", methods=["POST"])
@login_required
def save_settings():
    settings = _get_or_create_settings()
    try:
        capital_usd = float(request.form.get("capital_usd", settings.capital_usd).replace(",", "."))
        leverage = int(request.form.get("leverage", settings.leverage))
    except ValueError:
        flash("Valores invalidos.", "error")
        return redirect(url_for("autobot.dashboard"))
    if capital_usd < 50:
        flash("Capital minimo de $50 (cada uma das 10 fatias precisa cobrir o notional minimo da Binance).", "error")
        return redirect(url_for("autobot.dashboard"))
    if leverage < 1 or leverage > 20:
        flash("Alavancagem deve ficar entre 1x e 20x.", "error")
        return redirect(url_for("autobot.dashboard"))
    settings.capital_usd = capital_usd
    settings.leverage = leverage
    db.session.commit()
    flash("Configuracoes do Auto-bot salvas.", "success")
    return redirect(url_for("autobot.dashboard"))


@autobot_bp.route("/ligar", methods=["POST"])
@login_required
def toggle_enabled():
    settings = _get_or_create_settings()
    cred = AutoBotCredential.query.filter_by(user_id=current_user.id).first()
    if not settings.enabled and (not cred or not cred.is_valid):
        flash("Cadastre uma chave de API valida antes de ligar o Auto-bot.", "error")
        return redirect(url_for("autobot.dashboard"))
    settings.enabled = not settings.enabled
    db.session.commit()
    if settings.enabled:
        flash("Auto-bot ligado -- novas entradas serao abertas a partir do proximo ciclo.", "success")
    else:
        flash("Auto-bot desligado -- para de abrir posicoes novas. Posicoes ja abertas continuam sendo geridas (stop/EMA) ate fecharem sozinhas.", "success")
    return redirect(url_for("autobot.dashboard"))


@autobot_bp.route("/posicao/<int:position_id>/encerrar", methods=["POST"])
@login_required
def close_position(position_id):
    position = AutoBotPosition.query.get(position_id)
    if not position or position.user_id != current_user.id:
        flash("Posicao nao encontrada.", "error")
        return redirect(url_for("autobot.dashboard"))
    if position.status != "open":
        flash("Essa posicao ja esta fechada.", "error")
        return redirect(url_for("autobot.dashboard"))

    testnet = current_app.config["AUTOBOT_TESTNET"]
    encryption_key = current_app.config["ENCRYPTION_KEY"]
    broker, err = _build_autobot_broker(current_user.id, encryption_key, testnet)
    if not broker:
        flash(f"Nao foi possivel encerrar: {err}", "error")
        return redirect(url_for("autobot.dashboard"))

    real_close_price, confirmed_flat = _place_live_exit(broker, position.symbol, current_user.id)
    if not confirmed_flat:
        # The close order itself failed -- the real position may STILL
        # be open on Binance. Never mark our own record closed on an
        # unconfirmed close. OrderLog already has the real reason.
        flash("A ordem de fechamento falhou -- a posicao pode continuar aberta na Binance. Confira o log de ordens e tente de novo.", "error")
        return redirect(url_for("autobot.dashboard"))

    if position.leverage is not None:
        leverage = position.leverage
    else:
        settings = AutoBotSettings.query.get(current_user.id)
        leverage = settings.leverage if settings else 1
    # confirmed_flat with no fill price means the exchange was already
    # flat (nothing left to close, e.g. it was closed manually on
    # Binance itself) -- no real fill to compute a % move from, so
    # this records a flat 0% result rather than guessing.
    raw_pct = price_roi_pct(position.direction.lower(), position.entry_price, real_close_price, 1) if real_close_price is not None else 0.0
    real_close_price = real_close_price if real_close_price is not None else position.entry_price

    position.status = "green" if raw_pct >= 0 else "red"
    position.close_price = real_close_price
    position.close_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    position.closed_at = datetime.now(timezone.utc)
    position.result_pct = raw_pct
    position.realized_pnl_usd = position.allocated_usd * leverage * (raw_pct / 100)
    db.session.commit()
    flash(f"Posicao em {position.symbol} encerrada manualmente ({raw_pct:+.2f}%).", "success")
    return redirect(url_for("autobot.dashboard"))


@autobot_bp.route("/historico/limpar", methods=["POST"])
@login_required
def clear_history():
    """Deletes this account's own CLOSED AutoBotPosition rows only
    (green/red/stopped/liquidated) -- e.g. to start tracking cleanly
    after testnet-era trades mixed into the same history as real
    mainnet ones. Never touches a status="open" row: that would orphan
    a real, still-open Binance position with no record left for the
    engine (or the operator) to ever close it against."""
    deleted = AutoBotPosition.query.filter(
        AutoBotPosition.user_id == current_user.id,
        AutoBotPosition.status != "open",
    ).delete(synchronize_session=False)
    db.session.commit()
    flash(f"Historico limpo -- {deleted} operacao(oes) encerrada(s) removida(s). Posicoes abertas nao foram tocadas.", "success")
    return redirect(url_for("autobot.dashboard"))
