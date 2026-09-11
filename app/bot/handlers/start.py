from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards.main import main_menu_kb
from app.bot.utils import cb_edit_or_answer
from app.database import repositories as repo
from app.database.models import User
from app.logging import get_logger

logger = get_logger(__name__)
router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    """Always safe to call, for a brand-new or a long-time user alike: state
    is only FSM (in-memory conversation position, e.g. "waiting for a serial
    number"), never analysis/payment/report data. `user` is already
    resolved idempotently by DbSessionMiddleware (get_or_create_user), so
    this never creates a duplicate row. No AI call, no payment — this is
    pure navigation back to the main menu.
    """
    await state.clear()
    await repo.log_event(session, user.id, "start")
    await session.commit()
    await message.answer(texts.WELCOME, reply_markup=main_menu_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP, reply_markup=main_menu_kb())


@router.message(Command("about"))
async def cmd_about(message: Message) -> None:
    await message.answer(texts.ABOUT, reply_markup=main_menu_kb())


@router.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery) -> None:
    await cb_edit_or_answer(callback, texts.HELP, reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "about")
async def cb_about(callback: CallbackQuery) -> None:
    await cb_edit_or_answer(callback, texts.ABOUT, reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """The generic "🏠 В начало" escape hatch offered from failure/edge
    states (see retry_report_kb, back_to_start_kb) — same destination and
    same safety guarantees as /start, just reachable without typing a
    command. `user` is not re-logged here (unlike /start) since this isn't
    a funnel-relevant fresh session start, just in-place navigation.
    """
    await state.clear()
    await cb_edit_or_answer(callback, texts.WELCOME, reply_markup=main_menu_kb())
    await callback.answer()
