"""Tests for /history and the history_view callback — re-opening a past
analysis must never leave the user with a dead-end message.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.engine import analyze
from app.bot import texts
from app.bot.handlers.history import cb_history_view
from app.bot.keyboards.main import after_report_kb, teaser_kb
from app.database import repositories as repo


def _make_callback(callback_data: str) -> AsyncMock:
    callback = AsyncMock()
    callback.data = callback_data
    return callback


async def test_history_view_of_a_paid_report_includes_a_way_to_check_another_number(
    session: AsyncSession,
) -> None:
    """Regression test: re-opening a past *paid, delivered* report from
    history previously sent the report text with no keyboard at all — a
    real dead end distinct from a fresh delivery (which always attaches
    after_report_kb). Must now match that behavior.
    """
    user = await repo.get_or_create_user(session, telegram_id=910, username="u")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await repo.mark_analysis_paid(session, analysis)
    await repo.save_report(session, analysis, "Готовый отчёт из истории.")
    await session.commit()

    callback = _make_callback(f"history_view:{analysis.id}")
    await cb_history_view(callback, session, user)

    callback.bot.send_message.assert_awaited_once()
    call = callback.bot.send_message.await_args
    assert call.args[1] == "Готовый отчёт из истории."
    assert call.kwargs["reply_markup"] == after_report_kb()


async def test_history_view_of_an_unpaid_analysis_shows_teaser_with_its_keyboard(
    session: AsyncSession,
) -> None:
    """Unpaid (or paid-but-not-yet-generated) analyses must still show the
    teaser with its own navigation, not a bare report.
    """
    user = await repo.get_or_create_user(session, telegram_id=911, username="u")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await session.commit()

    callback = _make_callback(f"history_view:{analysis.id}")
    await cb_history_view(callback, session, user)

    call = callback.bot.send_message.await_args
    assert call.kwargs["reply_markup"] == teaser_kb(analysis.id)


async def test_history_view_cannot_access_another_users_analysis(session: AsyncSession) -> None:
    owner = await repo.get_or_create_user(session, telegram_id=912, username="owner")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=owner.id, result=result)
    await session.commit()

    attacker = await repo.get_or_create_user(session, telegram_id=913, username="attacker")
    await session.commit()

    callback = _make_callback(f"history_view:{analysis.id}")
    await cb_history_view(callback, session, attacker)

    callback.answer.assert_any_call(texts.ANALYSIS_NOT_FOUND, show_alert=True)
    callback.bot.send_message.assert_not_awaited()
