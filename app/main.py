"""Application entrypoint: starts the Telegram bot in long-polling mode."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Callable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.bot.error_handler import register_error_handler
from app.bot.handlers import build_root_router
from app.bot.middlewares.db import DbSessionMiddleware
from app.config import settings
from app.database.schema import SchemaOutOfDateError
from app.database.session import async_session_factory, init_db
from app.engagement.automation import WeeklyAutomationService
from app.engagement.channel_check import check_promo_channel
from app.engagement.periods import app_timezone, parse_schedule_time
from app.engagement.scheduler import WeeklyScheduler
from app.logging import get_logger, setup_logging
from app.single_instance import AlreadyRunningError, SingleInstance

logger = get_logger(__name__)

# Keeps scheduled stop tasks referenced until they finish.
_shutdown_tasks: set[asyncio.Task[None]] = set()


async def _stop_polling(dispatcher: Dispatcher) -> None:
    with contextlib.suppress(RuntimeError):  # polling not running (not started yet / already stopped)
        await dispatcher.stop_polling()


def install_graceful_shutdown(dispatcher: Dispatcher) -> Callable[[], None]:
    """Make Ctrl+C stop polling through aiogram's own graceful path.

    aiogram cannot register loop signal handlers on Windows, so Ctrl+C used to
    reach asyncio's default handler, which cancels the main task. aiogram then
    closed the HTTP session while its polling task was still inside
    getUpdates, logging "Failed to fetch updates - TelegramNetworkError ...
    ServerDisconnectedError" and leaving that task running until the loop was
    torn down. This handler asks the dispatcher to stop instead: the polling
    task is cancelled and awaited first, then the session is closed.

    A second Ctrl+C restores the previous handler, so it still interrupts
    immediately if a graceful stop hangs. Returns a function that restores
    the previous handler. On POSIX aiogram installs its own loop handlers
    while polling, which take precedence and do the same thing.
    """
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)

    def request_stop(signum: int, frame: object) -> None:
        signal.signal(signal.SIGINT, previous)
        logger.info("Shutdown requested; stopping polling (press Ctrl+C again to force)")

        def schedule() -> None:
            task = loop.create_task(_stop_polling(dispatcher))
            _shutdown_tasks.add(task)
            task.add_done_callback(_shutdown_tasks.discard)

        loop.call_soon_threadsafe(schedule)

    def restore() -> None:
        signal.signal(signal.SIGINT, previous)

    signal.signal(signal.SIGINT, request_stop)
    return restore


async def run() -> None:
    setup_logging()

    if not settings.bot_token:
        raise RuntimeError(
            "BOT_TOKEN is not configured. Copy .env.example to .env and set BOT_TOKEN."
        )

    # Before the database, the bot and the scheduler: a second instance must
    # not touch production state or Telegram at all (see app/single_instance.py).
    with SingleInstance() as instance:
        logger.info("Single-instance lock acquired (%s)", instance.path)
        await _run_bot()


async def _run_bot() -> None:
    logger.info("Starting LuckyNum bot (payment_provider=%s)", settings.payment_provider)

    # Fail at startup, not on Monday, if the week definition is misconfigured.
    app_timezone()
    parse_schedule_time(settings.weekly_schedule_time)

    await init_db()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Populates Telegram's native "/" command menu so /start and /help (the
    # bot's escape hatches out of any inline-keyboard flow) are always
    # visible, not just mentioned in message text. Runs once per process
    # startup — set_my_commands is a declarative "this is the current list"
    # call, so calling it again on every restart is naturally idempotent,
    # not additive.
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Начать заново"),
            BotCommand(command="analyze", description="Проверить купюру"),
            BotCommand(command="birthdate", description="Дата рождения"),
            BotCommand(command="history", description="Прошлые разборы"),
            BotCommand(command="stats", description="Моя статистика"),
            BotCommand(command="top", description="Топ исследователей"),
            BotCommand(command="promo", description="Ввести промокод"),
            BotCommand(command="about", description="О проекте"),
            BotCommand(command="help", description="Помощь"),
        ]
    )
    dispatcher = Dispatcher()
    dispatcher.update.middleware(DbSessionMiddleware())
    dispatcher.include_router(build_root_router())
    register_error_handler(dispatcher)

    scheduler: WeeklyScheduler | None = None
    if settings.weekly_automation_enabled:
        scheduler = WeeklyScheduler(WeeklyAutomationService(async_session_factory, bot))
    else:
        logger.info("Weekly automation disabled (WEEKLY_AUTOMATION_ENABLED=false)")
    if not settings.admin_telegram_ids:
        logger.info("No ADMIN_TELEGRAM_ID configured; admin panel is unavailable")

    restore_sigint = install_graceful_shutdown(dispatcher)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await check_promo_channel(bot)
        if scheduler is not None:
            scheduler.start()
        await dispatcher.start_polling(bot)
    finally:
        restore_sigint()
        if scheduler is not None:
            await scheduler.stop()
        await bot.session.close()
        logger.info("LuckyNum bot stopped")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Interrupted, shutting down")
    except SchemaOutOfDateError as exc:
        setup_logging()
        logger.error("%s", exc)
        raise SystemExit(1) from exc
    except AlreadyRunningError as exc:
        setup_logging()
        logger.error("%s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
