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
    # Read-only address for dry runs: quoting needs a swapper and balance checks
    # need an owner, but neither needs a key. Ignored once a key is configured.
    rh_wallet_address: str = ""
    # Robinhood Chain only ever deployed Universal Router 2.1.1; asking for 2.0
    # is an error there, so the version is pinned rather than left to a default.
    rh_universal_router_version: str = "2.1.1"
    # The only Universal Router deployed on Robinhood Chain. Swap calldata is
    # refused if it points anywhere else -- we are about to sign it, and a
    # transaction to an unexpected contract is not something to find out about
    # afterwards. Blank disables the check.
    rh_universal_router_address: str = "0x8876789976decbfcbbbe364623c63652db8c0904"

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

    # Canonical Permit2 deployment; the same address on every chain that has
    # one. Validated against the quote's own permitData before anything is
    # signed, so a wrong value here is caught rather than acted on.
    permit2_address: str = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
    # Permit2's design is a one-time unlimited ERC-20 approval to Permit2, with
    # the per-swap limit and expiry carried by the signed permit instead. Set
    # this to true to approve only the amount each swap needs, at one extra
    # approval transaction per trade.
    dex_approve_exact: bool = False

    uniswap_api_key: str = ""
    uniswap_api_base: str = "https://trade-api.gateway.uniswap.org/v1"
    # Uniswap advises refreshing a quote older than roughly half a minute.
    dex_max_quote_age_seconds: float = 30.0
    dex_receipt_timeout_seconds: float = 180.0
    # Nothing is broadcast while this is true; it is the default on purpose.
    dex_dry_run: bool = True

    # ---- DEX worker ------------------------------------------------------
    dex_poll_seconds: float = 5.0
    # How long a level waits for its price before it is given up on.
    dex_intent_ttl_hours: int = 168
    # A level blocked by a risk gate re-checks after this long.
    dex_blocked_retry_seconds: int = 30
    # A broadcast with no receipt after this is re-sent as-is.
    dex_rebroadcast_after_seconds: int = 60
    # A transaction still unmined after this is replaced at the same nonce.
    dex_stuck_after_seconds: int = 300
    dex_gas_bump_pct: Decimal = Decimal("25")
    # How many times a level may be re-armed after an abandoned attempt.
    dex_max_retries: int = 3

    # Price sampler cadence; samples aggregate into 1m candles.
    dex_sample_seconds: float = 15.0
    # Pairs the sampler watches on top of any profile using exchange=robinhood.
    dex_watch_symbols: str = "PONSUSDG"

    # ---- Operator authentication -----------------------------------------
    # Empty secret or hash means "not configured": reads stay open for local
    # work, and every endpoint that can spend money refuses to run at all.
    # That way a half-finished deploy cannot quietly expose the wallet.
    auth_secret: str = ""
    auth_password_hash: str = ""
    auth_token_ttl_minutes: int = 720
    # A long random string for machine callers (the FOMO collector), accepted
    # in place of a login token. Rotate it by changing this one value.
    auth_service_token: str = ""

    # ---- FOMO (internal API, research access under one's own account) -----
    # The JWT expires quickly (a ~1h Privy session); the page can override
    # this with a pasted-in session instead of restarting the process.
    fomo_jwt: str = ""
    fomo_base_url: str = "https://prod-api.fomo.family"
    fomo_supported_chains: str = "1,56,143,4663,8453,1399811149"
    # A token report fans out to every trader's trades -- this caps how often
    # any one FOMO endpoint is actually re-fetched.
    fomo_cache_seconds: float = 30.0
    fomo_leaderboard_limit: int = 50
    # 429 backoff, mirroring the known FOMO web client: start here, double on
    # every further 429, cap at the ceiling; the last good response is served
    # while a backoff window is open.
    fomo_backoff_start_seconds: float = 60.0
    fomo_backoff_max_seconds: float = 300.0
    # How often the trader registry re-polls the leaderboard and holders.
    fomo_registry_poll_seconds: float = 900.0
    # How far back (in blocks) a newly discovered wallet is backfilled, so the
    # trade that got it noticed is not the one trade that goes missing.
    # Robinhood Chain produces roughly 15 blocks a second, so this is about an
    # hour of chain time -- far enough back to catch the trade that surfaced a
    # wallet, without asking the RPC for a day's worth of history.
    fomo_new_wallet_backfill_blocks: int = 60_000
    # Raw-response inspection endpoint; off by default outside development.
    fomo_debug_api: bool = False
    # Optional manual helper: let the logged-in fomo.family tab POST a cached
    # session to this API (``scripts/fomo-token.sh --console``). Off
    # by default so nothing cross-origin can reach ``/api/fomo/session`` unless
    # a developer turns it on; when off, the token-grab snippet falls back to
    # copying the token for a manual paste, so the bridge is convenience only.
    fomo_token_bridge: bool = False

    # ---- automatic trader discovery --------------------------------------
    # The tape only watches wallets it already knows, so the roster has to
    # refresh itself or it goes stale the moment a new trader shows up.
    wallet_discovery_enabled: bool = True
    wallet_discovery_interval_seconds: float = 3600.0
    # One token-filtered getLogs over this many blocks -- roughly a minute of
    # chain time at ~15 blocks/second, which is enough to see who is active.
    wallet_discovery_blocks: int = 900
    wallet_discovery_top: int = 20

    # ---- Robinhood Chain trade tape (on-chain source of truth) ------------
    rh_chain_name: str = "Robinhood Chain"
    # Quote symbols close enough to $1 that the swap's own quote leg is a
    # better USD estimate than any external price feed.
    usd_quote_symbols: str = "USDG"
    chain_tape_poll_seconds: float = 5.0
    chain_tape_block_batch_max: int = 2000
    chain_tape_block_batch_min: int = 50
    # Blocks the tape stays behind the chain head before treating a block as
    # settled -- a cheap reorg guard, not a real one.
    chain_tape_confirmations: int = 3
    # 0 means "start from the current head" on a brand-new cursor.
    chain_tape_start_block: int = 0
    # Wallets backfilled per pass. They share one scan (a topic position takes
    # a set of values), so this is about how long one pass may hold up the
    # realtime cursor, not about RPC cost per wallet.
    chain_tape_backfill_wallets: int = 50

    # ---- token intelligence (what the top is buying, and is it safe) ------
    # Free, keyless sources only: DexScreener for market data on the chains it
    # indexes, GoPlus for contract safety, and our own tape for Robinhood
    # Chain, which neither of them covers.
    goplus_base_url: str = "https://api.gopluslabs.io"
    intel_refresh_seconds: float = 600.0
    # How old a market snapshot may be before the page refreshes it. Shorter
    # than the worker's period on purpose: the page is read on demand.
    intel_market_ttl_seconds: float = 300.0
    # Contract properties change rarely and cost a request each; holders and
    # taxes do drift, so this is a day, not a week.
    intel_security_ttl_hours: float = 24.0
    # Coins refreshed in one pass, most recently traded by the cohort first.
    intel_max_tokens: int = 120

    # Optional LLM pass over thesis text. Rules run first and always; the model
    # only sees the notes the rules could not classify. Blank key = off, and
    # off is a working configuration, not a degraded one.
    intel_llm_provider: str = "anthropic"
    intel_llm_api_key: str = ""
    # Opus by default because a misread thesis is a wrong signal, not a typo.
    # Bulk classification is where a cheaper model earns its place: set
    # INTEL_LLM_MODEL=claude-haiku-4-5 (or claude-sonnet-5) and it is used as is.
    intel_llm_model: str = "claude-opus-5"
    intel_llm_base_url: str = ""
    # A budget, not a target: one pass never spends more calls than this.
    intel_llm_max_calls: int = 40
    intel_llm_timeout_seconds: float = 30.0


settings = Settings()
