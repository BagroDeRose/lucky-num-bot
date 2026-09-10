"""Deterministic tests for app.formatting.to_telegram_html — no AI
dependency. Regression coverage for a real bug: literal "**text**" showing
up in Telegram because the bot's parse_mode is HTML, not Markdown.
"""

from __future__ import annotations

import pytest

from app.formatting import (
    MAX_TELEGRAM_TEXT_LENGTH,
    to_telegram_html,
    truncate_telegram_html,
)


def test_markdown_bold_converted_to_html_tag() -> None:
    result = to_telegram_html("Это **важно** для вас.")
    assert result == "Это <b>важно</b> для вас."
    assert "**" not in result


def test_markdown_underscore_bold_converted_to_html_tag() -> None:
    result = to_telegram_html("Это __важно__ для вас.")
    assert result == "Это <b>важно</b> для вас."
    assert "__" not in result


def test_markdown_italic_converted_to_html_tag() -> None:
    result = to_telegram_html("Обратите *внимание* на цифру.")
    assert result == "Обратите <i>внимание</i> на цифру."
    assert "*" not in result


def test_markdown_code_converted_to_html_tag() -> None:
    result = to_telegram_html("Формула: `5+3=8`.")
    assert result == "Формула: <code>5+3=8</code>."
    assert "`" not in result


def test_real_html_tags_pass_through_unchanged() -> None:
    result = to_telegram_html("<b>Главное число</b> — 8.")
    assert result == "<b>Главное число</b> — 8."


def test_mixed_markdown_and_plain_text() -> None:
    result = to_telegram_html("**8** — число результата, а *5* про движение.")
    assert result == "<b>8</b> — число результата, а <i>5</i> про движение."
    assert "*" not in result


def test_no_markdown_present_leaves_text_unchanged() -> None:
    text = "Обычный текст без разметки, с цифрами 12345."
    assert to_telegram_html(text) == text


def test_raw_angle_brackets_are_escaped_not_interpreted_as_tags() -> None:
    result = to_telegram_html("Значение 5 < 8, а 9 > 3.")
    assert "&lt;" in result
    assert "&gt;" in result
    assert "<" not in result.replace("&lt;", "").replace("&gt;", "")


def test_ampersand_is_escaped() -> None:
    result = to_telegram_html("Деньги & удача")
    assert "&amp;" in result


def test_unbalanced_tag_degrades_to_plain_text_instead_of_breaking() -> None:
    """A model that forgets a closing tag must not produce malformed HTML
    Telegram would reject outright — the whole message would fail to send.
    """
    result = to_telegram_html("Это <b>незакрытый тег без пары.")
    assert "<b>" not in result
    assert "</b>" not in result
    assert "незакрытый тег" in result


def test_unbalanced_markdown_leaves_stray_marker_as_literal_text() -> None:
    """An unpaired ** (no closing **) simply never matches the bold regex,
    so it must remain as harmless literal text — not a parse error.
    """
    result = to_telegram_html("Текст с **незакрытой разметкой без пары")
    assert "<b>" not in result
    assert "**незакрытой" in result


@pytest.mark.parametrize(
    "raw",
    [
        "**bold**",
        "*italic*",
        "`code`",
        "text with **bold** and *italic* and `code`",
    ],
)
def test_no_markdown_markers_leak_into_final_output(raw: str) -> None:
    result = to_telegram_html(raw)
    assert "**" not in result
    assert "`" not in result
    # a single leftover "*" is fine only if it was never part of a pair;
    # for these fixtures every asterisk is part of a well-formed pair
    assert "*" not in result


def test_emoji_and_newlines_preserved() -> None:
    text = "🔮 Заголовок\n\nВторой абзац с 💰 эмодзи."
    assert to_telegram_html(text) == text


def test_arithmetic_with_plus_signs_not_mistaken_for_markdown() -> None:
    text = "5 + 3 + 1 + 1 + 8 + 9 + 8 = 35\n3 + 5 = 8"
    assert to_telegram_html(text) == text


def test_truncate_leaves_short_text_untouched() -> None:
    text = "Короткий отчёт."
    assert truncate_telegram_html(text) == text


def test_truncate_enforces_telegram_length_limit() -> None:
    long_text = "Абзац один.\n\n" + ("Много текста. " * 500)
    result = truncate_telegram_html(long_text)
    assert len(result) < MAX_TELEGRAM_TEXT_LENGTH


def test_truncate_prefers_paragraph_boundary() -> None:
    first = "Первый абзац с важным содержанием.\n\n"  # 36 chars
    second = "Второй абзац. " * 400  # long enough to exceed the limit
    # limit chosen so the paragraph break (~index 34) is past half the
    # budget, satisfying the "don't throw away too much" heuristic.
    result = truncate_telegram_html(first + second, limit=60)
    assert result == first.rstrip()


def test_truncate_strips_tags_if_cut_leaves_them_unbalanced() -> None:
    text = "Текст перед. <b>" + ("слово " * 100)  # never closes the <b>
    result = truncate_telegram_html(text, limit=50)
    assert "<b>" not in result
    assert "</b>" not in result
