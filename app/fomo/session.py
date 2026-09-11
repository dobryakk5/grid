"""In-memory FOMO session for the ``/fomo`` page.

A JWT pasted into the page lives only in this API process's memory: never
written to disk, and never handed back to the browser once accepted -- the
page gets a status, not the token. A process restart drops it by design;
that is the point, not a gap. Workers do not share this state and keep
reading ``settings.fomo_jwt`` from ``.env`` regardless of what is pasted
here.
"""

from __future__ import annotations

import base64
import json
import time

from app.core.config import settings

__all__ = ["clear_session", "current_jwt", "session_status", "set_session"]

_state: dict = {"jwt": None, "set_at": None}


def set_session(jwt: str) -> None:
    _state["jwt"] = jwt.strip()
    _state["set_at"] = time.time()


def clear_session() -> None:
    _state["jwt"] = None
    _state["set_at"] = None


def current_jwt() -> str:
    """The pasted-in session if one is set, else whatever ``.env`` carries."""
    return _state["jwt"] or settings.fomo_jwt


def _exp_claim(token: str) -> int | None:
    """Best-effort, unsigned read of the JWT's ``exp`` claim.

    Not a verification -- this is our own token being reflected back as a
    countdown, not a credential being checked. Any failure to parse just
    means "unknown expiry", not "invalid token".
    """
    try:
        _, payload_b64, _ = token.split(".")
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def session_status() -> dict:
    jwt = current_jwt()
    if not jwt:
        return {"authenticated": False, "source": "none", "expires_in": None}
    source = "pasted" if _state["jwt"] else "env"
    exp = _exp_claim(jwt)
    expires_in = int(exp - time.time()) if exp is not None else None
    return {"authenticated": True, "source": source, "expires_in": expires_in}
