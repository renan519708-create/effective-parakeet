"""The copy-trading engine: pure calculation functions (unit-tested in
test_engine.py) plus the imperative tick loop that actually places real
Binance orders. Runs in its own daemon thread (see start_background_engine),
independent of any HTTP request or open browser tab -- a real stop-loss
must keep being checked even with zero site traffic.

Same "one failure never stops the whole cycle" philosophy as
gg-shot-monitor's background_loop: every follower is handled inside its
own try/except, logged to OrderLog, and never allowed to block anyone
else's account.
"""

import concurrent.futures
import threading
import time

import requests

from app.binance_broker import BinanceBroker, TESTNET_BASE_URL, MAINNET_BASE_URL
from app.crypto import decrypt_secret
from app.extensions import db
from app.models import (
    Campaign, CampaignSymbol, FollowerAllocation, FollowerCampaignState,
    FollowerSettings, OrderLog, Position, User,
)


# ---------------------------------------------------------------------------
# Pure functions -- no network, no DB. Fully unit-testable.
# ---------------------------------------------------------------------------

def price_roi_pct(direction, entry_price, current_price, leverage):
    """ROI% on margin, matching backtest_lab.html's pnlPctForTrade sign
    convention: positive = favorable, negative = adverse, already
    scaled by leverage (so stop_pct is compared directly against this,
    not against the raw unleveraged price move)."""
    raw = (current_price - entry_price) / entry_price
    if direction == "short":
        raw = -raw
    return raw * 100 * leverage


def is_stop_triggered(direction, entry_price, current_price, leverage, stop_pct):
    return price_roi_pct(direction, entry_price, current_price, leverage) <= -stop_pct


def pick_redirect_leader(open_rois):
    """open_rois: {symbol: roi_pct} for this account's other still-open
    positions. Returns the symbol with the best (highest) ROI, or None
    if there isn't one -- mirrors the prototype's redirectCapitalToLeader
    loop (bestScore starts at -Infinity, so an empty dict yields None)."""
    if not open_rois:
        return None
    return max(open_rois, key=open_rois.get)


def is_drawdown_triggered(peak_value, current_value, max_drawdown_pct):
    if peak_value <= 0:
        return False
    drawdown_pct = (peak_value - current_value) / peak_value * 100
    return drawdown_pct >= max_drawdown_pct


def compute_initial_slice(balance, risk_pct, num_symbols):
    if num_symbols <= 0 or balance <= 0:
        return 0.0
    return (balance * (risk_pct / 100)) / num_symbols


# ---------------------------------------------------------------------------
# Imperative engine -- DB + real Binance orders.
# ---------------------------------------------------------------------------

def fetch_prices(symbols, testnet):
    """One public, unauthenticated call for every symbol's current
    price -- same shape as gg-shot-monitor's data_fetcher.get_all_prices,
    shared across every follower this tick (price is the same for
    everyone; only order placement is per-account)."""
    base_url = TESTNET_BASE_URL if testnet else MAINNET_BASE_URL
    resp = requests.get(f"{base_url}/fapi/v1/ticker/price", timeout=10)
    resp.raise_for_status()
    wanted = set(symbols)
    return {row["symbol"]: float(row["price"]) for row in resp.json() if row["symbol"] in wanted}


def _log_order(user_id, symbol, side, qty, order_type, binance_order_id, status, error_message=None):
    db.session.add(OrderLog(
        user_id=user_id, symbol=symbol, side=side, qty=qty, order_type=order_type,
        binance_order_id=binance_order_id, status=status, error_message=error_message,
    ))


def _build_broker(user, encryption_key, testnet):
    cred = user.api_credential
    if not cred or not cred.is_valid:
        return None, "sem credencial valida"
    try:
        api_key = decrypt_secret(cred.encrypted_api_key, encryption_key)
        api_secret = decrypt_secret(cred.encrypted_api_secret, encryption_key)
    except ValueError as e:
        return None, str(e)
    return BinanceBroker(api_key, api_secret, testnet=testnet), None


def _get_or_create_state(campaign_id, user_id):
    state = FollowerCampaignState.query.filter_by(campaign_id=campaign_id, user_id=user_id).first()
    if not state:
        state = FollowerCampaignState(campaign_id=campaign_id, user_id=user_id, peak_portfolio_usd=0.0, status="active")
        db.session.add(state)
    return state


def _close_position(broker, position, fallback_price, reason, user_id, leverage, allocated_usd):
    """Closes one real position (reduce-only). Records the REAL fill
    price (the close order's own avgPrice) as the exit, not the ticker
    price used to decide to close -- a market order can slip between
    that decision and the actual fill, same lesson already learned the
    hard way on the paper-tracked Kairi strategy (see gg-shot-monitor).
    Returns (closed: bool, realized_pnl_usd: float | None)."""
    side = "SELL" if position.side == "long" else "BUY"
    pos_amt, err_amt = broker.get_position_amt(position.symbol)
    if err_amt or pos_amt is None:
        _log_order(user_id, position.symbol, side, None, "close", None, "failed", str(err_amt))
        return False, None
    qty = abs(pos_amt)
    if qty <= 0:
        return True, 0.0  # already flat on the exchange, nothing to do

    order, err = broker.place_market_order(position.symbol, side, qty, reduce_only=True)
    if err:
        _log_order(user_id, position.symbol, side, qty, "close", None, "failed", str(err))
        return False, None

    exit_price = float(order.get("avgPrice") or fallback_price)
    _log_order(user_id, position.symbol, side, qty, "close", order.get("orderId"), "filled")
    position.status = "closed"
    position.close_price = exit_price
    position.closed_at = db.func.now()
    position.close_reason = reason
    roi = price_roi_pct(position.side, position.entry_price, exit_price, leverage)
    position.realized_pnl_usd = allocated_usd * (roi / 100)
    return True, position.realized_pnl_usd


def run_tick(app):
    """One full pass over the single active/stopping campaign (if any).
    Meant to be called repeatedly by start_background_engine.

    Every account is processed in its own worker thread (ThreadPoolExecutor,
    ENGINE_MAX_WORKERS at a time) with its own Flask app context -- Flask-
    SQLAlchemy's session is scoped per app context, so each thread gets its
    own independent DB session rather than fighting over one shared session.
    Real Binance calls are I/O-bound, so this is genuine concurrency, not
    just cosmetic: with many accounts, "Encerrar operacoes" (or a stop-loss
    during a fast move) closes everyone at roughly the same time instead of
    working through the list one account at a time. Each account's own
    try/except still means one bad account never blocks the rest.

    Campaign.status:
    - "active": the normal Case A-D flow in _process_follower.
    - "stopping": operator clicked "Encerrar operacoes" -- the HTTP request
      handler just flips this flag and returns immediately (closing many
      real positions synchronously inside a web request would be slow and
      could time out); this tick is what actually places the real close
      orders, for every account with any open position regardless of their
      own following_enabled toggle. Once no open positions remain
      campaign-wide, flips to "stopped".
    """
    with app.app_context():
        campaign = Campaign.query.filter(Campaign.status.in_(["active", "stopping"])).first()
        if not campaign:
            return
        campaign_id = campaign.id
        campaign_status = campaign.status
        symbols = [cs.symbol for cs in campaign.symbols]

    try:
        prices = fetch_prices(symbols, app.config["BINANCE_TESTNET"])
    except requests.RequestException as e:
        app.logger.error(f"[engine] falha ao buscar precos: {e}")
        return

    max_workers = app.config["ENGINE_MAX_WORKERS"]

    if campaign_status == "stopping":
        with app.app_context():
            position_ids = [p.id for p in Position.query.filter_by(campaign_id=campaign_id, status="open").all()]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_close_one_position_isolated, app, position_id, prices) for position_id in position_ids]
            concurrent.futures.wait(futures)

        with app.app_context():
            if not Position.query.filter_by(campaign_id=campaign_id, status="open").first():
                campaign = Campaign.query.get(campaign_id)
                campaign.status = "stopped"
                campaign.ended_at = db.func.now()
                db.session.commit()
        return

    with app.app_context():
        eligible_ids = [
            u.id for u in User.query.join(FollowerSettings)
            .filter(FollowerSettings.following_enabled.is_(True)).all()
        ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_process_follower_isolated, app, campaign_id, user_id, prices) for user_id in eligible_ids]
        concurrent.futures.wait(futures)


def _process_follower_isolated(app, campaign_id, user_id, prices):
    """Worker-thread entry point: own app context (-> own DB session),
    re-fetches the campaign/user fresh rather than sharing ORM objects
    across threads (SQLAlchemy objects are bound to the session that
    loaded them, so passing them across a thread boundary is unsafe)."""
    with app.app_context():
        try:
            campaign = Campaign.query.get(campaign_id)
            user = User.query.get(user_id)
            if not campaign or not user:
                return
            _process_follower(app, campaign, user, prices)
            db.session.commit()
        except Exception as e:  # noqa: BLE001 -- one account's bug must never stop the others
            db.session.rollback()
            app.logger.error(f"[engine] erro processando usuario {user_id}: {e}")


def _close_one_position_isolated(app, position_id, prices):
    """Worker-thread entry point for the "stopping" flow -- same
    isolated-context reasoning as _process_follower_isolated."""
    with app.app_context():
        try:
            position = Position.query.get(position_id)
            if not position or position.status != "open":
                return
            user = User.query.get(position.user_id)
            settings = user.settings
            broker, err = _build_broker(user, app.config["ENCRYPTION_KEY"], app.config["BINANCE_TESTNET"])
            if not broker:
                return
            allocation = FollowerAllocation.query.filter_by(campaign_id=position.campaign_id, user_id=user.id, symbol=position.symbol).first()
            allocated_usd = allocation.allocated_usd if allocation else 0.0
            price = prices.get(position.symbol, position.entry_price)
            _close_position(broker, position, price, "Campanha-encerrada", user.id, settings.leverage, allocated_usd)
            if allocation:
                allocation.allocated_usd = 0.0
                allocation.state = "inactive"
            db.session.commit()
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            app.logger.error(f"[engine] erro encerrando posicao {position_id}: {e}")


def _process_follower(app, campaign, user, prices):
    settings = user.settings
    encryption_key = app.config["ENCRYPTION_KEY"]
    testnet = app.config["BINANCE_TESTNET"]

    broker, err = _build_broker(user, encryption_key, testnet)
    if not broker:
        return  # no valid credential yet -- nothing to do for this account

    state = _get_or_create_state(campaign.id, user.id)
    if state.status == "inactive":
        return  # already drawdown-halted or manually stopped for this campaign

    allocations = FollowerAllocation.query.filter_by(campaign_id=campaign.id, user_id=user.id).all()
    by_symbol = {a.symbol: a for a in allocations}

    # -- Case A: brand-new follower for this campaign -> open initial positions
    if not allocations:
        if not settings.following_enabled:
            return
        balance, err = broker.get_account_balance()
        if err:
            _log_order(user.id, "-", "-", None, "open", None, "failed", f"saldo indisponivel: {err}")
            return
        slice_usd = compute_initial_slice(balance, settings.risk_pct, len(campaign.symbols))
        side = "BUY" if campaign.direction == "long" else "SELL"
        for cs in campaign.symbols:
            symbol = cs.symbol
            price = prices.get(symbol)
            allocation = FollowerAllocation(campaign_id=campaign.id, user_id=user.id, symbol=symbol, allocated_usd=0.0, state="inactive")
            db.session.add(allocation)
            if price is None or slice_usd <= 0:
                continue
            broker.set_margin_type(symbol, "ISOLATED")
            _, lev_err = broker.set_leverage(symbol, settings.leverage)
            if lev_err:
                _log_order(user.id, symbol, side, None, "open", None, "failed", f"leverage: {lev_err}")
                continue
            qty, size_err = broker.size_order_quantity(symbol, slice_usd, price)
            if size_err:
                _log_order(user.id, symbol, side, None, "open", None, "failed", str(size_err))
                continue
            order, order_err = broker.place_market_order(symbol, side, qty)
            if order_err:
                _log_order(user.id, symbol, side, qty, "open", None, "failed", str(order_err))
                continue
            entry_price = float(order.get("avgPrice") or price)
            db.session.add(Position(campaign_id=campaign.id, user_id=user.id, symbol=symbol, side=campaign.direction, entry_price=entry_price, status="open"))
            allocation.allocated_usd = slice_usd
            allocation.state = "active"
            _log_order(user.id, symbol, side, qty, "open", order.get("orderId"), "filled")
        state.peak_portfolio_usd = sum(a.allocated_usd for a in by_symbol.values()) or state.peak_portfolio_usd
        return

    # -- Case B: follower switched off following mid-campaign -> close everything
    if not settings.following_enabled:
        open_positions = Position.query.filter_by(campaign_id=campaign.id, user_id=user.id, status="open").all()
        for position in open_positions:
            price = prices.get(position.symbol, position.entry_price)
            allocation = by_symbol.get(position.symbol)
            allocated_usd = allocation.allocated_usd if allocation else 0.0
            _close_position(broker, position, price, "Manual", user.id, settings.leverage, allocated_usd)
        for allocation in allocations:
            allocation.allocated_usd = 0.0
            allocation.state = "inactive"
        return

    # -- Case C: check stop-loss on every open position, redirect on trigger
    open_positions = {p.symbol: p for p in Position.query.filter_by(campaign_id=campaign.id, user_id=user.id, status="open").all()}
    for symbol, position in list(open_positions.items()):
        price = prices.get(symbol)
        if price is None:
            continue
        if not is_stop_triggered(position.side, position.entry_price, price, settings.leverage, campaign.stop_pct):
            continue

        allocation = by_symbol[symbol]
        closed, _realized = _close_position(broker, position, price, "SL", user.id, settings.leverage, allocation.allocated_usd)
        if not closed:
            continue
        freed_usd = allocation.allocated_usd * (1 - campaign.stop_pct / 100)
        allocation.allocated_usd = 0.0
        allocation.state = "inactive"
        del open_positions[symbol]

        open_rois = {
            s: price_roi_pct(p.side, p.entry_price, prices[s], settings.leverage)
            for s, p in open_positions.items()
            if by_symbol[s].state == "active" and s in prices
        }
        leader_symbol = pick_redirect_leader(open_rois)
        if leader_symbol and freed_usd > 0:
            leader_position = open_positions[leader_symbol]
            side = "BUY" if leader_position.side == "long" else "SELL"
            qty, size_err = broker.size_order_quantity(leader_symbol, freed_usd, prices[leader_symbol])
            if not size_err:
                order, order_err = broker.place_market_order(leader_symbol, side, qty)
                if not order_err:
                    _log_order(user.id, leader_symbol, side, qty, "add", order.get("orderId"), "filled")
                    new_entry, entry_err = broker.get_position_entry_price(leader_symbol)
                    if not entry_err and new_entry:
                        leader_position.entry_price = new_entry
                    by_symbol[leader_symbol].allocated_usd += freed_usd
                else:
                    _log_order(user.id, leader_symbol, side, qty, "add", None, "failed", str(order_err))

    # -- Case D: portfolio-level drawdown check for this account
    if settings.max_drawdown_enabled:
        portfolio_value = sum(a.allocated_usd for a in by_symbol.values())
        for symbol, position in open_positions.items():
            price = prices.get(symbol)
            if price is not None:
                roi = price_roi_pct(position.side, position.entry_price, price, settings.leverage)
                portfolio_value += by_symbol[symbol].allocated_usd * (roi / 100)
        state.peak_portfolio_usd = max(state.peak_portfolio_usd, portfolio_value)
        if is_drawdown_triggered(state.peak_portfolio_usd, portfolio_value, settings.max_drawdown_pct):
            for position in open_positions.values():
                price = prices.get(position.symbol, position.entry_price)
                allocation = by_symbol[position.symbol]
                _close_position(broker, position, price, "Drawdown", user.id, settings.leverage, allocation.allocated_usd)
            for allocation in by_symbol.values():
                allocation.allocated_usd = 0.0
                allocation.state = "inactive"
            state.status = "inactive"


def start_background_engine(app):
    """Starts the tick loop in a daemon thread. Called once from
    create_app() -- runs for the lifetime of the process, independent
    of any request or browser."""
    def _loop():
        interval = app.config["ENGINE_TICK_SECONDS"]
        while True:
            try:
                run_tick(app)
            except Exception as e:  # noqa: BLE001 -- the loop itself must never die
                app.logger.error(f"[engine] erro no ciclo: {e}")
            time.sleep(interval)

    thread = threading.Thread(target=_loop, daemon=True, name="copy-trading-engine")
    thread.start()
    return thread
