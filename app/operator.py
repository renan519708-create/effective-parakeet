"""Operator dashboard: trigger Long/Short campaigns, Encerrar
operacoes, Reiniciar. Also where the Owner promotes an account to
Operator (the socio) -- invite codes live under "Minha conta" (see
app/follower.py's create_invite)."""

from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.engine import campaign_result_pct, fetch_prices, price_roi_pct
from app.extensions import db
from app.models import Campaign, CampaignSymbol, FollowerAllocation, FollowerCampaignState, Position, User
from app.universe import resolve_symbol_universe

operator_bp = Blueprint("operator", __name__, url_prefix="/operador")


def operator_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.can_operate:
            flash("Acesso restrito ao operador.", "error")
            return redirect(url_for("follower.dashboard"))
        return view(*args, **kwargs)
    return wrapped


def owner_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != "owner":
            flash("Acesso restrito ao owner.", "error")
            return redirect(url_for("operator.dashboard"))
        return view(*args, **kwargs)
    return wrapped


@operator_bp.route("/")
@operator_required
def dashboard():
    campaign = Campaign.query.filter(Campaign.status.in_(["active", "stopping"])).first()
    last_campaigns = Campaign.query.order_by(Campaign.started_at.desc()).limit(20).all()

    live = {}
    if campaign:
        try:
            current_prices = fetch_prices([s.symbol for s in campaign.symbols], current_app.config["BINANCE_TESTNET"])
        except Exception:  # noqa: BLE001 -- a price hiccup must never break the dashboard, table just shows "-"
            current_prices = {}

        # Only a REAL average fill price from actual open positions
        # counts here -- the same avgPrice Binance itself reports.
        # Showing a synthetic reference price (captured at
        # campaign-start, before any real order necessarily filled) with
        # the same visual confidence as a real number is exactly what
        # made this disagree with Binance (confirmed live: AKEUSDT had
        # no real position at all yet still showed a tracked %;
        # FLOCKUSDT/BTRUSDT had real positions but a stale
        # reference-based Entrada that never got synced). No real
        # position backing a symbol -> "-", full stop, never a guess.
        real_entries = dict(
            db.session.query(Position.symbol, db.func.avg(Position.entry_price))
            .filter_by(campaign_id=campaign.id, status="open")
            .group_by(Position.symbol)
            .all()
        )

        changed = False
        for s in campaign.symbols:
            current = current_prices.get(s.symbol)
            real_entry = real_entries.get(s.symbol)
            if real_entry is not None and s.entry_price != real_entry:
                # Kept in sync so Ultimas campanhas' eventual %-result
                # (computed from entry_price after the campaign closes)
                # is accurate too, not just this live view.
                s.entry_price = real_entry
                changed = True
            pct = price_roi_pct(campaign.direction, real_entry, current, 1) if (real_entry and current) else None
            live[s.symbol] = {"entry": real_entry, "current": current, "pct": pct}
        if changed:
            db.session.commit()

    # Same "only count symbols with real trade evidence" rule for the
    # historical Resultado column -- CampaignSymbol.entry_price is
    # always set (a synthetic reference captured at campaign-start,
    # see start_campaign), even for a symbol whose real order never
    # filled, so entry_price alone can't tell "really traded" from
    # "never traded". Any Position row (open or closed) at any point
    # is real evidence; its absence means skip the symbol entirely.
    campaign_ids = [c.id for c in last_campaigns]
    traded_pairs = set(
        db.session.query(Position.campaign_id, Position.symbol)
        .filter(Position.campaign_id.in_(campaign_ids))
        .distinct()
        .all()
    ) if campaign_ids else set()
    results = {
        c.id: campaign_result_pct(c.direction, [s for s in c.symbols if (c.id, s.symbol) in traded_pairs])
        for c in last_campaigns
    }

    return render_template(
        "operator_dashboard.html",
        campaign=campaign,
        last_campaigns=last_campaigns,
        is_owner=current_user.role == "owner",
        live=live,
        results=results,
    )


def _parse_float(raw, default):
    """request.form values are always strings -- a browser/OS set to
    pt-BR can hand back "2,5" instead of "2.5" for a <input
    type="number">, which float() rejects outright. Accepting either
    decimal separator here is what stands between a stray comma and an
    unhandled 500 (this exact crash happened live)."""
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip().replace(",", "."))
    except ValueError:
        return default


def _parse_int(raw, default):
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


@operator_bp.route("/campanha/iniciar", methods=["POST"])
@operator_required
def start_campaign():
    if Campaign.query.filter(Campaign.status.in_(["active", "stopping"])).first():
        flash("Ja existe uma campanha ativa -- encerre antes de iniciar outra.", "error")
        return redirect(url_for("operator.dashboard"))

    direction = request.form.get("direction")
    if direction not in ("long", "short"):
        flash("Escolha Long ou Short.", "error")
        return redirect(url_for("operator.dashboard"))

    scope = request.form.get("scope", "single")
    stop_pct = _parse_float(request.form.get("stop_pct"), 2.5)
    params = {}
    if scope == "single":
        params["symbol"] = (request.form.get("symbol") or "BTCUSDT").upper()
    elif scope in ("topn", "relbtc", "relbtc_weak"):
        params["topN"] = _parse_int(request.form.get("top_n"), 10)
        if scope in ("relbtc", "relbtc_weak"):
            params["lookbackValue"] = _parse_int(request.form.get("lookback_value"), 30)
            params["lookbackUnit"] = "hours" if request.form.get("lookback_unit") == "hours" else "days"
    elif scope == "ranks":
        params["ranks"] = [int(r.strip()) for r in (request.form.get("ranks") or "").split(",") if r.strip().isdigit()]

    testnet = current_app.config["BINANCE_TESTNET"]
    try:
        resolved = resolve_symbol_universe(scope, params, int(datetime.now(timezone.utc).timestamp() * 1000), testnet)

        campaign = Campaign(direction=direction, universe_scope=scope, universe_params=params, stop_pct=stop_pct, status="active", started_by_id=current_user.id)
        db.session.add(campaign)
        db.session.flush()
        # Reference price at campaign start, for the dashboard's live
        # %-move column -- best effort: a failed price fetch here must
        # never block the campaign itself from starting, entry_price
        # just stays null and the dashboard shows "-" for that symbol.
        try:
            entry_prices = fetch_prices([item["symbol"] for item in resolved], testnet)
        except Exception:  # noqa: BLE001
            entry_prices = {}
        for item in resolved:
            db.session.add(CampaignSymbol(campaign_id=campaign.id, symbol=item["symbol"], rank=item["rank"], entry_price=entry_prices.get(item["symbol"])))
        db.session.commit()
    except Exception as e:  # noqa: BLE001 -- surface any failure as a flash, never a raw 500
        db.session.rollback()
        flash(f"Nao foi possivel iniciar a campanha: {e}", "error")
        return redirect(url_for("operator.dashboard"))

    flash(f"Campanha {direction.upper()} iniciada com {len(resolved)} simbolo(s).", "success")
    return redirect(url_for("operator.dashboard"))


@operator_bp.route("/campanha/encerrar", methods=["POST"])
@operator_required
def stop_campaign():
    campaign = Campaign.query.filter_by(status="active").first()
    if not campaign:
        flash("Nenhuma campanha ativa.", "error")
        return redirect(url_for("operator.dashboard"))
    campaign.status = "stopping"
    db.session.commit()
    flash("Encerrando -- o motor vai fechar as posicoes reais de todo mundo nos proximos ciclos.", "success")
    return redirect(url_for("operator.dashboard"))


@operator_bp.route("/campanha/reiniciar", methods=["POST"])
@operator_required
def reset_campaign():
    campaign = Campaign.query.filter(Campaign.status.in_(["active", "stopping"])).first()
    if campaign:
        # Escape hatch only -- normally a campaign reaches "stopped" on
        # its own once the engine confirms every real position closed.
        # Still capture an exit_price per symbol here (best effort) so
        # this path leaves Ultimas campanhas with a %-result too,
        # instead of only the engine's normal stopping->stopped path.
        try:
            exit_prices = fetch_prices([s.symbol for s in campaign.symbols], current_app.config["BINANCE_TESTNET"])
        except Exception:  # noqa: BLE001
            exit_prices = {}
        for s in campaign.symbols:
            if s.exit_price is None:
                s.exit_price = exit_prices.get(s.symbol)
        campaign.status = "stopped"
        campaign.ended_at = datetime.now(timezone.utc)
        db.session.commit()
    flash("Pronto para uma nova campanha.", "success")
    return redirect(url_for("operator.dashboard"))


@operator_bp.route("/campanha/<int:campaign_id>/apagar", methods=["POST"])
@operator_required
def delete_campaign(campaign_id):
    campaign = Campaign.query.get(campaign_id)
    if not campaign:
        flash("Campanha nao encontrada.", "error")
        return redirect(url_for("operator.dashboard"))
    if campaign.status in ("active", "stopping"):
        flash("Encerre a campanha antes de apagar.", "error")
        return redirect(url_for("operator.dashboard"))

    # CampaignSymbol cascades via the model relationship; these three
    # don't have a relationship/cascade defined on Campaign, so they're
    # cleared by hand -- otherwise the FK from each would block the
    # delete (or, worse on a backend without FK enforcement, leave
    # orphan rows behind).
    FollowerAllocation.query.filter_by(campaign_id=campaign.id).delete()
    Position.query.filter_by(campaign_id=campaign.id).delete()
    FollowerCampaignState.query.filter_by(campaign_id=campaign.id).delete()
    db.session.delete(campaign)
    db.session.commit()
    flash("Campanha apagada.", "success")
    return redirect(url_for("operator.dashboard"))


@operator_bp.route("/promover", methods=["POST"])
@owner_required
def promote_operator():
    email = request.form.get("email", "").strip().lower()
    user = User.query.filter_by(email=email).first()
    if not user:
        flash("Usuario nao encontrado.", "error")
    else:
        user.role = "operator"
        db.session.commit()
        flash(f"{email} agora e operador.", "success")
    return redirect(url_for("operator.dashboard"))
