"""Date-of-birth parsing, validation and derived numeric features.

Deliberately separate from the scoring engine: everything here turns a raw
user string into either a rejection or a plain `datetime.date`, and then a
`date` into a small set of deterministic numbers. The scoring engine and the
AI payload only ever see those derived numbers — the raw date never travels
further than the database row it is stored in (see app.bot.handlers.analyze
and app.ai.report_generator._build_ai_payload).
"""

from __future__ import annotations

import datetime as dt
import re

from app.analysis.rules import DIGIT_MEANINGS, MIN_BIRTH_YEAR

# Accepts 07.03.1990, 7.3.1990, 07/03/1990, 07-03-1990: one separator style
# per input, day and month may be 1 or 2 digits, year must be 4 digits so
# "90" can never be silently guessed as 1990 or 2090.
_DATE_RE = re.compile(r"^\s*(\d{1,2})\s*([./-])\s*(\d{1,2})\s*\2\s*(\d{4})\s*$")


class BirthDateError(Exception):
    """Raised when a user-supplied birth date cannot be accepted.

    `message` is a ready-to-display, Russian, user-facing explanation that
    never echoes internals.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def parse_birth_date(raw: str, *, today: dt.date | None = None) -> dt.date:
    """Parse and validate a birth date in DD.MM.YYYY form.

    Rejects malformed text, impossible calendar dates (31.02, 29.02 in a
    non-leap year), future dates, and years before MIN_BIRTH_YEAR. Accepts
    genuine leap days (29.02.2024).
    """
    match = _DATE_RE.match(raw or "")
    if match is None:
        raise BirthDateError(
            "Не получилось прочитать дату. Пришлите её в формате ДД.ММ.ГГГГ — "
            "например, <code>07.03.1990</code>."
        )

    day, _sep, month, year = match.groups()
    try:
        # date() itself rejects impossible combinations: month 13, day 31 in
        # February, 29.02 of a non-leap year. No manual calendar maths.
        parsed = dt.date(int(year), int(month), int(day))
    except ValueError as exc:
        raise BirthDateError(
            "Такой даты не существует. Проверьте число и месяц и пришлите ещё "
            "раз в формате ДД.ММ.ГГГГ."
        ) from exc

    current = today or dt.date.today()
    if parsed > current:
        raise BirthDateError("Дата рождения не может быть в будущем. Проверьте год.")
    if parsed.year < MIN_BIRTH_YEAR:
        raise BirthDateError(
            f"Год рождения выглядит неправдоподобно. Укажите год не раньше {MIN_BIRTH_YEAR}."
        )
    return parsed


def birth_digits(birth_date: dt.date) -> list[int]:
    """The date's digits in DD MM YYYY order — the input to the life-path sum."""
    return [int(ch) for ch in f"{birth_date.day:02d}{birth_date.month:02d}{birth_date.year:04d}"]


def birth_number(birth_date: dt.date) -> int:
    """Life-path digit: sum of all DD MM YYYY digits, reduced to one digit.

    Uses the engine's own reduction so the birth date and the serial number
    are reduced by identical rules (see app.analysis.rules for the worked
    example).
    """
    from app.analysis.engine import reduce_to_single_digit

    return reduce_to_single_digit(sum(birth_digits(birth_date)))


def birth_number_meaning(number: int) -> str:
    """Symbolic meaning of the life-path digit, from the shared rule book."""
    return DIGIT_MEANINGS[number]
