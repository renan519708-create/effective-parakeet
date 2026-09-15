"""Backtest 'Long Diária Composta' (estrategia_long_diaria_composta.pdf),
SIMPLIFIED per explicit request 2026-09-15: no stop-loss, no intraday
redirect -- every day, open all 53 universe assets LONG at 05:15
(Brasília), hold regardless of intraday move, force-close everything at
21:00, whatever the result at that moment. This isolates the core
mechanic (always-long, daily equal-split, capital compounding) from the
risk-management layer (sections 7-8 of the PDF), which is a separate
question.

Reuses app.universe.DAILY_LONG_TICKERS -- the exact same validated
53-symbol list the live feature trades -- so this tests exactly the
universe that's actually running in production.

Precision: uses 15-minute klines so the 05:15 and 21:00 entry/exit
prices are each an EXACT candle open, not interpolated from coarser
1h bars.

Usage:
    python daily_composto_backtest.py
"""

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
# Same 0.04%-per-side taker estimate the PDF itself assumes (section 11),
# applied round-trip (open + close) -- consistent with every other
# backtest this project has run.
ROUND_TRIP_FEE_PCT = 0.08
FETCH_WORKERS = 8

START_DATE = date(2026, 1, 1)
END_DATE = date.today()  # today's candle is likely still incomplete/missing -- dropped naturally if so


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
        if resp.status_code == 429 or resp.status_code == 418:
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


def daily_open_close_for_symbol(symbol, start_dt, end_dt):
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    raw = fetch_15m_klines(symbol, start_ms, end_ms)
    if not raw:
        return symbol, {}
    by_open_ms = {k[0]: float(k[1]) for k in raw}
    result = {}
    d = start_dt.date()
    last_day = end_dt.date()
    while d <= last_day:
        entry_dt = datetime(d.year, d.month, d.day, 5, 15, tzinfo=BRASILIA_TZ)
        exit_dt = datetime(d.year, d.month, d.day, 21, 0, tzinfo=BRASILIA_TZ)
        entry_ms = int(entry_dt.timestamp() * 1000)
        exit_ms = int(exit_dt.timestamp() * 1000)
        if entry_ms in by_open_ms and exit_ms in by_open_ms:
            entry_price = by_open_ms[entry_ms]
            exit_price = by_open_ms[exit_ms]
            if entry_price > 0:
                result[d] = (entry_price, exit_price)
        d += timedelta(days=1)
    return symbol, result


def main():
    valid = fetch_binance_futures_symbols(False)
    symbols = [f"{t}USDT" for t in DAILY_LONG_TICKERS if f"{t}USDT" in valid]
    print(f"{len(symbols)} simbolos validos de {len(DAILY_LONG_TICKERS)} no universo.")

    start_dt = datetime(START_DATE.year, START_DATE.month, START_DATE.day, 0, 0, tzinfo=BRASILIA_TZ)
    end_dt = datetime(END_DATE.year, END_DATE.month, END_DATE.day, 23, 59, tzinfo=BRASILIA_TZ)
    print(f"Periodo: {START_DATE} a {END_DATE} ({(END_DATE - START_DATE).days} dias corridos).")
    print(f"Buscando velas de 15m para {len(symbols)} simbolos ({FETCH_WORKERS} em paralelo)... isso pode levar alguns minutos.\n")

    per_symbol_days = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {pool.submit(daily_open_close_for_symbol, s, start_dt, end_dt): s for s in symbols}
        done = 0
        for fut in as_completed(futures):
            symbol, days = fut.result()
            per_symbol_days[symbol] = days
            done += 1
            if done % 10 == 0 or done == len(symbols):
                print(f"  {done}/{len(symbols)} simbolos processados ({time.time()-t0:.0f}s)...")

    # Build day -> {symbol: (entry, exit)} for symbols with real data that day
    by_day = defaultdict(dict)
    for symbol, days in per_symbol_days.items():
        for d, prices in days.items():
            by_day[d][symbol] = prices

    all_days = sorted(by_day.keys())
    print(f"\n{len(all_days)} dias com pelo menos 1 simbolo com dado valido.\n")

    balance_gross = START_CAPITAL
    balance_net = START_CAPITAL
    monthly_gross = {}
    monthly_net = {}
    negative_days_gross = 0
    negative_days_net = 0
    daily_rows = []

    for d in all_days:
        day_symbols = by_day[d]
        n = len(day_symbols)
        if n == 0:
            continue
        pct_list = []
        for symbol, (entry, exit_) in day_symbols.items():
            pct_list.append((exit_ - entry) / entry * 100)
        day_avg_gross = sum(pct_list) / n
        day_avg_net = sum(p - ROUND_TRIP_FEE_PCT for p in pct_list) / n

        balance_gross *= (1 + day_avg_gross / 100)
        balance_net *= (1 + day_avg_net / 100)
        if day_avg_gross < 0:
            negative_days_gross += 1
        if day_avg_net < 0:
            negative_days_net += 1

        key = (d.year, d.month)
        monthly_gross[key] = balance_gross
        monthly_net[key] = balance_net
        daily_rows.append((d, n, day_avg_gross, day_avg_net, balance_gross, balance_net))

    print(f"{'Mes':<10} {'Dias':>5} {'Saldo bruto':>14} {'Saldo liquido':>14}")
    print("-" * 48)
    seen_months = set()
    for d, n, dg, dn, bg, bn in daily_rows:
        key = (d.year, d.month)
        if key in seen_months:
            continue
        seen_months.add(key)
    prev_g, prev_n = START_CAPITAL, START_CAPITAL
    for key in sorted(set((d.year, d.month) for d in all_days)):
        y, m = key
        bg = monthly_gross[key]
        bn = monthly_net[key]
        label = f"{month_name[m][:3]}/{y}"
        print(f"{label:<10} {'':>5} {bg:>13,.2f} {bn:>13,.2f}")

    print("-" * 48)
    total_days = len(daily_rows)
    print(f"\nCapital inicial: ${START_CAPITAL:,.2f}")
    print(f"Saldo final BRUTO (sem taxa): ${balance_gross:,.2f}  ({(balance_gross/START_CAPITAL-1)*100:+.2f}%)")
    print(f"Saldo final LIQUIDO (com taxa {ROUND_TRIP_FEE_PCT}% round-trip): ${balance_net:,.2f}  ({(balance_net/START_CAPITAL-1)*100:+.2f}%)")
    print(f"\nDias operados: {total_days}")
    print(f"Dias negativos (bruto): {negative_days_gross} ({negative_days_gross/total_days*100:.1f}%)")
    print(f"Dias negativos (liquido): {negative_days_net} ({negative_days_net/total_days*100:.1f}%)")

    # Worst and best days, net
    worst = min(daily_rows, key=lambda r: r[3])
    best = max(daily_rows, key=lambda r: r[3])
    print(f"\nPior dia (liquido): {worst[0]}  {worst[3]:+.2f}%  ({worst[1]} simbolos)")
    print(f"Melhor dia (liquido): {best[0]}  {best[3]:+.2f}%  ({best[1]} simbolos)")


if __name__ == "__main__":
    main()
