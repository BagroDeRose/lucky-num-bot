"""The deterministic analysis engine — the core business logic of LuckyNum.

`analyze()` is a pure function: same input, same algorithm version -> same
output, always. No randomness, no network calls, no AI.
"""

from __future__ import annotations

from app.analysis.models import AnalysisResult
from app.analysis.rules import ALGORITHM_VERSION, MAX_SERIAL_LENGTH, MIN_SERIAL_LENGTH
from app.analysis.scoring import (
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


def analyze(raw_number: str) -> AnalysisResult:
    """Run the full deterministic analysis pipeline on a raw user input string.

    Raises ValidationError if the input is not a valid serial number.
    """
    normalized = validate_serial_number(raw_number)
    digits = [int(c) for c in normalized]

    digit_sum = sum(digits)
    reduced_number = reduce_to_single_digit(digit_sum)

    freq = digit_frequency(digits)
    repeated_digits = find_repeated_digits(freq)
    repeated_pairs = find_repeated_pairs(digits)
    patterns = detect_patterns(digits, normalized)

    money, luck, growth, stability, overall, breakdown = compute_scores(
        reduced_number=reduced_number,
        freq=freq,
        repeated_pairs=repeated_pairs,
        patterns=patterns,
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
