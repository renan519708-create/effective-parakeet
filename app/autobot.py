"""'Auto-bot': each user runs the Kairi strategy on their OWN account,
independently -- no operator, no campaign, nothing replicated between
accounts (see app/autobot_engine.py). This blueprint is just account
management (credential, capital/leverage, on/off, manual close); every
real order is placed by the engine's own background loop, same
separation of concerns as operator.py/follower.py vs engine.py."""

from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.autobot_engine import KAIRI_LOWER, KAIRI_STOP_PCT, KAIRI_UPPER, NUM_SLOTS, _build_autobot_broker, _place_live_exit
from app.binance_broker import BinanceBroker
from app.crypto import encrypt_secret
from app.engine import price_roi_pct
from app.extensions import db
from app.models import AutoBotCredential, AutoBotPosition, AutoBotSettings, OrderLog

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

    # Every Kairi signal that tried to open for this account but failed
    # (leverage, sizing/MIN_NOTIONAL, order rejection, etc.) -- without
    # this, a signal that fires but silently fails to open has zero
    # visibility anywhere on this page (confirmed live 2026-09-15: "UNI
    # abriu no dashboard do gg-shot-monitor e nao abriu aqui, por que?"
    # with no way to tell "never fired here" from "fired and failed").
    # Same idea as the operator dashboard's open_failures panel.
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_open_failures = (
        OrderLog.query
        .filter_by(user_id=current_user.id, order_type="open", status="failed")
        .filter(OrderLog.created_at >= since)
        .order_by(OrderLog.created_at.desc())
        .limit(20)
        .all()
    )

    # Summary scoreboard for the whole history, not just the 50 most
    # recent rows shown in the table below -- one aggregate query
    # rather than summing closed_positions in Python (which is capped
    # at 50 and would silently under-count for an active account).
    all_closed_q = AutoBotPosition.query.filter_by(user_id=current_user.id).filter(AutoBotPosition.status != "open")
    trade_count = all_closed_q.count()
    win_count = all_closed_q.filter(AutoBotPosition.status == "green").count()
    total_realized_pnl = db.session.query(db.func.coalesce(db.func.sum(AutoBotPosition.realized_pnl_usd), 0.0)).filter(
        AutoBotPosition.user_id == current_user.id, AutoBotPosition.status != "open",
    ).scalar()
    # Saldo total: the REAL current Binance balance, not
    # settings.capital_usd + total_realized_pnl. That formula was only
    # correct back when entries were sized off settings.capital_usd
    # directly -- since 2026-09-15 entries size off 10% of the REAL
    # balance instead (see autobot_engine.py's _check_entries_for_user),
    # so capital_usd is just a stale, user-edited number that no longer
    # tracks what the account actually has. The real balance already
    # reflects every deposit/withdrawal and every realized/unrealized
    # result, with no bookkeeping drift possible.
    #
    # % acumulado: computed against the IMPLIED starting balance
    # (saldo_total - total_realized_pnl), i.e. what the account would
    # have without the bot's own realized result -- same "% relative to
    # a starting basis" the user confirmed was the right idea for this
    # metric (2026-09-15, "ENTENDI O CALCULO, PODE MANTER ASSIM"), just
    # rebased onto the real balance instead of the stale capital_usd
    # field. Assumes no manual deposit/withdrawal happened mid-period on
    # top of the bot's own trades -- same limitation the old formula had.
    encryption_key = current_app.config["ENCRYPTION_KEY"]
    broker, broker_err = _build_autobot_broker(current_user.id, encryption_key, current_app.config["AUTOBOT_TESTNET"])
    real_balance, bal_err = broker.get_account_balance() if broker else (None, broker_err)
    if real_balance is not None:
        saldo_total = real_balance
        starting_balance = saldo_total - total_realized_pnl
        pct_total = (total_realized_pnl / starting_balance * 100) if starting_balance > 0 else 0.0
    else:
        # No valid credential yet, or the Binance call failed -- fall
        # back to the old capital_usd-based estimate rather than
        # showing nothing, and flag it as an estimate in the template.
        saldo_total = settings.capital_usd + total_realized_pnl
        pct_total = (total_realized_pnl / settings.capital_usd * 100) if settings.capital_usd > 0 else 0.0
    win_rate = (win_count / trade_count * 100) if trade_count > 0 else 0.0

    return render_template(
        "autobot_dashboard.html",
        settings=settings,
        has_key=cred is not None,
        key_valid=cred.is_valid if cred else None,
        open_positions=open_positions,
        closed_positions=closed_positions,
        recent_open_failures=recent_open_failures,
        num_slots=NUM_SLOTS,
        stop_pct=KAIRI_STOP_PCT,
        kairi_upper=KAIRI_UPPER,
        kairi_lower=KAIRI_LOWER,
        testnet=current_app.config["AUTOBOT_TESTNET"],
        trade_count=trade_count,
        win_rate=win_rate,
        total_realized_pnl=total_realized_pnl,
        pct_total=pct_total,
        saldo_total=saldo_total,
        saldo_is_real=real_balance is not None,
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


def _recalculate_from_real_trade(broker, position, default_leverage, until_ms=None):
    """Shared by the single-position and bulk 'Recalcular' actions:
    re-pulls one closed position's real fill from Binance's own trade
    history and overwrites close_price/result_pct/realized_pnl_usd/
    status. Returns (ok, message). `until_ms` bounds the search window
    on the Binance side (see get_last_trade_price's docstring) -- pass
    it in bulk runs so a symbol traded again later doesn't get
    mistaken for this position's own close."""
    real_price, trade_err = broker.get_last_trade_price(position.symbol, since_ms=position.entry_time, until_ms=until_ms)
    if real_price is None:
        return False, f"{position.symbol}: {trade_err}"

    leverage = position.leverage if position.leverage is not None else default_leverage
    raw_pct = price_roi_pct(position.direction.lower(), position.entry_price, real_price, 1)

    position.close_price = real_price
    position.result_pct = raw_pct
    position.realized_pnl_usd = position.allocated_usd * leverage * (raw_pct / 100)
    position.status = "green" if raw_pct >= 0 else "red"
    return True, f"{position.symbol}: {raw_pct:+.2f}% (${position.realized_pnl_usd:+.2f})"


@autobot_bp.route("/posicao/<int:position_id>/recalcular", methods=["POST"])
@login_required
def recalculate_position(position_id):
    """Re-pulls this ALREADY-CLOSED position's real result straight
    from Binance's own trade history and overwrites the stored
    close_price/result_pct/realized_pnl_usd/status -- for exactly the
    scenario _check_exit now handles going forward (a position found
    already flat, e.g. a fast liquidation, whose recorded result came
    from a stale theoretical price before that fix existed). Only
    touches rows the fix couldn't have reached automatically (this
    account's own already-closed positions), never an open one."""
    position = AutoBotPosition.query.get(position_id)
    if not position or position.user_id != current_user.id:
        flash("Posicao nao encontrada.", "error")
        return redirect(url_for("autobot.dashboard"))
    if position.status == "open":
        flash("Essa posicao ainda esta aberta -- nada a recalcular.", "error")
        return redirect(url_for("autobot.dashboard"))

    testnet = current_app.config["AUTOBOT_TESTNET"]
    encryption_key = current_app.config["ENCRYPTION_KEY"]
    broker, err = _build_autobot_broker(current_user.id, encryption_key, testnet)
    if not broker:
        flash(f"Nao foi possivel recalcular: {err}", "error")
        return redirect(url_for("autobot.dashboard"))

    settings = AutoBotSettings.query.get(current_user.id)
    ok, msg = _recalculate_from_real_trade(broker, position, settings.leverage if settings else 1)
    if not ok:
        flash(f"Nao encontrei um trade real da Binance pra recalcular: {msg}", "error")
        return redirect(url_for("autobot.dashboard"))
    db.session.commit()
    flash(f"Recalculado com o preco real da Binance -- {msg}.", "success")
    return redirect(url_for("autobot.dashboard"))


@autobot_bp.route("/historico/recalcular-tudo", methods=["POST"])
@login_required
def recalculate_all():
    """Bulk version of 'Recalcular' -- runs it over EVERY closed
    position in this account's history (not just the 50 shown), so the
    'Saldo total'/'PnL acumulado' scoreboard reflects Binance's real
    trade data end to end instead of whatever each row happened to
    record at close time (including rows closed before this session's
    price-accuracy fixes -- avgPrice truthy-string bug, missing
    real-entry-price check, theoretical-price fallback on an
    already-flat position). Bounds each lookup to
    [entry_time, close_time + 5min] via until_ms so a symbol traded
    again afterwards (common -- Kairi re-signals constantly) can't get
    mistaken for this row's own close, which the single-row button
    doesn't need to worry about since a human is checking one specific
    row they just saw."""
    testnet = current_app.config["AUTOBOT_TESTNET"]
    encryption_key = current_app.config["ENCRYPTION_KEY"]
    broker, err = _build_autobot_broker(current_user.id, encryption_key, testnet)
    if not broker:
        flash(f"Nao foi possivel recalcular: {err}", "error")
        return redirect(url_for("autobot.dashboard"))

    settings = AutoBotSettings.query.get(current_user.id)
    default_leverage = settings.leverage if settings else 1
    positions = AutoBotPosition.query.filter(
        AutoBotPosition.user_id == current_user.id,
        AutoBotPosition.status != "open",
    ).all()

    updated, failed = 0, []
    for position in positions:
        until_ms = (position.close_time + 5 * 60 * 1000) if position.close_time else None
        ok, msg = _recalculate_from_real_trade(broker, position, default_leverage, until_ms=until_ms)
        if ok:
            updated += 1
        else:
            failed.append(msg)
    db.session.commit()

    if failed:
        flash(f"Recalculadas {updated} de {len(positions)} operacoes. Sem trade real encontrado para: {'; '.join(failed[:10])}{' ...' if len(failed) > 10 else ''}.", "error")
    else:
        flash(f"Recalculadas {updated} operacoes com dados reais da Binance.", "success")
    return redirect(url_for("autobot.dashboard"))
