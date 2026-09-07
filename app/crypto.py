"""Encrypts/decrypts Binance API credentials at rest using Fernet
(symmetric, authenticated encryption -- AES128-CBC + HMAC under the
hood). The key comes from the environment (Config.ENCRYPTION_KEY),
never from the database or the codebase.

A decrypted secret must NEVER be sent back to a browser once saved --
every route that renders or returns credential state shows only a
masked placeholder (see follower.py), not the real value. The only
code that ever calls decrypt_secret() is engine.py, right before
handing the value to BinanceBroker for one signed request.
"""

from cryptography.fernet import Fernet, InvalidToken


def _fernet(encryption_key):
    key = encryption_key.encode("utf-8") if isinstance(encryption_key, str) else encryption_key
    return Fernet(key)


def encrypt_secret(raw_value, encryption_key):
    """raw_value: plaintext string (an API key or API secret).
    Returns the encrypted token as a string, safe to store in the DB."""
    token = _fernet(encryption_key).encrypt(raw_value.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_secret(token, encryption_key):
    """Returns the original plaintext string, or raises ValueError if
    the token is invalid/tampered/encrypted under a different key --
    callers should treat that as "credential unusable", the same as a
    revoked key, not let the exception escape to a user-facing 500."""
    try:
        return _fernet(encryption_key).decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("token de credencial invalido ou corrompido") from e
