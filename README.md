# Mini Grid Bot

Minimal spot grid bot built with **FastAPI + PostgreSQL + a separate Python worker + Bybit Demo Trading**.

This is an educational MVP, not production trading infrastructure. Start only on Bybit Demo. The API has no authentication and is bound to `127.0.0.1` by Docker Compose.

## How the grid works

The bot does not short and does not need BTC to seed the grid.

Example: BTC = 100,000 USDT, step = 0.5%, levels = 3.

It seeds BUY orders roughly at:

- 99,500
- 99,000
- 98,500

When a BUY fills, the bot creates a SELL one grid step above that BUY. When that SELL fills, it creates a BUY one grid step below. This repeats while the bot is enabled.

A small configurable quantity buffer (`GRID_FEE_BUFFER_PCT`, default 0.2%) is applied when turning a filled BUY into a SELL so the bot does not try to sell more base asset than is available after fees.

## Architecture

```text
                 PostgreSQL
                    ^   ^
                    |   |
FastAPI <-----------+   +---------- Grid worker
  :8000                               |
                                      |
                                      v
                                 Bybit V5 API
```

FastAPI only changes configuration and exposes status. The worker is the only process that manages the grid.

## 1. Create Bybit Demo API keys

Use **Demo Trading**, not Testnet Demo:

1. Sign in to the normal Bybit account.
2. Switch to **Demo Trading**.
3. In the Demo account, create an API key with Spot trading permission.
4. Copy the key and secret into `.env`.

Bybit Demo REST endpoint used by this repo:

```text
https://api-demo.bybit.com
```

## 2. Configure

```bash
cp .env.example .env
nano .env
```

Required:

```env
BYBIT_API_KEY=...
BYBIT_API_SECRET=...
BYBIT_BASE_URL=https://api-demo.bybit.com
```

## 3. Start

```bash
docker compose up -d --build
```

Check:

```bash
curl http://127.0.0.1:8000/health
```

Swagger:

```text
http://127.0.0.1:8000/docs
```

## 4. Add demo USDT

The Bybit Demo API supports requesting demo funds. This repo wraps that endpoint:

```bash
curl -X POST http://127.0.0.1:8000/api/demo/funds \
  -H 'Content-Type: application/json' \
  -d '{"usdt":"10000"}'
```

Check balance:

```bash
curl http://127.0.0.1:8000/api/balance
```

## 5. Start the grid

For a first test, use small amounts:

```bash
curl -X POST http://127.0.0.1:8000/api/bot/start \
  -H 'Content-Type: application/json' \
  -d '{
    "symbol":"BTCUSDT",
    "levels":3,
    "step_pct":"0.005",
    "quote_per_level":"25"
  }'
```

Meaning:

- `levels=3` — three BUY levels below the current market;
- `step_pct=0.005` — 0.5% spacing;
- `quote_per_level=25` — approximately 25 USDT per initial BUY.

## 6. Watch it

```bash
docker compose logs -f worker
```

Status:

```bash
curl http://127.0.0.1:8000/api/bot/status
```

Price:

```bash
curl http://127.0.0.1:8000/api/price/BTCUSDT
```

## 7. Stop

```bash
curl -X POST http://127.0.0.1:8000/api/bot/stop
```

On the next worker tick the bot cancels its tracked open orders.

## Tables

`bot_state` stores the singleton bot configuration.

`grid_orders` stores every exchange order and the replacement chain:

```text
BUY 99,500 -> FILLED
       |
       +-> SELL 99,997.5 -> FILLED
                  |
                  +-> BUY ~99,500 -> ...
```

## Important MVP limitations

Before using real money, add at least:

- authentication for FastAPI;
- API key encryption / secret manager;
- private WebSocket order stream instead of polling;
- reconciliation against all exchange open orders after restarts;
- deterministic retry/idempotency handling for order placement;
- daily loss and total-exposure limits;
- a hard kill switch;
- fee accounting from execution history;
- alerting;
- migrations (Alembic);
- integration tests against Demo.

The largest technical gap is crash consistency: if Bybit accepts an order and the process dies before PostgreSQL records it, this MVP can temporarily have an exchange order unknown to the database. Do not move to production before implementing reconciliation/idempotency.

## Local tests

Without Docker:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```
