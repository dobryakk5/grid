from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://grid:grid@127.0.0.1:5432/grid"

    # Default venue for profiles / endpoints that do not name one explicitly.
    exchange: str = "bybit"

    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_base_url: str = "https://api-demo.bybit.com"

    # MEXC spot is live trading only (no demo environment).
    mexc_api_key: str = ""
    mexc_api_secret: str = ""
    mexc_base_url: str = "https://api.mexc.com"

    grid_poll_seconds: float = 3.0
    grid_fee_buffer_pct: Decimal = Decimal("0.002")

    # ---- on-chain venue (Robinhood Chain / Uniswap) -----------------------
    # Read-only for now: no private key is read anywhere until execution lands.
    rh_chain_id: int = 4663
    rh_rpc_url: str = ""
    # Trading wallet key -- a dedicated bot account, never the main wallet's
    # seed. Read lazily by app/dex/chain.py and never logged or persisted.
    rh_private_key: str = ""
    # Robinhood Chain only ever deployed Universal Router 2.1.1; asking for 2.0
    # is an error there, so the version is pinned rather than left to a default.
    rh_universal_router_version: str = "2.1.1"

    dex_chain_slug: str = "robinhood"
    dexscreener_base_url: str = "https://api.dexscreener.com"
    # One upstream call per this many seconds, however often the worker ticks.
    dexscreener_cache_seconds: float = 10.0
    # JSON overrides for the token registry, e.g.
    # {"USDG": {"address": "0x...", "decimals": 6}}
    dex_tokens: str = ""
    dex_min_order_quote: Decimal = Decimal("10")

    # Risk gates evaluated before any swap is even quoted.
    dex_min_liquidity_usd: Decimal = Decimal("5000000")
    dex_min_volume_h24_usd: Decimal = Decimal("1000000")
    # Relative collapse guards: the level was armed against a healthier market.
    dex_max_liquidity_drop_pct: Decimal = Decimal("40")
    dex_max_volume_drop_pct: Decimal = Decimal("60")

    # How close to the limit the cheap DexScreener price must get before we
    # spend a Uniswap quote on it.
    dex_quote_trigger_band_pct: Decimal = Decimal("1")
    dex_max_slippage_pct: Decimal = Decimal("0.5")

    uniswap_api_key: str = ""
    uniswap_api_base: str = "https://trade-api.gateway.uniswap.org/v1"
    # Uniswap advises refreshing a quote older than roughly half a minute.
    dex_max_quote_age_seconds: float = 30.0
    dex_receipt_timeout_seconds: float = 180.0
    # Nothing is broadcast while this is true; it is the default on purpose.
    dex_dry_run: bool = True

    # Price sampler cadence; samples aggregate into 1m candles.
    dex_sample_seconds: float = 15.0
    # Pairs the sampler watches on top of any profile using exchange=robinhood.
    dex_watch_symbols: str = "PONSETH"


settings = Settings()
