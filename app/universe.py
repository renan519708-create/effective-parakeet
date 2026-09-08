"""Server-side port of backtest_lab.html's resolveSymbolUniverse --
same five scopes (single symbol, Top N marketcap, specific ranks, Top N
strength/weakness vs BTC), same CoinGecko + Binance Futures public
endpoints, just called once by the operator when starting a campaign
instead of live in the browser.
"""

import threading
import time

import requests

from app.binance_broker import TESTNET_BASE_URL, MAINNET_BASE_URL

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"
STABLE_EXCLUDE = {
    "usdt", "usdc", "dai", "busd", "tusd", "fdusd", "usde", "usds",
    "pyusd", "frax", "gusd", "lusd", "usdd", "eurt", "eurs",
}

# CoinGecko's key-less public endpoint has a low, shared rate limit --
# Render's outbound IPs are pooled across many customers' services, so
# a 429 ("Too Many Requests") can happen even from just this app's own
# occasional use (confirmed live). Market-cap rank barely moves minute
# to minute, so a short in-memory cache both avoids re-hitting
# CoinGecko every time the operator opens the "start campaign" form and
# gives a stale-but-good-enough fallback if a fresh fetch gets
# rate-limited. Per gunicorn worker process, not shared across workers
# -- still cuts real-world call volume drastically since one operator
# clicking "iniciar" a few times in a row is the common case.
_MARKETCAP_CACHE_TTL = 180  # seconds
_marketcap_cache = {"data": None, "ts": 0.0}
_marketcap_lock = threading.Lock()


def _base_url(testnet):
    return TESTNET_BASE_URL if testnet else MAINNET_BASE_URL


def fetch_marketcap_list():
    with _marketcap_lock:
        cached, age = _marketcap_cache["data"], time.time() - _marketcap_cache["ts"]
        if cached is not None and age < _MARKETCAP_CACHE_TTL:
            return cached

    last_error = None
    for attempt in range(3):
        try:
            resp = requests.get(COINGECKO_URL, params={
                "vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": 1,
            }, timeout=15)
            if resp.status_code == 429:
                last_error = requests.HTTPError(f"429 Client Error: Too Many Requests for url: {resp.url}")
                time.sleep(2 * (attempt + 1))  # backoff: 2s, then 4s
                continue
            resp.raise_for_status()
            data = resp.json()
            with _marketcap_lock:
                _marketcap_cache["data"] = data
                _marketcap_cache["ts"] = time.time()
            return data
        except requests.RequestException as e:
            last_error = e
            time.sleep(2 * (attempt + 1))

    # Every retry failed (CoinGecko still rate-limiting/down) -- serve a
    # stale cached list rather than blocking campaign creation entirely,
    # as long as it's not absurdly old. A slightly outdated market-cap
    # rank is far less disruptive than "nao foi possivel iniciar a
    # campanha" for something that changes this slowly.
    with _marketcap_lock:
        cached, age = _marketcap_cache["data"], time.time() - _marketcap_cache["ts"]
    if cached is not None and age < 1800:  # 30 min
        return cached
    raise last_error


def fetch_binance_futures_symbols(testnet):
    resp = requests.get(f"{_base_url(testnet)}/fapi/v1/exchangeInfo", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return {
        s["symbol"] for s in data["symbols"]
        if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"
    }


def _ranked_marketcap(market_list):
    ranked = [
        c for c in market_list
        if c.get("symbol") and c["symbol"].lower() not in STABLE_EXCLUDE and c.get("market_cap_rank") is not None
    ]
    ranked.sort(key=lambda c: c["market_cap_rank"])
    return ranked


def fetch_daily_return(symbol, from_ms, to_ms, testnet):
    resp = requests.get(f"{_base_url(testnet)}/fapi/v1/klines", params={
        "symbol": symbol, "interval": "1d", "startTime": from_ms, "endTime": to_ms, "limit": 1000,
    }, timeout=15)
    resp.raise_for_status()
    raw = resp.json()
    if len(raw) < 2:
        raise ValueError("dados insuficientes")
    first_close, last_close = float(raw[0][4]), float(raw[-1][4])
    if not first_close:
        raise ValueError("preco inicial invalido")
    return (last_close / first_close) - 1


def rank_by_relative_strength_vs_btc(candidate_symbols, ref_ms, lookback_days, testnet):
    from_ms = ref_ms - lookback_days * 24 * 3600 * 1000
    btc_return = fetch_daily_return("BTCUSDT", from_ms, ref_ms, testnet)
    results = []
    for symbol in candidate_symbols:
        if symbol == "BTCUSDT":
            continue
        try:
            ret = fetch_daily_return(symbol, from_ms, ref_ms, testnet)
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
        market_list = _ranked_marketcap(fetch_marketcap_list())
        valid_symbols = fetch_binance_futures_symbols(testnet)
        candidates = [
            f"{c['symbol'].upper()}USDT" for c in market_list
            if c["symbol"].lower() != "btc" and f"{c['symbol'].upper()}USDT" in valid_symbols
        ]
        # Cap the pool before the one-network-call-per-candidate ranking
        # below -- market_list can hand back 150-200+ eligible symbols,
        # and at ~0.1-0.3s per candidate (klines fetch + the sleep that
        # avoids hitting Binance's rate limit) that's well past gunicorn's
        # request timeout. Top 80 by market cap is still a wide enough
        # pool for the relative-strength ranking to mean something.
        candidates = candidates[:80]
        top_n = int(params.get("topN", 10))
        lookback_days = int(params.get("lookbackDays", 30))
        ranked = rank_by_relative_strength_vs_btc(candidates, ref_ms, lookback_days, testnet)
        if not ranked:
            raise ValueError("nao foi possivel calcular forca relativa vs BTC para nenhum candidato")
        is_weak = scope == "relbtc_weak"
        top = list(reversed(ranked[-top_n:])) if is_weak else ranked[:top_n]
        return [{"symbol": r["symbol"], "rank": i + 1} for i, r in enumerate(top)]

    market_list = _ranked_marketcap(fetch_marketcap_list())
    valid_symbols = fetch_binance_futures_symbols(testnet)
    if scope == "topn":
        selected = market_list[: int(params.get("topN", 10))]
    elif scope == "ranks":
        wanted_ranks = set(params.get("ranks", []))
        selected = [c for c in market_list if c["market_cap_rank"] in wanted_ranks]
    else:
        raise ValueError(f"escopo de universo desconhecido: {scope}")

    resolved = []
    for c in selected:
        symbol = f"{c['symbol'].upper()}USDT"
        if symbol in valid_symbols:
            resolved.append({"symbol": symbol, "rank": c["market_cap_rank"]})
    if not resolved:
        raise ValueError("nenhum simbolo do ranking foi encontrado na Binance Futures")
    return resolved
