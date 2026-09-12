"""Where the API decides whether a caller is the operator.

Two dependencies, because two different failure modes are correct:

``require_operator`` guards reading. With no credentials configured it lets
the request through, so a developer on a laptop is not locked out of their own
database and the test suite needs no fixtures.

``require_trading`` guards spending. It refuses when nothing is configured,
rather than inheriting the permissive path -- an unconfigured deploy must not
be an open door to the wallet. Fail-open on reads and fail-closed on money is
the whole point of having two.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from app.core.config import settings
from app.core.security import AuthError, constant_time_equals, read_token

__all__ = ["auth_configured", "require_operator", "require_trading", "bearer_of",
           "OPERATOR_TOKEN_HEADER"]

# Deliberately no ``WWW-Authenticate`` header on our 401s.
#
# The pages fetch this API with JavaScript and handle 401 themselves, so a
# challenge header buys nothing -- and it costs something real. The same origin
# also sits behind nginx basic auth: when the browser holds cached Basic
# credentials for it and then receives a fresh 401 carrying an auth challenge,
# it treats those credentials as rejected, drops them, and re-opens nginx's
# password dialog. Logging in to the app would then ask for the *server*
# password again. Answering 401 with no challenge leaves that session alone.
#
# ``OPERATOR_TOKEN_HEADER`` is the other half of coexisting with that basic
# auth; see ``bearer_of``.

#: Where the browser pages put the session token. Not ``Authorization``.
OPERATOR_TOKEN_HEADER = "X-Grid-Token"


def auth_configured() -> bool:
    return bool(settings.auth_secret.strip() and settings.auth_password_hash.strip())


def bearer_of(request: Request) -> str:
    """The operator's token: our own header first, ``Authorization`` after.

    A browser only attaches its cached Basic credentials to a request that
    carries no ``Authorization`` header of its own. While the pages put the
    session token there, every API call reached nginx as a Bearer, failed its
    ``auth_basic``, and came back 401 with a Basic challenge -- so the browser
    re-opened the *server* password dialog, once per call, forever. Carrying
    the token in a header of our own leaves that slot free for nginx, and the
    two layers stop fighting over it.

    ``Authorization: Bearer`` still works. Machine callers -- the FOMO
    collector, curl, the service token -- have no basic-auth layer to collide
    with, and breaking them to fix a browser problem would be a poor trade.
    """
    own = (request.headers.get(OPERATOR_TOKEN_HEADER) or "").strip()
    if own:
        return own
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _identify(token: str) -> str:
    service = settings.auth_service_token.strip()
    if service and constant_time_equals(token, service):
        return "service"
    try:
        return str(read_token(settings.auth_secret, token).get("sub") or "operator")
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from None


async def require_operator(request: Request) -> str:
    if not auth_configured():
        return "anonymous"
    token = bearer_of(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется вход")
    return _identify(token)


async def require_trading(caller: str = Depends(require_operator)) -> str:
    if not auth_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Торговые операции выключены: задайте AUTH_SECRET и AUTH_PASSWORD_HASH",
        )
    return caller
