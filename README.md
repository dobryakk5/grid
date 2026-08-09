# Mini Grid Bot v0.2

Простой **Spot Grid Bot**: FastAPI + PostgreSQL + отдельный worker + Bybit Demo + web-интерфейс.

> Пока использовать только Bybit Demo. Это MVP, а не production trading infrastructure.

## Что изменилось в v0.2

- несколько независимых **grid-профилей**;
- диапазон задаётся абсолютными ценами;
- шаг задаётся в USDT, а не в процентах;
- web-интерфейс на `/`;
- создание и редактирование профилей;
- Start / Stop каждого профиля отдельно;
- просмотр активных заявок и истории BUY / SELL по каждому профилю.

## Пример профиля

```text
Название:       BTC 62–67k
Пара:           BTCUSDT
Нижняя цена:    62000
Верхняя цена:   67000
Шаг:            1000 USDT
На один BUY:    25 USDT
```

Ценовые линии:

```text
62000  63000  64000  65000  66000  67000
```

Торговые ячейки:

```text
BUY 62000 -> SELL 63000 -> BUY 62000 -> ...
BUY 63000 -> SELL 64000 -> BUY 63000 -> ...
BUY 64000 -> SELL 65000 -> BUY 64000 -> ...
BUY 65000 -> SELL 66000 -> BUY 65000 -> ...
BUY 66000 -> SELL 67000 -> BUY 66000 -> ...
```

Верхняя граница `67000` не является BUY-уровнем: она нужна как последний SELL-уровень.

При старте Spot-профиль выставляет только пассивные BUY ниже текущей рыночной цены. Он не шортит и не предполагает наличие BTC для первоначальных SELL.

## Архитектура

```text
Browser
   |
   v
FastAPI :8000  <------> PostgreSQL
   ^                         ^
   |                         |
   +---------------- Grid worker
                            |
                            v
                       Bybit Demo API
```

## Запуск

```bash
cp .env.example .env
```

В `.env`:

```env
BYBIT_API_KEY=...
BYBIT_API_SECRET=...
BYBIT_BASE_URL=https://api-demo.bybit.com
```

Затем:

```bash
docker compose up -d --build
```

Открыть:

```text
http://127.0.0.1:8000/
```

Swagger:

```text
http://127.0.0.1:8000/docs
```

Логи worker:

```bash
docker compose logs -f worker
```

## Важно при обновлении с v0.1

Схема БД изменилась. В этом мини-репо Alembic ещё не добавлен, поэтому для старой тестовой БД проще пересоздать volume:

```bash
docker compose down -v
docker compose up -d --build
```

Это удалит локальные тестовые данные PostgreSQL.

## API

```text
GET  /api/profiles
POST /api/profiles
PUT  /api/profiles/{id}
POST /api/profiles/{id}/start
POST /api/profiles/{id}/stop
GET  /api/profiles/{id}/orders

GET  /api/price/BTCUSDT
GET  /api/balance
POST /api/demo/funds
```

## Что происходит при Stop

`Stop` выставляет `enabled=false`. Worker на следующем цикле отменяет отслеживаемые активные заявки этого профиля. История остаётся в PostgreSQL.

Редактирование профиля разрешено только после остановки и отмены его открытых заявок.

## Ограничения MVP

Перед реальными деньгами нужны как минимум:

- reconciliation всех открытых ордеров Bybit после рестарта;
- private WebSocket для order/execution stream вместо polling;
- идемпотентное восстановление после ситуации «Bybit принял ордер, PostgreSQL ещё не записал»;
- хранение фактических комиссий и расчёт net PnL;
- лимиты общего капитала и дневного убытка;
- authentication web/API;
- шифрование API credentials;
- Alembic migrations;
- алерты и kill switch.

## Тесты

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```
