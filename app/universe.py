"""Server-side port of backtest_lab.html's resolveSymbolUniverse --
five scopes (single symbol, Top N by volume, specific ranks, Top N
strength/weakness vs BTC), all sourced from Binance Futures' own public
endpoints, called once by the operator when starting a campaign instead
of live in the browser.

Ranked by 24h quote volume, not market cap. This used to pull
market-cap rank from CoinGecko's key-less public API, but that endpoint
has a low, shared rate limit -- Render's outbound IPs are pooled across
many customers, so it kept returning 429 "Too Many Requests" even from
this app's own light, occasional use (confirmed live, more than once).
Volume isn't identical to market cap, but for picking a liquid basket
of coins to actually trade in size it's arguably the more relevant
signal anyway, and sourcing it from Binance itself removes an external
dependency entirely -- no more cross-API symbol matching to get wrong,
and Binance's own rate limits are far more generous.
"""

import threading
import time

import requests

from app.binance_broker import TESTNET_BASE_URL, MAINNET_BASE_URL

STABLE_EXCLUDE = {
    "usdt", "usdc", "dai", "busd", "tusd", "fdusd", "usde", "usds",
    "pyusd", "frax", "gusd", "lusd", "usdd", "eurt", "eurs",
}

# Binance's rate limits are generous enough that this cache isn't load
# -bearing the way the old CoinGecko one was -- it's just here to avoid
# re-fetching and re-sorting a few hundred tickers every time the
# operator opens the "start campaign" form.
_RANKED_CACHE_TTL = 60  # seconds
_ranked_cache = {}  # testnet(bool) -> {"data": [...], "ts": float}
_ranked_lock = threading.Lock()


def _base_url(testnet):
    return TESTNET_BASE_URL if testnet else MAINNET_BASE_URL


def fetch_binance_futures_symbols(testnet):
    resp = requests.get(f"{_base_url(testnet)}/fapi/v1/exchangeInfo", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return {
        s["symbol"] for s in data["symbols"]
        if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"
    }


def _ranked_by_volume(testnet):
    """List of {"symbol": <base asset, lowercase>, "rank": int},
    sorted by 24h quote volume descending, USDT-margined perpetuals
    only -- the Binance-only replacement for the old CoinGecko
    market-cap ranking (see module docstring)."""
    with _ranked_lock:
        entry = _ranked_cache.get(testnet)
        if entry and time.time() - entry["ts"] < _RANKED_CACHE_TTL:
            return entry["data"]

    valid_symbols = fetch_binance_futures_symbols(testnet)
    resp = requests.get(f"{_base_url(testnet)}/fapi/v1/ticker/24hr", timeout=15)
    resp.raise_for_status()
    tickers = [t for t in resp.json() if t["symbol"] in valid_symbols]
    tickers.sort(key=lambda t: -float(t["quoteVolume"]))

    ranked = []
    for t in tickers:
        base = t["symbol"][:-4].lower()  # strip "USDT" -- every valid_symbols entry is USDT-quoted
        if base in STABLE_EXCLUDE:
            continue
        ranked.append({"symbol": base, "rank": len(ranked) + 1})

    with _ranked_lock:
        _ranked_cache[testnet] = {"data": ranked, "ts": time.time()}
    return ranked


def fetch_kline_return(symbol, from_ms, to_ms, interval, testnet):
    resp = requests.get(f"{_base_url(testnet)}/fapi/v1/klines", params={
        "symbol": symbol, "interval": interval, "startTime": from_ms, "endTime": to_ms, "limit": 1000,
    }, timeout=15)
    resp.raise_for_status()
    raw = resp.json()
    if len(raw) < 2:
        raise ValueError("dados insuficientes")
    first_close, last_close = float(raw[0][4]), float(raw[-1][4])
    if not first_close:
        raise ValueError("preco inicial invalido")
    return (last_close / first_close) - 1


def rank_by_relative_strength_vs_btc(candidate_symbols, ref_ms, window_ms, interval, testnet):
    """interval: a Binance kline interval ("1h" or "1d") matched to the
    comparison window's unit -- 1d candles have no useful resolution
    for a window measured in hours (e.g. "forca nas ultimas 4h" can't
    be measured with one whole day's candle), while 1d stays the more
    efficient choice for longer, day-scale windows."""
    from_ms = ref_ms - window_ms
    btc_return = fetch_kline_return("BTCUSDT", from_ms, ref_ms, interval, testnet)
    results = []
    for symbol in candidate_symbols:
        if symbol == "BTCUSDT":
            continue
        try:
            ret = fetch_kline_return(symbol, from_ms, ref_ms, interval, testnet)
            results.append({"symbol": symbol, "rel_strength": (ret - btc_return) * 100})
        except (requests.RequestException, ValueError):
            pass
        time.sleep(0.1)
    results.sort(key=lambda r: -r["rel_strength"])
    return results


def resolve_symbol_universe(scope, params, ref_ms, testnet):
    """Returns a list of {"symbol": ..., "rank": int|None}. Raises
    ValueError with a user-facing message on failure (no eligible
    symbols, upstream API error, etc.)."""
    if scope == "single":
        symbol = params["symbol"].upper()
        return [{"symbol": symbol, "rank": None}]

    if scope in ("relbtc", "relbtc_weak"):
        market_list = _ranked_by_volume(testnet)
        candidates = [f"{c['symbol'].upper()}USDT" for c in market_list if c["symbol"] != "btc"]
        # Cap the pool before the one-network-call-per-candidate ranking
        # below -- market_list can hand back 150-200+ eligible symbols,
        # and at ~0.1-0.3s per candidate (klines fetch + the sleep that
        # avoids hitting Binance's rate limit) that's well past gunicorn's
        # request timeout. Top 80 by volume is still a wide enough pool
        # for the relative-strength ranking to mean something.
        candidates = candidates[:80]
        top_n = int(params.get("topN", 10))
        lookback_value = int(params.get("lookbackValue", 30))
        lookback_unit = params.get("lookbackUnit", "days")
        if lookback_unit == "hours":
            window_ms = lookback_value * 3600 * 1000
            interval = "1h"
        else:
            window_ms = lookback_value * 24 * 3600 * 1000
            interval = "1d"
        ranked = rank_by_relative_strength_vs_btc(candidates, ref_ms, window_ms, interval, testnet)
        if not ranked:
            raise ValueError("nao foi possivel calcular forca relativa vs BTC para nenhum candidato")
        is_weak = scope == "relbtc_weak"
        top = list(reversed(ranked[-top_n:])) if is_weak else ranked[:top_n]
        return [{"symbol": r["symbol"], "rank": i + 1} for i, r in enumerate(top)]

    market_list = _ranked_by_volume(testnet)
    if scope == "topn":
        selected = market_list[: int(params.get("topN", 10))]
    elif scope == "ranks":
        wanted_ranks = set(params.get("ranks", []))
        selected = [c for c in market_list if c["rank"] in wanted_ranks]
    else:
        raise ValueError(f"escopo de universo desconhecido: {scope}")

    resolved = [{"symbol": f"{c['symbol'].upper()}USDT", "rank": c["rank"]} for c in selected]
    if not resolved:
        raise ValueError("nenhum simbolo encontrado na Binance Futures para esse escopo")
    return resolved
