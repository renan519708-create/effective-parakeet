"""Server-side port of backtest_lab.html's resolveSymbolUniverse --
now just two scopes (single symbol, Top N strength/weakness vs BTC),
sourced from Binance Futures' own public endpoints, called once by the
operator when starting a campaign instead of live in the browser.

"Top N marketcap" and "Ranks especificos" (and the CoinGecko/volume
ranking that backed them) were removed 2026-09-11 to match a redesign
made independently on the standalone backtest_lab.html prototype:
strength/weakness vs BTC is the only ranked scope now, and its
candidate pool is every active USDT-margined perpetual on Binance
Futures (no top-N-by-volume pre-filter) -- the operator reviews and
hand-picks the actual campaign symbols from the ranked results before
confirming (see operator.py's search/review step) rather than the
resolved Top N auto-starting a campaign.

Testing all ~500+ pairs (vs the previous 80-candidate cap) takes
noticeably longer -- see render.yaml/Procfile's gunicorn --timeout,
raised accordingly. This is deliberately a two-step flow now (search,
then confirm) specifically so a slow search never risks timing out
mid-campaign-creation; it only risks timing out the search itself,
which is safe to just retry.
"""

import concurrent.futures
import time

import requests

from app.binance_broker import TESTNET_BASE_URL, MAINNET_BASE_URL

STABLE_EXCLUDE = {
    "usdt", "usdc", "dai", "busd", "tusd", "fdusd", "usde", "usds",
    "pyusd", "frax", "gusd", "lusd", "usdd", "eurt", "eurs",
}

# Ranking ~525 candidates one at a time (confirmed live: ~130-260s) is
# too close to even a generous gunicorn --timeout for comfort. Fetching
# candidates concurrently (same ThreadPoolExecutor pattern as
# app/engine.py's per-account tick) cuts that to ~15-30s -- each worker
# still sleeps between its OWN requests (see _rank_one), so this isn't
# "no more throttling", just spread across more workers than one.
RANK_MAX_WORKERS = 15


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


def get_top_volume_symbols(testnet, top_n=50):
    """Top-N USDT perpetuals by 24h quote volume (ported from
    gg-shot-monitor/symbols.py's get_top_volume_symbols)."""
    valid = fetch_binance_futures_symbols(testnet)
    resp = requests.get(f"{_base_url(testnet)}/fapi/v1/ticker/24hr", timeout=15)
    resp.raise_for_status()
    rows = [d for d in resp.json() if d["symbol"] in valid]
    rows.sort(key=lambda d: float(d["quoteVolume"]), reverse=True)
    return [d["symbol"] for d in rows[:top_n]]


# Same manual watchlist gg-shot-monitor's symbols.py merges into its own
# Kairi universe (WATCHLIST_TICKERS there). Ported here too -- an
# earlier version of the Auto-bot deliberately dropped this, reasoning
# it was "specific to the user's chart setup, not part of the
# strategy's validated edge". That was wrong: every Kairi backtest this
# strategy was validated against (90/60/30-day, 2-year, out-of-sample,
# the compounding simulation in the report sent to the socio) called
# resolve_symbol_list(top_n=50), which INCLUDES this merge -- confirmed
# live 2026-09-14 when the Auto-bot missed real signals gg-shot-monitor's
# own live Kairi feed caught on watchlist-only tickers (VVVUSDT, 4USDT,
# UAIUSDT) that weren't in the top-50-by-volume alone. Keeping this in
# sync with gg-shot-monitor/symbols.py's WATCHLIST_TICKERS is what keeps
# the Auto-bot's real universe matching what was actually backtested.
KAIRI_WATCHLIST_TICKERS = [
    "BTC", "ETH", "XRP", "LINK", "DOT", "AVAX", "BNB", "LTC", "ADA", "SOL",
    "ZEC", "PUMP", "BANK", "CL", "TAIKO", "ONT", "VIRTUAL", "VELVET", "UAI",
    "NEAR", "RIF", "ENA", "KSM", "DEXE", "PENGU", "4", "CLO", "VVV",
]


def resolve_kairi_universe(testnet, top_n=50):
    """The Auto-bot's actual Kairi universe: watchlist tickers (that
    are still valid/tradeable) + top-N by volume, deduplicated,
    watchlist first -- same shape as gg-shot-monitor/symbols.py's
    resolve_symbol_list, so the Auto-bot scans exactly what the
    validated backtests scanned. One exchangeInfo fetch shared by both
    parts (not a separate call per part)."""
    valid = fetch_binance_futures_symbols(testnet)
    watchlist_symbols = [f"{t}USDT" for t in KAIRI_WATCHLIST_TICKERS if f"{t}USDT" in valid]

    resp = requests.get(f"{_base_url(testnet)}/fapi/v1/ticker/24hr", timeout=15)
    resp.raise_for_status()
    rows = [d for d in resp.json() if d["symbol"] in valid]
    rows.sort(key=lambda d: float(d["quoteVolume"]), reverse=True)
    top_symbols = [d["symbol"] for d in rows[:top_n]]

    return list(dict.fromkeys(watchlist_symbols + top_symbols))


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

    def _rank_one(symbol):
        if symbol == "BTCUSDT":
            return None
        try:
            ret = fetch_kline_return(symbol, from_ms, ref_ms, interval, testnet)
            return {"symbol": symbol, "rel_strength": (ret - btc_return) * 100}
        except (requests.RequestException, ValueError):
            return None
        finally:
            time.sleep(0.1)  # per-worker throttle -- see RANK_MAX_WORKERS

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=RANK_MAX_WORKERS) as pool:
        for r in pool.map(_rank_one, candidate_symbols):
            if r is not None:
                results.append(r)

    results.sort(key=lambda r: -r["rel_strength"])
    return results


def rank_symbol_universe(scope, params, ref_ms, testnet):
    """Resolves and ranks candidates for the vs-BTC scopes WITHOUT
    picking a final Top N -- returns the full ranked list (direction
    already applied: strongest-first for "relbtc", weakest-first for
    "relbtc_weak") so the operator can review it and hand-pick the
    actual campaign symbols (see operator.py's search/review step).
    Raises ValueError with a user-facing message on failure."""
    valid_symbols = fetch_binance_futures_symbols(testnet)
    candidates = [s for s in valid_symbols if s != "BTCUSDT" and s[:-4].lower() not in STABLE_EXCLUDE]

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
    if scope == "relbtc_weak":
        ranked = list(reversed(ranked))
    return ranked


def resolve_symbol_universe(scope, params, ref_ms, testnet):
    """Returns a list of {"symbol": ..., "rank": int|None}. Raises
    ValueError with a user-facing message on failure (no eligible
    symbols, upstream API error, etc.).

    scope == "single": resolves immediately, no ranking involved.
    scope in ("relbtc", "relbtc_weak"): resolves the FIRST params["topN"]
    of the full ranking (see rank_symbol_universe) -- used only as a
    fallback/default selection; the normal flow lets the operator
    override this via the search/review step instead of calling this
    directly with those scopes.
    """
    if scope == "single":
        symbol = params["symbol"].upper()
        return [{"symbol": symbol, "rank": None}]

    if scope in ("relbtc", "relbtc_weak"):
        ranked = rank_symbol_universe(scope, params, ref_ms, testnet)
        top_n = int(params.get("topN", 10))
        top = ranked[:top_n]
        return [{"symbol": r["symbol"], "rank": i + 1} for i, r in enumerate(top)]

    raise ValueError(f"escopo de universo desconhecido: {scope}")


# Fixed universe for the "Long Diária Composta" scheduled campaign (see
# app/engine.py's _check_daily_composto_schedule) -- from the strategy
# spec PDF (estrategia_long_diaria_composta.pdf), section 1. Validated
# live 2026-09-15 against /fapi/v1/exchangeInfo on mainnet: 53/53
# tickers resolve to a real tradeable USDT perpetual, including the 5
# the document's author flagged as uncertain (CC, LIT, VVV, STABLE,
# JST). SHIB/PEPE/TON were already excluded by the author on request.
DAILY_LONG_TICKERS = [
    "BTC", "ETH", "BNB", "XRP", "SOL", "TRX", "HYPE", "ZEC", "DOGE", "XMR",
    "LINK", "ADA", "XLM", "BCH", "LTC", "UNI", "CC", "HBAR", "AVAX", "NEAR",
    "SUI", "XAUT", "TAO", "AAVE", "ASTER", "PAXG", "WLFI", "ONDO", "DOT",
    "PUMP", "ICP", "SKY", "ENA", "WLD", "ETC", "MORPHO", "LIT", "VVV", "POL",
    "KAS", "JST", "ARB", "ALGO", "ATOM", "JUP", "FIL", "QNT", "CAKE",
    "STABLE", "RENDER", "DASH", "VET", "INJ",
]


def resolve_daily_long_universe(testnet):
    """Any ticker no longer tradeable is just dropped, never raises --
    matches the spec's "Qualquer par que não existir deve simplesmente
    ser removido do universo, sem travar a automação"."""
    valid = fetch_binance_futures_symbols(testnet)
    return [f"{t}USDT" for t in DAILY_LONG_TICKERS if f"{t}USDT" in valid]
