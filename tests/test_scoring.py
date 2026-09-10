from __future__ import annotations

import pytest

from app.analysis.engine import analyze
from app.analysis.rules import OVERALL_MAX, OVERALL_MIN, SCORE_MAX, SCORE_MIN

SAMPLE_NUMBERS = [
    "2200373",
    "0000000",
    "9999999",
    "1234567",
    "1111111",
    "5005069",
    "3144401",
    "1029384756",
]


@pytest.mark.parametrize("number", SAMPLE_NUMBERS)
def test_scores_within_bounds(number: str) -> None:
    result = analyze(number)
    for score in (result.money_score, result.luck_score, result.growth_score, result.stability_score):
        assert SCORE_MIN <= score <= SCORE_MAX
    assert OVERALL_MIN <= result.overall_score <= OVERALL_MAX


def test_overall_score_matches_subscore_formula() -> None:
    result = analyze("2200373")
    expected = round(
        (result.money_score + result.luck_score + result.growth_score + result.stability_score)
        * 2.5
    )
    assert result.overall_score == expected


def test_score_breakdown_has_at_least_one_factor_per_category() -> None:
    result = analyze("2200373")
    assert len(result.score_breakdown.money) >= 1
    assert len(result.score_breakdown.luck) >= 1
    assert len(result.score_breakdown.growth) >= 1
    assert len(result.score_breakdown.stability) >= 1


def test_scores_differentiate_between_numbers() -> None:
    """Not every number should get the same score — meaningful differentiation."""
    results = {n: analyze(n).overall_score for n in SAMPLE_NUMBERS}
    assert len(set(results.values())) > 1


def test_repeated_eights_boost_money_score() -> None:
    baseline = analyze("8123456")
    boosted = analyze("8123458")
    assert boosted.money_score > baseline.money_score


def test_repeated_neutral_zero_does_not_inflate_any_score() -> None:
    """Digit 0 has no dominant category in BASE_DIGIT_PROFILE (all tied), so
    repeating it must not grant a digit-emphasis bonus to any sub-score.
    Regression test for a bug where the old blanket "repeated_digits" bonus
    inflated money/luck even for thematically neutral repeated digits.
    """
    result = analyze("1020304")  # digit 0 repeats 3x, no other digit repeats
    emphasis_factors = [
        f
        for category in (
            result.score_breakdown.money,
            result.score_breakdown.luck,
            result.score_breakdown.growth,
            result.score_breakdown.stability,
        )
        for f in category
        if f.factor == "digit_emphasis_0"
    ]
    assert emphasis_factors == []


def test_digit_emphasis_is_not_double_counted() -> None:
    """A digit's repetition must be represented by exactly one breakdown
    factor per affected category — never both a generic "repeated" bonus
    and a digit-specific frequency bonus for the same underlying fact.
    """
    result = analyze("8888888")
    money_factor_names = [f.factor for f in result.score_breakdown.money]
    assert money_factor_names.count("digit_emphasis_8") == 1
    assert "repeated_digits" not in money_factor_names
    assert "digit_8_frequency" not in money_factor_names


def test_pure_money_digit_number_scores_well_above_baseline() -> None:
    """A banknote consisting entirely of the "money" digit (8) must score
    meaningfully higher on money than an unrelated number, even though its
    numerological digital-root reduction does not itself land on 8.
    """
    themed = analyze("8888888")
    baseline = analyze("1234567")
    assert themed.money_score > baseline.money_score


def test_digit_emphasis_bonus_is_capped() -> None:
    """Extreme repetition must not blow past the documented cap."""
    from app.analysis.rules import DIGIT_EMPHASIS_CAP

    result = analyze("77777777777777777777")  # 20x '7', max allowed length
    luck_bonus = next(
        f.effect for f in result.score_breakdown.luck if f.factor == "digit_emphasis_7"
    )
    assert luck_bonus == f"+{DIGIT_EMPHASIS_CAP}"
