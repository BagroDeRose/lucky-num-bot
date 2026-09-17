"""Regression coverage for conflicting score headings in the paid report.

Incident: serial 2210005 with birth number 3 produced
    💰 ПРОФИЛЬ УДАЧИ — 3/10
    🍀 ПРОФИЛЬ УДАЧИ — 4/10
The application had computed exactly one value per category (money 3, luck
4); the model mislabelled the money heading, and the application delivered
its free-form text unchecked. Headings and scores are now rendered by the
application (app.ai.report_contract) and model prose is validated against
the canonical analysis.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from _report_fakes import valid_report_json

from app.ai import report_generator
from app.ai.report_contract import (
    ReportContractError,
    build_report,
    score_headings,
    verify_canonical_scores,
)
from app.analysis.birth import birth_number
from app.analysis.engine import analyze
from app.analysis.interpreter import render_digit_sum_calculation, render_fallback_full_report
from app.analysis.models import AnalysisResult
from app.analysis.rules import PERSONAL_RESONANCE_CAP
from app.config import settings
from app.formatting import MAX_TELEGRAM_TEXT_LENGTH, to_telegram_html


def _date_with_birth_number(target: int) -> dt.date:
    date = dt.date(1980, 1, 1)
    while birth_number(date) != target:
        date += dt.timedelta(days=1)
    return date


def _canonical(result: AnalysisResult) -> dict[str, list[int]]:
    return {
        "money_score": [result.money_score],
        "luck_score": [result.luck_score],
        "growth_score": [result.growth_score],
        "stability_score": [result.stability_score],
        "overall_score": [result.overall_score],
    }


# The exact output shape from the incident, expressed in the new contract:
# the money body carries a mislabelled luck heading with money's value.
_INCIDENT_MONEY_BODY = "💰 <b>ПРОФИЛЬ УДАЧИ — 3/10</b>\nДеньги здесь приходят не сразу."


# --- F. the exact incident --------------------------------------------------


def test_incident_2210005_birth_number_3_canonical_facts() -> None:
    """The application's own data for the incident: 2+2+1+0+0+0+5 = 10 -> 1,
    birth number 3 absent from the serial, and — because resonance is
    "absent" — no personalization bonus, so every score equals the base.
    """
    result = analyze("2210005", _date_with_birth_number(3))
    base = analyze("2210005")

    assert result.digit_sum == 10
    assert result.reduced_number == 1
    assert result.birth_number == 3
    assert result.birth_resonance == "absent"
    assert result.birth_digit_in_serial_count == 0
    assert 3 not in result.digits
    for field in ("money_score", "luck_score", "growth_score", "stability_score", "overall_score"):
        assert getattr(result, field) == getattr(base, field)
    assert (result.money_score, result.luck_score) == (3, 4)


async def test_incident_output_is_rejected_not_delivered(monkeypatch) -> None:
    """Feeding the incident's mislabelled heading through the real
    generate_report path must raise the retryable error — the user never
    sees two luck scores.
    """
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    result = analyze("2210005", _date_with_birth_number(3))

    async def incident_model(system_prompt: str, user_prompt: str, **kwargs) -> str:
        return valid_report_json(money=_INCIDENT_MONEY_BODY)

    monkeypatch.setattr(report_generator, "complete_chat", incident_model)

    with pytest.raises(report_generator.ReportGenerationError):
        await report_generator.generate_report(result)


async def test_incident_internal_field_leak_is_rejected(monkeypatch) -> None:
    """The incident also printed "birth_resonance здесь absent" to the user."""
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    result = analyze("2210005", _date_with_birth_number(3))

    async def leaking_model(system_prompt: str, user_prompt: str, **kwargs) -> str:
        return valid_report_json(
            birth="В этом номере тройки нет, так что birth_resonance здесь absent."
        )

    monkeypatch.setattr(report_generator, "complete_chat", leaking_model)

    with pytest.raises(report_generator.ReportGenerationError):
        await report_generator.generate_report(result)


async def test_incident_example_with_valid_output_has_one_luck_heading(monkeypatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    result = analyze("2210005", _date_with_birth_number(3))

    async def model(system_prompt: str, user_prompt: str, **kwargs) -> str:
        return valid_report_json()

    monkeypatch.setattr(report_generator, "complete_chat", model)
    report = await report_generator.generate_report(result)

    headings = score_headings(report)
    assert headings == {
        "money_score": [3],
        "luck_score": [4],
        "growth_score": [result.growth_score],
        "stability_score": [result.stability_score],
        "overall_score": [result.overall_score],
    }
    assert report.count("ПРОФИЛЬ УДАЧИ") == 1
    assert "🎂 <b>ВАШЕ ЧИСЛО РОЖДЕНИЯ — 3</b>" in report
    assert "2 + 2 + 1 + 0 + 0 + 0 + 5 = 10\n1 + 0 = 1" in report
    assert "birth_resonance" not in report
    assert "absent" not in report


# --- A. no category can appear twice with different values -----------------


@pytest.mark.parametrize(
    "section,body",
    [
        ("money", "Про деньги — 4/10."),  # luck's value (4) inside the money body
        ("luck", "Удача тут на 3/10."),  # luck is 4, not 3
        ("summary", "Итоговая удача 4/10."),  # /10 outside its own profile
        ("verdict", "Это номер на 55/100."),  # overall is 50
        ("opening", "Удача — 4 из 10."),  # "из" form counts too
    ],
)
def test_bodies_cannot_restate_a_score_that_conflicts(section: str, body: str) -> None:
    result = analyze("2210005")
    with pytest.raises(ReportContractError):
        build_report(valid_report_json(**{section: body}), result)


def test_a_profile_may_restate_only_its_own_canonical_score() -> None:
    result = analyze("2210005")  # luck 4
    report = build_report(valid_report_json(luck="Удача здесь на свои 4/10."), result)
    assert score_headings(report)["luck_score"] == [4]


def test_verify_canonical_scores_rejects_a_duplicated_category() -> None:
    result = analyze("2210005")
    report = build_report(valid_report_json(), result)
    tampered = report + "\n\n🍀 <b>ПРОФИЛЬ УДАЧИ — 3/10</b>"
    with pytest.raises(ReportContractError):
        verify_canonical_scores(tampered, result)


def test_verify_canonical_scores_rejects_a_non_canonical_heading() -> None:
    result = analyze("2210005")
    report = build_report(valid_report_json(), result)
    tampered = report.replace(f"ИТОГ — {result.overall_score}/100", "ИТОГ — 99/100")
    with pytest.raises(ReportContractError):
        verify_canonical_scores(tampered, result)


@pytest.mark.parametrize(
    "body",
    [
        "🍀 <b>ПРОФИЛЬ УДАЧИ</b>\nЕщё раздел.",
        "<b>ИТОГ</b> получается спокойным.",
        "ПРОФИЛЬ РОСТА — всё хорошо.",
        "Здесь 💰 деньги.",
    ],
)
def test_bodies_cannot_open_a_second_section_heading(body: str) -> None:
    with pytest.raises(ReportContractError):
        build_report(valid_report_json(growth=body), analyze("2210005"))


def test_ordinary_prose_that_starts_like_a_heading_is_not_rejected() -> None:
    """A false rejection would cost the user a paid attempt."""
    report = build_report(
        valid_report_json(summary="Итог простой: номер спокойный и собранный."),
        analyze("2210005"),
    )
    assert "Итог простой" in report


# --- B. rendered scores equal canonical scores ------------------------------


@pytest.mark.parametrize(
    "number", ["2210005", "2200373", "555555", "123456", "121212", "1029384", "0000", "9" * 20]
)
@pytest.mark.parametrize("birth", [None, 1, 3, 8, 9])
def test_every_rendered_score_is_the_canonical_score_exactly_once(
    number: str, birth: int | None
) -> None:
    date = _date_with_birth_number(birth) if birth is not None else None
    result = analyze(number, date)
    report = build_report(valid_report_json(), result)

    assert score_headings(report) == _canonical(result)
    assert to_telegram_html(report) != "" and len(report) < MAX_TELEGRAM_TEXT_LENGTH


def test_ai_report_and_fallback_report_agree_on_every_score() -> None:
    """Both surfaces read the same AnalysisResult, so they can never show a
    user different numbers for the same analysis.
    """
    result = analyze("2210005", _date_with_birth_number(3))
    ai = score_headings(build_report(valid_report_json(), result))
    fallback = score_headings(render_fallback_full_report(result))
    assert ai == fallback == _canonical(result)


# --- C. the AI payload carries one authoritative score per category ---------


def test_ai_payload_contains_each_score_exactly_once() -> None:
    result = analyze("2210005", _date_with_birth_number(3))
    payload = report_generator._build_ai_payload(result)
    serialized = json.dumps(payload, ensure_ascii=False)

    for key in ("money_score", "luck_score", "growth_score", "stability_score", "overall_score"):
        assert serialized.count(f'"{key}"') == 1
        assert payload[key] == getattr(result, key)
    # No second source of numbers the model could pick from instead.
    assert "score_breakdown" not in serialized
    assert "base_" not in serialized
    assert "personal" not in serialized


# --- D. birth date changes scores only through the bounded mechanism --------


def test_rendered_personalized_scores_differ_from_base_only_within_the_cap() -> None:
    for number in ("2200373", "1111", "2210005"):
        base = analyze(number)
        for target in range(1, 10):
            result = analyze(number, _date_with_birth_number(target))
            headings = score_headings(build_report(valid_report_json(), result))
            for field in ("money_score", "luck_score", "growth_score", "stability_score"):
                assert headings[field] == [getattr(result, field)]
                assert 0 <= getattr(result, field) - getattr(base, field) <= PERSONAL_RESONANCE_CAP


# --- E. legacy analyses without birth fields --------------------------------


def test_legacy_analysis_renders_without_a_birth_section_even_if_model_writes_one() -> None:
    legacy = analyze("2210005").model_dump_public()
    for key in ("birth_number", "birth_number_meaning", "birth_resonance", "birth_digit_in_serial_count"):
        legacy.pop(key, None)
    result = AnalysisResult.model_validate(legacy)

    report = build_report(valid_report_json(birth="Число рождения здесь ни при чём."), result)
    assert "ЧИСЛО РОЖДЕНИЯ" not in report
    assert "Число рождения здесь ни при чём." not in report
    assert score_headings(report) == _canonical(result)


# --- structural rules of the contract ---------------------------------------


def test_non_json_output_is_rejected() -> None:
    with pytest.raises(ReportContractError):
        build_report("💰 <b>ДЕНЕЖНЫЙ ПРОФИЛЬ — 3/10</b>", analyze("2210005"))


def test_missing_required_section_is_rejected() -> None:
    data = json.loads(valid_report_json())
    del data["luck"]
    with pytest.raises(ReportContractError):
        build_report(json.dumps(data, ensure_ascii=False), analyze("2210005"))


def test_special_section_is_never_invented_for_a_number_without_patterns() -> None:
    result = analyze("1029384")
    assert not (result.repeated_digits or result.repeated_pairs or result.detected_patterns)
    report = build_report(valid_report_json(special="Выдуманный узор."), result)
    assert "ОСОБЫЕ СОЧЕТАНИЯ" not in report
    assert "Выдуманный узор." not in report


def test_empty_birth_body_falls_back_to_deterministic_copy() -> None:
    result = analyze("2210005", _date_with_birth_number(3))
    report = build_report(valid_report_json(birth=""), result)
    assert "🎂 <b>ВАШЕ ЧИСЛО РОЖДЕНИЯ — 3</b>" in report
    assert "Ваше число рождения — 3" in report


def test_unbalanced_tag_in_one_body_does_not_strip_formatting_elsewhere() -> None:
    report = build_report(valid_report_json(luck="Удача <b>где-то рядом."), analyze("2210005"))
    assert "<b>где-то" not in report  # the broken body degraded to plain text
    assert "🍀 <b>ПРОФИЛЬ УДАЧИ" in report  # application headings untouched
    assert report.count("<b>") == report.count("</b>")


# --- reduction arithmetic (same class: a number shown to users) -------------


def test_digit_sum_calculation_shows_every_reduction_step() -> None:
    """Previously a sum needing two reductions was printed as "2 + 9 = 2",
    which is false arithmetic shown to users.
    """
    result = analyze("9992")
    assert result.digit_sum == 29 and result.reduced_number == 2
    calc = render_digit_sum_calculation(result)
    assert calc == "9 + 9 + 9 + 2 = 29\n2 + 9 = 11\n1 + 1 = 2"
    assert "2 + 9 = 2" not in render_fallback_full_report(result)


@pytest.mark.parametrize("number", ["2210005", "9992", "9" * 20, "0000", "1234"])
def test_every_calculation_line_is_arithmetically_true(number: str) -> None:
    result = analyze(number)
    lines = render_digit_sum_calculation(result).splitlines()
    for line in lines:
        left, right = line.split(" = ")
        assert sum(int(part) for part in left.split(" + ")) == int(right)
    assert int(lines[-1].split(" = ")[1]) == result.reduced_number


@pytest.mark.parametrize(
    "fragment",
    ["</n>", "<br>", "<br/>", "<br />", "<p>", "</p>", "\\n"],
)
def test_line_break_markup_never_reaches_the_user_as_visible_text(fragment: str) -> None:
    """Seen in the live validation of this contract: the model ended a body
    with "</n>", which the sanitizer escaped into a visible "&lt;/n&gt;".
    Line-break markup must become a real line break instead.
    """
    body = f"Первый абзац.{fragment}{fragment}Второй абзац."
    report = build_report(valid_report_json(opening=body), analyze("2210005"))

    assert "Первый абзац.\n\nВторой абзац." in report
    for junk in ("&lt;", "&gt;", "</n>", "<br", "<p>", "\\n"):
        assert junk not in report


def test_prompt_no_longer_suggests_escaped_newlines() -> None:
    from app.ai.prompts import SYSTEM_PROMPT

    assert "escaped double newline" not in SYSTEM_PROMPT
    assert "never a tag such as" in SYSTEM_PROMPT
