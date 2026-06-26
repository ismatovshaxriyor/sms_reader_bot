"""Entry point: starts the Telegram bot and RingCentral listener in parallel."""

import asyncio
import logging

from app import config, db
from app.bot import create_bot, forward_sms, request_authorization
from app.gmail_listener import run_gmail_listener

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sms_reader_bot")


async def main() -> None:
    await db.init_db()
    logger.info("Database ready: %s", config.DATABASE_PATH)

    if not config.ADMIN_IDS:
        logger.warning(
            "ADMIN_IDS is empty! No one can access bot commands. Fill in .env."
        )

    bot, dp = create_bot()
    me = await bot.get_me()
    logger.info("Telegram bot started: @%s", me.username)

    async def _on_sms(sms: dict) -> None:
        sent = await forward_sms(sms)
        logger.info("SMS forwarded to %d group(s).", sent)

    async def _on_auth_needed(reason: str) -> None:
        await request_authorization(list(config.ADMIN_IDS), reason)

    polling_task = asyncio.create_task(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
        name="telegram-polling",
    )
    gmail_task = asyncio.create_task(
        run_gmail_listener(_on_sms, _on_auth_needed),
        name="gmail-listener",
    )

    try:
        await asyncio.gather(polling_task, gmail_task)
    finally:
        for task in (polling_task, gmail_task):
            task.cancel()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger("sms_reader_bot").info("Stopped.")
