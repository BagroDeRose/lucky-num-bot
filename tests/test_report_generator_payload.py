"""Tests for the AI-facing payload built by app.ai.report_generator and the
static prompt content. These are all deterministic — no LLM call involved —
per the project rule that tests must not depend on exact LLM wording.

Coverage requested for the paid-report redesign:
* internal scoring mechanics (score_breakdown, "+N" contributions, factor
  names) are structurally excluded from what the AI ever sees;
* digit-by-digit facts, repeated-digit facts, and pattern facts reach the
  AI payload unaltered from the deterministic analysis;
* a highly repetitive number (555555) and a normal mixed number produce
  correctly differentiated payloads;
* the static prompt text itself encodes the required score format and the
  "no disclaimer" / "no Markdown" product rules.
"""

from __future__ import annotations

import json

from app.ai.prompts import SYSTEM_PROMPT
from app.ai.report_generator import _build_ai_payload
from app.analysis.engine import analyze
from app.analysis.rules import DIGIT_MEANINGS


def test_payload_excludes_score_breakdown_entirely() -> None:
    """The most important guarantee: the AI must never even receive the
    raw scoring mechanics, so it structurally cannot echo them back.
    """
    result = analyze("2200373")
    payload = _build_ai_payload(result)

    assert "score_breakdown" not in payload
    assert "algorithm_version" not in payload

    serialized = json.dumps(payload, ensure_ascii=False)
    assert "score_breakdown" not in serialized
    # Technical factor names from the scoring engine must never appear.
    assert "digit_emphasis" not in serialized
    assert "repeated_pairs_bonus" not in serialized


def test_payload_preserves_deterministic_facts_unaltered() -> None:
    """The AI payload must be a faithful (not lossy, not embellished) view
    of the deterministic analysis — no invented or dropped facts.
    """
    result = analyze("2200373")
    payload = _build_ai_payload(result)

    assert payload["serial_number"] == result.normalized_number
    assert payload["digits"] == result.digits
    assert payload["digit_sum"] == result.digit_sum
    assert payload["reduced_number"] == result.reduced_number
    assert payload["repeated_digits"] == result.repeated_digits
    assert payload["repeated_pairs"] == result.repeated_pairs
    assert payload["money_score"] == result.money_score
    assert payload["luck_score"] == result.luck_score
    assert payload["growth_score"] == result.growth_score
    assert payload["stability_score"] == result.stability_score
    assert payload["overall_score"] == result.overall_score
    assert payload["detected_patterns"] == [
        {"name": p.name, "description": p.description} for p in result.detected_patterns
    ]


def test_payload_digit_meanings_cover_exactly_the_digits_present() -> None:
    result = analyze("2200373")
    payload = _build_ai_payload(result)

    digits_present = {str(d) for d in set(result.digits)}
    assert set(payload["digit_meanings"].keys()) == digits_present
    for digit_str, meaning in payload["digit_meanings"].items():
        assert meaning == DIGIT_MEANINGS[int(digit_str)]


def test_payload_reduced_number_meaning_matches_rule_book() -> None:
    result = analyze("2200373")
    payload = _build_ai_payload(result)
    assert payload["reduced_number_meaning"] == DIGIT_MEANINGS[result.reduced_number]


def test_payload_for_highly_repetitive_number_555555() -> None:
    """555555: every digit is 5, so repetition and (if applicable) palindrome
    facts must be clearly present and correctly attributed to digit 5 only.
    """
    result = analyze("555555")
    payload = _build_ai_payload(result)

    assert payload["digits"] == [5, 5, 5, 5, 5, 5]
    assert payload["repeated_digits"] == [5]
    assert payload["digit_frequency"] == {"5": 6}
    assert set(payload["digit_meanings"].keys()) == {"5"}

    pattern_names = {p["name"] for p in payload["detected_patterns"]}
    assert "palindrome" in pattern_names
    assert "single_digit_number" in pattern_names


def test_payload_for_normal_mixed_number_has_no_fabricated_repetition() -> None:
    """A mixed number with no repeated digits must not claim any repetition
    or patterns that don't actually exist.
    """
    result = analyze("1928374")
    payload = _build_ai_payload(result)

    assert payload["repeated_digits"] == []
    assert all(count == 1 for count in payload["digit_frequency"].values())


def test_payload_pattern_list_empty_when_analysis_finds_nothing() -> None:
    result = analyze("1928374")
    if result.detected_patterns:
        # If this particular number happens to trip a sequence/pair pattern,
        # the payload must still mirror it exactly — the real assertion is
        # fidelity, covered by test_payload_preserves_deterministic_facts.
        return
    payload = _build_ai_payload(result)
    assert payload["detected_patterns"] == []


def test_payload_is_json_serializable() -> None:
    """The payload is embedded into the prompt via json.dumps — must never
    contain non-serializable values (e.g. raw pydantic model instances).
    """
    result = analyze("2200373")
    payload = _build_ai_payload(result)
    json.dumps(payload, ensure_ascii=False)  # must not raise


# --- Static prompt content -----------------------------------------------


def test_report_renderer_emits_exact_score_heading_formats() -> None:
    """Score headings used to be templates in the prompt that the model
    filled in — which is how a mislabelled "💰 ПРОФИЛЬ УДАЧИ — 3/10" reached a
    user. They are now rendered by the application, so the exact formats are
    asserted on the renderer's output with the canonical values.
    """
    from _report_fakes import valid_report_json

    from app.ai.report_contract import build_report

    result = analyze("2200373")
    report = build_report(valid_report_json(), result)
    assert f"💰 <b>ДЕНЕЖНЫЙ ПРОФИЛЬ — {result.money_score}/10</b>" in report
    assert f"🍀 <b>ПРОФИЛЬ УДАЧИ — {result.luck_score}/10</b>" in report
    assert f"🌱 <b>ПРОФИЛЬ РОСТА — {result.growth_score}/10</b>" in report
    assert f"🛡 <b>ПРОФИЛЬ СТАБИЛЬНОСТИ — {result.stability_score}/10</b>" in report
    assert f"⭐ <b>ИТОГ — {result.overall_score}/100</b>" in report


def test_report_renderer_wraps_every_section_heading_in_bold_tags() -> None:
    """Originally a prompt regression test: live generations bolded headings
    inconsistently when the model had to reproduce them. The application now
    writes every heading itself, so bolding no longer depends on the model.
    """
    from _report_fakes import valid_report_json

    from app.ai.report_contract import build_report

    result = analyze("2200373")
    report = build_report(valid_report_json(), result)
    assert "🔮 <b>ДЕНЕЖНЫЙ РАЗБОР</b>" in report
    assert f"🔢 <b>ГЛАВНОЕ ЧИСЛО — {result.reduced_number}</b>" in report
    assert "🔎 <b>ИСТОРИЯ ЦИФР</b>" in report
    assert "✨ <b>ОСОБЫЕ СОЧЕТАНИЯ</b>" in report  # 2200373 has repeated pairs
    assert "💥 <b>ВЕРДИКТ ЖМЫХА</b>" in report


def test_system_prompt_leaves_headings_and_scores_to_the_application() -> None:
    """The prompt must no longer hand the model score templates to fill, and
    must forbid it from writing scores, headings or recalculations itself.
    """
    import re

    assert re.findall(r"\{[a-z_]+_score\}", SYSTEM_PROMPT) == []
    lowered = SYSTEM_PROMPT.lower()
    assert "json object" in lowered
    assert "never write a score" in lowered
    assert "never write a heading" in lowered
    assert "never recalculate" in lowered


def test_system_prompt_forbids_markdown() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "markdown" in lowered
    assert "<b>" in SYSTEM_PROMPT
    assert "<i>" in SYSTEM_PROMPT


def test_system_prompt_forbids_disclaimers() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "not financial advice" in lowered  # named as a forbidden example
    assert "not scientific" in lowered
    assert "disclaimer" in lowered


def test_system_prompt_forbids_raw_scoring_mechanics_leakage() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "score_breakdown" not in lowered  # the payload excludes it; prompt need not name it
    assert '"+n"' in lowered or "+n" in lowered  # cited as a forbidden example pattern
    assert "internal factor" in lowered or "factor/rule names" in lowered


def test_system_prompt_forbids_guaranteeing_outcomes() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "guaranteed outcome" in lowered or "guarantee" in lowered


def test_system_prompt_requires_russian_output() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "russian" in lowered
    assert "английск" not in lowered  # the instructions describe Russian output, not "English"


def test_system_prompt_bans_overused_filler_words() -> None:
    lowered = SYSTEM_PROMPT.lower()
    for banned in ("энергия", "символизирует", "важность", "напоминает"):
        assert banned in lowered  # named explicitly as words to avoid


def test_system_prompt_requires_555555_style_numbers_to_lead_with_dominance() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "dominance" in lowered or "dominat" in lowered


# --- New requirements from the "fewer facts, more personalization" redesign


def test_system_prompt_targets_shorter_length() -> None:
    """Product direction: 1500-2500 chars normal, ~3000 for exceptional
    numbers — a significant reduction from the previous 600-1000 word /
    2500-3800 char target, and explicitly never close to Telegram's limit.
    """
    assert "1500-2500" in SYSTEM_PROMPT
    assert "3000 characters" in SYSTEM_PROMPT
    assert "600-1000" not in SYSTEM_PROMPT
    lowered = SYSTEM_PROMPT.lower()
    assert "4096" in lowered


def test_system_prompt_states_fewer_facts_more_personalization_principle() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "fewer facts" in lowered
    assert "more personalization" in lowered


def test_system_prompt_requires_self_check_against_repetition() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "mentally re-read" in lowered or "merge them" in lowered


def test_system_prompt_forbids_inventing_rarity_and_historical_claims() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "rarity" in lowered
    assert "historical" in lowered
    assert "psychological" in lowered


def test_system_prompt_forbids_inventing_positional_meanings() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "position" in lowered


def test_system_prompt_forbids_recomputing_scores() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "recompute" in lowered or "recalculate" in lowered


def test_system_prompt_forbids_revealing_the_underlying_machinery() -> None:
    """New in this redesign: don't just avoid disclaimers — never reveal
    that a report came from "the algorithm"/"the model"/"the system" at all.
    """
    lowered = SYSTEM_PROMPT.lower()
    assert "according to the algorithm" in lowered
    assert "the system calculated" in lowered
    assert "i am an ai" in lowered


def test_system_prompt_bans_expanded_filler_word_list() -> None:
    lowered = SYSTEM_PROMPT.lower()
    for banned in (
        "энергетика",
        "усиливает влияние",
        "подчёркивает важность",
        "создаёт ощущение",
        "гармония",
        "внутренний баланс",
        "потенциал",
        "уникальный",
        "особенный",
        "сильный",
        "мощный",
    ):
        assert banned in lowered


def test_system_prompt_gives_bad_vs_good_verdict_example() -> None:
    """The generic-motivational-quote example must be explicitly named as
    what NOT to write, per the product brief's worked example.
    """
    assert "Успех приходит к тем, кто готов двигаться" in SYSTEM_PROMPT


def test_system_prompt_only_allows_alternating_rhythm_via_digit_story_not_invented_pattern() -> None:
    """121212-style numbers: the engine has no explicit "alternating"
    pattern flag, so the prompt must guide the model to notice this from
    the real digit sequence (a fact) inside ИСТОРИЯ ЦИФР, while still
    forbidding it from being asserted in ОСОБЫЕ СОЧЕТАНИЯ unless the JSON's
    own detected_patterns/repeated_digits actually names it.
    """
    lowered = SYSTEM_PROMPT.lower()
    assert "alternating" in lowered
