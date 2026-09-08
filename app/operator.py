"""Operator dashboard: trigger Long/Short campaigns, Encerrar
operacoes, Reiniciar. Also where the Owner manages invite codes and
promotes an account to Operator (the socio)."""

import secrets
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Campaign, CampaignSymbol, InviteCode, User
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
    invites = InviteCode.query.order_by(InviteCode.created_at.desc()).limit(20).all() if current_user.role == "owner" else []
    return render_template(
        "operator_dashboard.html",
        campaign=campaign,
        last_campaigns=last_campaigns,
        invites=invites,
        is_owner=current_user.role == "owner",
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
            params["lookbackDays"] = _parse_int(request.form.get("lookback_days"), 30)
    elif scope == "ranks":
        params["ranks"] = [int(r.strip()) for r in (request.form.get("ranks") or "").split(",") if r.strip().isdigit()]

    testnet = current_app.config["BINANCE_TESTNET"]
    try:
        resolved = resolve_symbol_universe(scope, params, int(datetime.now(timezone.utc).timestamp() * 1000), testnet)

        campaign = Campaign(direction=direction, universe_scope=scope, universe_params=params, stop_pct=stop_pct, status="active", started_by_id=current_user.id)
        db.session.add(campaign)
        db.session.flush()
        for item in resolved:
            db.session.add(CampaignSymbol(campaign_id=campaign.id, symbol=item["symbol"], rank=item["rank"]))
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
        campaign.status = "stopped"
        campaign.ended_at = datetime.now(timezone.utc)
        db.session.commit()
    flash("Pronto para uma nova campanha.", "success")
    return redirect(url_for("operator.dashboard"))


@operator_bp.route("/convites/gerar", methods=["POST"])
@owner_required
def create_invite():
    code = secrets.token_urlsafe(9)
    db.session.add(InviteCode(code=code, created_by_id=current_user.id))
    db.session.commit()
    flash(f"Codigo de convite gerado: {code}", "success")
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
