# Mini Grid Bot v0.3 — без Docker

Простой **Spot Grid Bot**: FastAPI + локальный PostgreSQL + отдельный worker + Bybit Demo + web-интерфейс.

> Пока использовать только Bybit Demo. Это MVP, а не production trading infrastructure.

## Что изменилось в v0.3

- Docker полностью удалён;
- PostgreSQL ожидается на `127.0.0.1:5432`;
- запуск через обычный Python `venv`;
- добавлены systemd units для API и worker;
- добавлен `/api/bybit/status`;
- web-панель показывает статус авторизации Bybit;
- перед Start профиля проверяются Bybit API key, `Read/Write` и разрешение `SpotTrade`.

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
- сумму USDT на одну покупку;
- запускать/останавливать профиль;
- видеть активные и исполненные BUY/SELL;
- видеть статус Bybit API key.

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
- учёт фактических комиссий и net PnL;
- лимиты общего капитала и дневного убытка;
- authentication web/API;
- шифрование API credentials, если появятся несколько аккаунтов;
- Alembic migrations;
- алерты и kill switch.
