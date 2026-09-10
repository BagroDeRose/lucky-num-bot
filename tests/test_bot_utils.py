"""Tests for app.bot.utils — the aiogram Optional/Union callback helpers.

Regression coverage for a real runtime failure: Telegram raises
TelegramBadRequest("message is not modified") when editing a message to
exactly the text/markup it already shows (e.g. tapping "О проекте" twice in
a row). That must be swallowed gracefully, not bubble up to the global
error handler as a logged error.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, Message
from aiogram.types import User as TgUser

from app.bot.utils import cb_answer, cb_edit_or_answer


def _make_message(chat_id: int = 555) -> Message:
    return Message(
        message_id=1,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=chat_id, type="private"),
    )


def _make_callback(message, bot: AsyncMock) -> CallbackQuery:
    callback = CallbackQuery(
        id="1",
        from_user=TgUser(id=1, is_bot=False, first_name="U"),
        chat_instance="x",
        data="whatever",
        message=message,
    )
    # CallbackQuery/Message are frozen pydantic models — `.as_(bot)` is
    # aiogram's own supported way to bind a Bot instance onto one.
    return callback.as_(bot)


def _not_modified_error() -> TelegramBadRequest:
    return TelegramBadRequest(
        method=EditMessageText(text="x"),
        message="Bad Request: message is not modified: specified new message content "
        "and reply markup are exactly the same as a current content and reply "
        "markup of the message",
    )


async def test_cb_edit_or_answer_swallows_message_not_modified() -> None:
    bot = AsyncMock()
    callback = _make_callback(_make_message(), bot)

    with patch.object(Message, "edit_text", new=AsyncMock(side_effect=_not_modified_error())):
        await cb_edit_or_answer(callback, "same text")  # must not raise

    bot.send_message.assert_not_awaited()


async def test_cb_edit_or_answer_reraises_other_bad_request() -> None:
    """Only the specific "not modified" case is swallowed — any other
    TelegramBadRequest is a genuine problem and must still surface.
    """
    bot = AsyncMock()
    callback = _make_callback(_make_message(), bot)

    other_error = TelegramBadRequest(
        method=EditMessageText(text="x"), message="Bad Request: message to edit not found"
    )

    with (
        patch.object(Message, "edit_text", new=AsyncMock(side_effect=other_error)),
        pytest.raises(TelegramBadRequest, match="message to edit not found"),
    ):
        await cb_edit_or_answer(callback, "new text")


async def test_cb_edit_or_answer_edits_successfully_when_content_differs() -> None:
    bot = AsyncMock()
    callback = _make_callback(_make_message(), bot)

    edit_mock = AsyncMock(return_value=None)
    with patch.object(Message, "edit_text", new=edit_mock):
        await cb_edit_or_answer(callback, "brand new text")

    edit_mock.assert_awaited_once()


async def test_cb_edit_or_answer_sends_new_message_when_inaccessible() -> None:
    bot = AsyncMock()
    inaccessible = InaccessibleMessage(chat=Chat(id=999, type="private"), message_id=1)
    callback = _make_callback(inaccessible, bot)

    await cb_edit_or_answer(callback, "hello")

    bot.send_message.assert_awaited_once_with(999, "hello", reply_markup=None)


async def test_cb_edit_or_answer_noop_when_no_message() -> None:
    bot = AsyncMock()
    callback = _make_callback(None, bot)

    await cb_edit_or_answer(callback, "hello")  # must not raise

    bot.send_message.assert_not_awaited()


async def test_cb_answer_sends_to_callback_chat() -> None:
    bot = AsyncMock()
    callback = _make_callback(_make_message(chat_id=42), bot)

    await cb_answer(callback, "hi there")

    bot.send_message.assert_awaited_once_with(42, "hi there", reply_markup=None)
