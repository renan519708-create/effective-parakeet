"""Parameter sweep for 'Long Diária Composta' (stop_pct x leverage),
reusing daily_composto_backtest_full.py's simulation logic. Fetches the
15m candle data ONCE (cached to disk), then re-runs the day-by-day
stop+redirect simulation for every (stop_pct, leverage) combination
without hitting the network again.

Usage:
    python daily_composto_sweep.py --fetch     # once, caches candles
    python daily_composto_sweep.py --sweep     # many times, instant after --fetch
"""

import argparse
import pickle
import time
from calendar import month_name
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

import requests

from app.universe import DAILY_LONG_TICKERS, fetch_binance_futures_symbols

MAINNET_BASE = "https://fapi.binance.com"
BRASILIA_TZ = timezone(timedelta(hours=-3))
START_CAPITAL = 1000.0
FEE_PCT_PER_SIDE = 0.04
FETCH_WORKERS = 8
CACHE_FILE = "data_daily_composto_candles.pkl"

START_DATE = date(2026, 1, 1)
END_DATE = date.today()


def fetch_15m_klines(symbol, start_ms, end_ms):
    all_candles = []
    cursor = start_ms
    for _ in range(60):
        try:
            resp = requests.get(f"{MAINNET_BASE}/fapi/v1/klines", params={
                "symbol": symbol, "interval": "15m", "startTime": cursor, "endTime": end_ms, "limit": 1000,
            }, timeout=20)
        except requests.RequestException:
            time.sleep(1)
            continue
        if resp.status_code in (429, 418):
            time.sleep(3)
            continue
        if resp.status_code != 200:
            return all_candles if all_candles else None
        batch = resp.json()
        if not batch:
            break
        all_candles.extend(batch)
        last_close = batch[-1][6]
        if last_close >= end_ms or len(batch) < 1000:
            break
        cursor = last_close + 1
        time.sleep(0.05)
    return all_candles


def fetch_symbol_days(symbol, start_dt, end_dt):
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    raw = fetch_15m_klines(symbol, start_ms, end_ms)
    if not raw:
        return symbol, {}
    bars = [(k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in raw]
    by_day = defaultdict(list)
    for open_ms, o, h, l, c in bars:
        dt = datetime.fromtimestamp(open_ms / 1000, tz=BRASILIA_TZ)
        t = dt.time()
        if (t.hour, t.minute) < (5, 15) or (t.hour, t.minute) > (21, 0):
            continue
        by_day[dt.date()].append((open_ms, o, h, l, c))
    for d in by_day:
        by_day[d].sort(key=lambda b: b[0])
    return symbol, dict(by_day)


def do_fetch():
    valid = fetch_binance_futures_symbols(False)
    symbols = [f"{t}USDT" for t in DAILY_LONG_TICKERS if f"{t}USDT" in valid]
    print(f"{len(symbols)} simbolos validos.")

    start_dt = datetime(START_DATE.year, START_DATE.month, START_DATE.day, 0, 0, tzinfo=BRASILIA_TZ)
    end_dt = datetime(END_DATE.year, END_DATE.month, END_DATE.day, 23, 59, tzinfo=BRASILIA_TZ)
    print(f"Buscando velas de 15m ({FETCH_WORKERS} em paralelo)...\n")

    per_symbol_days = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {pool.submit(fetch_symbol_days, s, start_dt, end_dt): s for s in symbols}
        done = 0
        for fut in as_completed(futures):
            symbol, days = fut.result()
            per_symbol_days[symbol] = days
            done += 1
            if done % 10 == 0 or done == len(symbols):
                print(f"  {done}/{len(symbols)} simbolos ({time.time()-t0:.0f}s)...")

    by_day = defaultdict(dict)
    for symbol, days in per_symbol_days.items():
        for d, bars in days.items():
            if not bars:
                continue
            first_t = datetime.fromtimestamp(bars[0][0] / 1000, tz=BRASILIA_TZ).time()
            last_t = datetime.fromtimestamp(bars[-1][0] / 1000, tz=BRASILIA_TZ).time()
            if (first_t.hour, first_t.minute) <= (5, 20) and (last_t.hour, last_t.minute) >= (20, 45):
                by_day[d][symbol] = bars

    with open(CACHE_FILE, "wb") as f:
        pickle.dump(dict(by_day), f)
    print(f"\nCache salvo em {CACHE_FILE}: {len(by_day)} dias.")


def simulate_day(day_bars_by_symbol, balance, leverage, stop_pct, redirect=True):
    """redirect=True: freed margin from a stopped symbol buys MORE of
    the current best-performing still-open symbol (a new tranche at
    that symbol's current price -- exposed to further market risk the
    rest of the day; PDF section 8's literal mechanic).
    redirect=False: "adicionar margem" instead of buying more -- the
    freed margin is just left uninvested (idle) for the rest of the
    day, no further price exposure at all. Since `balance` only ever
    moves via each REALIZED tranche's own PnL delta (never by the act
    of allocating/freeing margin itself), doing nothing here already
    IS "add margin, don't rebuy": the stopped tranche's own PnL already
    reflects losing exactly stop_pct% of its margin, and the remaining
    (1 - stop_pct/100) share stays correctly preserved inside `balance`
    with zero code needed to track it as a separate idle bucket."""
    raw_stop_pct = stop_pct / leverage
    symbols = [s for s, bars in day_bars_by_symbol.items() if bars and bars[0][1] > 0]
    n = len(symbols)
    if n == 0:
        return 0.0, 0

    margin_per_symbol = balance / n
    tranches = {s: [{"margin": margin_per_symbol, "entry": day_bars_by_symbol[s][0][1], "open": True}] for s in symbols}
    stopped = set()
    stop_events = 0

    all_ts = sorted(set(b[0] for bars in day_bars_by_symbol.values() for b in bars))
    bar_by_symbol_ts = {s: {b[0]: b for b in day_bars_by_symbol[s]} for s in symbols}

    def blended_roi(symbol, price_now):
        open_tranches = [t for t in tranches[symbol] if t["open"]]
        if not open_tranches:
            return None
        total_margin = sum(t["margin"] for t in open_tranches)
        if total_margin <= 0:
            return None
        weighted = sum(t["margin"] * ((price_now - t["entry"]) / t["entry"] * 100) for t in open_tranches)
        return weighted / total_margin

    for ts in all_ts:
        for symbol in symbols:
            if symbol in stopped:
                continue
            bar = bar_by_symbol_ts[symbol].get(ts)
            if not bar:
                continue
            _, o, h, l, c = bar
            entry = tranches[symbol][0]["entry"]
            adverse_pct = max(0.0, (entry - l) / entry * 100)
            if adverse_pct >= raw_stop_pct:
                stop_price = entry * (1 - raw_stop_pct / 100)
                total_margin = sum(t["margin"] for t in tranches[symbol] if t["open"])
                for t in tranches[symbol]:
                    if t["open"]:
                        t["exit"] = stop_price
                        t["open"] = False
                stopped.add(symbol)
                stop_events += 1
                freed_margin = total_margin * (1 - stop_pct / 100)
                if redirect and freed_margin > 0:
                    candidates = {}
                    for other in symbols:
                        if other == symbol or other in stopped:
                            continue
                        other_bar = bar_by_symbol_ts[other].get(ts)
                        if not other_bar:
                            continue
                        roi = blended_roi(other, other_bar[4])
                        if roi is not None:
                            candidates[other] = (roi, other_bar[4])
                    if candidates:
                        leader = max(candidates, key=lambda s: candidates[s][0])
                        leader_price = candidates[leader][1]
                        tranches[leader].append({"margin": freed_margin, "entry": leader_price, "open": True})

    for symbol in symbols:
        if symbol in stopped:
            continue
        last_bar = day_bars_by_symbol[symbol][-1]
        for t in tranches[symbol]:
            if t["open"]:
                t["exit"] = last_bar[4]
                t["open"] = False

    total_pnl = 0.0
    for symbol, tlist in tranches.items():
        for t in tlist:
            if "exit" not in t:
                continue
            raw_pct = (t["exit"] - t["entry"]) / t["entry"] * 100
            net_raw_pct = raw_pct - FEE_PCT_PER_SIDE * 2
            total_pnl += t["margin"] * leverage * (net_raw_pct / 100)
    return total_pnl, stop_events


def run_one(by_day, all_days, leverage, stop_pct, redirect=True):
    balance = START_CAPITAL
    negative_days = 0
    stop_count_total = 0
    min_balance = balance
    for d in all_days:
        pnl, stops_today = simulate_day(by_day[d], balance, leverage, stop_pct, redirect=redirect)
        balance += pnl
        min_balance = min(min_balance, balance)
        if pnl < 0:
            negative_days += 1
        stop_count_total += stops_today
        if balance <= 0.01:
            balance = 0.01  # floor -- a real account would be liquidated/wiped, stop compounding negative
    return balance, negative_days, stop_count_total, min_balance


def do_sweep():
    with open(CACHE_FILE, "rb") as f:
        by_day = pickle.load(f)
    all_days = sorted(by_day.keys())
    print(f"{len(all_days)} dias carregados do cache.\n")

    raw_stop_targets = [2.0, 3.0, 5.0, 8.0, 12.0, 20.0]
    leverages = [1, 2, 3, 5, 10]

    print(f"{'Raw stop':>9} {'Lev':>4} {'Stop cfg':>9} {'Saldo final':>14} {'Retorno':>10} {'Dias neg':>9} {'Stops tot':>10}")
    print("-" * 72)
    results = []
    for raw_stop in raw_stop_targets:
        for lev in leverages:
            stop_pct_cfg = raw_stop * lev  # what you'd type into the "Stop-loss" field
            balance, neg_days, stops, min_bal = run_one(by_day, all_days, lev, stop_pct_cfg)
            ret_pct = (balance / START_CAPITAL - 1) * 100
            results.append((raw_stop, lev, stop_pct_cfg, balance, ret_pct, neg_days, stops))
            print(f"{raw_stop:>8.1f}% {lev:>3}x {stop_pct_cfg:>8.1f}% {balance:>13,.2f} {ret_pct:>+9.1f}% {neg_days:>9} {stops:>10}")

    print("\nMelhores 5 por saldo final:")
    for r in sorted(results, key=lambda x: -x[3])[:5]:
        raw_stop, lev, stop_pct_cfg, balance, ret_pct, neg_days, stops = r
        print(f"  stop bruto {raw_stop}% a {lev}x (campo 'stop' = {stop_pct_cfg:.1f}%): ${balance:,.2f} ({ret_pct:+.1f}%), {stops} stops")


def do_sweep_margin_add():
    """Same grid as do_sweep(), but redirect=False: a stopped symbol's
    freed margin is left idle (uninvested) for the rest of the day
    instead of buying more of the current best performer -- "adicionar
    margem" instead of "comprar mais"."""
    with open(CACHE_FILE, "rb") as f:
        by_day = pickle.load(f)
    all_days = sorted(by_day.keys())
    print(f"{len(all_days)} dias carregados do cache. Modo: ADICIONAR MARGEM (sem recomprar).\n")

    raw_stop_targets = [2.0, 3.0, 5.0, 8.0, 12.0, 20.0]
    leverages = [1, 2, 3, 5, 10]

    print(f"{'Raw stop':>9} {'Lev':>4} {'Stop cfg':>9} {'Saldo final':>14} {'Retorno':>10} {'Dias neg':>9} {'Stops tot':>10}")
    print("-" * 72)
    results = []
    for raw_stop in raw_stop_targets:
        for lev in leverages:
            stop_pct_cfg = raw_stop * lev
            balance, neg_days, stops, min_bal = run_one(by_day, all_days, lev, stop_pct_cfg, redirect=False)
            ret_pct = (balance / START_CAPITAL - 1) * 100
            results.append((raw_stop, lev, stop_pct_cfg, balance, ret_pct, neg_days, stops))
            print(f"{raw_stop:>8.1f}% {lev:>3}x {stop_pct_cfg:>8.1f}% {balance:>13,.2f} {ret_pct:>+9.1f}% {neg_days:>9} {stops:>10}")

    print("\nMelhores 5 por saldo final:")
    for r in sorted(results, key=lambda x: -x[3])[:5]:
        raw_stop, lev, stop_pct_cfg, balance, ret_pct, neg_days, stops = r
        print(f"  stop bruto {raw_stop}% a {lev}x (campo 'stop' = {stop_pct_cfg:.1f}%): ${balance:,.2f} ({ret_pct:+.1f}%), {stops} stops")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--sweep-margin", action="store_true", help="redirect via 'adicionar margem' em vez de recomprar")
    args = parser.parse_args()
    if args.fetch:
        do_fetch()
    elif args.sweep:
        do_sweep()
    elif args.sweep_margin:
        do_sweep_margin_add()
    else:
        print("Use --fetch, --sweep ou --sweep-margin")


if __name__ == "__main__":
    main()
