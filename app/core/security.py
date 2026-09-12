"""Operator authentication: a password login that mints a signed session token.

HS256 and scrypt come from the standard library on purpose. The documented
deploy is ``git pull && systemctl restart`` with no ``pip install`` step, so a
new dependency here would not be a new dependency -- it would be an outage.

The threat this addresses is narrow and worth stating plainly: until now the
only thing between the open internet and ``/api/fomo/limit-order`` was one
shared basic-auth password in nginx, and that endpoint spends real money. A
token does not fix a stolen password; it does make the credential short-lived,
revocable by rotating one secret, and separate from the web server's config.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

__all__ = [
    "AuthError", "hash_password", "verify_password",
    "issue_token", "read_token", "constant_time_equals",
]

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
_PREFIX = "scrypt"


class AuthError(RuntimeError):
    """Credentials were absent, malformed, expired or wrong.

    Deliberately one error for every case: telling a caller *which* of those
    it was tells an attacker which half of a guess landed.
    """


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """``scrypt$<salt>$<hash>`` -- what goes in ``AUTH_PASSWORD_HASH``."""
    if not password:
        raise AuthError("Пустой пароль")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"{_PREFIX}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        prefix, salt, digest = stored.split("$")
    except (ValueError, AttributeError):
        return False
    if prefix != _PREFIX:
        return False
    try:
        computed = hashlib.scrypt(password.encode(), salt=_unb64(salt), **_SCRYPT)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(computed, _unb64(digest))


def _sign(secret: str, message: str) -> str:
    return _b64(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest())


def issue_token(secret: str, *, subject: str = "operator", ttl_seconds: int, now: float | None = None) -> str:
    if not secret:
        raise AuthError("AUTH_SECRET не задан")
    issued = int(now if now is not None else time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64(json.dumps(
        {"sub": subject, "iat": issued, "exp": issued + ttl_seconds},
        separators=(",", ":"),
    ).encode())
    body = f"{header}.{claims}"
    return f"{body}.{_sign(secret, body)}"


def read_token(secret: str, token: str, *, now: float | None = None) -> dict:
    """Claims of a token this server signed, or ``AuthError``.

    The algorithm is not read back out of the header: accepting whatever the
    header names is how "alg":"none" gets in. HS256 is the only thing issued
    here and the only thing verified here.
    """
    if not secret:
        raise AuthError("AUTH_SECRET не задан")
    parts = (token or "").split(".")
    if len(parts) != 3:
        raise AuthError("Недействительный токен")
    header, claims, signature = parts
    if not hmac.compare_digest(signature, _sign(secret, f"{header}.{claims}")):
        raise AuthError("Недействительный токен")
    try:
        payload = json.loads(_unb64(claims))
    except (ValueError, TypeError):
        raise AuthError("Недействительный токен") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("exp"), int):
        raise AuthError("Недействительный токен")
    if payload["exp"] <= int(now if now is not None else time.time()):
        raise AuthError("Срок действия токена истёк")
    return payload
