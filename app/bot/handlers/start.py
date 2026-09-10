from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
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
    await state.clear()
    await repo.log_event(session, user.id, "start")
    await session.commit()
    await message.answer(texts.WELCOME, reply_markup=main_menu_kb())


@router.message(F.text == "/help")
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP, reply_markup=main_menu_kb())


@router.message(F.text == "/about")
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
