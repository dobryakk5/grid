# Mini Grid Bot v0.7 — последовательные заявки, без Docker

Простой **Spot Grid Bot**: FastAPI + локальный PostgreSQL + отдельный worker + Bybit Demo + web-интерфейс.

> Пока использовать только Bybit Demo. Это MVP, а не production trading infrastructure.

## Что изменилось в v0.7

- BUY-заявки выставляются последовательно: только ближайшая ступень, следующая — после прохождения предыдущей;
- старые профили с несколькими открытыми BUY автоматически сворачиваются до ближайшей заявки;
- активную неисполненную заявку можно отменить крестиком;
- ручная отмена запоминается и не восстанавливается worker на следующем tick.

Возможности v0.6 сохранены:

- добавлена DCA Grid стратегия со стартовой рыночной покупкой;
- выше середины диапазона используется осторожная доля бюджета (по умолчанию 20%), ниже — дополняющая доля (80%);
- остаток бюджета распределяется по линейной или геометрической BUY-лестнице;
- каждый купленный DCA-лот продаётся частями по линейной или геометрической SELL-лестнице;
- после полного исполнения SELL-лестницы бот повторно выставляет BUY лота на его исходной цене;
- слишком мелкие ступени автоматически объединяются до минимально допустимого Bybit ордера;
- создание и управление профилями перенесено на `/profiles/new`;
- главная страница показывает только названия работающих профилей;
- из шапки удалены техническое описание и сведения об API-ключе.

Возможности v0.5 сохранены:

- профили поддерживают накопительную и классическую Spot Grid стратегии;
- классическая стратегия создаёт стартовый BTC-инвентарь для SELL-ячеек выше рынка;
- добавлены арифметическая и геометрическая (процентная) сетки;
- добавлены лимит бюджета, Stop Loss и Take Profit;
- защиты останавливают профиль и отменяют заявки, но не продают BTC;
- существующие профили автоматически мигрируют в накопительную арифметическую стратегию.

Возможности v0.4 по PnL сохранены:

- добавлена таблица `grid_executions` с фактическими исполнениями Bybit;
- worker синхронизирует `execPrice`, `execQty`, `execValue`, `execFee`, `feeCurrency`;
- добавлен `/api/profiles/{id}/pnl`;
- в web-панели есть PnL по каждой grid-ячейке и итог по профилю;
- считаются завершённые циклы, оборот, gross profit, комиссии и net profit;
- комиссия в базовой монете (например BTC) переводится в quote-валюту по цене конкретного fill;
- старые заполненные ордера автоматически backfill-ятся из Bybit execution history;
- Docker по-прежнему не используется: PostgreSQL + `venv` + systemd.

## Как Bybit авторизует сервис

Сервису не нужен логин/пароль от сайта Bybit.

Нужны две строки:

```text
API Key
API Secret
```

Они хранятся только на сервере в `.env`:

```env
BYBIT_API_KEY=...
BYBIT_API_SECRET=...
BYBIT_BASE_URL=https://api-demo.bybit.com
```

Для каждого приватного REST-запроса `app/exchanges/bybit.py` автоматически делает HMAC-SHA256 подпись и отправляет заголовки Bybit V5:

```text
X-BAPI-API-KEY
X-BAPI-TIMESTAMP
X-BAPI-RECV-WINDOW
X-BAPI-SIGN
```

Secret по сети не отправляется — он используется локально только для вычисления подписи.

### Как создать ключ именно для Demo

1. Войти в обычный аккаунт Bybit (`bybit.com`).
2. Переключить аккаунт в **Demo Trading**.
3. В Demo Trading открыть профиль → **API**.
4. Создать системный API key.
5. Для этого grid-бота дать только торговое разрешение **Spot / SpotTrade**, режим **Read-Write**.
6. Не давать `Withdraw` и другие ненужные разрешения.
7. Желательно привязать API key к публичному IP сервера.
8. Скопировать API Key и API Secret в `.env`.

Для Demo используется:

```env
BYBIT_BASE_URL=https://api-demo.bybit.com
```

Не путать с Testnet `api-testnet.bybit.com`: ключи привязаны к своему окружению.

## Установка без Docker (Ubuntu/Debian)

### 1. PostgreSQL и Python

```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib python3 python3-venv python3-pip
```

Проверить PostgreSQL:

```bash
sudo systemctl enable --now postgresql
sudo systemctl status postgresql
```

### 2. Создать БД

Открыть psql:

```bash
sudo -u postgres psql
```

Выполнить:

```sql
CREATE USER grid WITH PASSWORD 'CHANGE_ME_STRONG_PASSWORD';
CREATE DATABASE grid OWNER grid;
\q
```

### 3. Установить приложение

Например:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin gridbot || true
sudo mkdir -p /opt/mini-grid-bot
sudo chown -R gridbot:gridbot /opt/mini-grid-bot
```

Скопировать файлы репозитория в `/opt/mini-grid-bot`, затем:

```bash
cd /opt/mini-grid-bot
sudo -u gridbot python3 -m venv .venv
sudo -u gridbot .venv/bin/pip install --upgrade pip
sudo -u gridbot .venv/bin/pip install -r requirements.txt
```

### 4. Настроить `.env`

```bash
cp .env.example .env
nano .env
chmod 600 .env
```

Пример:

```env
DATABASE_URL=postgresql+asyncpg://grid:CHANGE_ME_STRONG_PASSWORD@127.0.0.1:5432/grid

BYBIT_API_KEY=ВАШ_DEMO_API_KEY
BYBIT_API_SECRET=ВАШ_DEMO_API_SECRET
BYBIT_BASE_URL=https://api-demo.bybit.com

GRID_POLL_SECONDS=3
GRID_FEE_BUFFER_PCT=0.002
```

Владельцем `.env` должен быть пользователь сервиса:

```bash
sudo chown gridbot:gridbot .env
sudo chmod 600 .env
```

## Проверить авторизацию Bybit до запуска торговли

Из каталога проекта:

```bash
sudo -u gridbot .venv/bin/python scripts/check_bybit.py
```

Нормальный ответ:

```json
{
  "ok": true,
  "api_key": "abcd…wxyz",
  "read_only": false,
  "spot_permissions": ["SpotTrade"],
  "ips": ["203.0.113.10"],
  "uta": 1
}
```

Если `read_only=true` или в `spot_permissions` нет `SpotTrade`, бот не разрешит Start профиля.

## Ручной запуск без systemd

Терминал 1:

```bash
cd /opt/mini-grid-bot
./scripts/run-api.sh
```

Терминал 2:

```bash
cd /opt/mini-grid-bot
./scripts/run-worker.sh
```

Открыть на самом сервере:

```text
http://127.0.0.1:8000/
```

Если сервис стоит на удалённом сервере, пока web-панель без собственной авторизации лучше открыть через SSH tunnel:

```bash
ssh -L 8000:127.0.0.1:8000 user@server
```

После этого на своём компьютере:

```text
http://127.0.0.1:8000/
```

Swagger:

```text
http://127.0.0.1:8000/docs
```

Проверка ключа через HTTP:

```bash
curl -s http://127.0.0.1:8000/api/bybit/status | python3 -m json.tool
```

## Запуск как сервис через systemd

В репозитории уже есть:

```text
deploy/systemd/mini-grid-api.service
deploy/systemd/mini-grid-worker.service
```

Установить:

```bash
sudo cp deploy/systemd/mini-grid-api.service /etc/systemd/system/
sudo cp deploy/systemd/mini-grid-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mini-grid-api mini-grid-worker
```

Проверить:

```bash
systemctl status mini-grid-api
systemctl status mini-grid-worker
```

Логи:

```bash
journalctl -u mini-grid-api -f
journalctl -u mini-grid-worker -f
```

Перезапуск после обновления кода:

```bash
sudo systemctl restart mini-grid-api mini-grid-worker
```

## Web-интерфейс

На `/` можно:

- создавать несколько grid-профилей;
- задавать пару;
- нижнюю/верхнюю цену;
- абсолютный шаг в USDT;
- процентный шаг сетки;
- сумму USDT на одну покупку;
- стратегию профиля, лимит бюджета, Stop Loss и Take Profit;
- запускать/останавливать профиль;
- видеть активные и исполненные BUY/SELL;
- видеть статус Bybit API key;
- видеть прибыль по каждой grid-ячейке: циклы, оборот, gross, комиссии и net;
- видеть суммарный realised PnL по профилю.

Пример профиля:

```text
BTC 62–67k
BTCUSDT
62000 — 67000
шаг 1000
25 USDT на BUY
```

Торговые ячейки:

```text
BUY 62000 -> SELL 63000 -> BUY 62000 -> ...
BUY 63000 -> SELL 64000 -> BUY 63000 -> ...
BUY 64000 -> SELL 65000 -> BUY 64000 -> ...
BUY 65000 -> SELL 66000 -> BUY 65000 -> ...
BUY 66000 -> SELL 67000 -> BUY 66000 -> ...
```


## Как считается PnL

PnL считается только после завершённого цикла `BUY -> SELL`, по фактическим executions, а не по цене лимитной заявки.

Для каждой ячейки показываются:

```text
64000 -> 65000
циклов: 7
оборот: 3420 USDT
gross profit: +52.61 USDT
комиссии: 6.84 USDT
net profit: +45.77 USDT
```

Если SELL-количество немного меньше BUY-количества из-за fee buffer, стоимость покупки распределяется только на реально проданное количество. Остаток показывается как inventory/dust и не считается реализованным.

Если комиссия списана в quote-валюте (например USDT), она учитывается напрямую. Если комиссия списана в base-валюте (например BTC), она переводится в USDT по фактической цене конкретного исполнения. Если комиссия придёт в третьей валюте, web-панель отдельно покажет её как не переведённую и предупредит, что такой fee пока не включён в net.

При запуске v0.5 `init_db()` создаёт недостающие таблицы и безопасно добавляет новые nullable/default-колонки профиля через `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. Существующие заявки и история исполнений сохраняются.

## Архитектура

```text
Browser
   |
   v
FastAPI :8000  <------> PostgreSQL :5432
   ^                         ^
   |                         |
   +---------------- Grid worker
                            |
                            v
                       Bybit Demo API
```

API и worker — два отдельных Linux-процесса. Рестарт FastAPI не должен останавливать торговый worker.

## Безопасность ключа

- никогда не класть `.env` в git;
- `chmod 600 .env`;
- разрешить только Spot trading;
- не включать Withdrawal;
- привязать ключ к IP сервера;
- сначала работать только на Demo;
- web-панель не публиковать напрямую наружу без authentication/reverse proxy.

## API

```text
GET  /api/bybit/status
GET  /api/balance
GET  /api/price/BTCUSDT
POST /api/demo/funds

GET  /api/profiles
POST /api/profiles
PUT  /api/profiles/{id}
POST /api/profiles/{id}/start
POST /api/profiles/{id}/stop
GET  /api/profiles/{id}/orders
POST /api/profiles/{id}/orders/{order_id}/cancel
GET  /api/profiles/{id}/pnl
```

## Тесты

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

## Что ещё обязательно до реальных денег

- reconciliation всех открытых ордеров Bybit после рестарта;
- private WebSocket для order/execution stream вместо polling;
- идемпотентное восстановление после ситуации «Bybit принял ордер, PostgreSQL ещё не записал»;
- лимиты общего капитала и дневного убытка;
- authentication web/API;
- шифрование API credentials, если появятся несколько аккаунтов;
- Alembic migrations;
- алерты и kill switch.
