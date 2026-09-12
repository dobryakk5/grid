import base64
import json
import time

import pytest

from app.core.config import settings
from app.fomo import session


def _jwt(exp: int | None) -> str:
    """A minimal unsigned JWT carrying just an ``exp`` claim (or none)."""
    claims = {} if exp is None else {"exp": exp}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.sig"


@pytest.fixture(autouse=True)
def clean_session(monkeypatch):
    monkeypatch.setattr(settings, "fomo_jwt", "")
    session.clear_session()
    yield
    session.clear_session()


def test_no_session_reports_unauthenticated():
    status = session.session_status()
    assert status == {"authenticated": False, "source": "none", "expires_in": None}
    assert session.current_jwt() == ""


def test_pasted_session_takes_precedence_over_env(monkeypatch):
    monkeypatch.setattr(settings, "fomo_jwt", "env-token")
    session.set_session("  pasted-token  ")
    assert session.current_jwt() == "pasted-token"  # stripped
    assert session.session_status()["source"] == "pasted"


def test_env_is_the_fallback_source(monkeypatch):
    monkeypatch.setattr(settings, "fomo_jwt", _jwt(int(time.time()) + 3600))
    status = session.session_status()
    assert status["authenticated"] is True
    assert status["source"] == "env"


def test_clear_reverts_to_env(monkeypatch):
    monkeypatch.setattr(settings, "fomo_jwt", "env-token")
    session.set_session("pasted")
    session.clear_session()
    assert session.current_jwt() == "env-token"
    assert session.session_status()["source"] == "env"


def test_expiry_countdown_is_derived_from_the_exp_claim():
    session.set_session(_jwt(int(time.time()) + 600))
    expires_in = session.session_status()["expires_in"]
    assert 590 <= expires_in <= 600


def test_unparseable_token_is_authenticated_but_expiry_unknown():
    session.set_session("not-a-jwt")
    status = session.session_status()
    assert status["authenticated"] is True
    assert status["expires_in"] is None
