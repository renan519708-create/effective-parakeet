"""Classifies whether an open Kairi mean-reversion trade has closed yet.

The strategy: buy (LONG) when KAIRI touches oversold, sell (SHORT) when it
touches overbought, and hold -- no target, no stop -- until price touches
the EMA25 back. LONG entries happen below the EMA (price is oversold), so
the exit condition is the candle's HIGH reaching back up to the EMA;
SHORT entries happen above the EMA, so the exit condition is the candle's
LOW reaching back down to it. Whichever side of zero the result lands on
when that happens decides green/red -- there's no fixed win threshold.

How far price moves against the position before it finally reverts is
tracked as max drawdown regardless of how the trade closes, so it can be
averaged across trades later (see run.py / dashboard.py) as a read on
how rough the ride typically gets.

Two hard exits sit ABOVE the EMA-touch rule, checked first every candle:

- stop_pct (default 15%, see run.py's kairi_stop_pct): real data showed
  trades held past this point either eventually reverted to only a
  small loss anyway (the EMA had time to fall/rise with the price) or
  never reverted at all and ran to liquidation -- holding indefinitely
  stopped paying off. Closes as "stopped" at exactly -stop_pct.
- LIQUIDATION_PCT (100%): on isolated margin, the exchange force-closes
  a position once its loss reaches the posted margin -- our own rules
  never get a say at that point. Closes as "liquidated" at exactly
  -100%. With the default 15% stop this is now effectively a dead
  backstop (the stop always fires first), kept for when stop_pct is
  raised or disabled.

If a candle's OHLC range would technically satisfy a hard exit AND the
EMA touch in the same candle, the adverse move is conservatively assumed
to have happened first -- same convention outcome_tracker.py uses for
its own same-candle ambiguity.
"""

LIQUIDATION_PCT = 100.0


def _result_pct(direction, entry_price, exit_price):
    if direction == "LONG":
        return (exit_price - entry_price) / entry_price * 100
    return (entry_price - exit_price) / entry_price * 100


def _price_at_pct(direction, entry_price, pct):
    """Price that would put the position exactly `pct`% against it."""
    if direction == "LONG":
        return entry_price * (1 - pct / 100)
    return entry_price * (1 + pct / 100)


def _adverse_pct(direction, entry_price, candle):
    """How far this candle's worst point moved AGAINST the position,
    as a positive percentage (0 if it never went against at all)."""
    if direction == "LONG":
        return max(0.0, (entry_price - candle["low"]) / entry_price * 100)
    return max(0.0, (candle["high"] - entry_price) / entry_price * 100)


def evaluate_kairi_outcome(direction, entry_price, candles_after, ema_after, stop_pct=None):
    """candles_after: closed (or still-forming) candles strictly after the
    trade's entry candle, oldest first. ema_after: the EMA25 value aligned
    with each of those same candles (same length, computed from a
    continuous series so the EMA itself is accurate). stop_pct: optional
    fixed hard-stop percentage (e.g. 15.0) checked before the EMA touch;
    None disables it (only LIQUIDATION_PCT still applies).

    Returns (status, exit_price, exit_close_time, result_pct,
    max_drawdown_pct) where status is one of "open", "green", "red",
    "stopped", "liquidated". max_drawdown_pct is the worst adverse move
    seen so far (up to the exit candle if resolved, up to the latest
    candle if still open) -- always returned, even while open, so it can
    be tracked live.
    """
    if not candles_after or not entry_price:
        return "open", None, None, None, 0.0

    max_drawdown_pct = 0.0
    for candle, ema in zip(candles_after, ema_after):
        adverse_pct = _adverse_pct(direction, entry_price, candle)
        max_drawdown_pct = max(max_drawdown_pct, adverse_pct)

        if stop_pct is not None and adverse_pct >= stop_pct:
            exit_price = _price_at_pct(direction, entry_price, stop_pct)
            return "stopped", exit_price, candle["close_time"], -stop_pct, max_drawdown_pct

        if adverse_pct >= LIQUIDATION_PCT:
            exit_price = _price_at_pct(direction, entry_price, LIQUIDATION_PCT)
            return "liquidated", exit_price, candle["close_time"], -LIQUIDATION_PCT, max_drawdown_pct

        if ema is None:
            continue
        touched = candle["high"] >= ema if direction == "LONG" else candle["low"] <= ema
        if not touched:
            continue
        result_pct = _result_pct(direction, entry_price, ema)
        status = "green" if result_pct >= 0 else "red"
        return status, ema, candle["close_time"], result_pct, max_drawdown_pct

    return "open", None, None, None, max_drawdown_pct
