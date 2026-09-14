"""Computes the KAIRI Relative Index from candles, mirroring the "KAIRI"
Pine indicator the user has on their 1h BTC chart (period 25, SMA of
close, +-3 overbought/oversold levels).

KAIRI = (close - SMA(close, length)) / SMA(close, length) * 100

Unlike GG Shot, this isn't a directional trade signal -- it's an
overbought/oversold oscillator. "Touching" the upper or lower line is
the whole alert, no entry/exit or green/red outcome.
"""


def compute_kairi_series(candles, length=25):
    """Returns a list the same length as `candles`, with the KAIRI value
    at each index once enough history exists (None before that)."""
    closes = [c["close"] for c in candles]
    n = len(closes)
    series = [None] * n
    for i in range(length - 1, n):
        window = closes[i - length + 1 : i + 1]
        sma = sum(window) / length
        if sma == 0:
            continue
        series[i] = (closes[i] - sma) / sma * 100
    return series


def latest_kairi(candles, length=25):
    """Convenience wrapper: KAIRI value for the last candle, or None if
    there isn't enough history yet."""
    series = compute_kairi_series(candles, length=length)
    return series[-1] if series else None


def compute_ema_series(candles, length=25):
    """Standard EMA(close, length), seeded with a plain SMA of the first
    `length` closes (the usual convention) then smoothed forward.

    Returns a list the same length as `candles`, None before the seed
    point. Used as the exit reference for the Kairi mean-reversion
    strategy: a trade holds until price touches this line back.
    """
    closes = [c["close"] for c in candles]
    n = len(closes)
    series = [None] * n
    if n < length:
        return series

    multiplier = 2 / (length + 1)
    seed = sum(closes[:length]) / length
    series[length - 1] = seed
    ema = seed
    for i in range(length, n):
        ema = (closes[i] - ema) * multiplier + ema
        series[i] = ema
    return series
