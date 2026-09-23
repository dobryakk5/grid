"""Position maths. The chain parts are exercised against a fake multicall."""

OWNER = "0xa37280C51518F7EB9cF2F12EBc1F7CB9278E1304"

from decimal import Decimal

import pytest

from app.dex import positions
from app.dex.positions import (
    Position,
    PositionReadError,
    fraction_amount,
    limit_from_quote,
    read_balances,
)


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


async def test_sweep_limits_rpc_concurrency(monkeypatch):
    active = peak = 0

    class SlowChain(FakeChain):
        def aggregate3(self, calls):
            nonlocal active, peak
            self.calls += 1
            outer = self

            class Call:
                async def call(self):
                    nonlocal active, peak
                    active += 1
                    peak = max(peak, active)
                    await __import__("asyncio").sleep(0.01)
                    active -= 1
                    return [(True, outer.balances[target].to_bytes(32, "big"))
                            for target, _allow, _data in calls]
            return Call()

    monkeypatch.setattr(positions, "_BATCH", 1)
    monkeypatch.setattr(positions, "_BATCH_CONCURRENCY", 2)
    chain = SlowChain({f"0x{i}": i for i in range(8)}, batches=8)
    assert len(await read_balances(chain, list(chain.balances), OWNER)) == 8
    assert peak == 2


async def test_sweep_turns_an_rpc_stall_into_a_bounded_error(monkeypatch):
    class StalledChain(FakeChain):
        def aggregate3(self, calls):
            class Call:
                async def call(self):
                    await __import__("asyncio").Event().wait()
            return Call()

    monkeypatch.setattr(positions, "_BALANCE_SCAN_TIMEOUT", 0.01)
    with pytest.raises(PositionReadError, match="exceeded"):
        await read_balances(StalledChain({"0xa": 1}, batches=1), ["0xa"], OWNER)


async def test_sweep_retries_a_transient_rpc_failure(monkeypatch):
    attempts = 0

    class FlakyChain(FakeChain):
        def aggregate3(self, calls):
            outer = self

            class Call:
                async def call(self):
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        raise TimeoutError("rate limited")
                    return [(True, outer.balances[target].to_bytes(32, "big"))
                            for target, _allow, _data in calls]
            return Call()

    monkeypatch.setattr(positions, "_BATCH_RETRY_DELAY", 0)
    balances = await read_balances(FlakyChain({"0xa": 7}, batches=1), ["0xa"], OWNER)
    assert balances == {"0xa": 7}
    assert attempts == 2


class Universe:
    """A session factory that records which table was asked for."""

    def __init__(self, rows):
        self.rows, self.model = rows, None

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        self.model = statement.column_descriptions[0]["entity"]
        outer = self

        class Result:
            def scalars(self):
                return outer.rows
        return Result()


class Token:
    def __init__(self, address):
        self.address, self.symbol, self.decimals = address, "ChatGpt", 18


async def test_the_page_asks_our_own_short_list_not_the_tapes_cache():
    """The whole point of the split.

    ``chain_tokens`` is the tape's, holds every contract that brushed past
    any tracked wallet, and passed eleven thousand rows in a week -- which is
    twenty-eight Multicalls per page load and, at the far end, a gateway
    timeout. The request path asks ``dex_wallet_tokens`` instead.
    """
    from app.db.models import ChainToken, DexWalletToken

    factory = Universe([Token("0xa")])
    chain = FakeChain({"0xa": 5 * 10 ** 18}, batches=1)
    chain.wallet_address = OWNER

    held = await positions.open_positions(factory, chain, chain_id=4663)

    assert factory.model is DexWalletToken
    assert [p.address for p in held] == ["0xa"]

    # The wide sweep still exists -- it is how an airdrop is ever noticed --
    # but only where a slow pass costs nothing: the background worker.
    await positions.open_positions(factory, chain, chain_id=4663, universe="chain")
    assert factory.model is ChainToken
