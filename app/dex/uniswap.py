"""Uniswap Trading API: quotes and swap calldata.

This is the executable half of the price path. DexScreener says the market is
near a level; only a quote for the actual size says what we would really get,
after routing and price impact.

Two deliberate restrictions in this stage:

* routing is limited to the AMM protocols so the response is a ``CLASSIC`` swap
  we can sign and send ourselves. UniswapX returns an order for ``/order``
  instead, which is a different flow.
* a quote that comes back carrying ``permitData`` is refused rather than sent
  unsigned. Permit2 signing lands with the ERC-20 stage; until then the only
  supported input is native ETH, which needs no approval.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

import httpx

from app.core.config import settings
from app.dex.chain import to_int
from app.dex.tokens import NATIVE_ADDRESS, DexPair, Token

__all__ = ["QuoteResult", "UniswapClient", "UniswapError"]

# Uniswap quotes only these; anything else routes through UniswapX.
CLASSIC_PROTOCOLS = ["V2", "V3", "V4"]


class UniswapError(RuntimeError):
    """Any Trading API failure, or a response this stage refuses to use."""


@dataclass(frozen=True)
class QuoteResult:
    """One quote plus what we need to decide whether to act on it."""

    raw: dict
    quote: dict
    routing: str
    amount_in: int
    amount_out: int
    # The floor the swap itself enforces: below this the transaction reverts.
    # It is the only amount the chain actually guarantees, which makes it the
    # one a limit has to be judged against.
    min_amount_out: int
    permit_data: dict | None
    # The router's own gas estimate: native wei, and its USD valuation.
    # (``gasFeeQuote`` is deliberately not read -- it is denominated in the
    # output token, not in the pair's quote currency, so it is not a fee figure
    # this code can use.)
    gas_fee_native_wei: int = 0
    gas_fee_usd: Decimal | None = None
    received_at: float = field(default_factory=time.monotonic)

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.received_at

    @property
    def is_stale(self) -> bool:
        return self.age_seconds > settings.dex_max_quote_age_seconds

    @property
    def needs_permit(self) -> bool:
        return bool(self.permit_data)

    def price(self, pair: DexPair, side: str = "Buy") -> Decimal:
        """Executable price of one base token, in quote terms.

        Derived from the quoted amounts rather than from any price field, so it
        already carries routing and price impact for this size. Both directions
        return quote-per-base, so a limit compares the same way regardless of
        which token is being spent.
        """
        return self._price_for(pair, side, self.amount_out)

    def worst_price(self, pair: DexPair, side: str = "Buy") -> Decimal:
        """The worst price this swap can produce without reverting.

        ``price`` is what the route is expected to give; slippage tolerance
        means the fill may land anywhere down to ``min_amount_out``. A limit
        promises "this price or better", and only this figure can keep that
        promise -- checking the expected price would let a fill breach the limit
        by up to the slippage tolerance.
        """
        return self._price_for(pair, side, self.min_amount_out or self.amount_out)

    def _price_for(self, pair: DexPair, side: str, amount_out: int) -> Decimal:
        if side.strip().lower() == "sell":
            base_in = pair.base.from_wei(self.amount_in)
            if base_in <= 0:
                raise UniswapError("quote has no input amount")
            return pair.quote.from_wei(amount_out) / base_in
        base_out = pair.base.from_wei(amount_out)
        if base_out <= 0:
            raise UniswapError("quote returned no output amount")
        return pair.quote.from_wei(self.amount_in) / base_out


def _amount_of(side: dict | None, *, key: str = "amount") -> int:
    if not isinstance(side, dict):
        return 0
    return to_int(side.get(key)) or 0


class UniswapClient:
    def __init__(self, *, http: httpx.AsyncClient | None = None) -> None:
        self.base_url = settings.uniswap_api_base.rstrip("/")
        self.api_key = settings.uniswap_api_key
        self.client = http or httpx.AsyncClient(timeout=20.0)
        self._owns_client = http is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _headers(self) -> dict:
        if not self.api_key:
            raise UniswapError("UNISWAP_API_KEY is not configured")
        return {
            "x-api-key": self.api_key,
            "accept": "application/json",
            "content-type": "application/json",
            # Robinhood Chain only ever deployed 2.1.1; leaving this to the
            # API's default would break the moment that default moves.
            "x-universal-router-version": settings.rh_universal_router_version,
        }

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            response = await self.client.post(
                f"{self.base_url}{path}", headers=self._headers(), json=payload
            )
        except httpx.HTTPError as exc:
            raise UniswapError(f"Uniswap {path} request failed: {exc}") from None
        if response.status_code >= 400:
            raise UniswapError(
                f"Uniswap {path} HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            data = response.json()
        except ValueError:
            raise UniswapError(
                f"Uniswap {path} non-JSON response: {response.text[:200]}"
            ) from None
        if not isinstance(data, dict):
            raise UniswapError(f"Uniswap {path} returned {type(data).__name__}")
        return data

    # ---- quote -----------------------------------------------------------

    async def quote_exact_in(
        self,
        *,
        pair: DexPair,
        side: str,
        amount_in_wei: int,
        swapper: str,
        slippage_pct: Decimal | None = None,
    ) -> QuoteResult:
        """Quote a sale of ``amount_in_wei`` of the input token.

        ``side`` is the trade direction in the app's vocabulary: ``"Buy"``
        spends quote to receive base, ``"Sell"`` the other way round.
        """
        token_in, token_out = _direction(pair, side)
        slippage = slippage_pct if slippage_pct is not None else settings.dex_max_slippage_pct
        payload = {
            "tokenIn": token_in.address or NATIVE_ADDRESS,
            "tokenOut": token_out.address or NATIVE_ADDRESS,
            "tokenInChainId": settings.rh_chain_id,
            "tokenOutChainId": settings.rh_chain_id,
            "amount": str(amount_in_wei),
            "type": "EXACT_INPUT",
            "swapper": swapper,
            "slippageTolerance": float(slippage),
            "protocols": CLASSIC_PROTOCOLS,
            "routingPreference": "BEST_PRICE",
        }
        data = await self._post("/quote", payload)

        routing = str(data.get("routing") or "")
        if routing != "CLASSIC":
            raise UniswapError(
                f"routing {routing or 'unknown'} is not a signable swap in this "
                "stage; only CLASSIC AMM routes are supported"
            )
        quote = data.get("quote")
        if not isinstance(quote, dict):
            raise UniswapError(f"quote response has no quote object: {data}")

        result = QuoteResult(
            raw=data,
            quote=quote,
            routing=routing,
            amount_in=_amount_of(quote.get("input")) or amount_in_wei,
            amount_out=_amount_of(quote.get("output")),
            min_amount_out=_amount_of(quote.get("output"), key="minimumAmount"),
            permit_data=data.get("permitData") or quote.get("permitData"),
            gas_fee_native_wei=to_int(quote.get("gasFee")) or 0,
            gas_fee_usd=(
                Decimal(str(quote["gasFeeUSD"]))
                if quote.get("gasFeeUSD") is not None
                else None
            ),
        )
        if result.amount_out <= 0:
            raise UniswapError("quote returned a zero output amount")
        return result

    # ---- swap ------------------------------------------------------------

    async def build_swap(self, quote: QuoteResult, *, signature: str | None = None) -> dict:
        """Turn a quote into calldata we can sign.

        A stale quote is refused here rather than sent: by the time a swap built
        on it lands, the price it promised is gone.
        """
        if quote.is_stale:
            raise UniswapError(
                f"quote is {quote.age_seconds:.0f}s old "
                f"(limit {settings.dex_max_quote_age_seconds:.0f}s); request a new one"
            )
        if quote.needs_permit and signature is None:
            raise UniswapError(
                "quote requires a Permit2 signature, which this stage cannot "
                "produce; native ETH input needs no approval"
            )
        payload = {"quote": quote.quote}
        if quote.permit_data is not None:
            payload["permitData"] = quote.permit_data
        if signature is not None:
            payload["signature"] = signature

        data = await self._post("/swap", payload)
        swap = data.get("swap")
        if not isinstance(swap, dict) or not swap.get("to") or not swap.get("data"):
            raise UniswapError(f"swap response carries no transaction: {data}")
        return swap


def _direction(pair: DexPair, side: str) -> tuple[Token, Token]:
    normalized = side.strip().lower()
    if normalized == "buy":
        return pair.quote, pair.base
    if normalized == "sell":
        return pair.base, pair.quote
    raise UniswapError(f"unknown side {side!r}")
