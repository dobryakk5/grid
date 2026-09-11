"""Recovery after a crash between broadcast and receipt.

The property under test is the one the whole design exists for: a worker that
comes back up must finish the transaction it already sent, and must never treat
"I do not know what happened" as permission to trade again.
"""

from decimal import Decimal

import pytest

from app.core.config import settings
from app.db.models import DexIntent
from app.dex.dexscreener import MarketSnapshot
from app.dex.intents import IntentStatus
from app.dex.receipts import TRANSFER_TOPIC
from app.dex.recovery import rebroadcast, settle
from app.dex.repository import DexIntentRepository
from app.dex.tokens import resolve_pair

WALLET = "0x071A4377479956fFBaB52d189b21491C9B895A5B"
POOL = "0x" + "aa" * 20
PONS = "0x39dbed3a2bd333467115de45665cc57f813c4571"
TX_HASH = "0x" + "ab" * 32
RAW_TX = "0x02f8b1" + "cd" * 40


class FakeSession:
    """Enough of a session for rows that are already in memory."""

    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def flush(self):
        pass


class FakeChain:
    def __init__(self, receipt=None):
        self._receipt = receipt
        self.broadcasts = []

    async def receipt(self, tx_hash):
        return self._receipt

    async def broadcast(self, payload):
        self.broadcasts.append(payload)
        return payload.tx_hash


class FakeMarket:
    async def snapshot(self, pair):
        return MarketSnapshot(
            symbol=pair.symbol,
            observed_at_ms=0,
            price_quote=Decimal("0.00025"),
            price_usd=Decimal("0.61"),
            pair_address="0xpair",
            pair_liquidity_usd=Decimal("6000000"),
            token_liquidity_usd=Decimal("23000000"),
            token_volume_h24=Decimal("90000000"),
            pools_considered=30,
        )


def receipt(*, status=1, received="1584900000000000000"):
    topic = lambda address: "0x" + address[2:].rjust(64, "0")  # noqa: E731
    return {
        "status": status,
        "logs": [
            {
                "address": PONS,
                "topics": [TRANSFER_TOPIC, topic(POOL), topic(WALLET)],
                "data": hex(int(received)),
            }
        ],
        "gasUsed": 180_000,
        "effectiveGasPrice": 1_500_000_000,
        "transactionHash": TX_HASH,
        "blockNumber": 60_291_825,
        "blockHash": "0x" + "cd" * 32,
    }


def intent(status=IntentStatus.SUBMITTING) -> DexIntent:
    return DexIntent(
        id=1,
        order_link_id="g1-abc",
        symbol="PONSETH",
        side="Buy",
        status=status,
        limit_price=Decimal("0.00026"),
        amount_in=Decimal("0.0004"),
        amount_in_coin="ETH",
        wallet_address=WALLET,
        nonce=137,
        tx_hash=TX_HASH,
        raw_tx=RAW_TX,
        retry_count=0,
    )


@pytest.fixture(autouse=True)
def settings_defaults(monkeypatch):
    monkeypatch.setattr(settings, "dex_tokens", "")
    monkeypatch.setattr(settings, "dex_chain_slug", "robinhood")
    monkeypatch.setattr(settings, "dex_min_order_quote", Decimal("10"))


async def test_a_restart_finishes_the_transaction_it_already_sent():
    # The worker died after broadcasting. Coming back, it finds the row, finds
    # the receipt, and books the fill -- it does not buy again.
    session, row = FakeSession(), intent()
    chain = FakeChain(receipt=receipt())

    outcome = await settle(
        session, DexIntentRepository(session), row,
        chain=chain, market=FakeMarket(),
    )

    assert outcome.status == IntentStatus.FILLED
    assert row.status == IntentStatus.FILLED
    assert chain.broadcasts == []
    assert row.filled_amount_out == Decimal("1.5849")
    assert row.filled_amount_in == Decimal("0.0004")
    # Realised price from the receipt, not from the quote.
    assert row.fill_price == Decimal("0.0004") / Decimal("1.5849")


async def test_an_unknown_outcome_is_never_read_as_permission_to_trade_again():
    session, row = FakeSession(), intent()

    outcome = await settle(
        session, DexIntentRepository(session), row,
        chain=FakeChain(receipt=None), market=FakeMarket(),
    )

    assert outcome is None
    # Still signed and still waiting: nothing was decided.
    assert row.status == IntentStatus.SUBMITTING
    assert row.tx_hash == TX_HASH


async def test_a_reverted_transaction_fails_the_intent_rather_than_filling_it():
    session, row = FakeSession(), intent(IntentStatus.PENDING)

    outcome = await settle(
        session, DexIntentRepository(session), row,
        chain=FakeChain(receipt=receipt(status=0)), market=FakeMarket(),
    )

    assert outcome.status == IntentStatus.FAILED
    assert row.status == IntentStatus.FAILED
    assert row.filled_amount_out is None


async def test_gas_is_recorded_in_both_currencies():
    session, row = FakeSession(), intent()

    await settle(
        session, DexIntentRepository(session), row,
        chain=FakeChain(receipt=receipt()), market=FakeMarket(),
    )

    # 180000 * 1.5 gwei, and the pair is quoted in ETH so the rate is 1.
    assert row.gas_native == Decimal("0.00027")
    assert row.gas_quote == Decimal("0.00027")
    assert row.native_quote_rate == Decimal("1")


async def test_the_confirmed_payload_is_dropped_once_it_can_no_longer_be_needed():
    session, row = FakeSession(), intent()

    await settle(
        session, DexIntentRepository(session), row,
        chain=FakeChain(receipt=receipt()), market=FakeMarket(),
    )

    assert row.raw_tx is None
    assert row.confirmed_block == 60_291_825


async def test_a_rebroadcast_sends_the_very_same_bytes():
    # Not a new transaction built from a fresh quote: the same payload, so the
    # nonce can only ever be spent once.
    chain = FakeChain()
    row = intent()

    returned = await rebroadcast(chain, row)

    payload = chain.broadcasts[0]
    assert returned == TX_HASH
    assert payload.tx_hash == TX_HASH
    assert payload.nonce == 137
    assert "0x" + payload.raw.hex() == RAW_TX


async def test_a_row_with_no_payload_cannot_be_rebroadcast():
    from app.dex.chain import ChainError

    row = intent()
    row.raw_tx = None

    with pytest.raises(ChainError):
        await rebroadcast(FakeChain(), row)
