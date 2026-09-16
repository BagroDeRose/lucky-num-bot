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


async def test_history_view_of_a_legacy_analysis_without_birth_fields(
    session: AsyncSession,
) -> None:
    """Records created before the birth-date feature have no birth_* keys in
    their stored payload. Re-opening them from history must still render,
    exactly as a serial-only analysis — not crash on the missing fields.
    """
    from app.analysis.models import AnalysisResult

    user = await repo.get_or_create_user(session, telegram_id=914, username="u")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)

    # Rewrite the stored payload into the exact pre-feature shape.
    legacy_payload = dict(analysis.analysis_payload)
    for key in (
        "birth_number",
        "birth_number_meaning",
        "birth_resonance",
        "birth_digit_in_serial_count",
    ):
        legacy_payload.pop(key, None)
    analysis.analysis_payload = legacy_payload
    await session.commit()

    callback = _make_callback(f"history_view:{analysis.id}")
    await cb_history_view(callback, session, user)

    callback.bot.send_message.assert_awaited_once()
    sent = callback.bot.send_message.await_args.args[1]
    assert sent
    assert "число рождения" not in sent.lower()
    restored = AnalysisResult.model_validate(analysis.analysis_payload)
    assert restored.birth_number is None


async def test_setting_a_birth_date_later_does_not_rewrite_past_analyses(
    session: AsyncSession,
) -> None:
    """The date lives on the user, but each analysis keeps the derived
    numbers it was actually computed with — so history stays truthful.
    """
    import datetime as dt

    from app.analysis.models import AnalysisResult

    user = await repo.get_or_create_user(session, telegram_id=915, username="u")
    await session.flush()
    before = await repo.create_analysis(session, user_id=user.id, result=analyze("2200373"))
    await session.commit()
    original_payload = dict(before.analysis_payload)

    await repo.set_user_birth_date(session, user, dt.date(1990, 3, 7))
    await session.commit()

    await session.refresh(before)
    assert before.analysis_payload == original_payload
    assert AnalysisResult.model_validate(before.analysis_payload).birth_number is None

    # A new analysis for the same user now picks the date up.
    after = await repo.create_analysis(
        session, user_id=user.id, result=analyze("2200373", user.birth_date)
    )
    await session.commit()
    assert AnalysisResult.model_validate(after.analysis_payload).birth_number == 2


async def test_history_view_of_a_personalized_paid_report_is_unchanged(
    session: AsyncSession,
) -> None:
    import datetime as dt

    user = await repo.get_or_create_user(session, telegram_id=916, username="u")
    await session.flush()
    analysis = await repo.create_analysis(
        session, user_id=user.id, result=analyze("2200373", dt.date(1990, 3, 7))
    )
    await repo.mark_analysis_paid(session, analysis)
    await repo.save_report(session, analysis, "Персональный отчёт.")
    await session.commit()

    callback = _make_callback(f"history_view:{analysis.id}")
    await cb_history_view(callback, session, user)

    call = callback.bot.send_message.await_args
    assert call.args[1] == "Персональный отчёт."
    assert call.kwargs["reply_markup"] == after_report_kb()
