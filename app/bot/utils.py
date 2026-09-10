"""Small helpers for dealing with aiogram's optional/union callback types.

`CallbackQuery.message` is typed as `Message | InaccessibleMessage | None`
because Telegram may report a message as inaccessible (e.g. older than 48h).
These helpers centralize the narrowing/fallback logic instead of repeating
asserts across every handler.
"""

from __future__ import annotations

from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


async def cb_answer(
    callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Send a new message in the callback's chat, regardless of whether the
    original message is still editable/accessible.
    """
    if callback.message is None or callback.bot is None:
        return
    await callback.bot.send_message(
        callback.message.chat.id, text, reply_markup=reply_markup
    )


async def cb_edit_or_answer(
    callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Edit the original message in place when possible, otherwise send a new one."""
    message = callback.message
    if message is None or callback.bot is None:
        return
    if isinstance(message, Message):
        await message.edit_text(text, reply_markup=reply_markup)
    else:
        await callback.bot.send_message(message.chat.id, text, reply_markup=reply_markup)


def require_callback_data(callback: CallbackQuery) -> str:
    assert callback.data is not None
    return callback.data
