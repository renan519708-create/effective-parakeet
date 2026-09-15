"""Full backtest of 'Long Diária Composta' (estrategia_long_diaria_composta.pdf)
-- includes stop-loss + intraday redirect-to-leader (sections 7-8),
unlike daily_composto_backtest.py's simplified hold-all-day version.
Explicit request 2026-09-15: stop_pct=20% (ROI on margin, leveraged --
same field the live campaign uses), leverage=10x (matches the account's
current "Minha conta" setting), capital compounding daily.

Simulation per day, per symbol:
  - 05:15: open LONG with capitalPorAtivo = saldo_total / N_simbolos as
    margin, leverage 10x.
  - Every 15m bar from 05:15 to 21:00: if a symbol's LOW breaches the
    stop (raw price move >= stop_pct/leverage against the position),
    close it at the exact stop price (not the bar's low) and, among
    symbols still open at that same bar, redirect the freed margin
    (allocated * (1 - stop_pct/100)) into an ADDITIONAL tranche on
    whichever has the best blended unrealized ROI right now (that
    bar's close) -- same "two entries summed, not blended" modeling the
    PDF specifies (section 8) and the live engine already implements.
  - 21:00: force-close everything still open, whatever the result.
  - Fee: 0.04%/side taker estimate (PDF section 11), applied per
    tranche (its own open + close), scaled by leverage same as the
    PDF's own roiPct formula.

Reuses the SAME 15m kline data shape as daily_composto_backtest.py, but
keeps full OHLC per bar (not just the 05:15/21:00 open) since the stop
check needs each bar's LOW.

Usage:
    python daily_composto_backtest_full.py
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
LEVERAGE = 10
STOP_PCT = 20.0  # leveraged ROI% stop, matches the live Campaign.stop_pct field
RAW_STOP_PCT = STOP_PCT / LEVERAGE  # raw (unleveraged) price-move threshold
FEE_PCT_PER_SIDE = 0.04  # PDF section 11's own taker estimate
FETCH_WORKERS = 8

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
    """Returns {date: [bar, bar, ...]} where each bar is
    (open_ms, open, high, low, close), restricted to the 05:15-21:00
    Brasilia window each day, in chronological order."""
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


def simulate_day(day_bars_by_symbol, balance):
    """day_bars_by_symbol: {symbol: [bar,...]} for symbols that traded
    fully from 05:15 to 21:00 this day. Returns (pnl_dollars, events,
    stop_events) where events is a list of dicts for reporting and
    stop_events is how many symbols got stopped out that day."""
    symbols = [s for s, bars in day_bars_by_symbol.items() if bars and bars[0][1] > 0]
    n = len(symbols)
    if n == 0:
        return 0.0, [], 0

    margin_per_symbol = balance / n
    # tranches[symbol] = list of {"margin": x, "entry": price, "open": True}
    tranches = {s: [{"margin": margin_per_symbol, "entry": day_bars_by_symbol[s][0][1], "open": True}] for s in symbols}
    stopped = set()
    stop_events = 0

    # align all symbols' bar timestamps -- use the union, walk chronologically
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
            entry = tranches[symbol][0]["entry"]  # original tranche's entry defines the stop level
            adverse_pct = max(0.0, (entry - l) / entry * 100)
            if adverse_pct >= RAW_STOP_PCT:
                stop_price = entry * (1 - RAW_STOP_PCT / 100)
                total_margin = sum(t["margin"] for t in tranches[symbol] if t["open"])
                for t in tranches[symbol]:
                    if t["open"]:
                        t["exit"] = stop_price
                        t["open"] = False
                stopped.add(symbol)
                stop_events += 1
                freed_margin = total_margin * (1 - STOP_PCT / 100)
                if freed_margin > 0:
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

    # force-close whatever's still open at 21:00
    last_ts = all_ts[-1] if all_ts else None
    for symbol in symbols:
        if symbol in stopped:
            continue
        last_bar = day_bars_by_symbol[symbol][-1]
        for t in tranches[symbol]:
            if t["open"]:
                t["exit"] = last_bar[4]
                t["open"] = False

    total_pnl = 0.0
    events = []
    for symbol, tlist in tranches.items():
        for t in tlist:
            if "exit" not in t:
                continue
            raw_pct = (t["exit"] - t["entry"]) / t["entry"] * 100
            net_raw_pct = raw_pct - FEE_PCT_PER_SIDE * 2
            pnl = t["margin"] * LEVERAGE * (net_raw_pct / 100)
            total_pnl += pnl
            events.append({"symbol": symbol, "margin": t["margin"], "raw_pct": raw_pct, "pnl": pnl})
    return total_pnl, events, stop_events


def main():
    valid = fetch_binance_futures_symbols(False)
    symbols = [f"{t}USDT" for t in DAILY_LONG_TICKERS if f"{t}USDT" in valid]
    print(f"{len(symbols)} simbolos validos.")

    start_dt = datetime(START_DATE.year, START_DATE.month, START_DATE.day, 0, 0, tzinfo=BRASILIA_TZ)
    end_dt = datetime(END_DATE.year, END_DATE.month, END_DATE.day, 23, 59, tzinfo=BRASILIA_TZ)
    print(f"Periodo: {START_DATE} a {END_DATE}. Alavancagem {LEVERAGE}x, stop {STOP_PCT}% (raw {RAW_STOP_PCT:.2f}%).")
    print(f"Buscando velas de 15m para {len(symbols)} simbolos ({FETCH_WORKERS} em paralelo)...\n")

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
                print(f"  {done}/{len(symbols)} simbolos processados ({time.time()-t0:.0f}s)...")

    by_day = defaultdict(dict)
    for symbol, days in per_symbol_days.items():
        for d, bars in days.items():
            # require the day actually starts at/near 05:15 and ends at/near 21:00
            if not bars:
                continue
            first_t = datetime.fromtimestamp(bars[0][0] / 1000, tz=BRASILIA_TZ).time()
            last_t = datetime.fromtimestamp(bars[-1][0] / 1000, tz=BRASILIA_TZ).time()
            if (first_t.hour, first_t.minute) <= (5, 20) and (last_t.hour, last_t.minute) >= (20, 45):
                by_day[d][symbol] = bars

    all_days = sorted(by_day.keys())
    print(f"\n{len(all_days)} dias com cobertura completa 05:15-21:00 em pelo menos 1 simbolo.\n")

    balance = START_CAPITAL
    monthly_end = {}
    negative_days = 0
    stop_count_total = 0
    daily_rows = []

    for d in all_days:
        pnl, events, stops_today = simulate_day(by_day[d], balance)
        day_pct = (pnl / balance * 100) if balance > 0 else 0.0
        balance += pnl
        if day_pct < 0:
            negative_days += 1
        stop_count_total += stops_today
        key = (d.year, d.month)
        monthly_end[key] = balance
        daily_rows.append((d, len(by_day[d]), day_pct, balance, stops_today))

    print(f"{'Mes':<10} {'Saldo (composto)':>18}")
    print("-" * 32)
    for key in sorted(set((d.year, d.month) for d in all_days)):
        y, m = key
        label = f"{month_name[m][:3]}/{y}"
        print(f"{label:<10} {monthly_end[key]:>17,.2f}")
    print("-" * 32)

    total_days = len(daily_rows)
    print(f"\nCapital inicial: ${START_CAPITAL:,.2f}")
    print(f"Saldo final: ${balance:,.2f}  ({(balance/START_CAPITAL-1)*100:+.2f}%)")
    print(f"Dias operados: {total_days}")
    print(f"Dias negativos: {negative_days} ({negative_days/total_days*100:.1f}%)")
    print(f"Total de stops disparados no periodo: {stop_count_total}")

    worst = min(daily_rows, key=lambda r: r[2])
    best = max(daily_rows, key=lambda r: r[2])
    print(f"\nPior dia: {worst[0]}  {worst[2]:+.2f}%  ({worst[1]} simbolos, {worst[4]} stops)")
    print(f"Melhor dia: {best[0]}  {best[2]:+.2f}%  ({best[1]} simbolos, {best[4]} stops)")


if __name__ == "__main__":
    main()
