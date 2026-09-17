"""Admin panel keyboards. Every callback starts with "adm:" so non-admin
presses are caught by a single denial handler.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.database.models import PromoCode


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


BACK_TO_ADMIN = [_btn("⬅️ Админ-панель", "adm:menu")]


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("📈 Статистика", "adm:stats")],
            [_btn("🏆 Рейтинг недели", "adm:live"), _btn("🗂 История недель", "adm:hist:0")],
            [_btn("🎟 Активные промокоды", "adm:promos:a"), _btn("⚪️ Истёкшие", "adm:promos:e")],
            [_btn("➕ Создать промокод", "adm:new")],
            [_btn("🎁 Выдать промокод пользователю", "adm:grant")],
            [_btn("✉️ Сообщение пользователю", "adm:msg")],
            [_btn("▶️ Запустить недельную задачу", "adm:run")],
            [_btn("🏠 В начало", "main_menu")],
        ]
    )


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[BACK_TO_ADMIN])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("✖️ Отмена", "adm:cancel")]])


def history_kb(offset: int, total: int, leaderboard_id: int | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if offset + 1 < total:
        nav.append(_btn("◀️ Раньше", f"adm:hist:{offset + 1}"))
    if offset > 0:
        nav.append(_btn("Позже ▶️", f"adm:hist:{offset - 1}"))
    if nav:
        rows.append(nav)
    if leaderboard_id is not None:
        rows.append(
            [
                _btn("🎁 Повторить награды", f"adm:rw:{leaderboard_id}"),
                _btn("📣 Повторить пост", f"adm:ch:{leaderboard_id}"),
            ]
        )
    rows.append(BACK_TO_ADMIN)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def promo_list_kb(promos: list[PromoCode]) -> InlineKeyboardMarkup:
    rows = [[_btn(f"🔍 {p.code}", f"adm:p:{p.id}")] for p in promos]
    rows.append([_btn("➕ Создать промокод", "adm:new")])
    rows.append(BACK_TO_ADMIN)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def promo_view_kb(promo: PromoCode) -> InlineKeyboardMarkup:
    rows = []
    if promo.is_active:
        rows.append([_btn("⛔ Деактивировать", f"adm:pd:{promo.id}")])
    rows.append([_btn("⬅️ К промокодам", "adm:promos:a")])
    rows.append(BACK_TO_ADMIN)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb(confirm_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn("✅ Подтвердить", confirm_data), _btn("✖️ Отмена", "adm:cancel")]]
    )
