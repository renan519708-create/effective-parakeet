"""Public (unauthenticated) OHLC candle reads from Binance Futures, for
the Auto-bot's Kairi engine. Ported from gg-shot-monitor/data_fetcher.py
(get_klines/get_latest_candle) -- same shape, parameterized by
`testnet` instead of a single hardcoded BASE_URL, since this app talks
to both environments depending on the caller (AUTOBOT_TESTNET vs the
campaign engine's BINANCE_TESTNET).
"""

import time

import requests

from app.binance_broker import MAINNET_BASE_URL, TESTNET_BASE_URL


def _base_url(testnet):
    return TESTNET_BASE_URL if testnet else MAINNET_BASE_URL


def _parse_candle(k):
    return {
        "open_time": k[0],
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "close_time": k[6],
    }


def get_klines(symbol, interval, limit, testnet, retries=3):
    """Fetches closed candles for a symbol/interval, dropping any
    still-forming (unclosed) bar -- the signal (zone crossing) must
    never react to an in-progress candle."""
    url = f"{_base_url(testnet)}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}

    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                raw = resp.json()
                now_ms = int(time.time() * 1000)
                return [_parse_candle(k) for k in raw if k[6] < now_ms]
            if resp.status_code in (429, 418):
                time.sleep(2 ** attempt * 5)
            else:
                return None
        except requests.RequestException:
            time.sleep(2 ** attempt)
    return None


def get_latest_candle(symbol, interval, testnet):
    """Fetches the most recent candle, INCLUDING the currently-forming
    one -- used only to check whether an already-open trade's stop/EMA
    has been touched intrabar (a high/low that already printed is real
    and can't un-happen, even on an unclosed candle)."""
    url = f"{_base_url(testnet)}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": 1}
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            raw = resp.json()
            if not raw:
                return None
            return _parse_candle(raw[-1])
    except requests.RequestException:
        pass
    return None
