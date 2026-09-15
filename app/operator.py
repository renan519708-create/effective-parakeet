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
from app.models import Campaign, CampaignSymbol, DailyCompostoSettings, FollowerAllocation, FollowerCampaignState, OrderLog, Position, User
from app.universe import rank_symbol_universe, resolve_symbol_universe

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
            # Leveraged against the VIEWING operator's own account, not a
            # flat 1x -- confirmed live 2026-09-14: showing the raw 1x
            # price move here read as "wrong" next to Binance's own
            # position view, which always shows ROI already scaled by
            # that account's real leverage (e.g. 6% raw read as 60% on
            # Binance at 10x). This is a live monitoring view for the
            # operator's own account specifically, unlike
            # campaign_result_pct's finished-campaign history figure
            # (kept unleveraged/1x there on purpose -- a %-history
            # figure has to stay comparable across campaigns regardless
            # of whatever leverage was configured at the time).
            leverage = (current_user.settings.leverage if current_user.settings else None) or 1
            pct = price_roi_pct(campaign.direction, real_entry, current, leverage) if (real_entry and current) else None
            live[s.symbol] = {"entry": real_entry, "current": current, "pct": pct}
        if changed:
            db.session.commit()

    # "stopping" means the engine is trying, every tick, to place a real
    # close order for each open Position -- if one keeps failing (bad
    # API key, symbol delisted, exchange rejection, etc.) the campaign
    # can sit in "stopping" forever with no visible reason on this page,
    # which is exactly what made a stuck close look like a silent error
    # (confirmed live 2026-09-13: the button below used to still say
    # "Encerrar operacoes" even while already stopping, and clicking it
    # again just flashed "Nenhuma campanha ativa" -- true, but useless,
    # since the actual problem is the engine's own retry failing, not
    # the campaign not being marked as stopping). Surface the most
    # recent failed close attempt per open position so the operator
    # doesn't need Render's worker logs just to see why.
    stuck_reasons = []
    if campaign and campaign.status == "stopping":
        open_positions = Position.query.filter_by(campaign_id=campaign.id, status="open").all()
        for p in open_positions:
            last_fail = (
                OrderLog.query.filter_by(user_id=p.user_id, symbol=p.symbol, order_type="close", status="failed")
                .order_by(OrderLog.created_at.desc())
                .first()
            )
            user = User.query.get(p.user_id)
            stuck_reasons.append({
                "symbol": p.symbol,
                "email": user.email if user else f"user#{p.user_id}",
                "error": last_fail.error_message if last_fail else None,
                "when": last_fail.created_at if last_fail else None,
            })

    # Same idea, for a campaign that IS running but never actually got
    # a real position on some (or all) symbols -- e.g. every entry
    # attempt hit Binance's min-notional filter because the sliced
    # margin was too small, or a leverage/margin-type call failed.
    # "Entrada: -" in the table above already says something's missing;
    # this says WHY, straight from the real error Binance returned,
    # instead of the operator having to guess or dig through Render's
    # worker logs (confirmed live 2026-09-15: a daily_composto campaign
    # sat "active" for hours with every one of 53 symbols still showing
    # "-", no visible reason anywhere on this page).
    open_failures = []
    if campaign and campaign.status == "active":
        for s in campaign.symbols:
            if live.get(s.symbol, {}).get("entry"):
                continue  # has a real position, nothing to explain
            last_fail = (
                OrderLog.query.filter_by(symbol=s.symbol, order_type="open", status="failed")
                .order_by(OrderLog.created_at.desc())
                .first()
            )
            if not last_fail:
                continue
            fail_user = User.query.get(last_fail.user_id)
            open_failures.append({
                "symbol": s.symbol,
                "email": fail_user.email if fail_user else f"user#{last_fail.user_id}",
                "error": last_fail.error_message,
                "when": last_fail.created_at,
            })

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

    daily_settings = DailyCompostoSettings.query.get(1)
    if daily_settings is None:
        daily_settings = DailyCompostoSettings(id=1)
        db.session.add(daily_settings)
        db.session.commit()

    # Most recent daily_composto participation per user, filtered down
    # to whoever's currently halted (status="inactive") -- these are
    # the accounts section 10's drawdown circuit breaker has stopped,
    # waiting on the "Reativar" button below (manual intervention, on
    # purpose -- see app/engine.py's _check_daily_composto_schedule).
    daily_campaign_ids = [c.id for c in Campaign.query.filter_by(universe_scope="daily_composto").all()]
    halted_followers = []
    if daily_campaign_ids:
        latest_per_user = {}
        rows = (
            FollowerCampaignState.query
            .join(Campaign, FollowerCampaignState.campaign_id == Campaign.id)
            .filter(FollowerCampaignState.campaign_id.in_(daily_campaign_ids))
            .order_by(Campaign.started_at.asc())
            .all()
        )
        for r in rows:
            latest_per_user[r.user_id] = r  # last write per user wins, rows are chronological
        for state in latest_per_user.values():
            if state.status == "inactive":
                user = User.query.get(state.user_id)
                halted_followers.append({"user_id": state.user_id, "email": user.email if user else f"user#{state.user_id}"})

    return render_template(
        "operator_dashboard.html",
        campaign=campaign,
        last_campaigns=last_campaigns,
        is_owner=current_user.role == "owner",
        live=live,
        results=results,
        stuck_reasons=stuck_reasons,
        open_failures=open_failures,
        daily_settings=daily_settings,
        halted_followers=halted_followers,
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


def _create_campaign(direction, scope, params, stop_pct, testnet, resolved):
    """Shared tail end of starting a campaign: creates the Campaign +
    CampaignSymbol rows for an already-resolved symbol list and best-
    effort captures each one's reference entry price. Flashes and
    redirects either way -- never raises back to the caller."""
    try:
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
    testnet = current_app.config["BINANCE_TESTNET"]

    if scope == "single":
        params = {"symbol": (request.form.get("symbol") or "BTCUSDT").upper()}
        try:
            resolved = resolve_symbol_universe(scope, params, int(datetime.now(timezone.utc).timestamp() * 1000), testnet)
        except Exception as e:  # noqa: BLE001
            flash(f"Nao foi possivel iniciar a campanha: {e}", "error")
            return redirect(url_for("operator.dashboard"))
        return _create_campaign(direction, scope, params, stop_pct, testnet, resolved)

    if scope in ("relbtc", "relbtc_weak"):
        top_n = _parse_int(request.form.get("top_n"), 10)
        lookback_value = _parse_int(request.form.get("lookback_value"), 30)
        lookback_unit = "hours" if request.form.get("lookback_unit") == "hours" else "days"
        params = {"topN": top_n, "lookbackValue": lookback_value, "lookbackUnit": lookback_unit}

        if request.form.get("step") == "confirm":
            # Coming back from the review screen -- the operator's own
            # picks (checked candidates + anything typed manually) are
            # the final list, no re-resolving/re-ranking here.
            chosen = request.form.getlist("symbols")
            extra = [s.strip().upper() for s in (request.form.get("extra_symbols") or "").split(",") if s.strip()]
            all_symbols = list(dict.fromkeys([*chosen, *extra]))  # de-dupe, keep order
            if not all_symbols:
                flash("Selecione pelo menos um simbolo antes de confirmar.", "error")
                return redirect(url_for("operator.dashboard"))
            resolved = [{"symbol": s, "rank": i + 1} for i, s in enumerate(all_symbols)]
            return _create_campaign(direction, scope, params, stop_pct, testnet, resolved)

        # First submission -- rank the full candidate pool (every active
        # USDT pair on Binance Futures, see universe.rank_symbol_universe)
        # and show it for review instead of starting the campaign
        # immediately; the operator can uncheck any of the Top N or add
        # symbols by hand before confirming.
        try:
            ranked = rank_symbol_universe(scope, params, int(datetime.now(timezone.utc).timestamp() * 1000), testnet)
        except Exception as e:  # noqa: BLE001
            flash(f"Nao foi possivel buscar candidatos: {e}", "error")
            return redirect(url_for("operator.dashboard"))
        return render_template(
            "operator_review.html",
            candidates=ranked[:top_n],
            direction=direction, scope=scope, stop_pct=stop_pct,
            top_n=top_n, lookback_value=lookback_value, lookback_unit=lookback_unit,
        )

    flash("Escopo de universo desconhecido.", "error")
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


@operator_bp.route("/campanha/diaria/ligar", methods=["POST"])
@operator_required
def start_daily_composto():
    """Doesn't create today's campaign directly -- the engine's own
    scheduler (_check_daily_composto_schedule) does that at the next
    05:15 Brasilia window, same "HTTP request just flips a flag, the
    engine thread does the real work" split as stop_campaign/
    reset_campaign. Blocked while the single active/stopping campaign
    slot is already taken (manual campaign, or a still-closing daily
    one), same rule start_campaign already enforces."""
    if Campaign.query.filter(Campaign.status.in_(["active", "stopping"])).first():
        flash("Ja existe uma campanha ativa -- encerre antes de ligar a Diaria Composta.", "error")
        return redirect(url_for("operator.dashboard"))

    stop_pct = _parse_float(request.form.get("stop_pct"), 10.0)
    settings = DailyCompostoSettings.query.get(1)
    if settings is None:
        settings = DailyCompostoSettings(id=1)
        db.session.add(settings)
    settings.enabled = True
    settings.stop_pct = stop_pct
    db.session.commit()
    flash(f"Diaria Composta ligada -- abre automaticamente as 05:15 (Brasilia), stop de {stop_pct}%.", "success")
    return redirect(url_for("operator.dashboard"))


@operator_bp.route("/campanha/diaria/desligar", methods=["POST"])
@operator_required
def stop_daily_composto():
    """Only stops future days -- today's campaign (if any) keeps
    running to its normal 21:00 close. Use the generic "Encerrar
    operacoes" button to end today's early instead."""
    settings = DailyCompostoSettings.query.get(1)
    if settings:
        settings.enabled = False
        db.session.commit()
    flash("Diaria Composta desligada -- nao abre mais amanha. A campanha de hoje (se houver) segue ate as 21:00.", "success")
    return redirect(url_for("operator.dashboard"))


@operator_bp.route("/campanha/diaria/reativar/<int:user_id>", methods=["POST"])
@operator_required
def reactivate_daily_composto(user_id):
    """The "manual intervention" the strategy's drawdown circuit
    breaker (section 10) requires before a halted account resumes --
    resets that account's most recent daily_composto
    FollowerCampaignState back to active with a fresh peak, so the next
    day it participates normally again."""
    state = (
        FollowerCampaignState.query
        .join(Campaign, FollowerCampaignState.campaign_id == Campaign.id)
        .filter(Campaign.universe_scope == "daily_composto", FollowerCampaignState.user_id == user_id)
        .order_by(Campaign.started_at.desc())
        .first()
    )
    if not state:
        flash("Nenhum historico de Diaria Composta encontrado para essa conta.", "error")
        return redirect(url_for("operator.dashboard"))
    state.status = "active"
    state.peak_portfolio_usd = 0.0
    db.session.commit()
    flash("Conta reativada -- volta a participar a partir do proximo dia.", "success")
    return redirect(url_for("operator.dashboard"))


@operator_bp.route("/campanha/diaria/minha-config", methods=["POST"])
@operator_required
def save_my_daily_composto_settings():
    """Shortcut so whoever is both operator and a follower (the common
    case today) can set their own leverage/capital cap for Diária
    Composta right here, without leaving this page -- these are still
    genuinely per-follower fields on FollowerSettings (app/follower.py's
    save_settings is the general-purpose route, used by any OTHER
    follower who isn't an operator and only sees "Minha conta"), this
    is just a second, more convenient entry point onto the SAME row for
    whoever can see this panel."""
    settings = current_user.settings
    try:
        settings.leverage = max(1, int(request.form.get("leverage", settings.leverage)))
        settings.daily_composto_capital_usd = max(0.0, float(request.form.get("daily_composto_capital_usd", settings.daily_composto_capital_usd)))
    except ValueError:
        flash("Valores invalidos.", "error")
        return redirect(url_for("operator.dashboard"))
    db.session.commit()
    flash("Configuracoes da sua conta pra Diaria Composta salvas.", "success")
    return redirect(url_for("operator.dashboard"))
