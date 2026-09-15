"""Auto-bot engine: runs the Kairi mean-reversion strategy continuously
for every user who has it enabled, on their OWN account -- no operator,
no campaign, no redirect-to-leader between accounts (confirmed with the
user: "cada usuario roda nas suas contas, sem replicar nas contas dos
usuarios"). The Kairi signal itself (zone crossing) is the same for
everyone -- tracked once per symbol in AutoBotSymbolState -- but whether
and how much any given user trades it is entirely their own account's
business (own capital, own leverage, own slot/daily-stop limits).

Same "one account's failure never blocks another's" philosophy as
app/engine.py's campaign engine -- see run_tick.
"""

import concurrent.futures
import threading
import time
from datetime import datetime, timedelta, timezone

from app.binance_broker import BinanceBroker, parse_avg_price
from app.crypto import decrypt_secret
from app.engine import _log_order, price_roi_pct
from app.extensions import db
from app.kairi import compute_ema_series, compute_kairi_series
from app.kairi_outcome import evaluate_kairi_outcome
from app.klines import get_klines, get_latest_candle
from app.models import AutoBotCredential, AutoBotPosition, AutoBotSettings, AutoBotSymbolState, User
from app.universe import resolve_kairi_universe

# Validated live config, ported as-is from gg-shot-monitor's config.json
# (see gg-shot-monitor-live-trading-plan.md: 2-year backtest, out-of-
# sample, and ~testnet track record on this exact combination) -- not
# user-configurable here, only capital_usd/leverage are per the user's
# own request.
KAIRI_LENGTH = 10
KAIRI_UPPER = 2.0
KAIRI_LOWER = -2.0
KAIRI_STOP_PCT = 10.0
MAX_DAILY_STOPS = 2
NUM_SLOTS = 10
CANDLE_LOOKBACK = KAIRI_LENGTH + 10
UNIVERSE_TOP_N = 50
ENGINE_MAX_WORKERS = 10


def _zone_for(value, upper, lower):
    if value >= upper:
        return "overbought"
    if value <= lower:
        return "oversold"
    return "neutral"


def _build_autobot_broker(user_id, encryption_key, testnet):
    cred = AutoBotCredential.query.filter_by(user_id=user_id).first()
    if not cred or not cred.is_valid:
        return None, "sem credencial valida"
    try:
        api_key = decrypt_secret(cred.encrypted_api_key, encryption_key)
        api_secret = decrypt_secret(cred.encrypted_api_secret, encryption_key)
    except ValueError as e:
        return None, str(e)
    return BinanceBroker(api_key, api_secret, testnet=testnet), None


def _place_live_entry(broker, symbol, direction, margin_usd, price, leverage, user_id):
    """Same margin/notional convention as gg-shot-monitor's
    _place_live_entry: margin_usd is what the user actually commits;
    notional (what the order is actually sized at) is margin_usd *
    leverage. Getting this backwards was a real bug caught live there
    -- see run.py's docstring."""
    # Best-effort only, matching gg-shot-monitor's _place_live_entry: a
    # failure here doesn't change the real leverage risk (our own
    # liquidation model just assumes isolated margin), so it's not
    # logged as an order failure -- it never blocks the order below.
    broker.set_margin_type(symbol, "ISOLATED")

    _, err = broker.set_leverage(symbol, leverage)
    if err:
        _log_order(user_id, symbol, "BUY" if direction == "LONG" else "SELL", None, "open", None, "failed", f"falha ao definir alavancagem: {err}")
        return None, None

    side = "BUY" if direction == "LONG" else "SELL"
    notional_usd = margin_usd * leverage
    qty, err = broker.size_order_quantity(symbol, notional_usd, price)
    if err:
        _log_order(user_id, symbol, side, None, "open", None, "failed", str(err))
        return None, None
    order, err = broker.place_market_order(symbol, side, qty)
    if err:
        _log_order(user_id, symbol, side, qty, "open", None, "failed", str(err))
        return None, None
    _log_order(user_id, symbol, side, qty, "open", order.get("orderId"), "filled")
    # Prefer the REAL position's own reported entry price over the
    # order response's own avgPrice -- confirmed live 2026-09-15
    # (reported as a wrong/sign-flipped result_pct on ARB and another
    # symbol): a market order's immediate synchronous response can
    # report avgPrice before the fill is fully confirmed/settled,
    # especially on thinner-liquidity symbols, so it doesn't always
    # match what Binance's own position endpoint later shows as the
    # true fill. Same fix already applied to the campaign engine's
    # Case A (app/engine.py) -- this brings Auto-bot's own entry in
    # line with it.
    real_entry, entry_err = broker.get_position_entry_price(symbol)
    entry_price = real_entry if (not entry_err and real_entry) else (parse_avg_price(order) or price)
    return entry_price, qty


def _place_live_exit(broker, symbol, user_id):
    """Returns (price, confirmed_flat). confirmed_flat=True means the
    symbol is confirmed flat on the exchange right now (either just
    closed here, or already flat) -- ONLY then is it safe for the
    caller to mark its own AutoBotPosition closed. False means the
    real position's state is unknown or the close order itself failed
    -- it may STILL be open on Binance, so the caller must leave its
    own record "open" and retry next tick rather than silently losing
    track of a real position. Same "always the real fill, never a
    theoretical price" rule the campaign engine already follows
    (app/engine.py's _close_position) -- evaluate_kairi_outcome only
    decides WHEN/WHY to exit, never what price gets recorded."""
    pos_amt, err = broker.get_position_amt(symbol)
    if err:
        _log_order(user_id, symbol, "-", None, "close", None, "failed", err)
        return None, False
    if pos_amt == 0:
        return None, True  # already flat, nothing to close
    side = "SELL" if pos_amt > 0 else "BUY"
    qty = abs(pos_amt)
    order, err = broker.place_market_order(symbol, side, qty, reduce_only=True)
    if err:
        _log_order(user_id, symbol, side, qty, "close", None, "failed", err)
        return None, False
    _log_order(user_id, symbol, side, qty, "close", order.get("orderId"), "filled")
    return parse_avg_price(order), True


def _stops_today(user_id, now_ms, tz_offset_hours=-3):
    """Same Brasilia-calendar-day boundary as gg-shot-monitor's
    kairi_store.count_stops_today -- feeds the daily circuit breaker."""
    tz = timezone(timedelta(hours=tz_offset_hours))
    today = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(tz).date()
    since_ms = now_ms - 2 * 86400_000  # 2 days of slack covers any timezone edge
    rows = AutoBotPosition.query.filter(
        AutoBotPosition.user_id == user_id,
        AutoBotPosition.status.in_(["stopped", "liquidated"]),
        AutoBotPosition.close_time >= since_ms,
    ).all()
    return sum(
        1 for r in rows
        if datetime.fromtimestamp(r.close_time / 1000, tz=timezone.utc).astimezone(tz).date() == today
    )


def _update_symbol_signal(symbol, candles):
    """Returns "LONG"/"SHORT" if `symbol` just had a fresh, non-neutral
    zone crossing this tick, else None. Side effect: seeds/updates
    AutoBotSymbolState (global, shared by every user) -- must run
    inside an app context with an open DB session. Mirrors gg-shot-
    monitor's check_kairi_levels state machine (silent-seed on first
    sight, no repeat signal while the zone hasn't changed)."""
    if not candles or len(candles) < KAIRI_LENGTH:
        return None
    value = compute_kairi_series(candles, length=KAIRI_LENGTH)[-1]
    if value is None:
        return None
    zone = _zone_for(value, KAIRI_UPPER, KAIRI_LOWER)

    state = AutoBotSymbolState.query.get(symbol)
    if state is None:
        db.session.add(AutoBotSymbolState(symbol=symbol, zone=zone))
        return None
    if zone == state.zone:
        return None
    state.zone = zone
    if zone == "neutral":
        return None
    return "LONG" if zone == "oversold" else "SHORT"


def _check_exit(app, position_id, testnet, encryption_key):
    with app.app_context():
        try:
            position = AutoBotPosition.query.get(position_id)
            if not position or position.status != "open":
                return
            candles = get_klines(position.symbol, "1h", CANDLE_LOOKBACK, testnet)
            forming = get_latest_candle(position.symbol, "1h", testnet)
            seq = (candles or []) + ([forming] if forming else [])
            if not seq:
                return
            ema_series = compute_ema_series(seq, length=KAIRI_LENGTH)
            after = [(c, e) for c, e in zip(seq, ema_series) if c["close_time"] >= position.entry_time]
            if not after:
                return
            candles_after = [c for c, _ in after]
            ema_after = [e for _, e in after]
            status, exit_price, exit_time, result_pct, max_dd = evaluate_kairi_outcome(
                position.direction, position.entry_price, candles_after, ema_after, stop_pct=KAIRI_STOP_PCT,
            )
            position.max_drawdown_pct = max_dd
            if status == "open":
                db.session.commit()
                return

            broker, _err = _build_autobot_broker(position.user_id, encryption_key, testnet)
            if not broker:
                # Credential missing/invalid -- can't confirm a real
                # close either way. Persist the drawdown update and
                # leave status "open" so this retries next tick instead
                # of silently losing track of what may still be a real
                # open position.
                db.session.commit()
                return

            real_close_price, confirmed_flat = _place_live_exit(broker, position.symbol, position.user_id)
            if not confirmed_flat:
                # The close order itself failed -- the real position
                # may STILL be open on Binance. Never mark our own
                # record closed on an unconfirmed close; retry next
                # tick (OrderLog already has the real failure reason).
                db.session.commit()
                return
            # Prefer the close order's own real fill -- same rule the
            # campaign engine already follows. If the exchange was
            # ALREADY flat by the time we checked (a fast adverse move,
            # possibly a real liquidation, closed it before this tick
            # got here), _place_live_exit has no fill to report -- ask
            # Binance's own trade history for the real last fill
            # instead of trusting the strategy's THEORETICAL EMA/stop
            # price, which can be badly wrong (even the wrong sign) once
            # the real market has already moved well past what that
            # theoretical value assumed. The theoretical exit_price is
            # the last resort, only if even the real trade history is
            # unavailable.
            overridden_by_real_trade = False
            if real_close_price is not None:
                final_close_price = real_close_price
            else:
                last_trade_price, _trade_err = broker.get_last_trade_price(position.symbol, since_ms=position.entry_time)
                if last_trade_price is not None:
                    final_close_price = last_trade_price
                    overridden_by_real_trade = True
                else:
                    final_close_price = exit_price

            # The leverage THIS position actually opened with, captured
            # at entry -- not whatever the account's leverage is set to
            # NOW, which may have changed since (confirmed live
            # 2026-09-15: closing with current settings silently
            # recomputed the wrong $ PnL for a position that spanned a
            # leverage change). Old rows predating this column fall
            # back to current settings, same as before.
            if position.leverage is not None:
                leverage = position.leverage
            else:
                settings = AutoBotSettings.query.get(position.user_id)
                leverage = settings.leverage if settings else 1
            # Raw (unleveraged) price-move %, recomputed from the two
            # real fill prices rather than trusting the theoretical
            # exit_price -- leverage is applied only for the dollar
            # PnL below, matching _place_live_entry's own margin/
            # notional convention (notional = margin * leverage).
            raw_pct = price_roi_pct(position.direction.lower(), position.entry_price, final_close_price, 1)

            # When the theoretical status/exit_price got overridden by
            # the real trade history above, the strategy's own
            # green/red/stopped label (decided against the theoretical
            # price) can no longer be trusted either -- relabel from
            # the REAL sign instead of showing e.g. "green" next to a
            # real loss.
            if overridden_by_real_trade:
                status = "green" if raw_pct >= 0 else "red"

            position.status = status
            position.close_price = final_close_price
            position.close_time = exit_time
            position.closed_at = db.func.now()
            position.result_pct = raw_pct
            position.realized_pnl_usd = position.allocated_usd * leverage * (raw_pct / 100)
            db.session.commit()
        except Exception as e:  # noqa: BLE001 -- one position's bug must never stop the rest
            db.session.rollback()
            app.logger.error(f"[autobot] erro fechando posicao {position_id}: {e}")


def _check_entries_for_user(app, user_id, testnet, encryption_key, signals, prices, now_ms):
    with app.app_context():
        try:
            settings = AutoBotSettings.query.get(user_id)
            if not settings or not settings.enabled:
                return
            open_count = AutoBotPosition.query.filter_by(user_id=user_id, status="open").count()
            if open_count >= NUM_SLOTS:
                return
            stops_today = _stops_today(user_id, now_ms)
            if stops_today >= MAX_DAILY_STOPS:
                return

            broker, _err = _build_autobot_broker(user_id, encryption_key, testnet)
            if not broker:
                return

            margin_usd = settings.capital_usd / NUM_SLOTS
            for symbol, direction in signals.items():
                if open_count >= NUM_SLOTS or stops_today >= MAX_DAILY_STOPS:
                    break
                already_open = AutoBotPosition.query.filter_by(user_id=user_id, symbol=symbol, status="open").first()
                if already_open:
                    continue
                price = prices.get(symbol)
                if not price:
                    continue

                fill_price, qty = _place_live_entry(broker, symbol, direction, margin_usd, price, settings.leverage, user_id)
                if fill_price is None:
                    # _place_live_entry already logged the failure via
                    # _log_order -- committed below, once, along with
                    # any other symbol's OrderLog/AutoBotPosition from
                    # this same tick, so a failed order is never lost
                    # even when nothing in this tick actually opened.
                    continue
                db.session.add(AutoBotPosition(
                    user_id=user_id, symbol=symbol, direction=direction, entry_price=fill_price,
                    entry_time=now_ms, status="open", allocated_usd=margin_usd,
                    leverage=settings.leverage,
                ))
                open_count += 1
            db.session.commit()
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            app.logger.error(f"[autobot] erro processando entradas do usuario {user_id}: {e}")


def run_tick(app):
    testnet = app.config["AUTOBOT_TESTNET"]
    encryption_key = app.config["ENCRYPTION_KEY"]
    now_ms = int(time.time() * 1000)

    with app.app_context():
        # Only bother fetching candles for symbols that could actually
        # be traded (someone enabled) or are already open somewhere.
        any_enabled = AutoBotSettings.query.filter_by(enabled=True).count() > 0
        open_symbols = {p.symbol for p in AutoBotPosition.query.filter_by(status="open").all()}
        if not any_enabled and not open_symbols:
            return
        universe = set(resolve_kairi_universe(testnet, top_n=UNIVERSE_TOP_N)) if any_enabled else set()
        symbols = universe | open_symbols

    signals = {}
    prices = {}
    with app.app_context():
        for symbol in symbols:
            candles = get_klines(symbol, "1h", CANDLE_LOOKBACK, testnet)
            forming = get_latest_candle(symbol, "1h", testnet)
            seq = (candles or []) + ([forming] if forming else [])
            if not seq:
                continue
            prices[symbol] = seq[-1]["close"]
            if symbol in universe:
                direction = _update_symbol_signal(symbol, seq)
                if direction:
                    signals[symbol] = direction
        db.session.commit()

    # Exits first, so a slot freed this tick is available to entries
    # processed right after.
    with app.app_context():
        open_position_ids = [p.id for p in AutoBotPosition.query.filter_by(status="open").all()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=ENGINE_MAX_WORKERS) as pool:
        futures = [pool.submit(_check_exit, app, pid, testnet, encryption_key) for pid in open_position_ids]
        concurrent.futures.wait(futures)

    if signals:
        with app.app_context():
            user_ids = [u.id for u in User.query.join(AutoBotSettings).filter(AutoBotSettings.enabled.is_(True)).all()]
        with concurrent.futures.ThreadPoolExecutor(max_workers=ENGINE_MAX_WORKERS) as pool:
            futures = [
                pool.submit(_check_entries_for_user, app, uid, testnet, encryption_key, signals, prices, now_ms)
                for uid in user_ids
            ]
            concurrent.futures.wait(futures)


def run_forever(app):
    """Blocking loop, same 'never let one bad cycle kill the loop'
    shape as app/engine.py's run_forever. Meant to be the main worker
    process's blocking call (see manage.py's run_engine)."""
    interval = app.config["AUTOBOT_TICK_SECONDS"]
    while True:
        try:
            run_tick(app)
        except Exception as e:  # noqa: BLE001
            app.logger.error(f"[autobot] erro no ciclo: {e}")
        time.sleep(interval)


def start_background_engine(app):
    """Local/dev convenience only -- same thread-wrapper shape as
    app/engine.py's start_background_engine. Production runs this as
    the main blocking loop of the dedicated worker process instead
    (see manage.py's run_engine)."""
    thread = threading.Thread(target=run_forever, args=(app,), daemon=True, name="autobot-engine")
    thread.start()
    return thread
