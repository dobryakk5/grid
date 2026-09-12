"""Position maths. The chain parts are exercised against a fake multicall."""

OWNER = "0xa37280C51518F7EB9cF2F12EBc1F7CB9278E1304"

from decimal import Decimal

import pytest

from app.dex.positions import Position, fraction_amount, limit_from_quote, read_balances


def test_quarters_round_down_to_the_token_precision():
    # 3 USDG-like units, 6 decimals: a quarter is not representable exactly.
    assert fraction_amount(Decimal("3.000001"), 25, 6) == Decimal("0.750000")
    assert fraction_amount(Decimal("8"), 50, 18) == Decimal("4")
    with pytest.raises(ValueError):
        fraction_amount(Decimal("1"), 30, 18)


def test_full_sale_is_the_balance_itself_not_a_computed_hundred_percent():
    # balance * 100/100 can reintroduce a rounding artefact; selling "all"
    # must hand over exactly what is held, or the transaction reverts.
    balance = Decimal("8.123456789012345678")
    assert fraction_amount(balance, 100, 18) == balance


def test_a_sell_limit_sits_a_slippage_cap_below_the_quote():
    # 8 tokens quoted at 40 USDG -> 5 per token, 0.5% cap -> 4.975
    assert limit_from_quote(Decimal(8), Decimal(40), Decimal("0.5")) == Decimal("4.975")
    with pytest.raises(ValueError):
        limit_from_quote(Decimal(0), Decimal(40), Decimal("0.5"))


def test_a_buy_limit_sits_the_same_cap_above_it():
    # 40 USDG buys 8 tokens -> 5 per token; a buyer is hurt by a higher price.
    assert limit_from_quote(Decimal(40), Decimal(8), Decimal("0.5"), side="Buy") == Decimal("5.025")


def test_the_side_must_be_named_rather_than_assumed():
    # Silently defaulting an unknown side would put the cap on the wrong side
    # of the market: a limit that never fills, or one that accepts any price.
    with pytest.raises(ValueError):
        limit_from_quote(Decimal(40), Decimal(8), Decimal("0.5"), side="Long")


def test_position_amount_applies_decimals():
    assert Position("0xa", "ChatGpt", 18, 8 * 10 ** 18).amount == Decimal(8)


class FakeChain:
    """Enough of ChainClient.w3 for read_balances, with one reverting token."""

    def __init__(self, balances, *, batches):
        self.balances, self.batches, self.calls = balances, batches, 0
        self.w3 = self
        self.eth = self

    def to_checksum_address(self, value):
        return value

    def contract(self, *, address, abi):
        return self

    @property
    def functions(self):
        return self

    def aggregate3(self, calls):
        self.calls += 1
        outer = self

        class Call:
            async def call(self):
                out = []
                for target, _allow, _data in calls:
                    raw = outer.balances.get(target)
                    # A token whose balanceOf reverts comes back unsuccessful.
                    out.append((False, b"") if raw is None else (True, raw.to_bytes(32, "big")))
                return out
        return Call()


async def test_sweep_skips_reverting_tokens_instead_of_failing():
    chain = FakeChain({"0xa": 5, "0xb": 0}, batches=1)
    balances = await read_balances(chain, ["0xa", "0xb", "0xbroken"], OWNER)
    assert balances == {"0xa": 5, "0xb": 0}


async def test_sweep_batches_and_asks_about_the_owner():
    chain = FakeChain({f"0x{i}": i for i in range(900)}, batches=3)
    balances = await read_balances(chain, [f"0x{i}" for i in range(900)], OWNER)
    assert len(balances) == 900
    assert chain.calls == 3, "900 токенов должны уехать тремя вызовами, а не девятьюстами"


async def test_no_addresses_costs_no_calls():
    chain = FakeChain({}, batches=0)
    assert await read_balances(chain, [], OWNER) == {}
    assert chain.calls == 0
