"""The deterministic analysis engine — the core business logic of LuckyNum.

`analyze()` is a pure function: same input, same algorithm version -> same
output, always. No randomness, no network calls, no AI.
"""

from __future__ import annotations

import datetime as dt

from app.analysis.birth import birth_number, birth_number_meaning
from app.analysis.models import AnalysisResult
from app.analysis.rules import ALGORITHM_VERSION, MAX_SERIAL_LENGTH, MIN_SERIAL_LENGTH
from app.analysis.scoring import (
    classify_resonance,
    compute_scores,
    detect_patterns,
    digit_frequency,
    find_repeated_digits,
    find_repeated_pairs,
)


class ValidationError(Exception):
    """Raised when a user-supplied serial number fails validation.

    `message` is a ready-to-display, Russian, user-facing explanation.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def normalize_serial_number(raw: str) -> str:
    """Trim whitespace only. Leading zeros and internal characters are preserved
    so the caller can validate the *original* content precisely.
    """
    return raw.strip()


def validate_serial_number(raw: str) -> str:
    """Validate a user-supplied serial number and return the normalized value.

    Raises ValidationError with a Russian user-facing message on failure.
    """
    normalized = normalize_serial_number(raw)

    if not normalized:
        raise ValidationError("Номер не может быть пустым. Отправьте, пожалуйста, серийный номер купюры.")

    if not normalized.isdigit():
        raise ValidationError(
            "Номер должен состоять только из цифр (0-9), без букв, пробелов внутри и "
            "других символов. Проверьте и отправьте ещё раз."
        )

    if len(normalized) < MIN_SERIAL_LENGTH:
        raise ValidationError(
            f"Номер слишком короткий. Минимальная длина — {MIN_SERIAL_LENGTH} цифры."
        )

    if len(normalized) > MAX_SERIAL_LENGTH:
        raise ValidationError(
            f"Номер слишком длинный. Максимальная длина — {MAX_SERIAL_LENGTH} цифр."
        )

    return normalized


def reduce_to_single_digit(n: int) -> int:
    """Numerological digit reduction. 0 stays 0 (all-zero input edge case)."""
    if n == 0:
        return 0
    while n >= 10:
        n = sum(int(c) for c in str(n))
    return n


def analyze(raw_number: str, birth_date: dt.date | None = None) -> AnalysisResult:
    """Run the full deterministic analysis pipeline on a raw user input string.

    Raises ValidationError if the input is not a valid serial number.

    `birth_date` is optional personalization. Passing None produces exactly
    the same result the engine has always produced for that serial number,
    which is what keeps pre-existing analyses and users who decline to share
    a date fully supported. Passing a date adds the deterministic life-path
    layer documented in app.analysis.rules — same date + same serial always
    yields the same result, with no randomness and no AI involvement.
    """
    normalized = validate_serial_number(raw_number)
    digits = [int(c) for c in normalized]

    digit_sum = sum(digits)
    reduced_number = reduce_to_single_digit(digit_sum)

    freq = digit_frequency(digits)
    repeated_digits = find_repeated_digits(freq)
    repeated_pairs = find_repeated_pairs(digits)
    patterns = detect_patterns(digits, normalized)

    personal_number = birth_number(birth_date) if birth_date is not None else None

    money, luck, growth, stability, overall, breakdown = compute_scores(
        reduced_number=reduced_number,
        freq=freq,
        repeated_pairs=repeated_pairs,
        patterns=patterns,
        birth_number=personal_number,
    )

    return AnalysisResult(
        algorithm_version=ALGORITHM_VERSION,
        normalized_number=normalized,
        digits=digits,
        digit_sum=digit_sum,
        reduced_number=reduced_number,
        digit_frequency=freq,
        repeated_digits=repeated_digits,
        repeated_pairs=repeated_pairs,
        detected_patterns=patterns,
        birth_number=personal_number,
        birth_number_meaning=(
            birth_number_meaning(personal_number) if personal_number is not None else None
        ),
        birth_resonance=(
            classify_resonance(reduced_number, freq, personal_number)
            if personal_number is not None
            else None
        ),
        birth_digit_in_serial_count=(
            freq.get(personal_number, 0) if personal_number is not None else 0
        ),
        money_score=money,
        luck_score=luck,
        growth_score=growth,
        stability_score=stability,
        overall_score=overall,
        score_breakdown=breakdown,
    )


def analyze_many(raw_numbers: list[str]) -> list[AnalysisResult]:
    """Analyze multiple serial numbers. Reserved for future comparison/ranking
    features; not exposed in the MVP UX yet.
    """
    return [analyze(n) for n in raw_numbers]
