"""Global fallback for exceptions raised inside handlers.

Without this, an unhandled exception anywhere in a handler (a DB error, a
Telegram API error, a bug) simply vanishes: aiogram logs it internally but
the user never gets a response. This registers a catch-all that logs with
full context (safe — no secrets ever flow through exception messages here)
and best-effort notifies the user with a friendly Russian message instead of
leaving them hanging.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent

from app.bot import texts
from app.logging import get_logger

logger = get_logger(__name__)


async def handle_unexpected_error(event: ErrorEvent, bot: Bot) -> bool:
    """aiogram auto-injects `bot` into the handler context; this is the same
    mechanism `session`/`user` rely on via DbSessionMiddleware.
    """
    logger.error(
        "Unhandled exception while processing update %s: %s",
        event.update.update_id,
        event.exception,
        exc_info=event.exception,
    )

    update = event.update
    chat_id: int | None = None
    if update.message is not None:
        chat_id = update.message.chat.id
    elif update.callback_query is not None and update.callback_query.message is not None:
        chat_id = update.callback_query.message.chat.id

    if chat_id is not None:
        try:
            await bot.send_message(chat_id, texts.GENERIC_ERROR)
        except Exception:  # noqa: BLE001 - best-effort notification only
            logger.warning("Failed to notify user %s about an internal error", chat_id)

    return True


def register_error_handler(dispatcher: Dispatcher) -> None:
    dispatcher.errors.register(handle_unexpected_error)
