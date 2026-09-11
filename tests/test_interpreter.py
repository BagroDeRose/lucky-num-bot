"""Tests for the deterministic (non-AI) report rendering in
app.analysis.interpreter — the free teaser and the AI-unavailable fallback
report. Both are pure templating over AnalysisResult, so fully testable
without any LLM dependency.
"""

from __future__ import annotations

from app.analysis.engine import analyze
from app.analysis.interpreter import (
    _REPEAT_EMPHASIS_TAILS,
    render_fallback_full_report,
    render_teaser,
)
from app.formatting import to_telegram_html


def test_teaser_shows_main_number_and_overall_score() -> None:
    result = analyze("2200373")
    teaser = render_teaser(result)
    assert str(result.reduced_number) in teaser
    assert f"{result.overall_score}/100" in teaser


def test_teaser_does_not_reveal_subscores() -> None:
    """The free teaser must not leak the paid report's money/luck/growth/
    stability breakdown — only the overall score and a strength label.
    """
    result = analyze("2200373")
    teaser = render_teaser(result)
    assert f"{result.money_score}/10" not in teaser
    assert f"{result.luck_score}/10" not in teaser
    assert f"{result.growth_score}/10" not in teaser
    assert f"{result.stability_score}/10" not in teaser


def test_teaser_html_is_well_formed() -> None:
    result = analyze("2200373")
    teaser = render_teaser(result)
    assert teaser.count("<b>") == teaser.count("</b>")


def test_fallback_report_does_not_contain_disclaimer() -> None:
    result = analyze("2200373")
    report = render_fallback_full_report(result)
    assert "развлекательная" not in report.lower()
    assert "не является финансовым советом" not in report.lower()
    assert "не имеет научной ценности" not in report.lower()


def test_fallback_report_shows_all_four_scores_and_overall() -> None:
    result = analyze("2200373")
    report = render_fallback_full_report(result)
    assert f"Денежный профиль — {result.money_score}/10" in report
    assert f"Профиль удачи — {result.luck_score}/10" in report
    assert f"Профиль роста — {result.growth_score}/10" in report
    assert f"Профиль стабильности — {result.stability_score}/10" in report
    assert f"Итог — {result.overall_score}/100" in report


def test_fallback_report_never_exposes_raw_score_contributions() -> None:
    """No "+6", "+1"-style score-breakdown artifacts must leak into the
    user-visible fallback report.
    """
    result = analyze("2200373")
    report = render_fallback_full_report(result)
    assert "score_breakdown" not in report
    assert "digit_emphasis" not in report
    assert "effect" not in report.lower()


def test_fallback_report_digit_story_mentions_every_digit_meaning_in_order() -> None:
    """The digit-by-digit section is a flowing narrative paragraph (not a
    one-line-per-digit list), but every digit's meaning must still appear,
    in the same order the digits occur in the number.
    """
    from app.analysis.interpreter import _digit_runs  # noqa: SLF001
    from app.analysis.rules import DIGIT_MEANINGS

    result = analyze("2200373")
    report = render_fallback_full_report(result)

    lines = report.splitlines()
    start = lines.index("🔎 <b>История цифр</b>") + 1
    story = lines[start]

    # Adjacent repeats (a "run") are merged into a single mention, so check
    # per-run, not per-raw-digit — and search forward from the previous
    # match so a digit returning later (a legitimately repeated meaning)
    # doesn't fool the ordering check by matching its first occurrence.
    positions = []
    search_from = 0
    for digit, _count in _digit_runs(result.digits):
        pos = story.index(DIGIT_MEANINGS[digit], search_from)
        positions.append(pos)
        search_from = pos + 1
    assert positions == sorted(positions)  # meanings appear in digit order


def test_fallback_report_flags_adjacent_repeated_digits() -> None:
    """2200373 has two adjacent-repeat runs ("22" and "00") — both must be
    called out as reinforcing their theme. The two mentions must use
    different closing clauses (see _REPEAT_EMPHASIS_TAILS) rather than
    chanting the exact same sentence twice in one paragraph.
    """
    result = analyze("2200373")
    report = render_fallback_full_report(result)
    assert result.repeated_pairs == ["22", "00"]
    tails_present = [t for t in _REPEAT_EMPHASIS_TAILS if t in report]
    assert len(tails_present) == 2
    assert len(set(tails_present)) == 2  # distinct wording, not repeated


def test_fallback_report_many_repeated_runs_do_not_chant_the_same_sentence() -> None:
    """Regression test: a number with several separate multi-digit runs
    (e.g. "1122334455" — five distinct "XX" runs) must not repeat the exact
    same closing clause for every run. Before the fix, every run used the
    literal phrase "и повтор явно усиливает эту тему" verbatim, producing a
    "История цифр" paragraph that chanted the same sentence five times and
    overused the word "усиливает" — exactly the mechanical repetition and
    filler-word overuse the product's AI report is also told to avoid.
    """
    result = analyze("1122334455")
    report = render_fallback_full_report(result)

    assert "усиливает" not in report.lower()

    start = report.index("🔎 <b>История цифр</b>")
    end = report.index("✨", start) if "✨" in report[start:] else len(report)
    story = report[start:end]
    tail_occurrences = [tail for tail in _REPEAT_EMPHASIS_TAILS for _ in range(story.count(tail))]
    # 5 runs of length 2 ("11","22","33","44","55") -> 5 emphasized mentions,
    # cycling through a 4-entry rotation so at most one repeat, never all 5 identical.
    assert len(tail_occurrences) == 5
    assert len(set(tail_occurrences)) >= 4


def test_fallback_report_flags_a_non_adjacent_returning_digit() -> None:
    """5311898: digit 8 appears twice, not adjacently — the second
    occurrence must be flagged as the theme returning.
    """
    result = analyze("5311898")
    assert result.digit_frequency[8] == 2
    report = render_fallback_full_report(result)
    assert "Эта тема здесь уже не впервые." in report


def test_fallback_report_normal_mixed_number_has_no_repetition_notes() -> None:
    result = analyze("1928374")
    report = render_fallback_full_report(result)
    assert result.repeated_digits == []
    assert result.repeated_pairs == []
    assert not any(tail in report for tail in _REPEAT_EMPHASIS_TAILS)
    assert "уже не впервые" not in report


def test_fallback_report_extreme_repetition_dominates_555555() -> None:
    """The dominant fact for 555555 (all six digits identical) must lead
    the report, not read like an ordinary repeated pair.
    """
    result = analyze("555555")
    report = render_fallback_full_report(result)
    assert "все шесть цифр" in report.lower() or "шесть цифр номера" in report.lower()
    assert "единственная тема" in report or "единственный, доминирующий мотив" in report
    # The extreme case must explicitly connect to the reduced number too.
    assert str(result.reduced_number) in report


def test_fallback_report_special_section_present_only_when_patterns_exist() -> None:
    repetitive = analyze("555555")
    assert repetitive.detected_patterns  # sanity: this number does have patterns
    report_with_patterns = render_fallback_full_report(repetitive)
    assert "✨ <b>Особые сочетания</b>" in report_with_patterns

    mixed = analyze("1928374")
    report_mixed = render_fallback_full_report(mixed)
    if not mixed.detected_patterns and not mixed.repeated_digits and not mixed.repeated_pairs:
        assert "✨ <b>Особые сочетания</b>" not in report_mixed


def test_fallback_report_special_section_reflects_actual_pattern_types() -> None:
    """Rather than printing the raw pattern.description strings verbatim
    (too generic/weak per product direction), the special section must
    still faithfully reflect which pattern TYPES were actually detected —
    no inventing, no omitting.
    """
    result = analyze("555555")
    pattern_names = {p.name for p in result.detected_patterns}
    assert "single_digit_number" in pattern_names
    assert "palindrome" in pattern_names

    report = render_fallback_full_report(result)
    special_start = report.index("✨ <b>Особые сочетания</b>")
    special_section = report[special_start:]

    assert "держится на одной цифре" in special_section
    assert "читается одинаково в обе стороны" in special_section


def test_fallback_report_special_section_does_not_duplicate_weak_pair_language_for_dominant_number() -> None:
    """For a number entirely made of one digit, every adjacent pair is
    trivially identical — the special section must lead with the real
    dominant fact, not also pad in a generic "pair adds an accent" line.
    """
    result = analyze("555555")
    report = render_fallback_full_report(result)
    assert "создаёт визуальный" not in report
    assert "добавляет уникальности" not in report


def test_fallback_report_html_is_well_formed_and_safe() -> None:
    """Even though the fallback template embeds tags directly (it's all our
    own fixed copy), running it through the sanitizer must be a no-op —
    proving there's nothing for it to fix.
    """
    for number in ("2200373", "555555", "1928374", "0012345"):
        result = analyze(number)
        report = render_fallback_full_report(result)
        assert report.count("<b>") == report.count("</b>")
        assert to_telegram_html(report) == report


def test_fallback_report_preserves_leading_zeroes_in_serial_number() -> None:
    result = analyze("0012345")
    report = render_fallback_full_report(result)
    assert "0012345" in report
