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

from eth_abi import decode
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import TransactionNotFound

from app.core.config import settings
from app.dex.tokens import Token

__all__ = [
    "MAX_UINT256",
    "ChainClient",
    "ChainError",
    "PreflightRevert",
    "SignedPayload",
    "decode_revert",
    "to_int",
]


class ChainError(RuntimeError):
    """RPC unreachable, wrong chain, or a transaction we refuse to send."""


class PreflightRevert(ChainError):
    """The calldata reverts against current state, so it is never signed.

    Separate from its parent because it is not an outage: the node answered,
    and the answer was that this swap cannot execute. Nothing has been signed
    and no nonce has been spent, so the level simply stands down and tries
    again on the next tick with a fresh quote.
    """


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


# Four bytes of revert data say nothing in a log line, so the errors this stack
# actually produces are named here. Anything not listed still comes back as its
# selector: an unrecognised error is a fact worth printing, not a reason to say
# "reverted" and lose the only evidence there is.
_REVERT_SELECTORS: dict[str, str] = {
    # Universal Router
    "0x5bf6f916": "TransactionDeadlinePassed (the quote expired before it was sent)",
    "0x2c4029e9": "ExecutionFailed (a router command reverted)",
    "0xd76a1e9e": "InvalidCommandType",
    "0x1231ae40": "ETHNotAccepted",
    "0x38bbd576": "InvalidEthSender",
    # Uniswap v4 pool manager and router
    "0x5212cba1": (
        "CurrencyNotSettled (the route leaves an unsettled balance in the pool "
        "manager -- usually a hooked pool this router cannot swap through)"
    ),
    "0x486aa307": "PoolNotInitialized (no such pool)",
    "0x8b063d73": "V4TooLittleReceived (slippage: the route no longer clears its own floor)",
    "0x39d35496": "V3TooLittleReceived (slippage)",
    "0x849eaf98": "V2TooLittleReceived (slippage)",
    "0x4e86d23a": "TooLittleReceived (slippage)",
    "0x675cae38": "InsufficientToken",
    "0x6a12f104": "InsufficientETH",
    "0xbe8b8507": "SwapAmountCannotBeZero",
    "0x6f5ffb7e": "ContractLocked",
    # Permit2
    "0xd81b2f2e": "AllowanceExpired (the Permit2 allowance needs renewing)",
    "0xf96fb071": "InsufficientAllowance (the Permit2 allowance is too small)",
    "0x7939f424": "TransferFromFailed (the wallet could not pay the input token)",
    "0xcd21db4f": "SignatureExpired",
    "0x815e1d64": "InvalidSigner",
}


def decode_revert(data) -> str:
    """Say what revert data means, as far as it can be read.

    ``Error(string)`` and ``Panic(uint256)`` carry their own reason; everything
    else on this stack is a custom error, which is nothing but a selector until
    it is looked up.
    """
    if isinstance(data, str):
        text = data.strip()
        if not text.startswith(("0x", "0X")):
            return text or "reverted without a reason"
        try:
            raw = bytes.fromhex(text[2:])
        except ValueError:
            return text
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        return "reverted without a reason"

    if not raw:
        return "reverted without a reason"
    if len(raw) < 4:
        return "0x" + raw.hex()

    selector = "0x" + raw[:4].hex()
    if selector == "0x08c379a0":  # Error(string)
        try:
            return decode(["string"], raw[4:])[0] or "reverted without a reason"
        except Exception:
            return selector
    if selector == "0x4e487b71":  # Panic(uint256)
        try:
            return f"panic {hex(decode(['uint256'], raw[4:])[0])}"
        except Exception:
            return selector
    named = _REVERT_SELECTORS.get(selector)
    return f"{named} [{selector}]" if named else f"unknown error {selector}"


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
        # Without an explicit timeout a rate-limited node can stall a request
        # indefinitely, which strands any transaction opened around it.
        self.w3 = AsyncWeb3(AsyncHTTPProvider(url, request_kwargs={"timeout": 30}))
        key = private_key if private_key is not None else settings.rh_private_key
        self._account = Account.from_key(key.strip()) if key and key.strip() else None
        # Answers that cannot change while this process lives. Robinhood's
        # public RPC is rate-limited, and a worker that re-asks the same two
        # questions for every level on every tick spends its budget on them
        # instead of on the reads a trade actually needs.
        self._chain_confirmed = False
        self._verified_decimals: dict[str, int] = {}

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
        """Fail before any money moves if the RPC points at the wrong chain.

        Asked once per process: the endpoint is fixed at construction and a
        chain does not change its id underneath a running worker.
        """
        if self._chain_confirmed:
            return
        try:
            actual = await self.w3.eth.chain_id
        except Exception as exc:
            raise ChainError(f"cannot reach Robinhood Chain RPC: {exc}") from None
        if actual != self.chain_id:
            raise ChainError(
                f"RPC reports chain {actual}, expected {self.chain_id}"
            )
        self._chain_confirmed = True

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

        The answer is remembered per address: an ERC-20's decimals are fixed at
        deployment, and what this guards against is a wrong *registry* entry,
        which is caught the first time the address is used. Keyed by address
        rather than symbol, so re-pointing a symbol at another contract -- or
        changing the decimals claimed for the same one -- is checked again.
        """
        address = (token.address or "").lower()
        if self._verified_decimals.get(address) == token.decimals:
            return
        on_chain = await self.token_decimals(token)
        if on_chain != token.decimals:
            raise ChainError(
                f"{token.symbol} reports {on_chain} decimals on chain, "
                f"registry says {token.decimals}; fix DEX_TOKENS before trading"
            )
        self._verified_decimals[address] = on_chain

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

    async def preflight(self, tx: dict) -> None:
        """Run this exact calldata against current state; raise if it reverts.

        A swap that cannot execute costs a nonce, a signature and the gas of
        the attempt -- a reverted transaction is paid for in full. Asking the
        node first costs one ``eth_call`` and turns that into a level that
        simply keeps waiting. It is deliberately the raw JSON-RPC call rather
        than ``w3.eth.call``: the reason a swap will not execute is in the
        error's ``data``, and every custom error would otherwise arrive as the
        same unhelpful "execution reverted".

        A simulation is not a promise -- it reads the current block, and the
        transaction lands in a later one -- but everything it does catch would
        otherwise have been paid for on chain.
        """
        payload = {
            "from": self.wallet_address,
            "to": AsyncWeb3.to_checksum_address(tx["to"]),
            "data": tx["data"],
            "value": hex(int(tx.get("value") or 0)),
        }
        if tx.get("gas"):
            payload["gas"] = hex(int(tx["gas"]))
        try:
            response = await self.w3.provider.make_request("eth_call", [payload, "latest"])
        except Exception as exc:  # the node, not the transaction
            raise ChainError(f"pre-flight call failed: {exc}") from None
        error = response.get("error")
        if error is None:
            return
        detail = decode_revert(error.get("data"))
        raise PreflightRevert(
            f"{detail}; {error.get('message', 'execution reverted')}"
        )

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
