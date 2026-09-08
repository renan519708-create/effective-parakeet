"""'Resultados': performance reporting shared by two views -- "Meus
resultados" (a user's own closed positions) and "Historico da
estrategia" (aggregate campaign performance, no dollar amounts,
visible to any logged-in user as proof the strategy works). Both
render the same filter + cumulative-%-line-chart + operations-table
shape; only where the trades come from differs (see
_mine_trades/_strategy_trades). See
docs/superpowers/specs/2026-09-08-resultados-tab-design.md.
"""

from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template, request
from flask_login import current_user, login_required

from app.engine import price_roi_pct
from app.models import Campaign, Position

results_bp = Blueprint("results", __name__, url_prefix="/resultados")

# Rolling windows from now, not calendar-aligned -- "Semanal" always
# means "last 7 days", not "since Monday".
PERIOD_CUTOFFS = {
    "daily": timedelta(hours=24),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
    "yearly": timedelta(days=365),
    "all": None,
}
PERIOD_LABELS = [
    ("daily", "Diário"),
    ("weekly", "Semanal"),
    ("monthly", "Mensal"),
    ("yearly", "Anual"),
    ("all", "Acumulado"),
]


# ---------------------------------------------------------------------------
# Pure functions -- no DB, no network. Fully unit-testable (test_results.py).
# ---------------------------------------------------------------------------

def build_result_series(trades):
    """trades: list of {"symbol", "entry", "exit", "direction", "at"}
    already sorted by "at" ascending (ties broken by symbol -- see
    _strategy_trades). Returns {"points": [{"t", "cum_pct"}], "rows":
    [{"symbol", "entry", "exit", "pct", "at"}]} -- "points" is the
    running cumulative %% for the line chart (each window restarts this
    at 0, callers only ever pass trades already inside one window);
    "rows" is each trade's own %%, newest first for the table."""
    points = []
    rows = []
    cum = 0.0
    for t in trades:
        pct = price_roi_pct(t["direction"], t["entry"], t["exit"], 1)
        cum += pct
        points.append({"t": t["at"], "cum_pct": cum})
        rows.append({"symbol": t["symbol"], "entry": t["entry"], "exit": t["exit"], "pct": pct, "at": t["at"]})
    rows.reverse()
    return {"points": points, "rows": rows}


def render_line_svg(points, width=680, height=200):
    """Server-rendered line chart, no JS library -- a single polyline of
    the cumulative %% plus a dashed zero line. Returns an SVG string
    ready for `| safe` in Jinja. Never raises: empty input draws just
    the zero line, a single point draws a flat line at its own value."""
    pad = 10
    zero_line = f'<line x1="{pad}" y1="{{y0:.1f}}" x2="{width - pad}" y2="{{y0:.1f}}" stroke="#8A91A6" stroke-width="1" stroke-dasharray="3,3"/>'

    if not points:
        y0 = height / 2
        return f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">{zero_line.format(y0=y0)}</svg>'

    values = [p["cum_pct"] for p in points]
    lo, hi = min(values + [0.0]), max(values + [0.0])
    span = (hi - lo) or 1.0  # every point identical (e.g. one trade at 0%) -- avoid /0

    def x_at(i):
        return width / 2 if len(points) == 1 else pad + (width - 2 * pad) * i / (len(points) - 1)

    def y_at(v):
        return pad + (height - 2 * pad) * (1 - (v - lo) / span)  # inverted: higher %% draws higher on screen

    coords = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(values))
    color = "#22C08E" if values[-1] >= 0 else "#F2555C"

    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
        f'{zero_line.format(y0=y_at(0.0))}'
        f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'</svg>'
    )


# ---------------------------------------------------------------------------
# Data layer -- DB queries, scoped by view + period.
# ---------------------------------------------------------------------------

def _cutoff_dt(period):
    delta = PERIOD_CUTOFFS.get(period)
    return None if delta is None else datetime.now(timezone.utc) - delta


def _mine_trades(user_id, period):
    query = Position.query.filter_by(user_id=user_id, status="closed")
    cutoff = _cutoff_dt(period)
    if cutoff is not None:
        query = query.filter(Position.closed_at >= cutoff)
    positions = query.order_by(Position.closed_at.asc(), Position.symbol.asc()).all()
    return [
        {"symbol": p.symbol, "entry": p.entry_price, "exit": p.close_price, "direction": p.side, "at": p.closed_at}
        for p in positions
        if p.entry_price and p.close_price
    ]


def _strategy_trades(period):
    query = Campaign.query.filter_by(status="stopped")
    cutoff = _cutoff_dt(period)
    if cutoff is not None:
        query = query.filter(Campaign.ended_at >= cutoff)
    campaigns = query.order_by(Campaign.ended_at.asc()).all()
    trades = []
    for c in campaigns:
        # Same "real trade evidence" rule as campaign_result_pct /
        # Ultimas campanhas: a symbol whose real order never filled has
        # no exit_price and is skipped, never counted as a "result".
        for s in sorted(c.symbols, key=lambda cs: cs.symbol):
            if s.entry_price and s.exit_price:
                trades.append({"symbol": s.symbol, "entry": s.entry_price, "exit": s.exit_price, "direction": c.direction, "at": c.ended_at})
    return trades


@results_bp.route("/")
@login_required
def dashboard():
    view = request.args.get("view", "mine")
    if view not in ("mine", "strategy"):
        view = "mine"
    period = request.args.get("period", "all")
    if period not in PERIOD_CUTOFFS:
        period = "all"

    trades = _mine_trades(current_user.id, period) if view == "mine" else _strategy_trades(period)
    series = build_result_series(trades)

    return render_template(
        "results.html",
        view=view,
        period=period,
        period_labels=PERIOD_LABELS,
        rows=series["rows"],
        chart_svg=render_line_svg(series["points"]),
        final_pct=(series["points"][-1]["cum_pct"] if series["points"] else None),
    )
