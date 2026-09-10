from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.interpreter import render_teaser
from app.analysis.models import AnalysisResult
from app.bot import texts
from app.bot.keyboards.main import history_kb, teaser_kb
from app.bot.utils import cb_answer, require_callback_data
from app.database import repositories as repo
from app.database.models import User

router = Router(name="history")

Sender = Callable[[str, InlineKeyboardMarkup | None], Awaitable[None]]


async def _send_history(send: Sender, session: AsyncSession, user: User) -> None:
    analyses = await repo.list_recent_analyses(session, user.id, limit=10)
    if not analyses:
        await send(texts.NO_HISTORY, None)
        return
    await send(texts.HISTORY_HEADER, history_kb(analyses))


@router.message(Command("history"))
async def cmd_history(message: Message, session: AsyncSession, user: User) -> None:
    async def send(text: str, kb: InlineKeyboardMarkup | None) -> None:
        await message.answer(text, reply_markup=kb)

    await _send_history(send, session, user)


@router.callback_query(F.data == "history")
async def cb_history(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    async def send(text: str, kb: InlineKeyboardMarkup | None) -> None:
        await cb_answer(callback, text, reply_markup=kb)

    await _send_history(send, session, user)
    await callback.answer()


@router.callback_query(F.data.startswith("history_view:"))
async def cb_history_view(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    data = require_callback_data(callback)
    analysis_id = int(data.split(":", 1)[1])
    analysis = await repo.get_analysis(session, analysis_id, user.id)
    if analysis is None:
        await callback.answer(texts.ANALYSIS_NOT_FOUND, show_alert=True)
        return

    if analysis.paid and analysis.report:
        await cb_answer(callback, analysis.report)
    else:
        result = AnalysisResult.model_validate(analysis.analysis_payload)
        await cb_answer(callback, render_teaser(result), reply_markup=teaser_kb(analysis.id))

    await callback.answer()
