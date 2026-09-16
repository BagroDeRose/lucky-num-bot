from __future__ import annotations

import datetime as dt

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.birth import BirthDateError, parse_birth_date
from app.analysis.engine import ValidationError, analyze
from app.analysis.interpreter import render_teaser
from app.bot import texts
from app.bot.keyboards.main import birth_date_kb, main_menu_kb, teaser_kb
from app.bot.states.analysis import AnalysisStates
from app.bot.utils import cb_answer
from app.database import repositories as repo
from app.database.models import User
from app.logging import get_logger

logger = get_logger(__name__)
router = Router(name="analyze")

# FSM key holding the already-validated serial number while we ask for a
# birth date. Nothing is written to the database until the analysis actually
# runs, so abandoning the flow here leaves no half-finished rows behind.
_PENDING_NUMBER = "pending_number"


async def _prompt_for_number(target: Message, state: FSMContext) -> None:
    await state.set_state(AnalysisStates.waiting_for_number)
    await target.answer(texts.ASK_NUMBER)


@router.message(Command("analyze"))
async def cmd_analyze(message: Message, state: FSMContext) -> None:
    await _prompt_for_number(message, state)


@router.callback_query(F.data == "analyze_new")
async def cb_analyze_new(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    await repo.log_event(session, user.id, "analyze_again")
    await session.commit()
    await state.set_state(AnalysisStates.waiting_for_number)
    await cb_answer(callback, texts.ASK_NUMBER)
    await callback.answer()


async def _run_analysis(
    send,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    raw_number: str,
    birth_date: dt.date | None,
) -> None:
    """Analyze, persist and present a teaser. The single place an analysis is
    created, whether or not a birth date takes part.
    """
    try:
        result = analyze(raw_number, birth_date)
    except ValidationError as exc:
        await send(exc.message, None)
        return

    # Note the event payload records only *whether* the analysis was
    # personalized — never the birth date itself.
    await repo.log_event(
        session,
        user.id,
        "analysis_started",
        {"number": result.normalized_number, "personalized": result.is_personalized},
    )

    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await repo.log_event(
        session,
        user.id,
        "analysis_completed",
        {"analysis_id": analysis.id, "overall_score": result.overall_score},
    )
    await session.commit()

    await state.clear()
    await send(render_teaser(result), teaser_kb(analysis.id))


@router.message(AnalysisStates.waiting_for_number, F.text, ~F.text.startswith("/"))
async def handle_number_input(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    raw = message.text or ""

    # Validate the serial first so an invalid number is rejected immediately,
    # before asking for anything as personal as a birth date.
    try:
        analyze(raw)
    except ValidationError as exc:
        await message.answer(exc.message)
        return

    async def send(text: str, kb=None) -> None:
        await message.answer(text, reply_markup=kb)

    if user.birth_date is None:
        await state.update_data({_PENDING_NUMBER: raw})
        await state.set_state(AnalysisStates.waiting_for_birth_date)
        await message.answer(texts.ASK_BIRTH_DATE, reply_markup=birth_date_kb())
        return

    await _run_analysis(send, state, session, user, raw, user.birth_date)


@router.message(AnalysisStates.waiting_for_birth_date, F.text, ~F.text.startswith("/"))
async def handle_birth_date_input(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    try:
        birth_date = parse_birth_date(message.text or "")
    except BirthDateError as exc:
        # Stay in this state so the user can simply correct the date; the
        # keyboard keeps both escape hatches (skip / main menu) available.
        await message.answer(exc.message, reply_markup=birth_date_kb())
        return

    await repo.set_user_birth_date(session, user, birth_date)
    await repo.log_event(session, user.id, "birth_date_set")
    await session.commit()

    data = await state.get_data()
    pending_number = data.get(_PENDING_NUMBER)

    async def send(text: str, kb=None) -> None:
        await message.answer(text, reply_markup=kb)

    if not pending_number:
        # Reached via /birthdate rather than mid-analysis: nothing to analyze.
        await state.clear()
        await message.answer(texts.BIRTH_DATE_SAVED, reply_markup=main_menu_kb())
        return

    await message.answer(texts.BIRTH_DATE_SAVED)
    await _run_analysis(send, state, session, user, pending_number, birth_date)


@router.callback_query(F.data == "skip_birth_date")
async def cb_skip_birth_date(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    """Deliberately NOT scoped to the waiting-for-birth-date state: inline
    keyboards stay tappable in the chat history forever, and a state-scoped
    handler would simply not match a tap on an older message — leaving the
    button spinning with no reply at all. Without a pending number there is
    nothing to skip, so it falls back to asking for one.
    """
    await callback.answer()
    data = await state.get_data()
    pending_number = data.get(_PENDING_NUMBER)

    async def send(text: str, kb=None) -> None:
        await cb_answer(callback, text, reply_markup=kb)

    if not pending_number:
        await state.clear()
        await cb_answer(callback, texts.ASK_NUMBER)
        await state.set_state(AnalysisStates.waiting_for_number)
        return

    await cb_answer(callback, texts.BIRTH_DATE_SKIPPED)
    await _run_analysis(send, state, session, user, pending_number, None)


@router.message(Command("birthdate"))
async def cmd_birthdate(message: Message, state: FSMContext) -> None:
    """Lets a user set or correct their birth date at any time — without it,
    a mistyped date would personalize every future analysis incorrectly with
    no way back.
    """
    await state.set_state(AnalysisStates.waiting_for_birth_date)
    await state.update_data({_PENDING_NUMBER: ""})
    await message.answer(texts.ASK_BIRTH_DATE_STANDALONE, reply_markup=birth_date_kb())
