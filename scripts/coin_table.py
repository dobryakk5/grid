"""Суточная таблица по монетам из `INTEL_WATCHLIST`.

Тот же вопрос, который иначе приходится собирать из новостей: какой flywheel
растёт, а какой затухает. Читается по сохранённым снимкам — по тем же строкам
`token_snapshots` и `token_holder_samples`, что пишет проход `app.intel.refresh`.

Без `--collect` скрипт **ничего не запрашивает**: он показывает то, что уже
собрано, со своими датами. С `--collect` сначала собираются ровно монеты списка
— рынок, имена и контракты, теми же функциями, что и в проходе воркера. Первый
запуск даст строку без единой дельты: для дельты нужны два замера, а второй
появится только на следующем проходе.

Usage::

    .venv/bin/python scripts/coin_table.py [--hours 24] [--collect] [--csv out.csv]
"""

import argparse
import asyncio
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select, tuple_  # noqa: E402
from sqlalchemy.orm import aliased  # noqa: E402

from app.api.fomo_activity import token_names  # noqa: E402
from app.db.models import TokenHolderSample, TokenSnapshot  # noqa: E402
from app.db.session import SessionLocal, database_target  # noqa: E402
from app.intel.daily import COLUMNS, HOURS, render, table_rows  # noqa: E402
from app.intel.refresh import collect  # noqa: E402
from app.intel.watchlist import watchlist  # noqa: E402

HOUR_MS = 3600_000


async def snapshots_at(session, keys, *, at_ms: int | None = None) -> dict:
    """Свежий снимок по каждой монете, при `at_ms` — свежий **не позже** этого.

    Не «ближайший к границе», а именно последний до неё: снимок, сделанный
    через двадцать минут после границы суточного окна, дал бы дельту за 23 часа
    под заголовком «за сутки».
    """
    if not keys:
        return {}
    query = select(TokenSnapshot).where(
        tuple_(TokenSnapshot.chain_id, TokenSnapshot.token_address).in_(keys))
    if at_ms is not None:
        query = query.where(TokenSnapshot.observed_at_ms <= at_ms)
    ranked = query.add_columns(func.row_number().over(
        partition_by=(TokenSnapshot.chain_id, TokenSnapshot.token_address),
        order_by=TokenSnapshot.observed_at_ms.desc(),
    ).label("position")).subquery()
    snapshot = aliased(TokenSnapshot, ranked)
    rows = (await session.execute(
        select(snapshot).where(ranked.c.position == 1))).scalars()
    return {(row.chain_id, row.token_address): row for row in rows}


async def holder_history(session, keys, *, since_ms: int) -> dict:
    if not keys:
        return {}
    rows = (await session.execute(select(TokenHolderSample).where(
        tuple_(TokenHolderSample.chain_id, TokenHolderSample.token_address).in_(keys),
        TokenHolderSample.observed_at_ms >= since_ms,
    ).order_by(TokenHolderSample.observed_at_ms))).scalars()
    history: dict[tuple[int, str], list] = {}
    for row in rows:
        history.setdefault((row.chain_id, row.token_address), []).append(row)
    return history


def write_csv(path: Path, rows) -> None:
    """Все поля строки, а не только напечатанные колонки.

    Таблица в терминале — это выжимка; файл, который копится изо дня в день,
    обрезать под ширину экрана незачем.
    """
    fields = list(rows[0]) if rows else [key for key, _title, _kind in COLUMNS]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fields})


async def main(hours: int, fetch: bool, csv_path: Path | None) -> int:
    keys, rejected = watchlist()
    for entry in rejected:
        print(f"не разобрал запись INTEL_WATCHLIST: {entry!r}", file=sys.stderr)
    if not keys:
        print("INTEL_WATCHLIST пуст: добавьте монеты как `solana:<mint>,4663:0x...` в .env",
              file=sys.stderr)
        return 1

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    async with SessionLocal() as session:
        if fetch:
            result = await collect(session, keys, now_ms=now_ms)
            print(f"собрано: снимков рынка {result['market']}, "
                  f"проверено контрактов {result['security']}", file=sys.stderr)
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        names = await token_names(session, keys)
        latest = await snapshots_at(session, keys)
        baseline = await snapshots_at(session, keys, at_ms=now_ms - hours * HOUR_MS)
        holders = await holder_history(session, keys, since_ms=now_ms - hours * 2 * HOUR_MS)

    # Опорный снимок для монеты, у которой он и есть самый свежий, — это та же
    # строка: сравнивать её с собой значит печатать «0.0%» там, где на самом
    # деле истории меньше окна.
    baseline = {key: row for key, row in baseline.items()
                if latest.get(key) is not None and row.observed_at_ms < latest[key].observed_at_ms}
    rows = table_rows(keys, names=names, snapshots=latest, baselines=baseline,
                      holders=holders, hours=hours)

    stamp = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"{stamp} · база {database_target()}")
    print(render(rows, hours=hours))
    if csv_path is not None:
        write_csv(csv_path, rows)
        print(f"\nCSV: {csv_path}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hours", type=int, default=HOURS,
                        help="окно, за которое считаются дельты (по умолчанию 24)")
    parser.add_argument("--collect", action="store_true",
                        help="сначала прогнать один проход сбора")
    parser.add_argument("--csv", type=Path, default=None, help="куда записать полную строку")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.hours, args.collect, args.csv)))
