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
    # Connections one process may hold. Deliberately small: every service here
    # runs the same engine, a worker uses one session per tick, and each idle
    # backend on the server costs real memory -- SQLAlchemy's own defaults
    # (5 plus 10 overflow, per process) are sized for a web server, not for six
    # of these sharing one small box.
    db_pool_size: int = 2
    db_max_overflow: int = 3
    grid_fee_buffer_pct: Decimal = Decimal("0.002")

    # ---- on-chain venue (Robinhood Chain / Uniswap) -----------------------
    # Read-only for now: no private key is read anywhere until execution lands.
    rh_chain_id: int = 4663
    rh_rpc_url: str = ""
    # The tape reads the same chain through its own endpoint, because it wants
    # the opposite thing from the same node. Trading needs a few small reads
    # to be answered now and never refused; the tape needs `eth_getLogs` over
    # thousands of blocks at a time, and does not care when the answer lands.
    # Provider tiers price those apart -- Alchemy's free tier serves execution
    # happily and caps getLogs at a 10-block range, which the tape cannot use
    # at all -- and, more to the point, a rate limit the tape walks into must
    # not be one a swap is standing behind. Blank means "same as rh_rpc_url",
    # which is the single-endpoint setup.
    chain_tape_rpc_url: str = ""
    # Trading wallet key -- a dedicated bot account, never the main wallet's
    # seed. Read lazily by app/dex/chain.py and never logged or persisted.
    rh_private_key: str = ""
    # Read-only address for dry runs: quoting needs a swapper and balance checks
    # need an owner, but neither needs a key. Ignored once a key is configured.
    rh_wallet_address: str = ""
    # Robinhood Chain only ever deployed Universal Router 2.1.1; asking for 2.0
    # is an error there, so the version is pinned rather than left to a default.
    rh_universal_router_version: str = "2.1.1"
    # Universal Routers we will sign calldata for, comma-separated. Anything
    # else is refused -- we are about to sign it, and a transaction to an
    # unexpected contract is not something to find out about afterwards.
    # Blank disables the check.
    #
    # A list rather than one address, because Robinhood Chain has more than one
    # live Universal Router and the Trading API decides which it builds for.
    # Both defaults below were verified on chain: each carries the
    # execute(bytes,bytes[],uint256) selector and embeds this chain's Permit2,
    # v4 PoolManager and V3 factory, and each is settling swaps in recent
    # blocks. Pinning only one of them is what refuses every quote the API
    # happens to route through the other.
    rh_universal_router_address: str = (
        "0x8876789976decbfcbbbe364623c63652db8c0904,"
        "0x204FAca1764B154221e35c0d20aBb3c525710498"
    )

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
    # How often the worker sweeps every token the tape knows, looking for
    # holdings nobody announced -- an airdrop, or a buy made outside the bot.
    # Anything we trade ourselves lands in ``dex_wallet_tokens`` immediately,
    # so this is slow on purpose: it is the expensive pass, and the only one
    # that must never run inside a request.
    dex_wallet_scan_seconds: float = 600.0
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
    # What to wait after the node refuses a pass for asking too often, and the
    # ceiling that wait doubles towards. The public endpoint rate-limits hard
    # enough that retrying every poll interval simply keeps the limit tripped.
    chain_tape_backoff_seconds: float = 5.0
    chain_tape_backoff_max_seconds: float = 120.0
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
    # How many wallets the tape actually follows, biggest traded volume
    # first. Discovery keeps meeting new addresses and every one of them was
    # scanned forever after: 698 wallets, whose transfer logs put eleven
    # thousand contracts into ``chain_tokens`` in a week -- two thirds of
    # which never appeared in a single swap. The roster is a top-N now, and
    # the rest of the table is history rather than a scan target. Our own
    # wallet is always followed on top of this, whatever it ranks.
    chain_tape_wallet_limit: int = 30
    # Trailing window the roster ranking is measured over. Long enough that
    # one big trade does not buy a slot for an hour, short enough that a
    # wallet which has stopped trading gives one up.
    chain_tape_rank_window_days: int = 7

    # ---- token intelligence (what the top is buying, and is it safe) ------
    # Free, keyless sources only: DexScreener for market data on the chains it
    # indexes -- Robinhood Chain included, since it started listing that one --
    # GoPlus for contract safety, and our own tape for whatever the screener
    # has not listed there yet.
    goplus_base_url: str = "https://api.gopluslabs.io"
    intel_refresh_seconds: float = 600.0
    # How old a market snapshot may be before the page refreshes it. Shorter
    # than the worker's period on purpose: the page is read on demand.
    intel_market_ttl_seconds: float = 300.0
    # Contract properties change rarely and cost a request each; holders and
    # taxes do drift, so this is a day, not a week.
    intel_security_ttl_hours: float = 24.0
    # The same GoPlus answer also carries the holder count -- and that is a
    # measurement, not a property of the contract. Its own, much shorter TTL is
    # therefore the sampling rate of the holder history: at a day there is no
    # second point inside an hour, so "держателей за час" could never exist.
    # The two are not independent, because one request answers both: a coin is
    # re-asked about once the *shorter* of the two has passed. An hour keeps
    # the 1h/6h/24h deltas answerable at roughly a hundred requests a day,
    # comfortably inside what GoPlus serves anonymously. Raise it to sample
    # less often, at the cost of the narrow windows.
    intel_holders_ttl_hours: float = 1.0
    # Coins refreshed in one pass, most recently traded by the cohort first.
    intel_max_tokens: int = 120
    # Монеты, которые собираются всегда, помимо того, что торгует когорта:
    # `<сеть>:<адрес>` через запятую, сеть — слагом (`solana`) или числом
    # (`4663`). Лимитом выше не вытесняются: это явно названный список.
    intel_watchlist: str = ""

    # Optional LLM pass over thesis text. Rules run first and always; the model
    # only sees the notes they could not classify. Blank key = off, and off is a
    # working configuration, not a degraded one.
    intel_llm_provider: str = "openrouter"
    intel_llm_api_key: str = ""
    # Falls back to the usual environment variable, so an OpenRouter key that is
    # already in the shell needs no second home in .env.
    openrouter_api_key: str = ""
    intel_llm_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    intel_llm_base_url: str = "https://openrouter.ai/api/v1"
    # "off", or an effort level ("low"/"medium"/"high") for the models that
    # reason. Off by default: this is labelling, not reasoning, and it runs over
    # every unread note on every pass.
    intel_llm_reasoning: str = "off"
    # A budget, not a target: one pass never spends more calls than this.
    intel_llm_max_calls: int = 40
    # OpenRouter's free tier allows about 20 requests a minute; this keeps a
    # pass comfortably inside that without thinking about it.
    intel_llm_pause_seconds: float = 3.0
    intel_llm_timeout_seconds: float = 120.0

    # ---- операции в Telegram ---------------------------------------------
    # Сетка и история сделок живут в вебе; бот нужен ровно для того, чтобы не
    # держать вкладку открытой. Пустой токен или пустой чат = выключено, и это
    # рабочая конфигурация: постановка в очередь просто не происходит, а не
    # копит недоставленное до лучших времён.
    telegram_bot_token: str = ""
    # Один чат или несколько через запятую. Группы и каналы — отрицательные id.
    telegram_chat_id: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    # Внешний адрес самого приложения, если он есть: сообщение тогда даёт
    # ссылку прямо на страницу истории, а не только пересказывает её. Пусто —
    # ссылки просто нет, и это нормально для локального запуска.
    public_base_url: str = ""
    notify_timeout_seconds: float = 15.0

    # Что именно отправлять: шаблоны через запятую по словарю app/notify/events.py
    # (`dex.filled`, `grid.order_filled`, `grid.recovery_*`, `*` — всё подряд).
    # По умолчанию — то, что двигало деньги, и то, что требует человека.
    notify_events: str = (
        "dex.filled,dex.failed,dex.missed,dex.expired,dex.cancelled,"
        "grid.order_filled,grid.order_cancel_refused,grid.grid_budget_blocked,"
        "grid.recovery_*,grid.trailing_buy_*,grid.recommendation_created"
    )
    notify_poll_seconds: float = 3.0
    notify_batch: int = 20
    # Telegram принимает около 20 сообщений в минуту в одну группу; пауза
    # между отправками держит серию исполнений внутри лимита, не дожидаясь 429.
    notify_send_pause_seconds: float = 0.4
    # После этого числа неудач сообщение помечается FAILED и больше не мешает
    # очереди: недоставленное уведомление не повод останавливать торговлю.
    notify_max_attempts: int = 6


settings = Settings()
