"""Async JSON-RPC access to Robinhood Chain.

Everything here is async on purpose. The rest of the app runs one event loop for
every profile on every venue, so a synchronous ``wait_for_transaction_receipt``
would stall Bybit and MEXC while an on-chain swap confirms. Waiting is therefore
split in two: :meth:`ChainClient.receipt` is a single non-blocking poll for a
worker tick, and :meth:`ChainClient.wait_for_receipt` exists for the manual
smoke-test script that has nothing else to do.

The private key is read lazily from settings, never logged, and never stored.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import TransactionNotFound

from app.core.config import settings
from app.dex.tokens import Token

__all__ = ["MAX_UINT256", "ChainClient", "ChainError", "SignedPayload", "to_int"]


class ChainError(RuntimeError):
    """RPC unreachable, wrong chain, or a transaction we refuse to send."""


_ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "name": "symbol",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]

MAX_UINT256 = 2**256 - 1


def to_int(value) -> int | None:
    """Uniswap returns numbers as int, decimal string or 0x-string."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"unsupported numeric value: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return int(text, 16) if text.startswith(("0x", "0X")) else int(text)
    raise TypeError(f"unsupported numeric value: {value!r}")


@dataclass(frozen=True)
class SignedPayload:
    """A signed transaction and the hash it will have once broadcast.

    The hash is known before the transaction ever reaches a node, which is what
    makes crash recovery safe: persist this, then broadcast, and a restarted
    process can look the hash up instead of deciding to trade again.
    """

    tx_hash: str
    raw: bytes
    nonce: int
    wallet_address: str

    @property
    def raw_hex(self) -> str:
        return "0x" + self.raw.hex()


class ChainClient:
    def __init__(self, *, rpc_url: str | None = None, private_key: str | None = None) -> None:
        url = (rpc_url if rpc_url is not None else settings.rh_rpc_url).strip()
        if not url:
            raise ChainError("RH_RPC_URL is not configured")
        self.chain_id = settings.rh_chain_id
        self.w3 = AsyncWeb3(AsyncHTTPProvider(url))
        key = private_key if private_key is not None else settings.rh_private_key
        self._account = Account.from_key(key.strip()) if key and key.strip() else None

    async def close(self) -> None:
        provider = getattr(self.w3, "provider", None)
        disconnect = getattr(provider, "disconnect", None)
        if disconnect is not None:
            await disconnect()

    # ---- identity --------------------------------------------------------

    @property
    def has_key(self) -> bool:
        return self._account is not None

    @property
    def wallet_address(self) -> str:
        """The wallet we act for.

        A dry run only needs the address -- to quote against, and to read
        balances and allowances for -- so a configured address stands in when no
        key is present. Signing still requires the key, and refuses without it.
        """
        if self._account is not None:
            return self._account.address
        configured = (settings.rh_wallet_address or "").strip()
        if configured:
            return AsyncWeb3.to_checksum_address(configured)
        raise ChainError(
            "no wallet: set RH_PRIVATE_KEY to trade, or RH_WALLET_ADDRESS to dry-run"
        )

    async def ensure_ready(self) -> None:
        """Fail before any money moves if the RPC points at the wrong chain."""
        try:
            actual = await self.w3.eth.chain_id
        except Exception as exc:
            raise ChainError(f"cannot reach Robinhood Chain RPC: {exc}") from None
        if actual != self.chain_id:
            raise ChainError(
                f"RPC reports chain {actual}, expected {self.chain_id}"
            )

    # ---- reads -----------------------------------------------------------

    async def native_balance(self, address: str | None = None) -> Decimal:
        target = AsyncWeb3.to_checksum_address(address or self.wallet_address)
        wei = await self.w3.eth.get_balance(target)
        return Decimal(wei).scaleb(-18)

    def _erc20(self, token: Token):
        return self.w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(token.address), abi=_ERC20_ABI
        )

    async def token_balance(self, token: Token, address: str | None = None) -> Decimal:
        target = AsyncWeb3.to_checksum_address(address or self.wallet_address)
        raw = await self._erc20(token).functions.balanceOf(target).call()
        return token.from_wei(raw)

    async def token_decimals(self, token: Token) -> int:
        return int(await self._erc20(token).functions.decimals().call())

    async def verify_token(self, token: Token) -> None:
        """Guard the registry's assumed decimals against the contract itself.

        A wrong decimals value silently rescales every amount by orders of
        magnitude, so this runs before the first transaction of a session.
        """
        on_chain = await self.token_decimals(token)
        if on_chain != token.decimals:
            raise ChainError(
                f"{token.symbol} reports {on_chain} decimals on chain, "
                f"registry says {token.decimals}; fix DEX_TOKENS before trading"
            )

    async def token_allowance(self, token: Token, spender: str, owner: str | None = None) -> int:
        """Raw allowance in the token's own units."""
        holder = AsyncWeb3.to_checksum_address(owner or self.wallet_address)
        return int(
            await self._erc20(token)
            .functions.allowance(holder, AsyncWeb3.to_checksum_address(spender))
            .call()
        )

    def encode_approve(self, token: Token, spender: str, amount: int) -> dict:
        """Calldata for an ERC-20 approve, with no network round-trip."""
        contract = self._erc20(token)
        return {
            "to": AsyncWeb3.to_checksum_address(token.address),
            "data": contract.encode_abi(
                abi_element_identifier="approve",
                args=[AsyncWeb3.to_checksum_address(spender), int(amount)],
            ),
            "value": 0,
        }

    async def pending_nonce(self, address: str | None = None) -> int:
        target = AsyncWeb3.to_checksum_address(address or self.wallet_address)
        return await self.w3.eth.get_transaction_count(target, "pending")

    async def fee_fields(self, *, priority_wei: int | None = None) -> dict:
        """EIP-1559 fields with room for one base-fee doubling."""
        block = await self.w3.eth.get_block("latest")
        base_fee = block.get("baseFeePerGas")
        if base_fee is None:
            return {"gasPrice": await self.w3.eth.gas_price}
        priority = priority_wei
        if priority is None:
            try:
                priority = await self.w3.eth.max_priority_fee
            except Exception:
                priority = AsyncWeb3.to_wei(1, "gwei")
        return {
            "maxFeePerGas": base_fee * 2 + priority,
            "maxPriorityFeePerGas": priority,
        }

    async def estimate_gas(self, tx: dict) -> int:
        payload = dict(tx)
        payload["from"] = self.wallet_address
        return int(await self.w3.eth.estimate_gas(payload))

    # ---- writes ----------------------------------------------------------

    def sign(self, tx: dict) -> SignedPayload:
        if self._account is None:
            raise ChainError("RH_PRIVATE_KEY is not configured")
        if tx.get("chainId") != self.chain_id:
            raise ChainError(
                f"refusing to sign a transaction for chain {tx.get('chainId')!r}"
            )
        signed = self._account.sign_transaction(tx)
        return SignedPayload(
            tx_hash="0x" + signed.hash.hex().removeprefix("0x"),
            raw=bytes(signed.raw_transaction),
            nonce=int(tx["nonce"]),
            wallet_address=self._account.address,
        )

    def sign_typed_data(self, *, domain: dict, types: dict, message: dict) -> str:
        """EIP-712 signature, returned as a 0x hex string.

        ``EIP712Domain`` is stripped from the type set: the domain is passed
        separately, and leaving it in makes the primary type ambiguous.
        """
        if self._account is None:
            raise ChainError("RH_PRIVATE_KEY is not configured")
        message_types = {
            name: fields for name, fields in types.items() if name != "EIP712Domain"
        }
        if not message_types:
            raise ChainError("typed data carries no message types to sign")
        try:
            signable = encode_typed_data(
                domain_data=domain, message_types=message_types, message_data=message
            )
            signed = self._account.sign_message(signable)
        except Exception as exc:
            raise ChainError(f"cannot sign typed data: {exc}") from None
        return "0x" + signed.signature.hex().removeprefix("0x")

    async def broadcast(self, payload: SignedPayload) -> str:
        """Send a signed payload; re-sending one already in the pool is fine.

        Recovery re-broadcasts verbatim, so "already known" is success, not an
        error -- the transaction we were told to send is exactly the one the
        node already has.
        """
        try:
            sent = await self.w3.eth.send_raw_transaction(payload.raw)
        except ValueError as exc:
            message = str(exc).lower()
            if "already known" in message or "known transaction" in message:
                return payload.tx_hash
            raise ChainError(f"broadcast failed: {exc}") from None
        return "0x" + sent.hex().removeprefix("0x")

    async def native_received(
        self,
        *,
        block_number: int,
        gas_wei: int,
        value_sent_wei: int = 0,
        address: str | None = None,
    ) -> int:
        """How much native coin the wallet gained in ``block_number``.

        Receiving ETH emits no log, so the only honest measure is the balance
        either side of the block, with what we spent ourselves added back:

            received = after - before + value_sent + gas_paid

        This assumes the wallet had no other transaction in that same block,
        which holds for a bot wallet sending one swap at a time. A second
        concurrent sender on the same key would make this wrong, which is one
        more reason the trading wallet is its own account.
        """
        target = AsyncWeb3.to_checksum_address(address or self.wallet_address)
        after = await self.w3.eth.get_balance(target, block_identifier=block_number)
        before = await self.w3.eth.get_balance(target, block_identifier=block_number - 1)
        return int(after) - int(before) + int(value_sent_wei) + int(gas_wei)

    async def receipt(self, tx_hash: str) -> dict | None:
        """One poll. ``None`` means still pending, not missing."""
        try:
            return dict(await self.w3.eth.get_transaction_receipt(tx_hash))
        except TransactionNotFound:
            return None

    async def wait_for_receipt(self, tx_hash: str, *, timeout: float | None = None) -> dict:
        deadline = timeout if timeout is not None else settings.dex_receipt_timeout_seconds
        waited = 0.0
        while waited < deadline:
            found = await self.receipt(tx_hash)
            if found is not None:
                return found
            await asyncio.sleep(2.0)
            waited += 2.0
        raise ChainError(f"no receipt for {tx_hash} after {deadline:.0f}s")
