"""Reads all sensitive/environment-specific settings from the process
environment -- nothing secret ever lives in this repo. See README.md
for the full list of variables a deployment must set.

Values default to None/blank here (not required at import time) so
pure-function modules can be imported for unit tests without a full
production environment configured -- create_app() is what actually
validates these are present, right before starting the real server.
"""

import os


class Config:
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL")
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Fernet key used to encrypt/decrypt every follower's Binance API
    # key+secret at rest (see app/crypto.py). Generate once with
    # `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
    # and store ONLY as an environment variable on the host -- never in
    # the database, never committed to git. Losing it means every
    # stored API credential becomes unrecoverable (by design: there is
    # no backdoor decryption path).
    ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")

    # testnet=True hits Binance's Demo Trading environment (fake
    # balance); testnet=False is real mainnet money. Deliberately an
    # explicit env var, not a hardcoded default, so a deployment can't
    # silently be pointed at the wrong one.
    BINANCE_TESTNET = os.environ.get("BINANCE_TESTNET", "true").lower() == "true"

    ENGINE_TICK_SECONDS = float(os.environ.get("ENGINE_TICK_SECONDS", "5"))

    # Auto-bot (Kairi) has its OWN testnet switch, deliberately separate
    # from BINANCE_TESTNET above -- the campaign engine and the Auto-bot
    # can go to mainnet on different schedules this way (a testnet key
    # can't authenticate against mainnet or vice-versa, so this also
    # means AutoBotCredential is validated against THIS flag, never
    # BINANCE_TESTNET). Defaults to testnet, same safety-first default
    # as the rest of the app.
    AUTOBOT_TESTNET = os.environ.get("AUTOBOT_TESTNET", "true").lower() == "true"

    # Kairi trades on 1h candles and exits on an EMA touch or a hard
    # stop -- it doesn't need the campaign engine's 5s cadence (that
    # exists for a tight campaign stop_pct). 300s matches
    # gg-shot-monitor's own poll_interval_seconds for the same strategy,
    # and is far lighter on Binance's rate limits across ~50 symbols.
    AUTOBOT_TICK_SECONDS = float(os.environ.get("AUTOBOT_TICK_SECONDS", "300"))

    # How many follower accounts the engine processes at once (real
    # Binance calls are I/O-bound, so threads give real concurrency
    # here) -- same idea as gg-shot-monitor's FETCH_WORKERS.
    ENGINE_MAX_WORKERS = int(os.environ.get("ENGINE_MAX_WORKERS", "20"))

    REQUIRED = ("SECRET_KEY", "SQLALCHEMY_DATABASE_URI", "ENCRYPTION_KEY")
