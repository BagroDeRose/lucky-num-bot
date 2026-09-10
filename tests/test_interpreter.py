"""Tests for the deterministic (non-AI) report rendering in
app.analysis.interpreter — the free teaser and the AI-unavailable fallback
report. Both are pure templating over AnalysisResult, so fully testable
without any LLM dependency.
"""

from __future__ import annotations

from app.analysis.engine import analyze
from app.analysis.interpreter import render_fallback_full_report, render_teaser
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
    assert f"Денежный потенциал: {result.money_score}/10" in report
    assert f"Энергия удачи: {result.luck_score}/10" in report
    assert f"Энергия роста: {result.growth_score}/10" in report
    assert f"Стабильность: {result.stability_score}/10" in report
    assert f"Общий показатель: {result.overall_score}/100" in report


def test_fallback_report_never_exposes_raw_score_contributions() -> None:
    """No "+6", "+1"-style score-breakdown artifacts must leak into the
    user-visible fallback report.
    """
    result = analyze("2200373")
    report = render_fallback_full_report(result)
    assert "score_breakdown" not in report
    assert "digit_emphasis" not in report
    assert "effect" not in report.lower()


def test_fallback_report_digit_by_digit_section_lists_every_digit_in_order() -> None:
    result = analyze("2200373")
    report = render_fallback_full_report(result)

    lines = report.splitlines()
    start = lines.index("🔎 <b>Что рассказывают цифры</b>") + 1
    digit_lines = []
    for line in lines[start:]:
        if line == "":
            break
        digit_lines.append(line)

    assert len(digit_lines) == len(result.digits)
    for digit, line in zip(result.digits, digit_lines, strict=True):
        assert line.startswith(f"{digit} —")


def test_fallback_report_flags_repeated_digits() -> None:
    """555555: digit 5 repeats 6 times — every one of its six lines in the
    digit-by-digit section must note the repetition.
    """
    result = analyze("555555")
    report = render_fallback_full_report(result)

    assert result.repeated_digits == [5]
    repeat_note_count = report.count("эта тема усиливается за счёт повторения")
    assert repeat_note_count == 6


def test_fallback_report_normal_mixed_number_has_no_repetition_notes() -> None:
    result = analyze("1928374")
    report = render_fallback_full_report(result)
    assert result.repeated_digits == []
    assert "эта тема усиливается за счёт повторения" not in report


def test_fallback_report_special_section_present_only_when_patterns_exist() -> None:
    repetitive = analyze("555555")
    assert repetitive.detected_patterns  # sanity: this number does have patterns
    report_with_patterns = render_fallback_full_report(repetitive)
    assert "✨ <b>Особые знаки</b>" in report_with_patterns

    mixed = analyze("1928374")
    report_mixed = render_fallback_full_report(mixed)
    if not mixed.detected_patterns and not mixed.repeated_digits and not mixed.repeated_pairs:
        assert "✨ <b>Особые знаки</b>" not in report_mixed


def test_fallback_report_mentions_every_detected_pattern_description() -> None:
    result = analyze("555555")
    report = render_fallback_full_report(result)
    for pattern in result.detected_patterns:
        assert pattern.description in report


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
