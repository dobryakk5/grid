"""Which wallets the tape follows, and which tokens it keeps.

Both bounds exist because neither had one. The roster grew to 698 wallets
because discovery only ever added, and ``chain_tokens`` grew to eleven
thousand rows because every address in a transfer log was kept whether it
traded or not -- and the bill for both arrived on the positions page, which
had to sweep that table for balances on every load.
"""

import pytest

from app.core.config import settings
from app.workers import chain_tape


class Rows:
    """Just enough of a SQLAlchemy result for ``_tracked_wallets``."""

    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self._values


class Session:
    def __init__(self, ranked):
        self.ranked, self.limit = ranked, None

    async def execute(self, statement):
        # The limit is the assertion worth making: it is what bounds the
        # scan, and a clause silently dropped would look exactly like this
        # test passing on a roster of one.
        self.limit = statement._limit
        return Rows(self.ranked[: self.limit])


async def test_the_tape_follows_only_the_top_of_the_ranking(monkeypatch):
    monkeypatch.setattr(settings, "chain_tape_wallet_limit", 3, raising=False)
    session = Session([f"0x{n:040x}" for n in range(1, 11)])

    wallets = await chain_tape._tracked_wallets(session)

    assert session.limit == 3
    assert len(wallets) == 3


async def test_our_own_wallet_is_followed_whatever_it_ranks(monkeypatch):
    """The regression this guards is silent, which is why it is here.

    Only the tape writes ``chain_swaps``, and the positions page reads its
    cost basis back out of that table. Our wallet is in the roster today
    only because discovery happened to notice it; bounding the roster by
    volume would drop it, and nothing would fail -- the page would simply
    stop knowing what we paid for anything.
    """
    monkeypatch.setattr(settings, "chain_tape_wallet_limit", 2, raising=False)
    session = Session([f"0x{n:040x}" for n in range(1, 11)])
    own = "0x071a4377479956ffbab52d189b21491c9b895a5b"

    wallets = await chain_tape._tracked_wallets(session, own)

    assert own in wallets
    # And it does not cost a slot: the ranked two are still both there.
    assert len(wallets) == 3


def test_the_own_wallet_comes_from_the_client_not_the_setting():
    """``RH_WALLET_ADDRESS`` is a dry-run stand-in and is normally blank --
    the address is derived from the signing key. Reading the setting would
    make the pin above a no-op on exactly the deploys that trade."""
    class Keyed:
        wallet_address = "0xabc"

    class Keyless:
        @property
        def wallet_address(self):
            raise chain_tape.ChainError("no wallet")

    assert chain_tape._own_wallet(Keyed()) == "0xabc"
    assert chain_tape._own_wallet(Keyless()) is None


async def test_only_tokens_that_turned_out_to_be_trades_are_kept(monkeypatch):
    """Read everything, keep what traded.

    A pass has to identify every address in a transfer log -- there is no
    classifying a swap without the decimals of both legs -- but keeping them
    all is what grew the cache by thousands of rows a day. On the history to
    date two thirds of it never appeared in a single classified swap.
    """
    from app.chain.tokens import TokenMeta

    spam, traded, quote = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20
    meta = {a: TokenMeta(a, "T", 18) for a in (spam, traded, quote)}

    asked = {}

    async def resolve(_client, _factory, addresses, *, chain_id, persist=True):
        asked["addresses"], asked["persist"] = set(addresses), persist
        return meta

    kept = []

    async def remember(_factory, metas, *, chain_id):
        kept.extend(m.address for m in metas)
        return len(metas)

    def classify(*_args, **_kwargs):
        return [type("Row", (), {"token_address": traded, "quote_address": quote})()]

    monkeypatch.setattr(chain_tape, "resolve_token_meta", resolve)
    monkeypatch.setattr(chain_tape, "remember_tokens", remember)
    monkeypatch.setattr(chain_tape, "classify_any", classify)
    monkeypatch.setattr(chain_tape, "group_by_tx", lambda transfers: {"0xtx": transfers})
    monkeypatch.setattr(chain_tape, "quote_assets", dict)

    transfers = [{"token_address": a} for a in (spam, traded, quote)]
    await chain_tape._rows_from(object(), transfers, set(), chain_id=4663)

    # Every address was identified...
    assert asked["addresses"] == {spam, traded, quote} and asked["persist"] is False
    # ...and only the two that were legs of a swap were written down.
    assert sorted(kept) == sorted([traded, quote])
    assert spam not in kept
