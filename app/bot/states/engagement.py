from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class PromoStates(StatesGroup):
    waiting_for_code = State()


class AdminStates(StatesGroup):
    promo_code = State()
    promo_percent = State()
    promo_limit = State()
    promo_days = State()
    grant_target = State()
    grant_percent = State()
    grant_days = State()
    message_target = State()
    message_text = State()
