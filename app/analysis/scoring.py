"""Deterministic pattern detection and scoring.

Every function here is a pure function of the input digits — no randomness,
no external calls, no AI. Given the same digits, it always returns the same
result, which is the core guarantee of the product.
"""

from __future__ import annotations

from app.analysis.models import DetectedPattern, ScoreBreakdown, ScoreFactor
from app.analysis.rules import (
    BASE_DIGIT_PROFILE,
    DIGIT_EMPHASIS_CAP,
    DIGIT_MEANINGS,
    OVERALL_MAX,
    OVERALL_MIN,
    REPEATED_DIGIT_THRESHOLD,
    SCORE_CATEGORIES,
    SCORE_MAX,
    SCORE_MIN,
)


def digit_frequency(digits: list[int]) -> dict[int, int]:
    freq: dict[int, int] = {}
    for d in digits:
        freq[d] = freq.get(d, 0) + 1
    return freq


def find_repeated_digits(freq: dict[int, int]) -> list[int]:
    return sorted(d for d, count in freq.items() if count >= REPEATED_DIGIT_THRESHOLD)


def find_repeated_pairs(digits: list[int]) -> list[str]:
    """Adjacent identical-digit pairs, e.g. "22" in [2, 2, 5]. Deduplicated, ordered."""
    pairs: list[str] = []
    seen: set[str] = set()
    for i in range(len(digits) - 1):
        if digits[i] == digits[i + 1]:
            pair = f"{digits[i]}{digits[i + 1]}"
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    return pairs


def _longest_run(digits: list[int], ascending: bool) -> int:
    best = 1
    current = 1
    for i in range(1, len(digits)):
        step = digits[i] - digits[i - 1]
        matches = step == 1 if ascending else step == -1
        current = current + 1 if matches else 1
        best = max(best, current)
    return best


def detect_patterns(digits: list[int], normalized_number: str) -> list[DetectedPattern]:
    patterns: list[DetectedPattern] = []

    if len(normalized_number) >= 3 and normalized_number == normalized_number[::-1]:
        patterns.append(
            DetectedPattern(
                name="palindrome",
                description="Номер читается одинаково в обе стороны — редкая симметрия.",
            )
        )

    if _longest_run(digits, ascending=True) >= 3:
        patterns.append(
            DetectedPattern(
                name="ascending_sequence",
                description="В номере встречается возрастающая последовательность цифр.",
            )
        )

    if _longest_run(digits, ascending=False) >= 3:
        patterns.append(
            DetectedPattern(
                name="descending_sequence",
                description="В номере встречается убывающая последовательность цифр.",
            )
        )

    freq = digit_frequency(digits)
    repeated = find_repeated_digits(freq)
    for d in repeated:
        patterns.append(
            DetectedPattern(
                name=f"repeated_digit_{d}",
                description=f"Цифра {d} встречается в номере {freq[d]} раз(а).",
            )
        )

    pairs = find_repeated_pairs(digits)
    for p in pairs:
        patterns.append(
            DetectedPattern(
                name=f"repeated_pair_{p}",
                description=f"Два одинаковых знака подряд — «{p}».",
            )
        )

    unique_count = len(freq)
    if unique_count == 1:
        patterns.append(
            DetectedPattern(
                name="single_digit_number",
                description="Номер состоит из одной-единственной повторяющейся цифры.",
            )
        )

    return patterns


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _dominant_categories(digit: int) -> list[str]:
    """Category/categories where `digit` scores highest in BASE_DIGIT_PROFILE.

    Returns all four category names if the digit has no unique preference
    (currently only digit 0 — "потенциал... не выбрало свой путь").
    """
    profile = BASE_DIGIT_PROFILE[digit]
    top = max(profile)
    return [cat for cat, value in zip(SCORE_CATEGORIES, profile, strict=True) if value == top]


def compute_scores(
    reduced_number: int,
    freq: dict[int, int],
    repeated_pairs: list[str],
    patterns: list[DetectedPattern],
) -> tuple[int, int, int, int, int, ScoreBreakdown]:
    """Returns (money, luck, growth, stability, overall, breakdown).

    See the module docstring in app.analysis.rules for the full formula.
    """

    scores = dict(zip(SCORE_CATEGORIES, BASE_DIGIT_PROFILE[reduced_number], strict=True))
    breakdown = ScoreBreakdown()
    breakdown_lists = {
        "money": breakdown.money,
        "luck": breakdown.luck,
        "growth": breakdown.growth,
        "stability": breakdown.stability,
    }

    def add(category: str, amount: int, factor: str, reason: str) -> None:
        if amount == 0:
            return
        scores[category] += amount
        breakdown_lists[category].append(
            ScoreFactor(factor=factor, effect=f"+{amount}", reason=reason)
        )

    # `scores` already holds the base profile values (set above); just log
    # the explanatory breakdown entries without mutating them again.
    for category, base_value in zip(SCORE_CATEGORIES, BASE_DIGIT_PROFILE[reduced_number], strict=True):
        breakdown_lists[category].append(
            ScoreFactor(
                factor="reduced_number",
                effect=f"+{base_value}",
                reason=(
                    f"Итоговое число {reduced_number} символизирует "
                    f"{DIGIT_MEANINGS[reduced_number]}."
                ),
            )
        )

    # Digit-emphasis bonus: the ONLY mechanism keyed by raw digit frequency,
    # so no digit's repetition is ever counted twice. Each digit with 2+
    # occurrences adds a capped bonus to *its own* dominant category/ies.
    for digit in sorted(freq):
        extra = freq[digit] - 1
        if extra <= 0:
            continue
        bonus = min(DIGIT_EMPHASIS_CAP, extra)
        categories = _dominant_categories(digit)
        if len(categories) == len(SCORE_CATEGORIES):
            continue  # no directional preference (digit 0) -> no emphasis bonus
        for category in categories:
            add(
                category,
                bonus,
                f"digit_emphasis_{digit}",
                f"Цифра {digit} встречается {freq[digit]} раз(а), усиливая "
                f"{DIGIT_MEANINGS[digit]}.",
            )

    # Repeated adjacent pairs: a distinct *structural* (positional) signal,
    # independent of raw frequency above.
    if repeated_pairs:
        bonus = min(2, len(repeated_pairs))
        add(
            "stability",
            bonus,
            "repeated_pairs",
            "Соседние одинаковые цифры создают ощущение устойчивости номера.",
        )
        add(
            "luck",
            1,
            "repeated_pairs",
            "Парные цифры добавляют номеру дополнительную «изюминку».",
        )

    pattern_names = {p.name for p in patterns}

    if "palindrome" in pattern_names:
        add(
            "luck",
            2,
            "palindrome",
            "Номер-палиндром — редкое и заметное свойство в нашей системе.",
        )
        add(
            "stability",
            1,
            "palindrome",
            "Симметрия номера ассоциируется с внутренним равновесием.",
        )

    if "ascending_sequence" in pattern_names:
        add(
            "growth",
            2,
            "ascending_sequence",
            "Возрастающая последовательность символизирует движение вперёд.",
        )

    if "descending_sequence" in pattern_names:
        add(
            "growth",
            1,
            "descending_sequence",
            "Убывающая последовательность символизирует завершение цикла.",
        )
        add(
            "stability",
            1,
            "descending_sequence",
            "Плавный спад ассоциируется с контролем и порядком.",
        )

    for category in SCORE_CATEGORIES:
        scores[category] = _clamp(scores[category], SCORE_MIN, SCORE_MAX)

    overall = _clamp(
        round(sum(scores.values()) * 2.5), OVERALL_MIN, OVERALL_MAX
    )

    return (
        scores["money"],
        scores["luck"],
        scores["growth"],
        scores["stability"],
        overall,
        breakdown,
    )
