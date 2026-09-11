# Mini Grid Bot v0.8 — Bybit Demo и backtest, без Docker

Простой **Spot Grid Bot**: FastAPI + локальный PostgreSQL + отдельный worker + Bybit Demo / MEXC + web-интерфейс.

> Bybit Demo — это бумажная торговля. **MEXC — реальные деньги** (демо-режима для spot у MEXC нет). Это MVP, а не production trading infrastructure.

## Мультибиржевость (Bybit Demo + MEXC)

- у каждого профиля есть поле `exchange` (`bybit` | `mexc`), выбирается в форме профиля;
- worker поднимает по одному клиенту на биржу и маршрутизирует профиль по его `exchange`;
- `EXCHANGE` в `.env` — биржа по умолчанию для профилей без явного выбора и для
  общих эндпоинтов;
- ключи MEXC: `MEXC_API_KEY` / `MEXC_API_SECRET` (разрешение **Spot**), `MEXC_BASE_URL`
  (по умолчанию `https://api.mexc.com`); сервер должен быть в IP-whitelist ключа;
- сбор часовых свечей (backtest / анализ) по-прежнему идёт с публичного Bybit —
  MEXC используется только для торговли;
- клиент MEXC (`app/exchanges/mexc.py`) нормализует ответы под тот же контракт,
  что и Bybit (`app/exchanges/base.py`), поэтому движок не различает биржи;
- ограничение MEXC: у части аккаунтов размещение spot-ордеров через API включается
  отдельно — если MEXC отклоняет `POST /api/v3/order`, это видно в логах worker
  на каждом тике.

## Robinhood Chain / Uniswap (on-chain venue)

Третья площадка (`exchange=robinhood`) добавляется тем же способом, что и MEXC —
через `ExchangeClient`, поэтому `grid.py` не должен знать, что под ним DEX. Но на
DEX нет лимитных ордеров, поэтому `place_limit_order` там будет создавать
**synthetic limit order** (`dex_intents`) в Postgres, а исполнять его будет
отдельный DEX-worker.

Read-only слой (цена, свечи, риск-гейты):

- `app/dex/tokens.py` — реестр `symbol -> (адрес, decimals)`. Адрес PONS зашит,
  остальные (USDG, WETH, CASHCAT) задаются через `DEX_TOKENS` в `.env`; пока не
  задан — пара не резолвится с явной ошибкой, а не с выдуманным адресом;
- `app/dex/dexscreener.py` — цена пула и метрики здоровья, с TTL-кэшем (тик
  гридa идёт каждые 3 с, апстрим-вызов — раз в `DEXSCREENER_CACHE_SECONDS`).
  Цена берётся из пула **нашей** котировки, а ликвидность и объём суммируются по
  всем пулам токена;
- `app/dex/risk.py` — гейты: абсолютные пороги и падение относительно момента,
  когда уровень был взведён (цена дошла до уровня, потому что монета умирает →
  `BLOCKED`);
- `app/dex/intents.py` — state machine:
  `WAITING → TRIGGERED → QUOTED → SIGNING → SUBMITTING → PENDING → FILLED`
  плюс `BLOCKED` (не терминальный), `CANCELLED`, `EXPIRED`, `FAILED`. Переход
  `SIGNING → SUBMITTING` — дверь в одну сторону: дальше nonce сожжён и
  восстановление может только пере-broadcast'ить ту же транзакцию;
- `app/dex/candles.py` + `app/workers/dex_sampler.py` — на DEX нет klines,
  поэтому цена сэмплируется и сворачивается в 1m/15m/60m свечи `market_candles`,
  которые `RobinhoodClient.klines` отдаёт движку обратно. Объём остаётся `NULL`:
  DexScreener даёт только скользящие 24 ч, а `volume24h / 1440` — выдуманное
  число в той же колонке, где лежат настоящие.

Запуск сэмплера: `make dex-sampler` (или unit `mini-grid-dex-sampler.service`).

Проверить пару без торговли:

```bash
curl -s "http://127.0.0.1:8000/api/dex/PONSETH/snapshot" | python3 -m json.tool
```

`price_quote` оттуда — **индикативный** mid пула, а не цена исполнения: решение
«уровень достигнут» на следующих этапах принимает Uniswap quote на реальный
размер.

### Исполнение

- `app/dex/chain.py` — AsyncWeb3: проверка `chainId`, балансы, nonce, подпись,
  broadcast, receipt. Ожидание разделено: `receipt()` — один неблокирующий
  опрос для тика воркера, `wait_for_receipt()` — для ручного скрипта. Приватный
  ключ читается лениво, не логируется и никуда не сохраняется;
- `app/dex/uniswap.py` — `/quote` и `/swap` с `x-api-key` и закреплённым
  `x-universal-router-version: 2.1.1`. Маршрутизация ограничена V2/V3/V4, чтобы
  ответ был подписываемым `CLASSIC`; котировка с `permitData` отклоняется, а не
  отправляется без подписи; котировка старше `DEX_MAX_QUOTE_AGE_SECONDS` — тоже;
- `app/dex/receipts.py` — фактический fill считается как **дельта баланса
  кошелька** по каждому токену, а не поиском одного `Transfer`: маршрут
  `USDG → WETH → PONS` пишет цепочку логов, большая часть которых между пулами.
  Нативная сторона считается вне логов: вход — по `value` транзакции, выход — по
  балансу кошелька вокруг блока (`received = after - before + value + gas`), так
  как приходящий ETH тоже не пишет события. Это предполагает, что в том же блоке
  у кошелька не было других транзакций — ещё одна причина держать торговый
  кошелёк отдельным;
- `app/dex/pricing.py` — газ платится в ETH, который не является ни базой, ни
  котировкой пары PONS/USDG. Без конверсии он попадает в `unconverted_fees`
  ([pnl.py:56](app/trading/pnl.py:56)) и тихо исчезает из PnL. Курс берётся из
  пулов, которые мы и так читаем: DexScreener отдаёт базовый токен и в USD, и в
  котировке, поэтому `price_usd / price_quote` — это цена котируемого токена в
  долларах, а тот же расчёт по ETH-пулу даёт цену газа. Если пара уже
  котируется в ETH, курс равен 1 и запрос не делается;
- `app/dex/accounting.py` — перевод подтверждённого swap'а в поля
  `GridExecution`: `exec_fee` в котировке (её суммирует PnL), а исходные
  `fee_native_amount` / `fee_native_coin` остаются рядом. Хеш транзакции
  выступает `exec_id`. Чистая функция — считается без цепочки и без БД;
- `app/dex/execution.py` — порядок, ради которого всё это и делалось:

```text
quote → проверка лимита → build → sign → PERSIST → broadcast → receipt
```

  Хеш подписанной транзакции известен **до** broadcast, поэтому строка
  `SUBMITTING + nonce + tx_hash` коммитится до отправки. Упавший процесс находит
  её и либо спрашивает сеть, либо пере-broadcast'ит ту же транзакцию — но
  никогда не решает купить заново. Всё, что упало до коммита, не потратило
  nonce и ничего не отправило.

- `app/dex/approvals.py` — ERC-20 вход требует двух разрешений, а не одного:
  обычный `approve` на **Permit2** (один раз на токен) и EIP-712 подпись на
  каждый swap, с суммой, спендером и сроком. Поэтому первый approve по умолчанию
  безлимитный: постоянное разрешение выдаётся Permit2, а каждый реальный перевод
  всё равно гейтится свежей подписью с истечением. `DEX_APPROVE_EXACT=true`
  переключает на точную сумму ценой лишней транзакции на сделку.

  `permitData` из котировки подписывается только после проверки, что его
  `verifyingContract` совпадает с `PERMIT2_ADDRESS`, а `chainId` — с нашей
  сетью: иначе мы бы подписали разрешение контракту, которому ничего не выдавали.
  `EIP712Domain` из `types` вырезается — иначе eth_account не может определить
  primary type.

`DEX_DRY_RUN=true` по умолчанию: без явного выключения ничего не подписывается.

### Synthetic limit orders и воркер

`RobinhoodClient.place_limit_order()` ничего не отправляет в сеть: он пишет
уровень (`dex_intents`, статус `WAITING`) и сразу возвращает локальный
`orderId`. Исполняет уровень отдельный процесс:

```text
grid.py → RobinhoodClient → DexIntentRepository
                                   ↑
DexWorker → RiskGuard → Uniswap → ChainClient → ExecutionRecorder
```

- `app/dex/repository.py` — единственное место, читающее и пишущее
  `dex_intents`. **Ничего не коммитит сам**: границы транзакции принадлежат
  вызывающему, потому что от них зависит вся crash-safety — резервирование
  nonce и запись интента, который его потратит, обязаны попасть в один коммит,
  и этот коммит обязан случиться до broadcast;
- **менеджер nonce** берёт `SELECT … FOR UPDATE` по строке кошелька
  (`dex_wallets`) и выдаёт `max(next_nonce, pending_nonce с цепочки)`. БД —
  авторитет по «этот nonce мы уже потратили», цепочка — нижняя граница: так ни
  транзакция, отправленная мимо бота, ни БД, восстановленная из бэкапа, не
  заставят переиспользовать nonce;
- `app/dex/scheduling.py` — чистые решения «что делать с этим интентом»,
  отдельно от воркера, поэтому тестируются без цепочки и без БД. Главная
  асимметрия: неподписанный уровень свободно ждёт, перепроверяется и истекает;
  подписанный уже потратил nonce, и для него остаются только вопросы «долетел?»
  и «не пора ли заменить по тому же nonce»;
- `app/dex/recovery.py` — `settle` (есть receipt → фиксируем fill или revert),
  `rebroadcast` (тот же payload байт в байт; «already known» от ноды — это
  успех, а не ошибка) и `replace_stuck`.

**Почему застрявший swap не «разгоняется» газом.** Bump с новой calldata
исполнился бы по котировке, которую никто не перепроверял. Вместо этого по тому
же nonce отправляется нулевой перевод самому себе с повышенной комиссией: что бы
из двух ни легло, nonce потрачен ровно один раз и ни один swap не прошёл по
непроверенной цене. Уровень после этого переармируется **новой** строкой
(`parent_intent_id` хранит связь), а не откатом старой: интент — это одна
попытка под одним nonce, и перемотка подписанной строки — ровно та ошибка, ради
которой state machine и существует.

Каждый проход воркер сначала добивает подписанные интенты и только потом
смотрит на ждущие уровни: перезапустившийся воркер, который взвёл бы новую
попытку, пока старая транзакция ещё подтверждается, — это единственный баг,
ради которого вся конструкция и сделана.

```bash
make dex-worker
```

Отмена уровня возможна, пока он не подписан; после broadcast
`cancel_order` отказывает и говорит, что дальше только replace воркером.

Обе стороны идут через одну функцию: покупка тратит котировку и получает базу,
продажа наоборот, но лимит всегда в «котировка за базу» — покупка исполняется по
нему или ниже, продажа по нему или выше.

Живой прогон требует трёх отдельных действий, потому что тратит настоящие
деньги: `DEX_DRY_RUN=false`, `--execute` и `--confirm-live`. Без последнего
скрипт запрашивает котировку, печатает экран `LIVE ORDER` с худшим возможным
исполнением и останавливается. Котировка для подписи запрашивается заново после
подтверждения — та, которую человек только что прочитал, для подписи уже стара.

Ручной end-to-end прогон:

```bash
scripts/dex_swap.py --symbol PONSUSDG --side buy  --amount 10 --limit 0.55
scripts/dex_swap.py --symbol PONSUSDG --side sell --amount 20 --limit 0.62
scripts/dex_swap.py --symbol PONSETH  --side buy  --amount 0.0004 --limit 0.00026 \
    --execute --confirm-live
```

Торговая пара — `PONSUSDG`: уровень в долларах не уезжает вместе с ETH. `PONSETH`
годится как smoke-test исполнения, но не как сетка — там уровень выражен в ETH
за PONS, и движение самого ETH сдвигает все уровни относительно доллара. ETH на
бот-кошельке нужен под газ.

`--execute` дополнительно требует `DEX_DRY_RUN=false` в `.env` — одного флага
недостаточно. Для покупки за USDG approve на Permit2 отправляется автоматически
и дожидается receipt до swap'а; посмотреть или отозвать его:

```bash
scripts/dex_allowance.py --token USDG
scripts/dex_allowance.py --token USDG --revoke
```

Лимит проверяется по **исполнимой** цене из котировки на реальный размер, а не
по mid пула: котировка хуже лимита → `WAITING`, а не «купим по рынку».

Что ещё не сделано (по этапам): подключение к гриду → hardening (reorg,
kill switch, spend cap).

Известное место под этап 6: [grid.py:1267](app/trading/grid.py:1267) считает
quote-комиссией только `USDT`/`USDC`, а [grid.py:1235](app/trading/grid.py:1235)
не умеет отрезать `USDG` от символа. `accounting.py` уже отдаёт готовые поля, но
движок их пока не принимает.

## Что изменилось в v0.8

- основной сценарий создания профиля упрощён до ручной арифметической spot-grid;
- `Bybit Demo` используется как единственный режим paper trading;
- боевой API и локальная имитация исполнения заявок не подключены;
- две закрытые часовые свечи ниже/выше диапазона переводят профиль в
  `BREAK_DOWN`/`BREAK_UP`;
- действие для каждого направления настраивается в профиле: продолжать либо
  остановить профиль и снять заявки;
- по умолчанию `BREAK_DOWN` продолжает работу, а `BREAK_UP` останавливает профиль;
- для покупок ниже основной сетки задаётся отдельный нижний предел;
- нижние BUY можно отключить, а SELL для купленных там лотов настраивается
  независимо (по умолчанию BUY включены, SELL выключены — режим накопления BTC);
- расширение использует тот же фиксированный шаг и не переносит основной диапазон;
- кнопка `Backtest` слева от создания/сохранения профиля проверяет текущие
  настройки на последних 30 днях и показывает результат в модальном окне;
- backtest показывает realized/unrealized/total PnL, циклы, комиссии,
  max drawdown, время вне диапазона и BTC inventory;
- Hurst, ADX и автоматическое перестроение диапазона в MVP не добавлялись.

Backtest использует закрытия часовых свечей из PostgreSQL для пересечения grid-уровней.
Свечи BTCUSDT, ADAUSDT, XRPUSDT и SUIUSDT обновляются отдельным ежедневным
systemd timer, поэтому запуск backtest не обращается к Bybit.
`BREAK_DOWN`/`BREAK_UP` подтверждаются двумя часовыми закрытиями и выполняют
настроенное в профиле действие. Внутричасовые касания не учитываются, поэтому
оценка консервативна; комиссия по умолчанию принимается равной 0,1%.

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
deploy/systemd/mini-grid-market-data.service
deploy/systemd/mini-grid-market-data.timer
```

Установить:

```bash
sudo cp deploy/systemd/mini-grid-api.service /etc/systemd/system/
sudo cp deploy/systemd/mini-grid-worker.service /etc/systemd/system/
sudo cp deploy/systemd/mini-grid-market-data.service /etc/systemd/system/
sudo cp deploy/systemd/mini-grid-market-data.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mini-grid-api mini-grid-worker
sudo systemctl enable --now mini-grid-market-data.timer
```

Первичную загрузку истории за 365 дней выполнить сразу после установки:

```bash
sudo systemctl start mini-grid-market-data.service
```

Дальше timer запускает инкрементальную дозагрузку каждый день в 00:15 UTC
(с небольшим случайным сдвигом до пяти минут). Пропущенный запуск выполняется
после включения сервера благодаря `Persistent=true`.

Проверить:

```bash
systemctl status mini-grid-api
systemctl status mini-grid-worker
systemctl status mini-grid-market-data.timer
systemctl list-timers mini-grid-market-data.timer
```

Логи:

```bash
journalctl -u mini-grid-api -f
journalctl -u mini-grid-worker -f
journalctl -u mini-grid-market-data.service -f
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

Daily market-data timer ---> PostgreSQL
           |
           v
      Bybit public API

DEX sampler ---> PostgreSQL (dex_price_observations -> market_candles)
     |
     v
DexScreener API
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
# Single-symbol Grid analysis

`POST /api/grid-analysis` accepts `{"symbol":"XRPUSDT"}` and optionally a
matching `profile_id`. It calculates a 90-day market regime, builds no more than
eight candidates exclusively from the first 20 days of the optimization window,
and ranks the best two by their independent 10-day TEST score. The profile UI
shows a short result and `/analysis?symbol=XRPUSDT` opens the detailed report.
