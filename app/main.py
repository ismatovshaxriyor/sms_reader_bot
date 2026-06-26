"""Kirish nuqtasi: Telegram botni va RingCentral tinglovchisini parallel ishga tushiradi."""

import asyncio
import logging

from app import config, db
from app.bot import create_bot, forward_sms
from app.gmail_listener import run_gmail_listener

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sms_reader_bot")


async def main() -> None:
    await db.init_db()
    logger.info("Ma'lumotlar bazasi tayyor: %s", config.DATABASE_PATH)

    if not config.ADMIN_IDS:
        logger.warning(
            "ADMIN_IDS bo'sh! Bot komandalariga hech kim kira olmaydi. .env'ni to'ldiring."
        )

    bot, dp = create_bot()
    me = await bot.get_me()
    logger.info("Telegram bot ishga tushdi: @%s", me.username)

    async def _on_sms(sms: dict) -> None:
        sent = await forward_sms(sms)
        logger.info("SMS %d ta guruhga uzatildi.", sent)

    polling_task = asyncio.create_task(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
        name="telegram-polling",
    )
    gmail_task = asyncio.create_task(
        run_gmail_listener(_on_sms),
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
        logging.getLogger("sms_reader_bot").info("To'xtatildi.")
