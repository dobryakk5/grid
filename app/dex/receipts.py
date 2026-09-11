"""What a swap actually filled, read back out of its receipt.

A swap's real fill is never the quote: routing, price impact and any hop in
between move it. And a multi-hop route (USDG -> WETH -> PONS) writes a whole
chain of ``Transfer`` logs, most of which are between pools and have nothing to
do with us.

So the fill is computed as the *wallet's* net balance change per token, not by
picking a Transfer out of the list. Everything here is pure: it takes a receipt
dict and returns numbers, which makes it testable without a chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.dex.tokens import Token

__all__ = ["FillReport", "ReceiptError", "TRANSFER_TOPIC", "parse_swap_fill", "wallet_deltas"]

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class ReceiptError(RuntimeError):
    """A reverted transaction, or a receipt whose fill cannot be read."""


def _hex(value) -> str:
    """Normalise HexBytes / bytes / str to a lowercase 0x string."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    text = str(value).strip().lower()
    return text if text.startswith("0x") else "0x" + text


def _address_from_topic(topic) -> str:
    """Indexed address topics are left-padded to 32 bytes."""
    text = _hex(topic)[2:].rjust(64, "0")
    return "0x" + text[-40:]


def _uint(value) -> int:
    text = _hex(value)
    return int(text, 16) if len(text) > 2 else 0


@dataclass(frozen=True)
class FillReport:
    amount_in_wei: int
    amount_out_wei: int
    gas_used: int
    effective_gas_price_wei: int
    tx_hash: str
    block_number: int | None
    block_hash: str | None

    @property
    def gas_native_wei(self) -> int:
        return self.gas_used * self.effective_gas_price_wei

    @property
    def gas_native(self) -> Decimal:
        # Gas is always paid in the chain's 18-decimals native coin.
        return Decimal(self.gas_native_wei).scaleb(-18)

    def amount_in(self, token: Token) -> Decimal:
        return token.from_wei(self.amount_in_wei)

    def amount_out(self, token: Token) -> Decimal:
        return token.from_wei(self.amount_out_wei)

    def price(self, *, token_in: Token, token_out: Token) -> Decimal:
        """Realised price: input spent per unit of output received."""
        received = self.amount_out(token_out)
        if received <= 0:
            raise ReceiptError("receipt shows no tokens received")
        return self.amount_in(token_in) / received


def wallet_deltas(logs, wallet: str) -> dict[str, int]:
    """Net ERC-20 balance change for ``wallet``, keyed by token address.

    Transfers between third parties -- every intermediate hop of a route -- are
    ignored, because the wallet is neither sender nor recipient.
    """
    target = wallet.strip().lower()
    deltas: dict[str, int] = {}
    for log in logs or []:
        topics = log.get("topics") or []
        if len(topics) < 3 or _hex(topics[0]) != TRANSFER_TOPIC:
            continue
        token = _hex(log.get("address"))
        sender = _address_from_topic(topics[1])
        recipient = _address_from_topic(topics[2])
        if target not in (sender, recipient):
            continue
        value = _uint(log.get("data"))
        if sender == target:
            deltas[token] = deltas.get(token, 0) - value
        if recipient == target:
            deltas[token] = deltas.get(token, 0) + value
    return deltas


def parse_swap_fill(
    receipt: dict,
    *,
    wallet: str,
    token_in: Token,
    token_out: Token,
    sent_value_wei: int = 0,
) -> FillReport:
    """Read the realised amounts of one swap.

    ``sent_value_wei`` is the transaction's ``value`` and is how a native-coin
    input is measured: ETH movements produce no ``Transfer`` log. The router
    refunds unspent ETH by plain transfer, which is likewise invisible here, so
    for an exact-input swap treat this as the amount committed.
    """
    if int(receipt.get("status", 0)) != 1:
        raise ReceiptError(
            f"transaction {_hex(receipt.get('transactionHash'))} reverted"
        )

    deltas = wallet_deltas(receipt.get("logs"), wallet)

    amount_out = deltas.get(token_out.address.lower(), 0)
    if token_out.native:
        raise ReceiptError("native-coin output is not supported yet")
    if amount_out <= 0:
        raise ReceiptError(
            f"receipt shows no {token_out.symbol} arriving at {wallet}"
        )

    if token_in.native:
        amount_in = int(sent_value_wei)
    else:
        amount_in = -deltas.get(token_in.address.lower(), 0)
    if amount_in <= 0:
        raise ReceiptError(f"receipt shows no {token_in.symbol} leaving {wallet}")

    return FillReport(
        amount_in_wei=amount_in,
        amount_out_wei=amount_out,
        gas_used=int(receipt.get("gasUsed") or 0),
        effective_gas_price_wei=int(receipt.get("effectiveGasPrice") or 0),
        tx_hash=_hex(receipt.get("transactionHash")),
        block_number=(
            int(receipt["blockNumber"]) if receipt.get("blockNumber") is not None else None
        ),
        block_hash=_hex(receipt.get("blockHash")) or None,
    )
