"""Application entrypoint: starts the Telegram bot in long-polling mode."""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import build_root_router
from app.bot.middlewares.db import DbSessionMiddleware
from app.config import settings
from app.database.session import init_db
from app.logging import get_logger, setup_logging

logger = get_logger(__name__)


async def run() -> None:
    setup_logging()

    if not settings.bot_token:
        raise RuntimeError(
            "BOT_TOKEN is not configured. Copy .env.example to .env and set BOT_TOKEN."
        )

    logger.info("Starting LuckyNum bot (payment_provider=%s)", settings.payment_provider)

    await init_db()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.update.middleware(DbSessionMiddleware())
    dispatcher.include_router(build_root_router())

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        logger.info("LuckyNum bot stopped")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Interrupted, shutting down")


if __name__ == "__main__":
    main()
