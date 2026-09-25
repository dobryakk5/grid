"""Worker that drains the notification outbox into Telegram.

Its own process for the same reason the DEX worker is: the thing that sends
messages is paced by somebody else's rate limit, and a trading tick must never
be. Nothing here decides *whether* an operation is notable -- that was settled
at enqueue time, in the transaction that made the operation real (see
:mod:`app.notify.outbox`). This loop only delivers.

One row is one transaction. A crash mid-send re-delivers that one message on
restart, which is the trade this design accepts: a repeated fill notice is
noise, a missing one is a fill nobody heard about.
"""

import asyncio
import logging

from app.core.config import settings
from app.db.init import init_db
from app.db.session import SessionLocal
from app.dex.dynamic_tokens import load_dynamic_tokens
from app.notify.commands import CommandPoller
from app.notify.events import is_configured
from app.notify.outbox import claim, mark_failed, mark_sent
from app.notify.render import render
from app.notify.telegram import TelegramClient, TelegramError, TelegramRetry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def tick(session, client: TelegramClient) -> int:
    """Deliver everything currently due; returns how many were sent.

    One row per transaction rather than one batch per transaction: the row is
    locked across its own ``sendMessage`` call and released the moment it is
    resolved, so the pacing sleep between messages never holds a lock, and a
    slow Telegram cannot keep a transaction open over a whole batch.
    """
    sent = 0
    for _ in range(max(1, settings.notify_batch)):
        rows = await claim(session, limit=1)
        if not rows:
            await session.rollback()
            break
        row = rows[0]

        text = await render(session, row)
        if text is None:
            # Nothing left to describe -- the profile or the level is gone.
            mark_sent(row)
            logger.info("notification %s (%s) dropped: subject is gone", row.id, row.kind)
            await session.commit()
            continue

        delivered = False
        try:
            await client.send_message(row.chat_id, text)
        except TelegramRetry as exc:
            mark_failed(row, str(exc), retry_after=exc.retry_after)
            logger.warning("notification %s deferred: %s", row.id, exc)
        except TelegramError as exc:
            # Telegram understood and refused. Burn the attempt rather than
            # loop: the same bytes will be refused the same way.
            mark_failed(row, str(exc))
            logger.warning("notification %s refused: %s", row.id, exc)
        else:
            mark_sent(row)
            delivered = True
            sent += 1
        await session.commit()

        if delivered and settings.notify_send_pause_seconds > 0:
            await asyncio.sleep(settings.notify_send_pause_seconds)
    return sent


async def main() -> None:
    await init_db()
    if not is_configured():
        # Not an error: a deployment without a bot simply queues nothing, and
        # this loop would have nothing to do. Saying so once beats a silent
        # process someone later assumes is working.
        logger.warning(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set: nothing is queued or sent"
        )
    client = TelegramClient()
    commands = CommandPoller(client)
    logger.info("Notifier started (every %ss)", settings.notify_poll_seconds)

    try:
        while True:
            try:
                # A fill can name a token the tape verified on chain rather than
                # one pinned in the registry; without this the message would say
                # the pair key instead of the ticker.
                await load_dynamic_tokens(SessionLocal)
                async with SessionLocal() as session:
                    await tick(session, client)
            except Exception:
                logger.exception("Notifier tick failed")
            if commands is not None and is_configured():
                # Apart from the outbox, so that a bad command can never hold
                # up a fill notice, nor a stuck queue leave /open unanswered.
                try:
                    await commands.poll(SessionLocal)
                except TelegramRetry as exc:
                    logger.warning("Telegram commands deferred: %s", exc)
                except TelegramError as exc:
                    # Typically a 409: a webhook is set on this bot, and
                    # getUpdates cannot be used next to one. Asking again
                    # every few seconds would only fill the log.
                    logger.warning("Telegram commands disabled: %s", exc)
                    commands = None
                except Exception:
                    logger.exception("Telegram commands failed")
            await asyncio.sleep(settings.notify_poll_seconds)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
