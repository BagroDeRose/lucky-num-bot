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
    assert boosted.money_score >= baseline.money_score
