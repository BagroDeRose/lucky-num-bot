from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AnalysisStates(StatesGroup):
    waiting_for_number = State()
    # Entered only when a serial number is already accepted and the user has
    # no birth date on file yet. The pending serial is carried in FSM data so
    # nothing is written to the database until the analysis actually runs.
    waiting_for_birth_date = State()
