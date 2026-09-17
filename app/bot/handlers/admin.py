"""Admin panel.

AUTHORIZATION: `router` carries the IsAdmin filter on messages and callback
queries, so every admin update is checked against ADMIN_TELEGRAM_ID on the
server before any handler runs. `denied_router` (included right after it)
answers non-admins who reach an admin command, button or state with an
explicit access-denied message instead of silently falling through.

Weekly-job actions call the same WeeklyAutomationService the scheduler uses.
"""

from __future__ import annotations

import datetime as dt
import html
from collections.abc import Callable

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.admin_access import IsAdmin
from app.bot.keyboards import admin as kb
from app.bot.states.engagement import AdminStates
from app.bot.utils import cb_answer, cb_edit_or_answer, require_callback_data
from app.config import settings
from app.database import repositories as repo
from app.database.models import PromoType, User
from app.database.session import async_session_factory
from app.engagement import automation, messages
from app.engagement import promo as promo_service
from app.engagement.admin_stats import admin_overview, render_admin_overview
from app.engagement.automation import WeeklyAutomationService, WeeklyRunReport
from app.engagement.leaderboard import (
    compute_standings,
    count_leaderboards,
    get_entries,
    list_leaderboards,
    period_of,
)
from app.engagement.periods import as_utc, last_valid_day, utcnow, week_containing
from app.engagement.rewards import RewardSummary, describe_error, reward_views
from app.logging import get_logger

logger = get_logger(__name__)

router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

denied_router = Router(name="admin_denied")

ACCESS_DENIED = "⛔ Доступ запрещён."
ADMIN_MENU = "🛠 <b>Админ-панель</b>"
# Telegram rejects messages longer than 4096 characters. Admin text is sent
# as plain text (no parse mode), so what is validated is exactly what is sent;
# the limit leaves room for the confirmation preview's header.
TELEGRAM_MESSAGE_LIMIT = 4096
MESSAGE_PREVIEW_HEADER = "Отправить это сообщение?\n\n"
MAX_MESSAGE_LENGTH = TELEGRAM_MESSAGE_LIMIT - len(MESSAGE_PREVIEW_HEADER)
MAX_VALID_DAYS = 365
MAX_ACTIVATIONS = 1_000_000

# Overridable in tests.
session_factory: Callable[[], AsyncSession] = async_session_factory


def automation_service(bot: Bot) -> WeeklyAutomationService:
    return WeeklyAutomationService(session_factory, bot)


def _admin_id(event: Message | CallbackQuery) -> int | None:
    return event.from_user.id if event.from_user is not None else None


def _parse_int(text: str | None, low: int, high: int) -> int | None:
    value = (text or "").strip().rstrip("%").strip()
    if not value.isdigit():
        return None
    number = int(value)
    return number if low <= number <= high else None


# --- menu -------------------------------------------------------------------


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(ADMIN_MENU, reply_markup=kb.admin_menu_kb())


@router.callback_query(F.data == "adm:menu")
async def cb_admin_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb_edit_or_answer(callback, ADMIN_MENU, reply_markup=kb.admin_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "adm:cancel")
async def cb_admin_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb_edit_or_answer(callback, "Отменено.\n\n" + ADMIN_MENU, reply_markup=kb.admin_menu_kb())
    await callback.answer()


# --- statistics and leaderboards ------------------------------------------


@router.callback_query(F.data == "adm:stats")
async def cb_admin_stats(callback: CallbackQuery, session: AsyncSession) -> None:
    text = render_admin_overview(await admin_overview(session))
    await cb_edit_or_answer(callback, text, reply_markup=kb.back_kb())
    await callback.answer()


def _standing_line(rank: int, username: str | None, count: int) -> str:
    return f"{rank}. {messages.display_name(username)} — {count}"


@router.callback_query(F.data == "adm:live")
async def cb_admin_live(callback: CallbackQuery, session: AsyncSession) -> None:
    period = week_containing(utcnow())
    standings = await compute_standings(session, period)
    lines = [f"🏆 <b>Текущая неделя</b> ({period.label}), live", ""]
    if standings:
        lines += [_standing_line(s.rank, s.username, s.report_count) for s in standings[:20]]
        lines += ["", f"Участников: {len(standings)}"]
    else:
        lines.append("Пока нет завершённых разборов.")
    await cb_edit_or_answer(callback, "\n".join(lines), reply_markup=kb.back_kb())
    await callback.answer()


async def render_history(session: AsyncSession, offset: int) -> tuple[str, int, int | None]:
    total = await count_leaderboards(session)
    found = await list_leaderboards(session, offset=offset, limit=1)
    if not found:
        return "🗂 Завершённых недель пока нет.", total, None
    leaderboard = found[0]
    period = period_of(leaderboard)
    lines = [f"🗂 <b>Неделя {period.label}</b> ({offset + 1}/{total})", ""]

    entries = await get_entries(session, leaderboard.id, limit=10)
    if entries:
        lines += [_standing_line(e.rank, e.username_snapshot, e.report_count) for e in entries]
    else:
        lines.append("Рейтинг пуст.")

    rewards = await reward_views(session, leaderboard.id)
    lines += ["", "🎁 <b>Награды TOP-5</b>"]
    if rewards:
        for r in rewards:
            label = messages.DELIVERY_LABELS.get(r.delivery_status, r.delivery_status)  # type: ignore[call-overload]
            line = (
                f"#{r.rank} {messages.display_name(r.username)} — "
                f"<code>{html.escape(r.code)}</code> — {label}"
            )
            if r.delivery_error:
                line += f"\n   <i>{html.escape(r.delivery_error)}</i>"
            lines.append(line)
    else:
        lines.append("нет")

    lines += ["", "📣 <b>Публичный промокод и канал</b>"]
    if leaderboard.public_promo_id is not None:
        promo = await promo_service.get_promo(session, leaderboard.public_promo_id)
        if promo is not None:
            lines.append(messages.render_promo_line(promo, utcnow()))
    else:
        lines.append("промокод не создан")
    channel = messages.DELIVERY_LABELS.get(leaderboard.channel_status, leaderboard.channel_status)  # type: ignore[call-overload]
    lines.append(f"Канал: {channel} (попыток: {leaderboard.channel_attempts})")
    if leaderboard.channel_error:
        lines.append(f"<i>{html.escape(leaderboard.channel_error)}</i>")
    return "\n".join(lines), total, leaderboard.id


@router.callback_query(F.data.startswith("adm:hist:"))
async def cb_admin_history(callback: CallbackQuery, session: AsyncSession) -> None:
    offset = _parse_int(require_callback_data(callback).rsplit(":", 1)[1], 0, 10_000) or 0
    text, total, leaderboard_id = await render_history(session, offset)
    await cb_edit_or_answer(
        callback, text, reply_markup=kb.history_kb(offset, total, leaderboard_id)
    )
    await callback.answer()


def render_reward_summary(summary: RewardSummary) -> str:
    return (
        f"🎁 Награды: создано {summary.generated}, отправлено {summary.sent}, "
        f"ошибок {summary.failed}, уже доставлено {summary.already_sent}, "
        f"пропущено {summary.not_retried}"
    )


@router.callback_query(F.data.startswith("adm:rw:"))
async def cb_admin_retry_rewards(callback: CallbackQuery) -> None:
    await callback.answer()
    leaderboard_id = _parse_int(require_callback_data(callback).rsplit(":", 1)[1], 1, 10**12)
    if leaderboard_id is None or callback.bot is None:
        return
    logger.info("Admin %s retries rewards (leaderboard_id=%s)", _admin_id(callback), leaderboard_id)
    summary = await automation_service(callback.bot).retry_rewards(leaderboard_id)
    text = "Неделя не найдена." if summary is None else render_reward_summary(summary)
    await cb_answer(callback, text, reply_markup=kb.back_kb())


@router.callback_query(F.data.startswith("adm:ch:"))
async def cb_admin_retry_channel(callback: CallbackQuery) -> None:
    await callback.answer()
    leaderboard_id = _parse_int(require_callback_data(callback).rsplit(":", 1)[1], 1, 10**12)
    if leaderboard_id is None or callback.bot is None:
        return
    logger.info("Admin %s retries channel post (leaderboard_id=%s)", _admin_id(callback), leaderboard_id)
    status = await automation_service(callback.bot).retry_channel(leaderboard_id)
    if status is None:
        text = "Неделя не найдена."
    else:
        text = f"📣 Пост в канал: {messages.DELIVERY_LABELS.get(status, status)}"  # type: ignore[call-overload]
    await cb_answer(callback, text, reply_markup=kb.back_kb())


# --- weekly job ---------------------------------------------------------------


@router.callback_query(F.data == "adm:run")
async def cb_admin_run(callback: CallbackQuery) -> None:
    text = (
        "▶️ Запустить недельную задачу сейчас?\n\n"
        "Будет завершена прошлая неделя (если ещё не завершена), выданы и отправлены "
        "награды TOP-5, создан промокод недели и опубликован пост. Повторный запуск "
        "безопасен: уже сделанное не повторяется."
    )
    await cb_edit_or_answer(callback, text, reply_markup=kb.confirm_kb("adm:run_go"))
    await callback.answer()


def render_run_report(report: WeeklyRunReport) -> str:
    lines = [
        f"✅ Недельная задача выполнена: неделя {report.week_label}"
        if report.ok
        else f"⚠️ Недельная задача: неделя {report.week_label}, есть ошибки",
        "",
        f"Снимок рейтинга: {'создан сейчас' if report.finalized_now else 'уже существовал'} "
        f"(участников: {report.entries})",
        render_reward_summary(report.rewards),
    ]
    if report.public_promo_code:
        state = "создан" if report.public_promo_created else "уже был"
        lines.append(f"🎟 Промокод недели <code>{html.escape(report.public_promo_code)}</code> — {state}")
    else:
        lines.append("🎟 Промокод недели не создан")
    lines.append(f"⛔ Деактивировано старых публичных промокодов: {report.deactivated_promos}")
    channel = report.channel_status or "—"
    lines.append(f"📣 Канал: {messages.DELIVERY_LABELS.get(channel, channel)}")  # type: ignore[call-overload]
    if report.channel_error:
        lines.append(f"   <i>{html.escape(report.channel_error)}</i>")
    for reason in dict.fromkeys(report.rewards.errors):
        lines.append(f"🎁 Ошибка доставки награды: <i>{html.escape(reason)}</i>")
    if report.pending_retries:
        lines.append(f"🔁 Автоматических повторов впереди: {report.pending_retries}")
    if report.errors:
        lines += ["", "Ошибки: " + html.escape(", ".join(report.errors))]
    return "\n".join(lines)


@router.callback_query(F.data == "adm:run_go")
async def cb_admin_run_go(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.bot is None:
        return
    if automation.is_running():
        await cb_answer(callback, "⏳ Недельная задача уже выполняется, дождитесь её окончания.")
        return
    logger.info("Admin %s started the weekly job manually", _admin_id(callback))
    report = await automation_service(callback.bot).run(manual=True)
    await cb_answer(callback, render_run_report(report), reply_markup=kb.back_kb())


# --- promo codes --------------------------------------------------------------


@router.callback_query(F.data.startswith("adm:promos:"))
async def cb_admin_promos(callback: CallbackQuery, session: AsyncSession) -> None:
    active = require_callback_data(callback).endswith(":a")
    now = utcnow()
    promos = await promo_service.list_promos(session, active=active, now=now)
    title = "🎟 <b>Активные промокоды</b>" if active else "⚪️ <b>Истёкшие / выключенные</b>"
    lines = [title, ""]
    lines += [messages.render_promo_line(p, now) for p in promos] or ["нет"]
    await cb_edit_or_answer(callback, "\n".join(lines), reply_markup=kb.promo_list_kb(promos))
    await callback.answer()


async def render_promo_details(session: AsyncSession, promo_id: int) -> str | None:
    promo = await promo_service.get_promo(session, promo_id)
    if promo is None:
        return None
    counts = await promo_service.redemption_counts(session, promo.id)
    target = "все пользователи"
    if promo.target_user_id is not None:
        target_user = await session.get(User, promo.target_user_id)
        target = (
            messages.display_name(target_user.username) + f" (id {target_user.telegram_id})"
            if target_user is not None
            else f"user #{promo.target_user_id}"
        )
    now = utcnow()
    live = promo.is_active and as_utc(promo.valid_until) > now
    lines = [
        f"🎟 <code>{html.escape(promo.code)}</code>",
        "",
        f"Тип: {messages.PROMO_TYPE_LABELS.get(PromoType(promo.promo_type), promo.promo_type)}",
        f"Скидка: {promo.discount_percent}% → {promo_service.discounted_price(settings.price_rub, promo.discount_percent)} ₽",
        f"Активаций: {promo.activations_count}/{promo.max_activations}",
        f"Использовано при оплате: {counts.get('consumed', 0)}, "
        f"ждут оплаты: {counts.get('applied', 0) + counts.get('reserved', 0)}",
        f"Действует: {messages.format_day(last_valid_day(promo.valid_from))} — "
        f"{messages.format_day(last_valid_day(promo.valid_until))} включительно",
        f"Для кого: {target}",
        f"Статус: {'🟢 активен' if live else '⚪️ не действует'}",
    ]
    return "\n".join(lines)


@router.callback_query(F.data.startswith("adm:p:"))
async def cb_admin_promo_view(callback: CallbackQuery, session: AsyncSession) -> None:
    promo_id = _parse_int(require_callback_data(callback).rsplit(":", 1)[1], 1, 10**12)
    promo = await promo_service.get_promo(session, promo_id) if promo_id else None
    text = await render_promo_details(session, promo.id) if promo else None
    if promo is None or text is None:
        await callback.answer("Промокод не найден", show_alert=True)
        return
    await cb_edit_or_answer(callback, text, reply_markup=kb.promo_view_kb(promo))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:pd:"))
async def cb_admin_promo_deactivate(callback: CallbackQuery, session: AsyncSession) -> None:
    promo_id = _parse_int(require_callback_data(callback).rsplit(":", 1)[1], 1, 10**12)
    changed = await promo_service.deactivate(session, promo_id) if promo_id else False
    await session.commit()
    promo = await promo_service.get_promo(session, promo_id) if promo_id else None
    if promo is None:
        await callback.answer("Промокод не найден", show_alert=True)
        return
    await session.refresh(promo)
    logger.info("Admin %s deactivated promo_id=%s (changed=%s)", _admin_id(callback), promo_id, changed)
    text = await render_promo_details(session, promo.id) or ""
    await cb_edit_or_answer(callback, text, reply_markup=kb.promo_view_kb(promo))
    await callback.answer("Промокод деактивирован" if changed else "Уже был неактивен")


# create: code -> percent -> limit -> days


@router.callback_query(F.data == "adm:new")
async def cb_admin_promo_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.promo_code)
    await cb_edit_or_answer(
        callback,
        "➕ Новый промокод.\n\nВведите код (латиница, цифры, дефис) или «-», чтобы сгенерировать.",
        reply_markup=kb.cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.promo_code, F.text, ~F.text.startswith("/"))
async def admin_promo_code(message: Message, state: FSMContext, session: AsyncSession) -> None:
    raw = (message.text or "").strip()
    if raw == "-":
        code = promo_service.generate_code("PROMO", 6)
    else:
        normalized = promo_service.normalize_code(raw)
        if normalized is None:
            await message.answer(
                "Код должен быть 3–32 символа: латинские буквы, цифры и дефис внутри.",
                reply_markup=kb.cancel_kb(),
            )
            return
        if await promo_service.get_by_code(session, normalized) is not None:
            await message.answer("Такой промокод уже существует. Введите другой.", reply_markup=kb.cancel_kb())
            return
        code = normalized
    await state.update_data(code=code)
    await state.set_state(AdminStates.promo_percent)
    await message.answer(
        f"Код: <code>{html.escape(code)}</code>\n\nСкидка в процентах "
        f"({promo_service.MIN_DISCOUNT_PERCENT}–{promo_service.MAX_DISCOUNT_PERCENT}):",
        reply_markup=kb.cancel_kb(),
    )


@router.message(AdminStates.promo_percent, F.text, ~F.text.startswith("/"))
async def admin_promo_percent(message: Message, state: FSMContext) -> None:
    percent = _parse_int(
        message.text, promo_service.MIN_DISCOUNT_PERCENT, promo_service.MAX_DISCOUNT_PERCENT
    )
    if percent is None:
        await message.answer("Нужно целое число от 1 до 99.", reply_markup=kb.cancel_kb())
        return
    await state.update_data(percent=percent)
    await state.set_state(AdminStates.promo_limit)
    await message.answer("Максимум активаций (целое число ≥ 1):", reply_markup=kb.cancel_kb())


@router.message(AdminStates.promo_limit, F.text, ~F.text.startswith("/"))
async def admin_promo_limit(message: Message, state: FSMContext) -> None:
    limit = _parse_int(message.text, 1, MAX_ACTIVATIONS)
    if limit is None:
        await message.answer("Нужно целое число от 1.", reply_markup=kb.cancel_kb())
        return
    await state.update_data(limit=limit)
    await state.set_state(AdminStates.promo_days)
    await message.answer(
        f"Сколько дней действует (1–{MAX_VALID_DAYS}), начиная с сегодня:", reply_markup=kb.cancel_kb()
    )


@router.message(AdminStates.promo_days, F.text, ~F.text.startswith("/"))
async def admin_promo_days(message: Message, state: FSMContext, session: AsyncSession) -> None:
    days = _parse_int(message.text, 1, MAX_VALID_DAYS)
    if days is None:
        await message.answer(f"Нужно целое число от 1 до {MAX_VALID_DAYS}.", reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    now = utcnow()
    try:
        promo = await promo_service.create_promo(
            session,
            code=data["code"],
            promo_type=PromoType.ADMIN_MANUAL,
            discount_percent=int(data["percent"]),
            max_activations=int(data["limit"]),
            valid_from=now,
            valid_until=now + dt.timedelta(days=days),
            created_by_telegram_id=_admin_id(message),
        )
        await session.commit()
    except promo_service.PromoValidationError as exc:
        await session.rollback()
        await state.set_state(AdminStates.promo_code)
        await message.answer(exc.message + "\n\nВведите код заново.", reply_markup=kb.cancel_kb())
        return
    await state.clear()
    logger.info("Admin %s created promo_id=%s", _admin_id(message), promo.id)
    await message.answer(
        "✅ Промокод создан\n\n" + (await render_promo_details(session, promo.id) or ""),
        reply_markup=kb.promo_view_kb(promo),
    )


# grant a personal code: target -> percent -> days


@router.callback_query(F.data == "adm:grant")
async def cb_admin_grant(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.grant_target)
    await cb_edit_or_answer(
        callback,
        "🎁 Персональный промокод.\n\nВведите @username или Telegram ID пользователя "
        "(он должен хотя бы раз запускать бота).",
        reply_markup=kb.cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.grant_target, F.text, ~F.text.startswith("/"))
async def admin_grant_target(message: Message, state: FSMContext, session: AsyncSession) -> None:
    target = await repo.find_user_by_handle(session, message.text or "")
    if target is None:
        await message.answer("Пользователь не найден. Попробуйте ещё раз.", reply_markup=kb.cancel_kb())
        return
    await state.update_data(target_user_id=target.id)
    await state.set_state(AdminStates.grant_percent)
    await message.answer(
        f"Пользователь: {messages.display_name(target.username)} (id {target.telegram_id})\n\n"
        "Скидка в процентах (1–99):",
        reply_markup=kb.cancel_kb(),
    )


@router.message(AdminStates.grant_percent, F.text, ~F.text.startswith("/"))
async def admin_grant_percent(message: Message, state: FSMContext) -> None:
    percent = _parse_int(
        message.text, promo_service.MIN_DISCOUNT_PERCENT, promo_service.MAX_DISCOUNT_PERCENT
    )
    if percent is None:
        await message.answer("Нужно целое число от 1 до 99.", reply_markup=kb.cancel_kb())
        return
    await state.update_data(percent=percent)
    await state.set_state(AdminStates.grant_days)
    await message.answer(f"Сколько дней действует (1–{MAX_VALID_DAYS}):", reply_markup=kb.cancel_kb())


GRANT_MESSAGE = (
    "🎁 <b>Вам персональный промокод!</b>\n\n"
    "<code>{code}</code>\n\n"
    "Скидка {percent}% на следующий полный разбор.\n"
    "Промокод личный, действует один раз до {until} включительно.\n\n"
    "Ввести его можно кнопкой «🎟 Промокод» в меню."
)


@router.message(AdminStates.grant_days, F.text, ~F.text.startswith("/"))
async def admin_grant_days(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot
) -> None:
    days = _parse_int(message.text, 1, MAX_VALID_DAYS)
    if days is None:
        await message.answer(f"Нужно целое число от 1 до {MAX_VALID_DAYS}.", reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    target = await session.get(User, int(data["target_user_id"]))
    if target is None:
        await state.clear()
        await message.answer("Пользователь не найден.", reply_markup=kb.back_kb())
        return
    now = utcnow()
    promo = await promo_service.create_generated_promo(
        session,
        prefix="GIFT",
        length=6,
        promo_type=PromoType.ADMIN_MANUAL,
        discount_percent=int(data["percent"]),
        max_activations=1,
        valid_from=now,
        valid_until=now + dt.timedelta(days=days),
        target_user_id=target.id,
        created_by_telegram_id=_admin_id(message),
    )
    await session.commit()
    await state.clear()
    logger.info(
        "Admin %s granted promo_id=%s to user_id=%s", _admin_id(message), promo.id, target.id
    )

    text = GRANT_MESSAGE.format(
        code=html.escape(promo.code),
        percent=promo.discount_percent,
        until=messages.format_day(last_valid_day(promo.valid_until)),
    )
    try:
        await bot.send_message(target.telegram_id, text)
        delivery = "✅ Пользователь получил сообщение."
    except Exception as exc:  # the code exists either way; report, don't crash
        reason = describe_error(exc)
        logger.warning("Grant DM failed (promo_id=%s, user_id=%s): %s", promo.id, target.id, reason)
        delivery = (
            f"❌ Сообщение не доставлено ({html.escape(reason)}). "
            "Код создан — его можно передать вручную."
        )
    await message.answer(
        f"🎁 Создан <code>{html.escape(promo.code)}</code> для "
        f"{messages.display_name(target.username)}.\n{delivery}",
        reply_markup=kb.back_kb(),
    )


# targeted message: target -> text -> confirm


@router.callback_query(F.data == "adm:msg")
async def cb_admin_message(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.message_target)
    await cb_edit_or_answer(
        callback, "✉️ Введите @username или Telegram ID получателя:", reply_markup=kb.cancel_kb()
    )
    await callback.answer()


@router.message(AdminStates.message_target, F.text, ~F.text.startswith("/"))
async def admin_message_target(message: Message, state: FSMContext, session: AsyncSession) -> None:
    target = await repo.find_user_by_handle(session, message.text or "")
    if target is None:
        await message.answer("Пользователь не найден. Попробуйте ещё раз.", reply_markup=kb.cancel_kb())
        return
    await state.update_data(target_user_id=target.id)
    await state.set_state(AdminStates.message_text)
    await message.answer(
        f"Получатель: {messages.display_name(target.username)}\n\nВведите текст сообщения:",
        reply_markup=kb.cancel_kb(),
    )


@router.message(AdminStates.message_text, F.text, ~F.text.startswith("/"))
async def admin_message_text(message: Message, state: FSMContext) -> None:
    body = (message.text or "").strip()
    if not body or len(body) > MAX_MESSAGE_LENGTH:
        await message.answer(
            f"Текст должен быть от 1 до {MAX_MESSAGE_LENGTH} символов.", reply_markup=kb.cancel_kb()
        )
        return
    await state.update_data(body=body)
    # Plain text (parse_mode=None): admin input can never inject markup, and
    # nothing is escaped, so the validated length is the length Telegram sees.
    await message.answer(
        MESSAGE_PREVIEW_HEADER + body,
        parse_mode=None,
        reply_markup=kb.confirm_kb("adm:msg_go"),
    )


@router.callback_query(F.data == "adm:msg_go")
async def cb_admin_message_go(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    await callback.answer()
    data = await state.get_data()
    await state.clear()
    if "body" not in data or "target_user_id" not in data or callback.bot is None:
        await cb_answer(callback, "Нечего отправлять — начните заново.", reply_markup=kb.back_kb())
        return
    target = await session.get(User, int(data["target_user_id"]))
    if target is None:
        await cb_answer(callback, "Пользователь не найден.", reply_markup=kb.back_kb())
        return
    try:
        await callback.bot.send_message(target.telegram_id, str(data["body"]), parse_mode=None)
    except Exception as exc:
        reason = describe_error(exc)
        logger.warning(
            "Admin %s message to user_id=%s failed: %s", _admin_id(callback), target.id, reason
        )
        result = f"❌ Не доставлено: {html.escape(reason)}"
    else:
        logger.info("Admin %s messaged user_id=%s", _admin_id(callback), target.id)
        result = "✅ Отправлено."
    await cb_answer(callback, result, reply_markup=kb.back_kb())


# --- denial for everyone else ---------------------------------------------------


@denied_router.message(~IsAdmin(), Command("admin"))
@denied_router.message(~IsAdmin(), StateFilter(AdminStates))
async def deny_admin_message(message: Message, state: FSMContext) -> None:
    await state.clear()
    logger.warning("Non-admin %s tried an admin command", _admin_id(message))
    await message.answer(ACCESS_DENIED)


@denied_router.callback_query(~IsAdmin(), F.data.startswith("adm:"))
async def deny_admin_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    logger.warning("Non-admin %s pressed an admin button", _admin_id(callback))
    await callback.answer(ACCESS_DENIED, show_alert=True)
