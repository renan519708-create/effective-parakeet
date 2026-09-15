"""Signed Binance Futures client for placing REAL orders -- testnet by
default (see BinanceBroker's `testnet` flag). Separate from
data_fetcher.py, which only does public, unauthenticated market-data
reads and is never given credentials.

This module deliberately does nothing on import and places no order on
its own -- run.py's live-execution path (kept off by a config flag) is
what decides when to call into it.
"""

import hashlib
import hmac
import math
import threading
import time
import urllib.parse

import requests

TESTNET_BASE_URL = "https://demo-fapi.binance.com"
MAINNET_BASE_URL = "https://fapi.binance.com"

# Shared, process-wide cache for GET /fapi/v1/exchangeInfo -- a ~1-2MB
# response listing every symbol's trading rules, which rarely change.
# Confirmed live 2026-09-15: every BinanceBroker instance had its OWN
# cache (self._symbol_filters_cache below), reset on every fresh
# instance (a new one is created per engine tick per account), AND a
# failed fetch cached nothing at all -- so one early 429 inside a
# single Case A pass (opening a multi-symbol campaign) cascaded into a
# fresh exchangeInfo request for EVERY remaining symbol in that same
# loop (up to 53 in a row for Diária Composta), which is exactly the
# kind of burst that gets an IP rate-limited by Binance in the first
# place. This cache is keyed by base_url (testnet/mainnet kept
# separate) and shared across every thread, every BinanceBroker
# instance, and every feature in this app (campaign engine, universe
# search, Auto-bot) -- a 429/other failure is NOT cached, so the next
# call retries normally, but a SUCCESS is reused for
# EXCHANGE_INFO_TTL_SECONDS regardless of who asks.
_exchange_info_cache = {}  # base_url -> (fetched_at_monotonic, data)
_exchange_info_lock = threading.Lock()
EXCHANGE_INFO_TTL_SECONDS = 300


def get_exchange_info(base_url):
    """Returns the parsed exchangeInfo JSON for this base_url, cached
    process-wide for EXCHANGE_INFO_TTL_SECONDS. Raises
    requests.RequestException (including a non-2xx via raise_for_status)
    on a fresh-fetch failure -- callers already know how to handle
    that; a cache hit never raises."""
    with _exchange_info_lock:
        cached = _exchange_info_cache.get(base_url)
        if cached and (time.monotonic() - cached[0]) < EXCHANGE_INFO_TTL_SECONDS:
            return cached[1]
    resp = requests.get(f"{base_url}/fapi/v1/exchangeInfo", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    with _exchange_info_lock:
        _exchange_info_cache[base_url] = (time.monotonic(), data)
    return data


def parse_avg_price(order):
    """Binance Futures market order responses often report avgPrice as
    the literal string "0.00000000" in the instant right after the
    order is placed -- the real fill average isn't synchronously
    available yet. That string is truthy in Python, so the common
    `float(order.get("avgPrice") or fallback)` pattern silently keeps
    the useless "0.0" instead of ever reaching `fallback` (confirmed
    live 2026-09-15: this is what several entry_price/exit_price/
    close_price call sites across the app were doing). Returns the
    real avgPrice as a float, or None if it's missing/zero/unparseable
    -- callers should do `parse_avg_price(order) or fallback`."""
    raw = order.get("avgPrice")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def sign_query(secret, params):
    """Builds the query string Binance expects: every param, in
    insertion order, with an HMAC-SHA256 signature over that exact
    string appended as the last field.

    Pure function (no network, no clock reads beyond what's already in
    `params`) so it's fully unit-testable -- getting this wrong means
    every authenticated call fails with -1022 (bad signature).
    """
    query = urllib.parse.urlencode(params)
    signature = hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{query}&signature={signature}"


def round_step_size(quantity, step_size):
    """Rounds DOWN to the nearest multiple of step_size -- Binance
    rejects an order whose quantity doesn't land exactly on the
    symbol's LOT_SIZE step. Rounding down (never up) so we never risk
    ordering more than the sized position calls for.

    Returns 0.0 if the quantity doesn't reach even one step.
    """
    if step_size <= 0:
        return quantity
    # Tiny epsilon guards against float division landing just under a
    # whole step count (e.g. 2.5/0.5 coming out as 4.999999999999) --
    # negligible relative to any real step size, never enough to round
    # up past what the quantity actually supports.
    steps = math.floor(quantity / step_size + 1e-9)
    raw = steps * step_size
    # Round to the step's own decimal precision (not a log10-derived
    # guess, which breaks for non-power-of-ten steps like 0.5) so the
    # result matches exactly instead of carrying float noise.
    step_str = str(step_size)
    decimals = len(step_str.split(".")[1]) if "." in step_str else 0
    return round(raw, decimals)


class BinanceBroker:
    """testnet=True (the default) points every call at demo-fapi.binance.com
    -- Binance's "Demo Trading" environment (what testnet.binancefuture.com
    now redirects to). Logged in through the user's real Binance account,
    but the Demo Trading API key itself only has access to a separate fake
    balance -- it can't touch real funds regardless. Never flip this to
    False from inside this codebase; it's a deliberate, explicit choice
    the user makes in config.json, not a default any code path assumes."""

    def __init__(self, api_key, api_secret, testnet=True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.base_url = TESTNET_BASE_URL if testnet else MAINNET_BASE_URL
        self._symbol_filters_cache = {}

    def _headers(self):
        return {"X-MBX-APIKEY": self.api_key}

    def _signed_request(self, method, path, params=None):
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", 5000)
        query = sign_query(self.api_secret, params)
        url = f"{self.base_url}{path}?{query}"
        try:
            resp = requests.request(method, url, headers=self._headers(), timeout=10)
            body = resp.json()
        except requests.RequestException as e:
            return None, str(e)
        except ValueError:
            return None, f"resposta nao-JSON (status {resp.status_code})"
        if resp.status_code != 200:
            return None, body
        return body, None

    def get_account_balance(self, asset="USDT"):
        """Returns (available_balance, error) -- error is None on success."""
        data, err = self._signed_request("GET", "/fapi/v2/account")
        if err:
            return None, err
        for a in data.get("assets", []):
            if a["asset"] == asset:
                return float(a["availableBalance"]), None
        return None, f"ativo {asset} nao encontrado na conta"

    def get_symbol_filters(self, symbol):
        """Fetches (and caches) exchangeInfo's LOT_SIZE / MIN_NOTIONAL
        filters for one symbol -- needed to size an order Binance will
        actually accept. Reads through the shared, process-wide
        exchangeInfo cache (get_exchange_info above) -- not just this
        one broker instance's own _symbol_filters_cache -- so a burst of
        many symbols in one Case A pass makes at most one real HTTP call
        between them, not one per symbol."""
        if symbol in self._symbol_filters_cache:
            return self._symbol_filters_cache[symbol], None
        try:
            data = get_exchange_info(self.base_url)
        except requests.RequestException as e:
            return None, str(e)
        for s in data.get("symbols", []):
            filters = {f["filterType"]: f for f in s["filters"]}
            self._symbol_filters_cache[s["symbol"]] = filters
        if symbol not in self._symbol_filters_cache:
            return None, f"simbolo {symbol} nao encontrado no exchangeInfo"
        return self._symbol_filters_cache[symbol], None

    def size_order_quantity(self, symbol, usd_amount, price):
        """Converts a USD position size into a base-asset quantity that
        respects the symbol's LOT_SIZE step and MIN_NOTIONAL floor.

        Returns (quantity, error). error is set (quantity is None) if
        the sized amount doesn't even clear MIN_NOTIONAL -- that's a
        real rejection Binance would give, surfaced here before ever
        sending the order.
        """
        filters, err = self.get_symbol_filters(symbol)
        if err:
            return None, err
        step_size = float(filters["LOT_SIZE"]["stepSize"])
        raw_qty = usd_amount / price
        qty = round_step_size(raw_qty, step_size)

        min_notional = filters.get("MIN_NOTIONAL")
        if min_notional and qty * price < float(min_notional["notional"]):
            return None, f"tamanho ${usd_amount:.2f} fica abaixo do MIN_NOTIONAL de {symbol}"
        if qty <= 0:
            return None, f"tamanho ${usd_amount:.2f} arredonda para 0 no step de {symbol}"
        return qty, None

    def place_market_order(self, symbol, side, quantity, reduce_only=False):
        """side: 'BUY' or 'SELL'. Returns (order_response, error)."""
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._signed_request("POST", "/fapi/v1/order", params)

    def get_position_amt(self, symbol):
        """Returns (signed position size, error) -- positive for LONG,
        negative for SHORT, 0.0 if flat."""
        data, err = self._signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if err:
            return None, err
        for p in data:
            if p["symbol"] == symbol:
                return float(p["positionAmt"]), None
        return 0.0, None

    def get_position_entry_price(self, symbol):
        """Returns (entry_price, error) -- the exchange's own recorded
        average entry price for the symbol's current position, 0.0 if
        flat. Vendored addition (not in gg-shot-monitor's original):
        needed here because adding to an already-open position (the
        copy-trading platform's redirect-to-leader mechanic) blends a
        NEW weighted-average entry price server-side on Binance's end
        -- a single order's own `avgPrice` only reflects that one
        fill, not the position's overall blended entry."""
        data, err = self._signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if err:
            return None, err
        for p in data:
            if p["symbol"] == symbol:
                return float(p["entryPrice"]), None
        return 0.0, None

    def get_last_trade_price(self, symbol, since_ms=None):
        """Returns (price, error) -- the average price of this
        account's most recent REAL fill on `symbol` (optionally only
        trades at/after `since_ms`), straight from Binance's own trade
        record. Used when a position is found already flat before our
        own close order gets a chance to run (confirmed live
        2026-09-15: a fast adverse move on a volatile day -- possibly a
        real liquidation -- closed a position before the engine's next
        5-minute check; falling back to the strategy's own THEORETICAL
        exit price in that case produced a real, plausible-looking but
        completely wrong result, even the wrong sign, since the actual
        market had already moved well past what the theoretical price
        assumed). This is the authoritative source for what a symbol
        actually closed at when no order response is available to ask
        directly."""
        params = {"symbol": symbol, "limit": 10}
        if since_ms:
            params["startTime"] = since_ms
        data, err = self._signed_request("GET", "/fapi/v1/userTrades", params)
        if err:
            return None, err
        if not data:
            return None, "sem trades recentes para este simbolo"
        last = max(data, key=lambda t: t["time"])
        return float(last["price"]), None

    def set_leverage(self, symbol, leverage):
        """Sets the leverage used for a symbol's FUTURE orders -- Binance
        does not default new orders to 1x on its own; whatever leverage
        was last set for that symbol (possibly from years of manual UI
        use) stays in effect until explicitly changed. Must be called
        before an entry order for every symbol, not just once at
        startup, since it's per-symbol state on the account."""
        return self._signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        """Sets isolated vs cross margin for a symbol. Binance returns
        error code -4046 ("No need to change margin type") when it's
        already set to the requested type -- treated here as success,
        not a failure, since that's the desired end state either way."""
        data, err = self._signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type})
        if err and isinstance(err, dict) and err.get("code") == -4046:
            return {"msg": "already set"}, None
        return data, err
