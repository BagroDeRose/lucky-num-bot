from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AnalysisStates(StatesGroup):
    waiting_for_number = State()
