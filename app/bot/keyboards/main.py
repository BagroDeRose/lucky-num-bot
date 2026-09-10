"""Inline keyboards used across the bot."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.database.models import Analysis


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Проверить купюру", callback_data="analyze_new")],
            [InlineKeyboardButton(text="📜 История", callback_data="history")],
            [
                InlineKeyboardButton(text="ℹ️ О проекте", callback_data="about"),
                InlineKeyboardButton(text="❓ Помощь", callback_data="help"),
            ],
        ]
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
            ]
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
