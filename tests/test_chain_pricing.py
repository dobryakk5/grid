from decimal import Decimal

from app.chain.tape import ChainSwapRow, price_swap


def _row(*, pricing_source="UNPRICED", value_usd=None, symbol="PONS", token_amount=Decimal("100")):
    return ChainSwapRow(
        tx_hash="0xtx1",
        wallet_address="0x" + "aa" * 20,
        chain_id=4663,
        block_number=100,
        block_time_ms=1_700_000_000_000,
        token_address="0x" + "11" * 20,
        symbol=symbol,
        side="BUY",
        token_amount=token_amount,
        quote_address="0x" + "22" * 20,
        quote_symbol="USDG",
        quote_amount=Decimal("55"),
        price=Decimal("0.55"),
        value_usd=value_usd,
        pricing_source=pricing_source,
    )


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    """Returns queued results in order -- one per `select(...)` the pricing
    fallback issues (observation, then candle)."""

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _statement):
        return FakeScalarResult(self._results.pop(0))


async def test_quote_leg_row_is_returned_unchanged_with_no_query():
    row = _row(pricing_source="QUOTE_LEG", value_usd=Decimal("55"))
    session = FakeSession([])  # would raise IndexError if a query were issued

    result = await price_swap(row, session)

    assert result is row


async def test_unpriced_row_uses_the_observation_price_first():
    row = _row()
    session = FakeSession([Decimal("0.6")])

    result = await price_swap(row, session)

    assert result.pricing_source == "MARKET_PRICE"
    assert result.value_usd == Decimal("100") * Decimal("0.6")


async def test_unpriced_row_falls_back_to_the_candle_close_when_no_observation():
    row = _row()
    session = FakeSession([None, Decimal("0.5")])

    result = await price_swap(row, session)

    assert result.pricing_source == "MARKET_PRICE"
    assert result.value_usd == Decimal("100") * Decimal("0.5")


async def test_unpriced_row_stays_unpriced_when_nothing_is_found_anywhere():
    row = _row()
    session = FakeSession([None, None])

    result = await price_swap(row, session)

    assert result.pricing_source == "UNPRICED"
    assert result.value_usd is None


async def test_unpriced_row_with_no_symbol_is_left_alone():
    row = _row(symbol=None)
    session = FakeSession([])  # no symbol -> no query at all

    result = await price_swap(row, session)

    assert result is row
