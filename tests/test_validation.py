from __future__ import annotations

import pytest

from app.analysis.engine import ValidationError, validate_serial_number
from app.analysis.rules import MAX_SERIAL_LENGTH, MIN_SERIAL_LENGTH


def test_valid_numeric_number() -> None:
    assert validate_serial_number("2200373") == "2200373"


def test_strips_surrounding_whitespace() -> None:
    assert validate_serial_number("  2200373  ") == "2200373"


def test_preserves_leading_zeroes() -> None:
    assert validate_serial_number("0012345") == "0012345"


@pytest.mark.parametrize(
    "raw",
    [
        "22A0373",
        "2200373a",
        "abcdefg",
        "22 00373",
        "2200-373",
        "22.00373",
    ],
)
def test_rejects_letters_and_symbols(raw: str) -> None:
    with pytest.raises(ValidationError):
        validate_serial_number(raw)


def test_rejects_empty_input() -> None:
    with pytest.raises(ValidationError):
        validate_serial_number("")


def test_rejects_whitespace_only_input() -> None:
    with pytest.raises(ValidationError):
        validate_serial_number("    ")


def test_rejects_too_short() -> None:
    with pytest.raises(ValidationError):
        validate_serial_number("12")


def test_rejects_too_long() -> None:
    with pytest.raises(ValidationError):
        validate_serial_number("1" * 25)


def test_accepts_exactly_minimum_length() -> None:
    number = "1" * MIN_SERIAL_LENGTH
    assert validate_serial_number(number) == number


def test_rejects_one_below_minimum_length() -> None:
    with pytest.raises(ValidationError):
        validate_serial_number("1" * (MIN_SERIAL_LENGTH - 1))


def test_accepts_exactly_maximum_length() -> None:
    number = "1" * MAX_SERIAL_LENGTH
    assert validate_serial_number(number) == number


def test_rejects_one_above_maximum_length() -> None:
    with pytest.raises(ValidationError):
        validate_serial_number("1" * (MAX_SERIAL_LENGTH + 1))


def test_error_message_is_user_facing_russian_text() -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_serial_number("abc")
    assert exc_info.value.message
    assert "цифр" in exc_info.value.message.lower()
