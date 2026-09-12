"""Reads may be open; spending never is."""

import httpx
import pytest

from app.core import auth as auth_module
from app.core.config import settings
from app.core.security import AuthError, hash_password, issue_token, read_token, verify_password


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "auth_secret", "test-secret", raising=False)
    monkeypatch.setattr(settings, "auth_password_hash", hash_password("hunter2"), raising=False)
    monkeypatch.setattr(settings, "auth_service_token", "", raising=False)
    monkeypatch.setattr(settings, "auth_token_ttl_minutes", 10, raising=False)
    auth_module.__dict__.setdefault("_noop", None)
    from app.api import auth_routes
    auth_routes._attempts.clear()
    return settings


def client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_password_hash_is_salted_and_verifies():
    first, second = hash_password("hunter2"), hash_password("hunter2")
    assert first != second, "одинаковая соль сделала бы хэши сравнимыми между собой"
    assert verify_password("hunter2", first) and not verify_password("hunter3", first)
    assert not verify_password("hunter2", "not-a-hash")


def test_expired_token_is_refused():
    token = issue_token("s", ttl_seconds=60, now=1_000)
    assert read_token("s", token, now=1_050)["sub"] == "operator"
    with pytest.raises(AuthError):
        read_token("s", token, now=1_061)


def test_algorithm_is_not_taken_from_the_token():
    # "alg":"none" with an empty signature is the classic JWT forgery; the
    # verifier must not consult the header to decide how to check.
    import base64, json
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    claims = base64.urlsafe_b64encode(json.dumps({"sub": "operator", "exp": 9_999_999_999}).encode()).decode().rstrip("=")
    with pytest.raises(AuthError):
        read_token("s", f"{header}.{claims}.")


async def test_unconfigured_leaves_reads_open_but_blocks_trading(monkeypatch):
    monkeypatch.setattr(settings, "auth_secret", "", raising=False)
    monkeypatch.setattr(settings, "auth_password_hash", "", raising=False)
    async with client() as http:
        assert (await http.get("/api/auth/status")).json() == {
            "configured": False, "authenticated": False, "trading_enabled": False}
        assert (await http.post("/api/auth/login", json={"password": "x"})).status_code == 503
        # The money endpoint refuses rather than inheriting the open path.
        response = await http.post("/api/fomo/limit-order", json={
            "symbol": "PONSUSDG", "side": "Sell", "amount": "1", "limit_price": "1"})
        assert response.status_code == 503, response.text


async def test_configured_requires_a_token_on_every_api_call(configured):
    async with client() as http:
        assert (await http.get("/api/fomo/activity")).status_code == 401
        assert (await http.post("/api/auth/login", json={"password": "wrong"})).status_code == 401
        login = await http.post("/api/auth/login", json={"password": "hunter2"})
        assert login.status_code == 200, login.text
        token = login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        # /dex/pairs touches no database -- this asserts the gate, not the handler.
        assert (await http.get("/api/dex/pairs", headers=headers)).status_code != 401
        assert (await http.get("/api/auth/status", headers=headers)).json()["authenticated"] is True
        # A token this server did not sign is not a token.
        forged = issue_token("someone-elses-secret", ttl_seconds=60)
        assert (await http.get("/api/fomo/activity",
                               headers={"Authorization": f"Bearer {forged}"})).status_code == 401


async def test_service_token_lets_the_collector_in_without_a_password(configured, monkeypatch):
    monkeypatch.setattr(settings, "auth_service_token", "collector-secret-value", raising=False)
    async with client() as http:
        response = await http.get("/api/dex/pairs",
                                  headers={"Authorization": "Bearer collector-secret-value"})
        assert response.status_code != 401
        # A prefix of the secret is not the secret.
        assert (await http.get("/api/dex/pairs",
                               headers={"Authorization": "Bearer collector-secret"})).status_code == 401


async def test_login_throttles_repeated_guesses(configured):
    async with client() as http:
        codes = [(await http.post("/api/auth/login", json={"password": "no"})).status_code
                 for _ in range(10)]
        assert 429 in codes, "перебор пароля не должен идти со скоростью сети"
        # Throttling must not become a lockout of the real password forever.
        from app.api import auth_routes
        auth_routes._attempts.clear()
        assert (await http.post("/api/auth/login", json={"password": "hunter2"})).status_code == 200


async def test_token_rides_its_own_header_so_nginx_keeps_its_basic_session(configured):
    # A browser attaches cached Basic credentials only to a request with no
    # Authorization header of its own. The pages must leave that slot to nginx,
    # or every API call fails auth_basic and re-opens the server's password box.
    async with client() as http:
        login = await http.post("/api/auth/login", json={"password": "hunter2"})
        token = login.json()["token"]
        page = await http.get("/api/dex/pairs", headers={
            "X-Grid-Token": token,
            # What nginx forwards upstream once the browser is basic-authorised.
            "Authorization": "Basic b3BlcmF0b3I6c2VydmVy",
        })
        assert page.status_code != 401, page.text
        assert (await http.get("/api/auth/status", headers={
            "X-Grid-Token": token})).json()["authenticated"] is True
        # An empty header falls through rather than shadowing Authorization.
        assert (await http.get("/api/dex/pairs", headers={
            "X-Grid-Token": "   ",
            "Authorization": f"Bearer {token}"})).status_code != 401


async def test_our_401_carries_no_auth_challenge(configured):
    # The same origin sits behind nginx basic auth. A challenge header here
    # makes the browser drop its cached Basic credentials and re-prompt for
    # the *server* password right after the operator logged in.
    async with client() as http:
        response = await http.get("/api/dex/pairs")
    assert response.status_code == 401
    assert "www-authenticate" not in response.headers
