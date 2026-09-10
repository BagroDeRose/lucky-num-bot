"""Deterministic pattern detection and scoring.

Every function here is a pure function of the input digits — no randomness,
no external calls, no AI. Given the same digits, it always returns the same
result, which is the core guarantee of the product.
"""

from __future__ import annotations

from app.analysis.models import DetectedPattern, ScoreBreakdown, ScoreFactor
from app.analysis.rules import (
    BASE_DIGIT_PROFILE,
    DIGIT_MEANINGS,
    OVERALL_MAX,
    OVERALL_MIN,
    REPEATED_DIGIT_THRESHOLD,
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


def _is_ascending_run(digits: list[int]) -> bool:
    return len(digits) >= 3 and all(
        digits[i + 1] == digits[i] + 1 for i in range(len(digits) - 1)
    )


def _is_descending_run(digits: list[int]) -> bool:
    return len(digits) >= 3 and all(
        digits[i + 1] == digits[i] - 1 for i in range(len(digits) - 1)
    )


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
                description=f"Цифра {d} встречается {freq[d]} раз(а) — усиленное влияние.",
            )
        )

    pairs = find_repeated_pairs(digits)
    for p in pairs:
        patterns.append(
            DetectedPattern(
                name=f"repeated_pair_{p}",
                description=f"Пара «{p}» создаёт визуальный и символический акцент.",
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


def compute_scores(
    reduced_number: int,
    digits: list[int],
    freq: dict[int, int],
    repeated_digits: list[int],
    repeated_pairs: list[str],
    patterns: list[DetectedPattern],
) -> tuple[int, int, int, int, int, ScoreBreakdown]:
    """Returns (money, luck, growth, stability, overall, breakdown)."""

    base_money, base_luck, base_growth, base_stability = BASE_DIGIT_PROFILE[reduced_number]
    breakdown = ScoreBreakdown()

    money, luck, growth, stability = base_money, base_luck, base_growth, base_stability

    breakdown.money.append(
        ScoreFactor(
            factor="reduced_number",
            effect=f"+{base_money}",
            reason=(
                f"Итоговое число {reduced_number} символизирует "
                f"{DIGIT_MEANINGS[reduced_number]}."
            ),
        )
    )
    breakdown.luck.append(
        ScoreFactor(
            factor="reduced_number",
            effect=f"+{base_luck}",
            reason=(
                f"Итоговое число {reduced_number} символизирует "
                f"{DIGIT_MEANINGS[reduced_number]}."
            ),
        )
    )
    breakdown.growth.append(
        ScoreFactor(
            factor="reduced_number",
            effect=f"+{base_growth}",
            reason=(
                f"Итоговое число {reduced_number} символизирует "
                f"{DIGIT_MEANINGS[reduced_number]}."
            ),
        )
    )
    breakdown.stability.append(
        ScoreFactor(
            factor="reduced_number",
            effect=f"+{base_stability}",
            reason=(
                f"Итоговое число {reduced_number} символизирует "
                f"{DIGIT_MEANINGS[reduced_number]}."
            ),
        )
    )

    # Repeated digits: a small, capped bonus to money and luck.
    if repeated_digits:
        bonus = min(2, len(repeated_digits))
        money += bonus
        luck += bonus
        breakdown.money.append(
            ScoreFactor(
                factor="repeated_digits",
                effect=f"+{bonus}",
                reason="Повторяющиеся цифры усиливают символическую значимость номера.",
            )
        )
        breakdown.luck.append(
            ScoreFactor(
                factor="repeated_digits",
                effect=f"+{bonus}",
                reason="Повторяющиеся цифры усиливают символическую значимость номера.",
            )
        )

    # Repeated adjacent pairs: bonus to stability and luck.
    if repeated_pairs:
        bonus = min(2, len(repeated_pairs))
        stability += bonus
        luck += 1
        breakdown.stability.append(
            ScoreFactor(
                factor="repeated_pairs",
                effect=f"+{bonus}",
                reason="Соседние одинаковые цифры создают ощущение устойчивости номера.",
            )
        )
        breakdown.luck.append(
            ScoreFactor(
                factor="repeated_pairs",
                effect="+1",
                reason="Парные цифры добавляют номеру дополнительную «изюминку».",
            )
        )

    pattern_names = {p.name for p in patterns}

    if "palindrome" in pattern_names:
        luck += 2
        stability += 1
        breakdown.luck.append(
            ScoreFactor(
                factor="palindrome",
                effect="+2",
                reason="Номер-палиндром — редкое и заметное свойство в нашей системе.",
            )
        )
        breakdown.stability.append(
            ScoreFactor(
                factor="palindrome",
                effect="+1",
                reason="Симметрия номера ассоциируется с внутренним равновесием.",
            )
        )

    if "ascending_sequence" in pattern_names:
        growth += 2
        breakdown.growth.append(
            ScoreFactor(
                factor="ascending_sequence",
                effect="+2",
                reason="Возрастающая последовательность символизирует движение вперёд.",
            )
        )

    if "descending_sequence" in pattern_names:
        growth += 1
        stability += 1
        breakdown.growth.append(
            ScoreFactor(
                factor="descending_sequence",
                effect="+1",
                reason="Убывающая последовательность символизирует завершение цикла.",
            )
        )
        breakdown.stability.append(
            ScoreFactor(
                factor="descending_sequence",
                effect="+1",
                reason="Плавный спад ассоциируется с контролем и порядком.",
            )
        )

    # Extra copies of digit 8 beyond the first reinforce the "money" theme.
    extra_eights = max(0, freq.get(8, 0) - 1)
    if extra_eights:
        bonus = min(2, extra_eights)
        money += bonus
        breakdown.money.append(
            ScoreFactor(
                factor="digit_8_frequency",
                effect=f"+{bonus}",
                reason="Дополнительные восьмёрки усиливают денежную символику номера.",
            )
        )

    # Extra copies of digit 7 beyond the first reinforce the "luck" theme.
    extra_sevens = max(0, freq.get(7, 0) - 1)
    if extra_sevens:
        bonus = min(2, extra_sevens)
        luck += bonus
        breakdown.luck.append(
            ScoreFactor(
                factor="digit_7_frequency",
                effect=f"+{bonus}",
                reason="Дополнительные семёрки усиливают символику удачи.",
            )
        )

    money = _clamp(money, SCORE_MIN, SCORE_MAX)
    luck = _clamp(luck, SCORE_MIN, SCORE_MAX)
    growth = _clamp(growth, SCORE_MIN, SCORE_MAX)
    stability = _clamp(stability, SCORE_MIN, SCORE_MAX)

    overall = _clamp(round((money + luck + growth + stability) * 2.5), OVERALL_MIN, OVERALL_MAX)

    return money, luck, growth, stability, overall, breakdown
