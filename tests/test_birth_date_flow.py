"""Telegram-flow tests for the date-of-birth step: when it is asked, how an
invalid date recovers, that skipping still delivers the serial-only product,
and that none of it can touch payments or OpenAI.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.models import AnalysisResult
from app.bot import texts
from app.bot.handlers.analyze import (
    cb_skip_birth_date,
    cmd_birthdate,
    handle_birth_date_input,
    handle_number_input,
)
from app.bot.states.analysis import AnalysisStates
from app.database import repositories as repo
from app.database.models import Analysis, Payment


class _FakeState:
    """Minimal FSMContext stand-in: real enough to assert on state and data."""

    def __init__(self) -> None:
        self.state: object | None = None
        self.data: dict = {}

    async def set_state(self, state) -> None:
        self.state = state

    async def update_data(self, data=None, **kwargs) -> dict:
        self.data.update(data or {})
        self.data.update(kwargs)
        return self.data

    async def get_data(self) -> dict:
        return dict(self.data)

    async def clear(self) -> None:
        self.state = None
        self.data = {}


def _message(text: str) -> AsyncMock:
    message = AsyncMock()
    message.text = text
    return message


def _sent(message: AsyncMock) -> list[str]:
    return [c.args[0] for c in message.answer.await_args_list]


async def test_serial_input_asks_for_birth_date_when_user_has_none(
    session: AsyncSession, monkeypatch
) -> None:
    user = await repo.get_or_create_user(session, telegram_id=901, username="u")
    await session.commit()
    assert user.birth_date is None

    state = _FakeState()
    message = _message("2200373")
    await handle_number_input(message, state, session, user)

    assert texts.ASK_BIRTH_DATE in _sent(message)
    assert state.state == AnalysisStates.waiting_for_birth_date
    # Nothing persisted yet — the analysis only runs once the date step ends.
    rows = (await session.execute(select(Analysis))).scalars().all()
    assert rows == []


async def test_serial_input_skips_the_question_when_date_already_known(
    session: AsyncSession,
) -> None:
    user = await repo.get_or_create_user(session, telegram_id=902, username="u")
    await repo.set_user_birth_date(session, user, dt.date(1990, 3, 7))
    await session.commit()

    state = _FakeState()
    message = _message("2200373")
    await handle_number_input(message, state, session, user)

    assert texts.ASK_BIRTH_DATE not in _sent(message)
    analysis = (await session.execute(select(Analysis))).scalars().one()
    result = AnalysisResult.model_validate(analysis.analysis_payload)
    assert result.birth_number == 2, "the remembered date must be applied automatically"


async def test_invalid_date_keeps_the_user_in_the_step_with_a_clear_message(
    session: AsyncSession,
) -> None:
    user = await repo.get_or_create_user(session, telegram_id=903, username="u")
    await session.commit()

    state = _FakeState()
    state.state = AnalysisStates.waiting_for_birth_date
    await state.update_data({"pending_number": "2200373"})

    await handle_birth_date_input(_message("31.02.2000"), state, session, user)

    await session.refresh(user)
    assert user.birth_date is None, "an invalid date must never be stored"
    assert state.state == AnalysisStates.waiting_for_birth_date
    assert (await session.execute(select(Analysis))).scalars().all() == []

    # ...and a correction immediately afterwards works.
    message = _message("07.03.1990")
    await handle_birth_date_input(message, state, session, user)
    await session.refresh(user)
    assert user.birth_date == dt.date(1990, 3, 7)
    analysis = (await session.execute(select(Analysis))).scalars().one()
    assert AnalysisResult.model_validate(analysis.analysis_payload).birth_number == 2


async def test_skipping_still_produces_a_full_serial_only_analysis(
    session: AsyncSession,
) -> None:
    user = await repo.get_or_create_user(session, telegram_id=904, username="u")
    await session.commit()

    state = _FakeState()
    state.state = AnalysisStates.waiting_for_birth_date
    await state.update_data({"pending_number": "2200373"})

    callback = AsyncMock()
    callback.data = "skip_birth_date"
    await cb_skip_birth_date(callback, state, session, user)

    await session.refresh(user)
    assert user.birth_date is None, "skipping must not store anything"
    analysis = (await session.execute(select(Analysis))).scalars().one()
    result = AnalysisResult.model_validate(analysis.analysis_payload)
    assert result.birth_number is None
    assert result.overall_score > 0


async def test_birthdate_command_sets_the_date_without_running_an_analysis(
    session: AsyncSession,
) -> None:
    user = await repo.get_or_create_user(session, telegram_id=905, username="u")
    await session.commit()

    state = _FakeState()
    await cmd_birthdate(_message("/birthdate"), state)
    assert state.state == AnalysisStates.waiting_for_birth_date

    await handle_birth_date_input(_message("07.03.1990"), state, session, user)

    await session.refresh(user)
    assert user.birth_date == dt.date(1990, 3, 7)
    assert (await session.execute(select(Analysis))).scalars().all() == [], (
        "/birthdate alone must not create an analysis"
    )


async def test_birth_date_step_never_creates_a_payment_or_calls_openai(
    session: AsyncSession, monkeypatch
) -> None:
    from app.ai import client as ai_client

    def fail_if_called(*args, **kwargs):
        raise AssertionError("the birth-date step must never touch OpenAI")

    monkeypatch.setattr(ai_client, "get_openai_client", fail_if_called)

    user = await repo.get_or_create_user(session, telegram_id=906, username="u")
    await session.commit()

    state = _FakeState()
    await handle_number_input(_message("2200373"), state, session, user)
    await handle_birth_date_input(_message("не дата"), state, session, user)
    await handle_birth_date_input(_message("07.03.1990"), state, session, user)

    assert (await session.execute(select(Payment))).scalars().all() == []


async def test_birth_date_is_never_written_into_the_event_log(
    session: AsyncSession,
) -> None:
    """Privacy: events record that an analysis was personalized, never the
    date behind it.
    """
    from app.database.models import Event

    user = await repo.get_or_create_user(session, telegram_id=907, username="u")
    await session.commit()

    state = _FakeState()
    await handle_number_input(_message("2200373"), state, session, user)
    await handle_birth_date_input(_message("07.03.1990"), state, session, user)

    events = (await session.execute(select(Event))).scalars().all()
    assert events
    for event in events:
        serialized = str(event.payload)
        for fragment in ("1990", "07.03", "1990-03-07"):
            assert fragment not in serialized


async def test_stale_skip_tap_outside_the_step_still_gets_a_reply(
    session: AsyncSession,
) -> None:
    """Inline keyboards remain tappable forever in Telegram. Tapping "Без
    даты рождения" on an old message — when the FSM has long since moved on
    — must still answer the callback and point the user somewhere, never
    leave the button spinning unanswered.
    """
    user = await repo.get_or_create_user(session, telegram_id=908, username="u")
    await session.commit()

    state = _FakeState()  # no state, no pending number: a stale tap
    callback = AsyncMock()
    callback.data = "skip_birth_date"

    await cb_skip_birth_date(callback, state, session, user)

    callback.answer.assert_awaited()
    sent = [c.args[1] for c in callback.bot.send_message.await_args_list]
    assert texts.ASK_NUMBER in sent
    assert state.state == AnalysisStates.waiting_for_number
