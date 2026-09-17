"""User screens: 📊 statistics, 🏆 TOP researchers, 🎟 promo code entry.

All text is deterministic (app/engagement/messages.py) — no AI involved.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.admin_access import is_admin_id
from app.bot.keyboards.main import engagement_back_kb, main_menu_kb, promo_input_kb
from app.bot.states.engagement import PromoStates
from app.bot.utils import cb_edit_or_answer
from app.database import repositories as repo
from app.database.models import User
from app.engagement import messages
from app.engagement import promo as promo_service
from app.engagement.leaderboard import compute_standings, user_stats
from app.engagement.periods import utcnow, week_containing
from app.logging import get_logger

logger = get_logger(__name__)
router = Router(name="engagement")

_OUTCOME_TEXTS = {
    promo_service.ActivationOutcome.INVALID: messages.PROMO_INVALID,
    promo_service.ActivationOutcome.ALREADY_USED: messages.PROMO_ALREADY_USED,
    promo_service.ActivationOutcome.EXHAUSTED: messages.PROMO_EXHAUSTED,
    promo_service.ActivationOutcome.NOT_FOR_YOU: messages.PROMO_NOT_FOR_YOU,
}


async def stats_text(session: AsyncSession, user: User) -> str:
    return messages.render_user_stats(await user_stats(session, user.id))


async def top_text(session: AsyncSession, user: User) -> str:
    period = week_containing(utcnow())
    standings = await compute_standings(session, period)
    return messages.render_top(standings, period, viewer_user_id=user.id)


@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession, user: User) -> None:
    await message.answer(await stats_text(session, user), reply_markup=engagement_back_kb())


@router.callback_query(F.data == "my_stats")
async def cb_stats(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await cb_edit_or_answer(callback, await stats_text(session, user), reply_markup=engagement_back_kb())
    await callback.answer()


@router.message(Command("top"))
async def cmd_top(message: Message, session: AsyncSession, user: User) -> None:
    await message.answer(await top_text(session, user), reply_markup=engagement_back_kb())


@router.callback_query(F.data == "top")
async def cb_top(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await cb_edit_or_answer(callback, await top_text(session, user), reply_markup=engagement_back_kb())
    await callback.answer()


@router.message(Command("promo"))
async def cmd_promo(message: Message, state: FSMContext) -> None:
    await state.set_state(PromoStates.waiting_for_code)
    await message.answer(messages.ASK_PROMO_CODE, reply_markup=promo_input_kb())


@router.callback_query(F.data == "promo")
async def cb_promo(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoStates.waiting_for_code)
    await cb_edit_or_answer(callback, messages.ASK_PROMO_CODE, reply_markup=promo_input_kb())
    await callback.answer()


@router.message(PromoStates.waiting_for_code, F.text, ~F.text.startswith("/"))
async def handle_promo_code(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    result = await promo_service.activate(session, user, message.text or "")
    await repo.log_event(session, user.id, "promo_activation", {"outcome": result.outcome.value})
    await session.commit()

    if result.outcome is promo_service.ActivationOutcome.ACTIVATED and result.promo is not None:
        await state.clear()
        from_user = message.from_user
        await message.answer(
            messages.PROMO_ACTIVATED.format(discount=result.promo.discount_percent),
            reply_markup=main_menu_kb(is_admin_id(from_user.id if from_user else None)),
        )
        return

    # Stay in the input state so a typo can simply be retyped.
    await message.answer(_OUTCOME_TEXTS[result.outcome], reply_markup=promo_input_kb())
