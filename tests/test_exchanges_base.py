import pytest

from app.exchanges.base import split_symbol


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("PONSUSDG", ("PONS", "USDG")),
        ("BTCUSDT", ("BTC", "USDT")),
        ("PONSETH", ("PONS", "ETH")),
        ("ponsusdg", ("PONS", "USDG")),
        # A bare quote coin is not a pair: it must not read as base "".
        ("USDG", ("", "")),
        ("USDT", ("", "")),
        # No recognised quote suffix at all.
        ("CASHCAT", ("", "")),
    ],
)
def test_split_symbol(symbol, expected):
    assert split_symbol(symbol) == expected
