"""FOMO theses: the note a trader publishes next to a trade.

A thesis is a different kind of record from a swap and is fetched from a
different endpoint, so it gets its own module rather than growing
``app.fomo.activity``. Observed endpoints (internal API, not a stable
contract):

* ``/feed/token/thesis?tokenAddress=&networkId=&threshold=``
* ``/feed/token/sortedThesis`` with ``afterTime``/``beforeTime`` in ms

Both are scoped to a token, never to a user -- see ``BrowserCollector.theses``
for what follows from that.

Reference: https://github.com/cyberknight01/fomo-monitor/blob/main/接口说明.md
"""
from app.fomo.activity import number, timestamp_ms

# Whitelist of what may leave the browser. Everything else the feed carries
# (session-bound flags, telemetry, the viewer's own like state) stays there.
THESIS_FIELDS = (
    "id", "userId", "userHandle", "displayName", "createdAt", "tokenAddress",
    "networkId", "tradeId", "type", "thesis", "text", "message", "content",
    "body", "comment", "likes", "likeCount", "replies", "replyCount",
    "usdAmount",
)
USER_FIELDS = ("id", "userId", "userHandle", "displayName")

# A thesis is a short note; anything longer is a payload, not an opinion.
MAX_TEXT = 4000
# The keys that have been seen carrying the note itself. Order matters: the
# first non-empty one wins.
TEXT_KEYS = ("thesis", "text", "message", "content", "body", "comment")


def thesis_rows(payload):
    """``(items, has_next_page)``; ``None`` when the feed did not say."""
    if isinstance(payload, dict):
        payload = payload.get("responseObject", payload)
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        for key in ("items", "theses", "list"):
            if isinstance(payload.get(key), list):
                more = payload.get("hasNextPage")
                return payload[key], more if isinstance(more, bool) else None
    raise ValueError("FOMO theses: неизвестный формат ответа")


def public_thesis(row):
    """One feed item reduced to the fields the Grid API is allowed to see."""
    if not isinstance(row, dict):
        return {}
    item = {key: row.get(key) for key in THESIS_FIELDS if key in row}
    user = row.get("user")
    if isinstance(user, dict):
        item["user"] = {key: user.get(key) for key in USER_FIELDS if key in user}
    return item


def _text(row):
    for key in TEXT_KEYS:
        value = row.get(key)
        if isinstance(value, dict):
            value = next((value.get(k) for k in TEXT_KEYS if isinstance(value.get(k), str)), None)
        if isinstance(value, str) and value.strip():
            return value.strip()[:MAX_TEXT]
    return None


def _identity(row):
    user = row.get("user") if isinstance(row.get("user"), dict) else {}
    for source, key in ((row, "userId"), (user, "id"), (user, "userId")):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:64]
    return None


def _count(row, *keys):
    for key in keys:
        value = number(row.get(key))
        if value is not None:
            return int(value)
    return None


def normalize_thesis(row, *, chain_id, token_address):
    """One stored thesis, or ``None`` if the row is not one.

    The coin comes from the request, not from the row: we asked this endpoint
    about exactly one ``(chain_id, token_address)``, and a row that disagrees
    with what was asked is more likely a renamed field than a different coin.
    An empty note is not a thesis -- a feed item with no text is a trade the
    ``excludeThesis`` filter simply did not exclude.
    """
    if not isinstance(row, dict):
        return None
    thesis_id = row.get("id")
    user_id = _identity(row)
    text = _text(row)
    created = timestamp_ms(row.get("createdAt"))
    if (not isinstance(thesis_id, str) or not 1 <= len(thesis_id) <= 160
            or user_id is None or text is None or created is None):
        return None
    trade_id = row.get("tradeId")
    usd = number(row.get("usdAmount"))
    return {
        "thesis_id": thesis_id,
        "user_id": user_id,
        "chain_id": chain_id,
        "token_address": token_address.lower() if token_address.startswith("0x") else token_address,
        "trade_id": trade_id[:160] if isinstance(trade_id, str) and trade_id.strip() else None,
        "text": text,
        "likes": _count(row, "likes", "likeCount"),
        "replies": _count(row, "replies", "replyCount"),
        "usd_amount": usd,
        "created_at_ms": created,
    }
