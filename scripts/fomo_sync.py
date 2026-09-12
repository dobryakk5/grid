"""Local browser-assisted name sync. Run `make fomo-token` or --help."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx

from app.fomo.browser import (
    MAX_SWAPS_PER_TRADER, BrowserCollector, BrowserSyncError, FOMO_ORIGIN,
    import_activity, check_api, validate_base,
)


def arguments():
    parser = argparse.ArgumentParser(description="Имена FOMO через браузер, без копирования куки/JWT")
    parser.add_argument("--base", default=os.environ.get("FOMO_API_BASE", "http://127.0.0.1:8000"))
    parser.add_argument("--profile", type=Path, default=ROOT / ".fomo-browser")
    parser.add_argument("--browser", choices=["chrome", "chromium"], default="chrome")
    parser.add_argument("--login-timeout", type=int, default=300)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--max-swaps", type=int, default=MAX_SWAPS_PER_TRADER,
                        metavar="N", help="потолок истории на трейдера (не цель)")
    parser.add_argument("--period", choices=["24h", "7d", "30d"], default="30d")
    parser.add_argument("--watch", type=int, default=0, metavar="SECONDS",
                        help="повторять сбор, минимум 300 секунд; 0 = один проход")
    parser.add_argument("--dry-run", action="store_true", help="проверить сбор без записи в Grid")
    args = parser.parse_args()
    if not 1 <= args.limit <= 500 or args.login_timeout < 1 or (args.watch and args.watch < 300):
        parser.error("limit: 1..500; login-timeout > 0; watch: 0 или >= 300")
    if not 1 <= args.max_swaps <= MAX_SWAPS_PER_TRADER:
        parser.error(f"max-swaps: 1..{MAX_SWAPS_PER_TRADER}")
    return args


async def run(args):
    base = validate_base(args.base)
    async with httpx.AsyncClient(timeout=30) as http:
        database = await check_api(http, base)
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise BrowserSyncError("Установите сборщик: make fomo-browser-install") from None
        args.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        args.profile.chmod(0o700)
        async with async_playwright() as playwright:
            try:
                context = await playwright.chromium.launch_persistent_context(
                    str(args.profile.resolve()), headless=False,
                    channel="chrome" if args.browser == "chrome" else None,
                    chromium_sandbox=True,
                )
            except Exception:
                raise BrowserSyncError(
                    "Не удалось открыть браузер. Закройте другой сборщик с тем же профилем. "
                    "Для Chromium: .venv/bin/python -m playwright install chromium; "
                    "затем добавьте --browser chromium."
                ) from None
            collector = None
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                collector = BrowserCollector(page)
                collector.listen()
                print(f"API: {base}; топ-{args.limit} за {args.period}; все сети", flush=True)
                # The server's database, not this shell's .env: the two diverge
                # the moment uvicorn is started with its own DATABASE_URL.
                print(f"Пишу в БД: {database}", flush=True)
                print("Войдите в FOMO в открытом окне. Сайт сохранит сессию в отдельном профиле браузера.", flush=True)
                await page.goto(FOMO_ORIGIN, wait_until="domcontentloaded")
                await collector.wait_session(args.login_timeout)
                while True:
                    payload = await collector.collect(period=args.period, limit=args.limit,
                                                      max_swaps=args.max_swaps)
                    if args.dry_run:
                        from app.api.fomo_activity import ActivityImport, prepare_import
                        legs, coverage = prepare_import(ActivityImport.model_validate(payload))
                        print(f"Проверка: {len(legs)} сторон swaps; диагностика: {coverage}. Без записи в Grid.", flush=True)
                    else:
                        result = await import_activity(http, base, payload)
                        print(f"Импортировано трейдеров: {result['traders']}; "
                              f"сторон swaps: {result['swap_legs']}; "
                              f"названий монет добавлено: {result.get('named_tokens', 0)}; "
                              f"БД: {result.get('database', database)}", flush=True)
                        if not result["coverage"]["history_complete"]:
                            print("История неполная: API вернул ограниченную выборку. Покрытие показано на /fomo.", flush=True)
                    print(f"Агрегаты: {base}/fomo", flush=True)
                    if not args.watch:
                        return
                    print(f"Следующий проход через {args.watch} секунд. Ctrl+C — завершить.", flush=True)
                    await asyncio.sleep(args.watch)
                    database = await check_api(http, base)
                    collector._ready.clear()
                    await page.reload(wait_until="domcontentloaded")
                    await collector.wait_session(args.login_timeout)
            finally:
                if collector:
                    await collector.close()
                await context.close()


def main():
    try:
        asyncio.run(run(arguments()))
    except BrowserSyncError as exc:
        print(f"FOMO: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Browser exceptions may include headers or full response bodies.
        print("FOMO: браузер закрылся или запрос не завершился. Повторите запуск.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
