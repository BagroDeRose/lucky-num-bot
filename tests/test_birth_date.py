"""Tests for the date-of-birth layer: validation, the deterministic
calculation, its bounded effect on scoring, and backwards compatibility with
analyses created before the feature existed.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.ai.report_generator import _build_ai_payload
from app.analysis.birth import (
    BirthDateError,
    birth_digits,
    birth_number,
    parse_birth_date,
)
from app.analysis.engine import analyze
from app.analysis.interpreter import render_fallback_full_report, render_teaser
from app.analysis.models import AnalysisResult
from app.analysis.rules import MIN_BIRTH_YEAR, PERSONAL_RESONANCE_CAP
from app.formatting import MAX_TELEGRAM_TEXT_LENGTH, to_telegram_html

# --- validation ---------------------------------------------------------

TODAY = dt.date(2026, 9, 16)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("07.03.1990", dt.date(1990, 3, 7)),
        ("7.3.1990", dt.date(1990, 3, 7)),
        ("07/03/1990", dt.date(1990, 3, 7)),
        ("07-03-1990", dt.date(1990, 3, 7)),
        ("  07.03.1990  ", dt.date(1990, 3, 7)),
        ("29.02.2024", dt.date(2024, 2, 29)),  # real leap day
        ("01.01.1900", dt.date(1900, 1, 1)),  # lower boundary
    ],
)
def test_valid_birth_dates(raw: str, expected: dt.date) -> None:
    assert parse_birth_date(raw, today=TODAY) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "31.02.2000",  # February never has 31 days
        "29.02.2023",  # 2023 is not a leap year
        "32.01.2000",
        "07.13.1990",  # month 13
        "00.03.1990",
        "07.00.1990",
        "",
        "   ",
        "вчера",
        "1990",
        "07.03.90",  # two-digit year must not be silently guessed
        "07.03.19900",
        "07032026",
        "07.03-1990",  # mixed separators
    ],
)
def test_invalid_birth_dates_are_rejected(raw: str) -> None:
    with pytest.raises(BirthDateError):
        parse_birth_date(raw, today=TODAY)


def test_future_date_is_rejected() -> None:
    with pytest.raises(BirthDateError):
        parse_birth_date("17.09.2026", today=TODAY)


def test_today_is_accepted_as_a_boundary() -> None:
    assert parse_birth_date("16.09.2026", today=TODAY) == TODAY


def test_year_before_minimum_is_rejected() -> None:
    with pytest.raises(BirthDateError):
        parse_birth_date(f"01.01.{MIN_BIRTH_YEAR - 1}", today=TODAY)


def test_rejection_messages_are_user_facing_russian_without_internals() -> None:
    for raw in ("31.02.2000", "не дата", "17.09.2026"):
        with pytest.raises(BirthDateError) as exc_info:
            parse_birth_date(raw, today=TODAY)
        message = exc_info.value.message
        assert message
        for leak in ("traceback", "valueerror", "regex", "none", "exception"):
            assert leak not in message.lower()


# --- the deterministic calculation --------------------------------------


def test_birth_number_matches_the_documented_worked_example() -> None:
    """app.analysis.rules documents 07.03.1990 -> 0+7+0+3+1+9+9+0 = 29
    -> 2+9 = 11 -> 1+1 = 2. The rule book and the code must agree.
    """
    date = dt.date(1990, 3, 7)
    assert birth_digits(date) == [0, 7, 0, 3, 1, 9, 9, 0]
    assert sum(birth_digits(date)) == 29
    assert birth_number(date) == 2


def test_birth_number_is_always_a_single_digit() -> None:
    date = dt.date(1900, 1, 1)
    while date.year < 2027:
        assert 0 <= birth_number(date) <= 9
        date += dt.timedelta(days=997)


def test_same_inputs_always_produce_the_same_result() -> None:
    first = analyze("2200373", dt.date(1990, 3, 7))
    second = analyze("2200373", dt.date(1990, 3, 7))
    assert first.model_dump() == second.model_dump()


def test_different_birth_dates_can_produce_different_results() -> None:
    a = analyze("2200373", dt.date(1990, 3, 7))
    b = analyze("2200373", dt.date(1988, 8, 8))
    assert (a.birth_number, a.birth_resonance) != (b.birth_number, b.birth_resonance)


def test_resonance_classification_uses_only_checkable_facts() -> None:
    # 1111 reduces to 4; 03.01.1980 has life path 4 -> "same_number".
    same = analyze("1111", dt.date(1980, 1, 3))
    assert same.reduced_number == 4
    assert same.birth_number == 4
    assert same.birth_resonance == "same_number"

    # digit 2 occurs in 2200373 but the serial reduces to 8 -> "present"
    present = analyze("2200373", dt.date(1990, 3, 7))
    assert present.birth_number == 2
    assert present.birth_resonance == "present"
    assert present.birth_digit_in_serial_count == 2

    # life path 2 does not occur in 1111 and 1111 reduces to 4 -> "absent"
    absent = analyze("1111", dt.date(1990, 3, 7))
    assert absent.birth_number == 2
    assert absent.birth_resonance == "absent"
    assert absent.birth_digit_in_serial_count == 0


# --- bounded, non-dominating effect on scoring --------------------------


def test_serial_only_scoring_is_completely_unchanged_by_the_feature() -> None:
    """The strongest backwards-compatibility guarantee: passing no birth
    date must reproduce the historical result exactly.
    """
    for number in ("2200373", "555555", "123456", "1029384", "0000", "9" * 20):
        plain = analyze(number)
        assert plain.birth_number is None
        assert plain.birth_resonance is None
        assert plain.birth_digit_in_serial_count == 0
        # Scores identical to the documented serial-only formula inputs.
        assert plain.overall_score == round(
            (
                plain.money_score
                + plain.luck_score
                + plain.growth_score
                + plain.stability_score
            )
            * 2.5
        )


@pytest.mark.parametrize("number", ["2200373", "555555", "123456", "8888", "1029384"])
def test_birth_date_never_moves_a_subscore_by_more_than_the_cap(number: str) -> None:
    plain = analyze(number)
    for birth in (dt.date(1990, 3, 7), dt.date(1988, 8, 8), dt.date(2001, 1, 1)):
        personalized = analyze(number, birth)
        for field in ("money_score", "luck_score", "growth_score", "stability_score"):
            delta = getattr(personalized, field) - getattr(plain, field)
            assert 0 <= delta <= PERSONAL_RESONANCE_CAP, (
                f"{field} moved by {delta} for {number}/{birth}"
            )


def test_overall_score_shift_from_personalization_is_bounded() -> None:
    """The per-sub-score cap implies a bounded overall shift too. Among
    digits 1..9 only digit 9 has two tied dominant categories in
    BASE_DIGIT_PROFILE (money and growth), so at most two categories can
    receive the bonus: 2 categories x PERSONAL_RESONANCE_CAP x the 2.5
    overall factor = 10 points out of 100, and only for the rarest
    "same_number" alignment. Anything larger would mean personalization had
    started to dominate the banknote's own reading.
    """
    from app.analysis.birth import birth_number as life_path

    max_shift = PERSONAL_RESONANCE_CAP * 2 * 2.5

    representative: dict[int, dt.date] = {}
    date = dt.date(1950, 1, 1)
    while len(representative) < 9 and date < dt.date(2020, 1, 1):
        representative.setdefault(life_path(date), date)
        date += dt.timedelta(days=1)

    for number in ("2200373", "1111", "555555", "123456", "8888", "1029384", "9" * 20):
        base = analyze(number).overall_score
        for birth in representative.values():
            shift = analyze(number, birth).overall_score - base
            assert 0 <= shift <= max_shift, f"{number}/{birth} shifted the total by {shift}"


def test_scores_stay_within_their_documented_bounds_when_personalized() -> None:
    for number in ("8888", "2200373", "9" * 20):
        for birth in (dt.date(1979, 12, 26), dt.date(1990, 3, 7)):
            result = analyze(number, birth)
            for field in ("money_score", "luck_score", "growth_score", "stability_score"):
                assert 0 <= getattr(result, field) <= 10
            assert 0 <= result.overall_score <= 100


def test_life_path_is_never_zero_for_any_valid_date() -> None:
    """A documented property of the calculation rather than an assumption:
    every accepted year (>= MIN_BIRTH_YEAR) contributes at least one nonzero
    digit, so the life path always lands in 1..9. This is why the digit-0
    exclusion in compute_scores is defensive only.
    """
    seen = set()
    date = dt.date(MIN_BIRTH_YEAR, 1, 1)
    end = dt.date(2026, 12, 31)
    while date <= end:
        seen.add(birth_number(date))
        date += dt.timedelta(days=17)
    assert 0 not in seen
    assert seen <= set(range(1, 10))


def test_undirected_birth_digit_grants_no_bonus() -> None:
    """compute_scores is a public function that accepts any digit, so its
    digit-0 guard is exercised directly: an undirected digit (tied across
    all four categories in BASE_DIGIT_PROFILE) must add nothing, mirroring
    the existing digit-emphasis exclusion.
    """
    from app.analysis.scoring import compute_scores, detect_patterns, digit_frequency

    digits = [2, 2, 0, 0, 3, 7, 3]
    freq = digit_frequency(digits)
    patterns = detect_patterns(digits, "2200373")
    plain = compute_scores(8, freq, ["22", "00"], patterns)
    with_zero = compute_scores(8, freq, ["22", "00"], patterns, birth_number=0)
    assert plain[:5] == with_zero[:5]


# --- rendering ----------------------------------------------------------


def test_teaser_mentions_personalization_only_when_present() -> None:
    plain = render_teaser(analyze("2200373"))
    assert "число рождения" not in plain.lower()

    personalized = render_teaser(analyze("2200373", dt.date(1990, 3, 7)))
    assert "число рождения" in personalized.lower()
    assert to_telegram_html(personalized) == personalized
    assert len(personalized) < MAX_TELEGRAM_TEXT_LENGTH


def test_fallback_report_gains_a_personal_section_only_when_personalized() -> None:
    plain = render_fallback_full_report(analyze("2200373"))
    assert "ВАШЕ ЧИСЛО РОЖДЕНИЯ" not in plain.upper()

    personalized = render_fallback_full_report(analyze("2200373", dt.date(1990, 3, 7)))
    assert "Ваше число рождения" in personalized
    assert to_telegram_html(personalized) == personalized
    assert personalized.count("<b>") == personalized.count("</b>")
    assert len(personalized) < MAX_TELEGRAM_TEXT_LENGTH


def test_absent_resonance_is_phrased_neutrally_not_as_bad_news() -> None:
    report = render_fallback_full_report(analyze("1111", dt.date(1990, 3, 7)))
    for alarming in ("плохо", "неудач", "отрицатель", "проблем"):
        assert alarming not in report.lower()


# --- AI payload / privacy ------------------------------------------------


def test_ai_payload_has_no_birth_fields_for_serial_only_analysis() -> None:
    payload = _build_ai_payload(analyze("2200373"))
    for key in ("birth_number", "birth_number_meaning", "birth_resonance"):
        assert key not in payload


def test_ai_payload_carries_derived_birth_facts_but_never_the_raw_date() -> None:
    birth = dt.date(1990, 3, 7)
    payload = _build_ai_payload(analyze("2200373", birth))

    assert payload["birth_number"] == 2
    assert payload["birth_resonance"] == "present"
    assert payload["birth_digit_in_serial_count"] == 2
    assert payload["birth_number_meaning"]

    # The date itself must not be reconstructible from the payload.
    import json

    serialized = json.dumps(payload, ensure_ascii=False)
    for fragment in ("1990", "07.03", "1990-03-07", "03.1990"):
        assert fragment not in serialized


def test_ai_payload_still_excludes_internal_scoring_mechanics() -> None:
    payload = _build_ai_payload(analyze("2200373", dt.date(1990, 3, 7)))
    assert "score_breakdown" not in payload
    assert "algorithm_version" not in payload


# --- legacy compatibility ------------------------------------------------


def test_analysis_payload_from_before_the_feature_still_validates() -> None:
    """Exactly the shape stored by the previous release: no birth_* keys at
    all. Re-validating it (which history rendering and report generation
    both do) must not raise and must behave as a serial-only analysis.
    """
    legacy = analyze("2200373").model_dump_public()
    for key in ("birth_number", "birth_number_meaning", "birth_resonance", "birth_digit_in_serial_count"):
        legacy.pop(key, None)

    restored = AnalysisResult.model_validate(legacy)
    assert restored.birth_number is None
    assert restored.is_personalized is False
    assert restored.birth_digit_in_serial_count == 0
    # and it still renders both surfaces
    assert render_teaser(restored)
    assert render_fallback_full_report(restored)
    assert "birth_number" not in _build_ai_payload(restored)


def test_personalized_payload_round_trips_through_storage() -> None:
    result = analyze("2200373", dt.date(1990, 3, 7))
    restored = AnalysisResult.model_validate(result.model_dump_public())
    assert restored.birth_number == result.birth_number
    assert restored.birth_resonance == result.birth_resonance
    assert restored.overall_score == result.overall_score
    assert render_fallback_full_report(restored) == render_fallback_full_report(result)
