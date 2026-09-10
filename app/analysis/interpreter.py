"""Deterministic, non-AI text rendering of an AnalysisResult.

Used for the free teaser (always) and as a fallback full report if the AI
report generator is temporarily unavailable — a paying user must never be
left with nothing. All text here is our own fixed Russian copy (the only
interpolated values are digits/scores), so it's safe to embed Telegram HTML
tags directly without running it through app.formatting.to_telegram_html.
"""

from __future__ import annotations

from app.analysis.models import AnalysisResult
from app.analysis.rules import DIGIT_MEANINGS

_MONEY_STRENGTH_LABELS = (
    (8, "исключительная"),
    (6, "сильная"),
    (4, "умеренная"),
    (0, "слабая"),
)

# Used for the fallback report's profile sections — a generic but non-robotic
# strength phrase per score. The AI-generated report does this far more
# richly and specifically; this is only the deterministic safety net.
_STRENGTH_PHRASES = (
    (8, "ярко выражена в этом номере"),
    (6, "заметно проявлена в этом номере"),
    (4, "присутствует в умеренной степени"),
    (0, "выражена мягко, на фоне остального"),
)


def _strength_label(score: int) -> str:
    for threshold, label in _MONEY_STRENGTH_LABELS:
        if score >= threshold:
            return label
    return "слабая"


def _strength_phrase(score: int) -> str:
    for threshold, phrase in _STRENGTH_PHRASES:
        if score >= threshold:
            return phrase
    return _STRENGTH_PHRASES[-1][1]


def render_teaser(result: AnalysisResult) -> str:
    """Free teaser message shown after analysis, before payment.

    Answers "what's the main number and what's the basic impression?" —
    deliberately leaves the digit-by-digit story, detected patterns, and
    the other three profiles for the paid report.
    """
    money_strength = _strength_label(result.money_score)

    highlight = ""
    if result.repeated_digits or result.repeated_pairs or result.detected_patterns:
        highlight = (
            "\n✨ В номере есть повторяющиеся элементы — в полном разборе "
            "видно, какие именно и что они символизируют."
        )

    lines = [
        "💰 Ваша купюра проанализирована.",
        f"🔢 Главное число: <b>{result.reduced_number}</b>",
        f"💵 Денежная символика: {money_strength}",
        highlight.strip(),
        "",
        f"🏆 Предварительный балл: {result.overall_score}/100",
        "",
        "🔮 Хотите узнать полный разбор — что рассказывают отдельные цифры, "
        "какие узоры скрыты в номере и какую купюру стоит оставить себе как "
        "денежный талисман?",
    ]
    return "\n".join(line for line in lines if line != "")


def render_fallback_full_report(result: AnalysisResult) -> str:
    """Plain deterministic full report, used if the AI is unavailable.

    Follows the same section structure as the AI-generated report, just
    without the richer narrative prose an LLM provides — scores are always
    shown as clean "X/10" figures with a short strength phrase, never as a
    raw contribution breakdown.
    """
    meaning = DIGIT_MEANINGS[result.reduced_number]

    parts: list[str] = [
        "🔮 <b>Денежный разбор купюры</b>",
        f"Серийный номер: <b>{result.normalized_number}</b>",
        "",
        f"🔢 <b>Главное число — {result.reduced_number}</b>",
        " + ".join(str(d) for d in result.digits) + f" = {result.digit_sum}",
    ]
    if result.digit_sum >= 10:
        parts.append(
            " + ".join(str(d) for d in str(result.digit_sum)) + f" = {result.reduced_number}"
        )
    parts += [
        f"Главное число символизирует: {meaning}.",
        "",
        "🔎 <b>Что рассказывают цифры</b>",
    ]

    for digit in result.digits:
        suffix = " — эта тема усиливается за счёт повторения" if digit in result.repeated_digits else ""
        parts.append(f"{digit} — {DIGIT_MEANINGS[digit]}{suffix}.")
    parts.append("")

    if result.repeated_digits or result.repeated_pairs or result.detected_patterns:
        parts.append("✨ <b>Особые знаки</b>")
        if result.repeated_digits:
            digits_str = ", ".join(str(d) for d in result.repeated_digits)
            parts.append(f"• Повторяющиеся цифры: {digits_str}.")
        if result.repeated_pairs:
            parts.append(f"• Повторяющиеся пары: {', '.join(result.repeated_pairs)}.")
        for pattern in result.detected_patterns:
            parts.append(f"• {pattern.description}")
        parts.append("")

    parts += [
        "💰 <b>Денежная энергия</b>",
        f"💰 Денежный потенциал: {result.money_score}/10",
        f"Денежная энергия номера {_strength_phrase(result.money_score)}.",
        "",
        "🍀 <b>Энергия удачи</b>",
        f"🍀 Энергия удачи: {result.luck_score}/10",
        f"Символика удачи {_strength_phrase(result.luck_score)}.",
        "",
        "🌱 <b>Энергия роста</b>",
        f"🌱 Энергия роста: {result.growth_score}/10",
        f"Тема развития и движения {_strength_phrase(result.growth_score)}.",
        "",
        "🛡 <b>Стабильность</b>",
        f"🛡 Стабильность: {result.stability_score}/10",
        f"Тяга к порядку и постоянству {_strength_phrase(result.stability_score)}.",
        "",
        "✨ <b>Итог</b>",
        f"⭐ Общий показатель: {result.overall_score}/100",
        f"Главная тема этого номера — {meaning}.",
    ]

    return "\n".join(parts)
