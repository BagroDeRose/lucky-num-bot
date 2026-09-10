from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.engine import ValidationError, analyze
from app.analysis.interpreter import render_teaser
from app.bot import texts
from app.bot.keyboards.main import teaser_kb
from app.bot.states.analysis import AnalysisStates
from app.bot.utils import cb_answer
from app.database import repositories as repo
from app.database.models import User
from app.logging import get_logger

logger = get_logger(__name__)
router = Router(name="analyze")


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


@router.message(AnalysisStates.waiting_for_number, F.text)
async def handle_number_input(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    raw = message.text or ""

    try:
        result = analyze(raw)
    except ValidationError as exc:
        await message.answer(exc.message)
        return

    await repo.log_event(session, user.id, "analysis_started", {"number": result.normalized_number})

    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await repo.log_event(
        session,
        user.id,
        "analysis_completed",
        {"analysis_id": analysis.id, "overall_score": result.overall_score},
    )
    await session.commit()

    await state.clear()

    teaser = render_teaser(result)
    await message.answer(teaser, reply_markup=teaser_kb(analysis.id))
