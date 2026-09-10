"""Tests for Telegram-layer glue that has no other test coverage: the
catch-all fallback handler and the global error handler. These use aiogram
objects directly with mocked I/O — no real Telegram connection.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, ErrorEvent, Message, Update

from app.bot import texts
from app.bot.error_handler import handle_unexpected_error
from app.bot.handlers import analyze as analyze_handlers
from app.bot.handlers.fallback import handle_unrecognized_message


def _make_message(chat_id: int = 555) -> Message:
    return Message(
        message_id=1,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=chat_id, type="private"),
    )


async def test_fallback_handler_responds_with_unknown_message() -> None:
    message = AsyncMock(wraps=_make_message())
    await handle_unrecognized_message(message)
    message.answer.assert_awaited_once_with(texts.UNKNOWN_MESSAGE)


async def test_error_handler_notifies_user_via_message_chat() -> None:
    bot = AsyncMock()
    update = Update(update_id=1, message=_make_message(chat_id=777))
    event = ErrorEvent(update=update, exception=RuntimeError("boom"))

    handled = await handle_unexpected_error(event, bot=bot)

    assert handled is True
    bot.send_message.assert_awaited_once_with(777, texts.GENERIC_ERROR)


async def test_error_handler_never_leaks_exception_details_to_user() -> None:
    """The user-facing message must be the generic constant, never the raw
    exception text (which could contain internal details).
    """
    bot = AsyncMock()
    update = Update(update_id=2, message=_make_message())
    event = ErrorEvent(update=update, exception=RuntimeError("db password is hunter2"))

    await handle_unexpected_error(event, bot=bot)

    sent_text = bot.send_message.call_args.args[1]
    assert "hunter2" not in sent_text
    assert sent_text == texts.GENERIC_ERROR


async def test_error_handler_swallows_notification_failure() -> None:
    """If even sending the friendly error message fails (e.g. bot blocked),
    the error handler itself must not raise.
    """
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("Forbidden: bot was blocked by the user")
    update = Update(update_id=3, message=_make_message())
    event = ErrorEvent(update=update, exception=RuntimeError("original failure"))

    handled = await handle_unexpected_error(event, bot=bot)
    assert handled is True


@pytest.mark.parametrize(
    "text,should_match",
    [
        ("2200373", True),
        ("не число", True),  # garbage text still routes to validation, not swallowed
        ("/history", False),
        ("/help", False),
        ("/about", False),
        ("/analyze", False),
    ],
)
def test_number_input_filter_excludes_slash_commands(text: str, should_match: bool) -> None:
    """Regression test: while the FSM is waiting for a serial number, a
    slash command (e.g. /history) must NOT be swallowed by the free-text
    handler and misreported as an invalid number — it must fall through so
    the real command handler (in a different router) can run instead.

    Reads the filters straight off the real registered handler in
    app.bot.handlers.analyze, so it fails if the source drifts.
    """
    handlers = analyze_handlers.router.message.handlers
    handler = next(h for h in handlers if h.callback.__name__ == "handle_number_input")
    magic_filters = [f.magic for f in handler.filters if f.magic is not None]
    assert magic_filters, "expected handle_number_input to carry magic filters"

    fake_message = SimpleNamespace(text=text)
    matched = all(mf.resolve(fake_message) for mf in magic_filters)
    assert matched is should_match


async def test_error_handler_noop_when_no_chat_available() -> None:
    bot = AsyncMock()
    update = Update(update_id=4)  # no message, no callback_query
    event = ErrorEvent(update=update, exception=RuntimeError("boom"))

    handled = await handle_unexpected_error(event, bot=bot)

    assert handled is True
    bot.send_message.assert_not_awaited()
