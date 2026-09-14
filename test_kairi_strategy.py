"""Unit tests for the Kairi pure functions vendored from
gg-shot-monitor (app/kairi.py, app/kairi_outcome.py) -- confirms the
port didn't change the math the backtests already validated."""

from app.kairi import compute_ema_series, compute_kairi_series
from app.kairi_outcome import evaluate_kairi_outcome


def _candle(close, high=None, low=None, close_time=0):
    return {"open": close, "close": close, "high": high if high is not None else close, "low": low if low is not None else close, "close_time": close_time}


def test_compute_kairi_series_matches_formula():
    # SMA(3) of [10, 10, 10, 20] at i=3 is (10+10+10)/... wait window is last 3: [10,10,20]/3? use length=3
    candles = [_candle(c) for c in [10, 10, 10, 20]]
    series = compute_kairi_series(candles, length=3)
    assert series[0] is None and series[1] is None
    # i=2: window [10,10,10] sma=10, kairi=(10-10)/10*100=0
    assert series[2] == 0.0
    # i=3: window [10,10,20] sma=40/3, kairi=(20-40/3)/(40/3)*100
    sma = (10 + 10 + 20) / 3
    expected = (20 - sma) / sma * 100
    assert abs(series[3] - expected) < 1e-9


def test_compute_ema_series_seeds_with_sma():
    candles = [_candle(c) for c in [10, 10, 10, 20, 20]]
    series = compute_ema_series(candles, length=3)
    assert series[0] is None and series[1] is None
    assert series[2] == 10.0  # seed = SMA of first 3 closes
    multiplier = 2 / 4
    expected_3 = (20 - 10) * multiplier + 10
    assert abs(series[3] - expected_3) < 1e-9


def test_evaluate_kairi_outcome_stops_before_ema_touch():
    # LONG entered at 100, stop_pct=10 -- a candle whose low hits 88
    # (12% adverse) should close "stopped" at exactly -10%, not ride
    # further even if the EMA is still far away.
    candles_after = [_candle(close=90, high=91, low=88, close_time=1)]
    ema_after = [150.0]  # EMA far above, no touch possible on a LONG here anyway
    status, exit_price, exit_time, result_pct, max_dd = evaluate_kairi_outcome(
        "LONG", 100.0, candles_after, ema_after, stop_pct=10.0,
    )
    assert status == "stopped"
    assert result_pct == -10.0
    assert exit_price == 90.0  # 100 * (1 - 10/100)
    assert max_dd == 12.0


def test_evaluate_kairi_outcome_green_on_ema_touch():
    # SHORT entered at 100 (above EMA), price falls and touches EMA=95
    # before any stop -- closes "green".
    candles_after = [_candle(close=96, high=97, low=94, close_time=1)]
    ema_after = [95.0]
    status, exit_price, exit_time, result_pct, max_dd = evaluate_kairi_outcome(
        "SHORT", 100.0, candles_after, ema_after, stop_pct=10.0,
    )
    assert status == "green"
    assert exit_price == 95.0
    assert result_pct == 5.0


def test_evaluate_kairi_outcome_open_when_nothing_touched():
    candles_after = [_candle(close=101, high=102, low=100, close_time=1)]
    ema_after = [110.0]
    status, exit_price, exit_time, result_pct, max_dd = evaluate_kairi_outcome(
        "LONG", 100.0, candles_after, ema_after, stop_pct=10.0,
    )
    assert status == "open"
    assert exit_price is None
