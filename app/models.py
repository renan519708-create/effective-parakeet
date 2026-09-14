"""SQLAlchemy models -- mirrors the data model in
docs/superpowers/specs/2026-09-07-copy-trading-platform-design.md 1:1.
No migrations for v1 (YAGNI): db.create_all() runs once at startup
against a brand-new database (see app/__init__.py)."""

from datetime import datetime, timezone

from flask_login import UserMixin

from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    # "owner" | "operator" | "follower"
    role = db.Column(db.String(20), nullable=False, default="follower")
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)

    api_credential = db.relationship("ApiCredential", back_populates="user", uselist=False, cascade="all, delete-orphan")
    settings = db.relationship("FollowerSettings", back_populates="user", uselist=False, cascade="all, delete-orphan")

    @property
    def can_operate(self):
        """Owner and operator both see the campaign controls -- owner
        is just an operator who can additionally promote others."""
        return self.role in ("owner", "operator")


class InviteCode(db.Model):
    __tablename__ = "invite_codes"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    used_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)

    @property
    def is_used(self):
        return self.used_by_id is not None


class ApiCredential(db.Model):
    __tablename__ = "api_credentials"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    encrypted_api_key = db.Column(db.Text, nullable=False)
    encrypted_api_secret = db.Column(db.Text, nullable=False)
    is_valid = db.Column(db.Boolean, default=True, nullable=False)
    last_validated_at = db.Column(db.DateTime(timezone=True), default=_utcnow)

    user = db.relationship("User", back_populates="api_credential")


class FollowerSettings(db.Model):
    __tablename__ = "follower_settings"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    risk_pct = db.Column(db.Float, nullable=False, default=10.0)
    leverage = db.Column(db.Integer, nullable=False, default=1)
    max_drawdown_enabled = db.Column(db.Boolean, nullable=False, default=True)
    max_drawdown_pct = db.Column(db.Float, nullable=False, default=20.0)
    following_enabled = db.Column(db.Boolean, nullable=False, default=False)

    user = db.relationship("User", back_populates="settings")


class Campaign(db.Model):
    __tablename__ = "campaigns"

    id = db.Column(db.Integer, primary_key=True)
    direction = db.Column(db.String(5), nullable=False)  # "long" | "short"
    universe_scope = db.Column(db.String(20), nullable=False)
    universe_params = db.Column(db.JSON, nullable=False, default=dict)
    stop_pct = db.Column(db.Float, nullable=False)
    # "active" | "stopped"
    status = db.Column(db.String(20), nullable=False, default="active")
    started_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    started_at = db.Column(db.DateTime(timezone=True), default=_utcnow)
    ended_at = db.Column(db.DateTime(timezone=True), nullable=True)

    symbols = db.relationship("CampaignSymbol", backref="campaign", cascade="all, delete-orphan")


class CampaignSymbol(db.Model):
    __tablename__ = "campaign_symbols"

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"), nullable=False)
    symbol = db.Column(db.String(20), nullable=False)
    rank = db.Column(db.Integer, nullable=True)
    # Reference price at the moment the campaign started (not any one
    # follower's real fill -- followers open at slightly different
    # prices/times). Lets the operator dashboard show a live %-move per
    # symbol without depending on any follower having opened a position
    # yet. Nullable: a failed price fetch at campaign-start time must
    # never block campaign creation (see operator.start_campaign).
    entry_price = db.Column(db.Float, nullable=True)
    # Same idea, captured once when the campaign finishes (engine's
    # stopping->stopped transition, or the operator's "Reiniciar" escape
    # hatch) -- lets Ultimas campanhas show a final %-result per symbol
    # forever after, with no live price fetch needed to display history.
    exit_price = db.Column(db.Float, nullable=True)


class FollowerAllocation(db.Model):
    __tablename__ = "follower_allocations"
    __table_args__ = (db.UniqueConstraint("campaign_id", "user_id", "symbol", name="uq_allocation_campaign_user_symbol"),)

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    symbol = db.Column(db.String(20), nullable=False)
    allocated_usd = db.Column(db.Float, nullable=False, default=0.0)
    # "active" | "inactive"
    state = db.Column(db.String(20), nullable=False, default="active")
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class Position(db.Model):
    __tablename__ = "positions"

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    symbol = db.Column(db.String(20), nullable=False)
    side = db.Column(db.String(5), nullable=False)  # "long" | "short"
    entry_price = db.Column(db.Float, nullable=False)
    opened_at = db.Column(db.DateTime(timezone=True), default=_utcnow)
    # "open" | "closed"
    status = db.Column(db.String(20), nullable=False, default="open")
    close_price = db.Column(db.Float, nullable=True)
    closed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # "SL" | "Manual" | "Campanha-encerrada" | "Drawdown"
    close_reason = db.Column(db.String(30), nullable=True)
    realized_pnl_usd = db.Column(db.Float, nullable=True)


class FollowerCampaignState(db.Model):
    """One row per (campaign, follower) -- tracks state that doesn't
    belong to any single symbol allocation: the portfolio peak used for
    that account's own max-drawdown check, and whether this account is
    still active in the campaign at all (distinct from any single
    symbol's allocation state -- a drawdown-halt or a manual
    follow-off deactivates the whole campaign for this account, not
    just one symbol)."""
    __tablename__ = "follower_campaign_state"
    __table_args__ = (db.UniqueConstraint("campaign_id", "user_id", name="uq_state_campaign_user"),)

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    peak_portfolio_usd = db.Column(db.Float, nullable=False, default=0.0)
    # "active" | "inactive" (drawdown-halted or manually stopped)
    status = db.Column(db.String(20), nullable=False, default="active")
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class AutoBotCredential(db.Model):
    """Binance key dedicated to the Auto-bot -- deliberately separate
    from ApiCredential (the campaign/follower key). A testnet key
    can't authenticate against mainnet and vice-versa, so sharing one
    credential would force the campaign feature and the Auto-bot to
    always sit in the same environment; keeping them apart lets the
    Auto-bot go to mainnet on its own schedule (see AUTOBOT_TESTNET in
    app/config.py)."""
    __tablename__ = "autobot_credentials"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    encrypted_api_key = db.Column(db.Text, nullable=False)
    encrypted_api_secret = db.Column(db.Text, nullable=False)
    is_valid = db.Column(db.Boolean, default=True, nullable=False)
    last_validated_at = db.Column(db.DateTime(timezone=True), default=_utcnow)

    user = db.relationship("User")


class AutoBotSettings(db.Model):
    """One row per user. capital_usd is the TOTAL the user set aside
    for the Auto-bot (not per-trade) -- each entry sizes at
    capital_usd / NUM_SLOTS (see app/autobot_engine.py), non-compounding
    (recomputed from this fixed setting, never from the running
    balance)."""
    __tablename__ = "autobot_settings"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    capital_usd = db.Column(db.Float, nullable=False, default=100.0)
    leverage = db.Column(db.Integer, nullable=False, default=6)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    user = db.relationship("User")


class AutoBotSymbolState(db.Model):
    """GLOBAL, one row per symbol -- the Kairi zone crossing is a
    market fact, identical for every user, so it's tracked once here
    rather than duplicated per account. Per-account decisions (open a
    trade or not) are made separately in autobot_engine using this
    shared signal."""
    __tablename__ = "autobot_symbol_state"

    symbol = db.Column(db.String(20), primary_key=True)
    # "oversold" | "overbought" | "neutral"
    zone = db.Column(db.String(12), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class AutoBotPosition(db.Model):
    """One real Auto-bot trade for one user. status follows
    kairi_outcome.py's own vocabulary directly ("open" then exactly one
    of "green"/"red"/"stopped"/"liquidated") -- no generic "closed"
    state, so the outcome is visible without a second field."""
    __tablename__ = "autobot_positions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    symbol = db.Column(db.String(20), nullable=False)
    direction = db.Column(db.String(5), nullable=False)  # "LONG" | "SHORT"
    entry_price = db.Column(db.Float, nullable=False)
    # epoch ms of the entry candle's close_time -- doubles as the
    # dedupe key so the same fresh crossing never opens twice for one
    # user (see autobot_engine.run_tick).
    entry_time = db.Column(db.BigInteger, nullable=False)
    opened_at = db.Column(db.DateTime(timezone=True), default=_utcnow)
    # "open" | "green" | "red" | "stopped" | "liquidated"
    status = db.Column(db.String(12), nullable=False, default="open")
    close_price = db.Column(db.Float, nullable=True)
    close_time = db.Column(db.BigInteger, nullable=True)
    closed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    result_pct = db.Column(db.Float, nullable=True)
    max_drawdown_pct = db.Column(db.Float, nullable=False, default=0.0)
    allocated_usd = db.Column(db.Float, nullable=False)
    realized_pnl_usd = db.Column(db.Float, nullable=True)

    user = db.relationship("User")


class OrderLog(db.Model):
    __tablename__ = "order_log"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    symbol = db.Column(db.String(20), nullable=False)
    side = db.Column(db.String(5), nullable=False)  # "BUY" | "SELL"
    qty = db.Column(db.Float, nullable=True)
    # "open" | "add" | "close"
    order_type = db.Column(db.String(10), nullable=False)
    binance_order_id = db.Column(db.String(40), nullable=True)
    # "filled" | "failed"
    status = db.Column(db.String(10), nullable=False)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)
