"""Server-side port of backtest_lab.html's resolveSymbolUniverse --
same five scopes (single symbol, Top N marketcap, specific ranks, Top N
strength/weakness vs BTC), same CoinGecko + Binance Futures public
endpoints, just called once by the operator when starting a campaign
instead of live in the browser.
"""

import time

import requests

from app.binance_broker import TESTNET_BASE_URL, MAINNET_BASE_URL

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"
STABLE_EXCLUDE = {
    "usdt", "usdc", "dai", "busd", "tusd", "fdusd", "usde", "usds",
    "pyusd", "frax", "gusd", "lusd", "usdd", "eurt", "eurs",
}


def _base_url(testnet):
    return TESTNET_BASE_URL if testnet else MAINNET_BASE_URL


def fetch_marketcap_list():
    resp = requests.get(COINGECKO_URL, params={
        "vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": 1,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


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
