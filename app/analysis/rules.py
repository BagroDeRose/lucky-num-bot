"""The entertainment numerology "rule book".

Everything in this module is symbolic and for entertainment purposes only.
It is intentionally explicit and data-driven so the model can be tuned or
extended (e.g. new algorithm versions) without touching engine logic.
"""

from __future__ import annotations

ALGORITHM_VERSION = "1.0"

MIN_SERIAL_LENGTH = 4
MAX_SERIAL_LENGTH = 20

# Symbolic meaning of each reduced digit (0-9), entertainment-oriented only.
DIGIT_MEANINGS: dict[int, str] = {
    0: "потенциал и незаписанная страница — число ещё не выбрало свой путь",
    1: "начало и независимость",
    2: "партнёрство и баланс",
    3: "рост и общение",
    4: "порядок и фундамент",
    5: "движение и перемены",
    6: "стабильность и комфорт",
    7: "удача и интуиция",
    8: "деньги и материальный результат",
    9: "завершение и итог",
}

# Base per-category strength of each reduced digit, on a 0-6 scale.
# Bonuses from detected patterns are added on top and the total is clamped to 0-10.
# Order: money, luck, growth, stability
BASE_DIGIT_PROFILE: dict[int, tuple[int, int, int, int]] = {
    0: (2, 2, 2, 2),
    1: (3, 3, 6, 2),
    2: (3, 3, 3, 6),
    3: (3, 3, 6, 3),
    4: (3, 2, 3, 6),
    5: (3, 4, 5, 2),
    6: (5, 3, 3, 6),
    7: (3, 6, 3, 4),
    8: (6, 4, 4, 4),
    9: (5, 4, 5, 4),
}

SCORE_MIN = 0
SCORE_MAX = 10
OVERALL_MIN = 0
OVERALL_MAX = 100

# Minimum number of occurrences of a digit within the serial to count as "repeated".
REPEATED_DIGIT_THRESHOLD = 3
