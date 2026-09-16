"""The entertainment numerology "rule book".

Everything in this module is symbolic and for entertainment purposes only.
It is intentionally explicit and data-driven so the model can be tuned or
extended (e.g. new algorithm versions) without touching engine logic.

SCORING FORMULA (see app.analysis.scoring.compute_scores for the implementation)
----------------------------------------------------------------------------
For each of the four sub-scores (money, luck, growth, stability), the value
is built additively from independent, non-overlapping signals and then
clamped to [SCORE_MIN, SCORE_MAX]:

1. Base value from BASE_DIGIT_PROFILE[reduced_number] — the numerology
   "final number" of the banknote, on a 0-6 scale per category.
2. Digit-emphasis bonus — for every digit that appears more than once,
   a capped bonus (see DIGIT_EMPHASIS_CAP) is added *only* to that digit's
   own dominant category/categories, as looked up from BASE_DIGIT_PROFILE.
   A digit whose profile has no unique dominant category (only digit 0,
   "потенциал... не выбрало свой путь") contributes no emphasis bonus to
   any category — this is deliberate: an undirected digit shouldn't inflate
   a specific theme just by repeating.
   This is the ONLY mechanism keyed by raw digit frequency, so a digit's
   repetition is never counted twice.
3. Repeated-pairs bonus — adjacent identical digits (e.g. "22") are a
   distinct *structural* signal (visual/positional, not just frequency) and
   add a small bonus to stability and luck.
4. Pattern bonuses — palindrome (+luck, +stability), ascending sequence
   (+growth), descending sequence (+growth, +stability). Independent
   structural signals, do not overlap with 2 or 3.

overall_score = round((money + luck + growth + stability) * 2.5), which is
mathematically guaranteed to land in [0, 100] because each sub-score is
already clamped to [0, 10] before the sum.
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

# Minimum number of occurrences of a digit within the serial to count as
# "repeated" for *display* purposes (AnalysisResult.repeated_digits and the
# "repeated_digit_N" detected pattern). Independent of the scoring bonus
# below, which activates from 2 occurrences.
REPEATED_DIGIT_THRESHOLD = 3

# Category names in the same order as BASE_DIGIT_PROFILE tuples.
SCORE_CATEGORIES = ("money", "luck", "growth", "stability")

# Cap on the digit-emphasis scoring bonus (see module docstring, point 2).
DIGIT_EMPHASIS_CAP = 3

# --- Date-of-birth personalization -------------------------------------
#
# OPTIONAL layer. An analysis without a birth date is scored exactly as
# before — every rule below contributes nothing when birth_number is None,
# so historical serial-only results stay reproducible byte-for-byte.
#
# THE CALCULATION, STATED PLAINLY
#
# 1. birth_number ("life path") — take the birth date's digits in the order
#    DD MM YYYY, sum them, and reduce that sum to a single digit with the
#    SAME reduction the serial number already uses
#    (app.analysis.engine.reduce_to_single_digit). No new arithmetic is
#    introduced: 07.03.1990 -> 0+7+0+3+1+9+9+0 = 29 -> 2+9 = 11 -> 1+1 = 2.
#    Its symbolic meaning is read from DIGIT_MEANINGS above — the same table
#    the serial's own digits use, so nothing is invented for birth dates.
#
# 2. resonance — how the banknote relates to this particular person. Only
#    objectively checkable relations are used, never a vibe:
#      "same_number" — the serial's reduced_number equals birth_number;
#      "present"     — birth_number literally occurs among the serial digits;
#      "absent"      — it does not occur.
#
# 3. the bonus — resonance adds a small, bounded amount to the birth digit's
#    own dominant category/categories, looked up from BASE_DIGIT_PROFILE by
#    exactly the same "which category does this digit score highest in" rule
#    the digit-emphasis bonus already uses. Digit 0 is tied across all four
#    categories and therefore grants no bonus, mirroring the existing
#    exclusion for undirected digits — that guard is defensive only, since
#    any accepted year contributes a nonzero digit and the life path is
#    therefore always 1..9 (pinned by a test).
#
# The bonus is deliberately capped at +2 — smaller than DIGIT_EMPHASIS_CAP
# (3) and well below a digit's base profile (up to 6) — so personalization
# adjusts the reading without ever dominating what the banknote itself says.
PERSONAL_RESONANCE_CAP = 2
RESONANCE_BONUS: dict[str, int] = {
    "same_number": 2,
    "present": 1,
    "absent": 0,
}

# Accepted birth years. The lower bound is a sanity floor rather than a
# claim about users; the upper bound is "not in the future", enforced
# against the current date at validation time.
MIN_BIRTH_YEAR = 1900
