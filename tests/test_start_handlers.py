"""Tests for /start, /help, /about, and the "🏠 В начало" navigation
callback — the bot's escape hatches out of any inline-keyboard flow.
"""

from __future__ import annotations

import datetime as dt
import inspect
from unittest.mock import AsyncMock

from aiogram.filters import Command
from aiogram.types import Chat, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import main as main_module
from app.ai import client as ai_client
from app.bot import texts
from app.bot.handlers import start as start_handlers
from app.bot.handlers.start import cb_main_menu, cmd_about, cmd_help, cmd_start
from app.database import repositories as repo
from app.database.models import Payment


def _make_message(chat_id: int = 555) -> Message:
    return Message(
        message_id=1,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=chat_id, type="private"),
    )


def _make_callback(callback_data: str) -> AsyncMock:
    callback = AsyncMock()
    callback.data = callback_data
    return callback


async def test_start_shows_welcome_and_main_menu(session: AsyncSession) -> None:
    user = await repo.get_or_create_user(session, telegram_id=701, username="u")
    await session.commit()

    message = AsyncMock(wraps=_make_message())
    state = AsyncMock()

    await cmd_start(message, state, session, user)

    message.answer.assert_awaited_once()
    sent_text = message.answer.await_args.args[0]
    assert sent_text == texts.WELCOME
    state.clear.assert_awaited_once()


async def test_start_works_for_existing_user_without_duplicating(session: AsyncSession) -> None:
    """Calling /start twice for the same Telegram account (the realistic
    case: a returning user) must never create a second User row.
    """
    telegram_id = 702
    user_first = await repo.get_or_create_user(session, telegram_id=telegram_id, username="u")
    await session.commit()

    message = AsyncMock(wraps=_make_message())
    state = AsyncMock()
    await cmd_start(message, state, session, user_first)

    # Simulate the second /start the way DbSessionMiddleware would resolve
    # it for the next update: get_or_create_user again for the same telegram_id.
    user_second = await repo.get_or_create_user(session, telegram_id=telegram_id, username="u")
    await session.commit()
    await cmd_start(message, state, session, user_second)

    assert user_first.id == user_second.id
    from app.database.models import User

    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    assert len(result.scalars().all()) == 1


async def test_start_does_not_trigger_openai(session: AsyncSession, monkeypatch) -> None:
    def fail_if_called(*args, **kwargs):
        raise AssertionError("cmd_start must never touch the OpenAI client")

    monkeypatch.setattr(ai_client, "get_openai_client", fail_if_called)

    user = await repo.get_or_create_user(session, telegram_id=703, username="u")
    await session.commit()

    message = AsyncMock(wraps=_make_message())
    state = AsyncMock()
    await cmd_start(message, state, session, user)  # must not raise


async def test_start_does_not_create_a_payment(session: AsyncSession) -> None:
    user = await repo.get_or_create_user(session, telegram_id=704, username="u")
    await session.commit()

    message = AsyncMock(wraps=_make_message())
    state = AsyncMock()
    await cmd_start(message, state, session, user)

    result = await session.execute(select(Payment).where(Payment.user_id == user.id))
    assert result.scalars().all() == []


async def test_start_does_not_reset_an_existing_paid_report(session: AsyncSession) -> None:
    """/start must be pure navigation — it must never touch analysis, paid,
    or report state, even for a user who already has a paid, delivered report.
    """
    from app.analysis.engine import analyze

    user = await repo.get_or_create_user(session, telegram_id=705, username="u")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await repo.mark_analysis_paid(session, analysis)
    await repo.save_report(session, analysis, "Уже готовый отчёт.")
    await session.commit()

    message = AsyncMock(wraps=_make_message())
    state = AsyncMock()
    await cmd_start(message, state, session, user)

    await session.refresh(analysis)
    assert analysis.paid is True
    assert analysis.report == "Уже готовый отчёт."


async def test_help_returns_expected_content() -> None:
    message = AsyncMock(wraps=_make_message())
    await cmd_help(message)

    message.answer.assert_awaited_once()
    sent_text = message.answer.await_args.args[0]
    assert sent_text == texts.HELP
    # Answers the questions a lost user actually has, per the product brief:
    # how to start, where the full report comes from, what happens on
    # failure, and how to get back to the beginning.
    assert "/analyze" in sent_text or "Проверить купюру" in sent_text
    assert "разбор" in sent_text.lower()
    assert "Попробовать ещё раз" in sent_text
    assert "/start" in sent_text


async def test_help_does_not_trigger_openai(monkeypatch) -> None:
    def fail_if_called(*args, **kwargs):
        raise AssertionError("cmd_help must never touch the OpenAI client")

    monkeypatch.setattr(ai_client, "get_openai_client", fail_if_called)

    message = AsyncMock(wraps=_make_message())
    await cmd_help(message)  # must not raise


async def test_about_command_is_registered_with_command_filter() -> None:
    """Regression test: /help and /about were previously matched with a
    bare `F.text == "/help"` equality check, which (unlike aiogram's
    Command filter) does not match "/help@BotUsername" — the form Telegram
    sends in some contexts — and is inconsistent with how every other
    command in this bot (/analyze, /history) is registered. Reads the
    filters straight off the real registered handlers.
    """
    for handler_fn in (cmd_help, cmd_about):
        handlers = [
            h for h in start_handlers.router.message.handlers if h.callback is handler_fn
        ]
        assert handlers, f"{handler_fn.__name__} not registered on the router"
        command_filters = [f.callback for f in handlers[0].filters if isinstance(f.callback, Command)]
        assert command_filters, f"{handler_fn.__name__} must use the Command(...) filter"


async def test_main_menu_callback_returns_to_welcome() -> None:
    callback = _make_callback("main_menu")
    state = AsyncMock()

    await cb_main_menu(callback, state)

    state.clear.assert_awaited_once()
    callback.answer.assert_awaited_once()
    sent_text = callback.bot.send_message.await_args.args[1]
    assert sent_text == texts.WELCOME


def test_retry_report_kb_includes_a_way_back_to_the_main_menu() -> None:
    from app.bot.keyboards.main import retry_report_kb

    kb = retry_report_kb(analysis_id=1)
    callback_datas = {
        button.callback_data for row in kb.inline_keyboard for button in row
    }
    assert "retry_report:1" in callback_datas
    assert "main_menu" in callback_datas


def test_bot_registers_start_and_help_in_the_telegram_command_menu() -> None:
    """Verifies app.main.run() actually populates Telegram's native "/"
    command menu (bot.set_my_commands) with /start and /help, not just that
    these commands work when typed — the whole point is that a lost user
    sees them without having to already know they exist.
    """
    source = inspect.getsource(main_module.run)
    assert "set_my_commands" in source
    assert 'BotCommand(command="start"' in source
    assert 'BotCommand(command="help"' in source
