"""Inline keyboards used across the bot."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.database.models import Analysis


def main_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔎 Проверить купюру", callback_data="analyze_new")],
        [InlineKeyboardButton(text="📜 История", callback_data="history")],
        [
            InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats"),
            InlineKeyboardButton(text="🏆 Топ исследователей", callback_data="top"),
        ],
        [InlineKeyboardButton(text="🎟 Промокод", callback_data="promo")],
        [
            InlineKeyboardButton(text="ℹ️ О проекте", callback_data="about"),
            InlineKeyboardButton(text="❓ Помощь", callback_data="help"),
        ],
    ]
    if is_admin:
        # Only a convenience: every admin action is re-checked server-side.
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="adm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def engagement_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="my_stats"),
                InlineKeyboardButton(text="🏆 Топ", callback_data="top"),
            ],
            [InlineKeyboardButton(text="🏠 В начало", callback_data="main_menu")],
        ]
    )


def promo_input_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 В начало", callback_data="main_menu")]]
    )


def teaser_kb(analysis_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔮 Открыть полный разбор", callback_data=f"get_report:{analysis_id}"
                )
            ],
            [InlineKeyboardButton(text="🔁 Проверить другую купюру", callback_data="analyze_new")],
        ]
    )


def mock_payment_kb(analysis_id: int, amount: int, currency: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✅ Оплатить {amount} {currency} (тестовый режим)",
                    callback_data=f"mock_pay:{analysis_id}",
                )
            ]
        ]
    )


def yookassa_payment_kb(
    analysis_id: int, amount: int, currency: str, confirmation_url: str
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💳 Оплатить {amount} {currency} через YooKassa",
                    url=confirmation_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил, проверить статус",
                    callback_data=f"yookassa_check:{analysis_id}",
                )
            ],
        ]
    )


def birth_date_kb() -> InlineKeyboardMarkup:
    """Shown while waiting for a birth date. Skipping is a first-class
    choice, not a hidden one: the serial-number analysis is a complete
    product on its own, and a user who would rather not share a date must
    never be stuck at this step.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➡️ Без даты рождения", callback_data="skip_birth_date")],
            [InlineKeyboardButton(text="🏠 В начало", callback_data="main_menu")],
        ]
    )


def payment_recheck_kb(analysis_id: int) -> InlineKeyboardMarkup:
    """Shown when a payment for this analysis already exists but its payment
    link can't be handed over right now. Deliberately offers *no* "pay"
    button: re-checking the existing payment is the only safe action, since
    minting a second payment for the same analysis risks charging twice. If
    the existing payment turns out to be canceled/expired, the status check
    marks it failed, which frees the normal flow to create a fresh one.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил, проверить статус",
                    callback_data=f"yookassa_check:{analysis_id}",
                )
            ],
            [InlineKeyboardButton(text="🏠 В начало", callback_data="main_menu")],
        ]
    )


def after_report_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Проверить другую купюру", callback_data="analyze_new")],
        ]
    )


def retry_report_kb(analysis_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Попробовать ещё раз",
                    callback_data=f"retry_report:{analysis_id}",
                )
            ],
            [InlineKeyboardButton(text="🏠 В начало", callback_data="main_menu")],
        ]
    )


def back_to_start_kb() -> InlineKeyboardMarkup:
    """A single, minimal escape hatch — used where the current state has no
    other meaningful action (e.g. the generation-attempt safety limit was
    hit) rather than the full main_menu_kb, to avoid suggesting actions
    ("Проверить купюру" would start an unrelated new analysis) that don't
    fit that moment.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 В начало", callback_data="main_menu")],
        ]
    )


def history_kb(analyses: list[Analysis]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{a.number} — {a.overall_score}/100",
                callback_data=f"history_view:{a.id}",
            )
        ]
        for a in analyses
    ]
    rows.append([InlineKeyboardButton(text="🔎 Проверить купюру", callback_data="analyze_new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
