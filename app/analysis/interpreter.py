"""Deterministic, non-AI text rendering of an AnalysisResult.

Used for the free teaser (and as a fallback if the AI report generator is
temporarily unavailable). This is plain templating over already-computed
data — no interpretation happens here that isn't already in the scores.
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


def _strength_label(score: int) -> str:
    for threshold, label in _MONEY_STRENGTH_LABELS:
        if score >= threshold:
            return label
    return "слабая"


def render_teaser(result: AnalysisResult) -> str:
    """Free teaser message shown after analysis, before payment."""
    money_strength = _strength_label(result.money_score)

    highlight = ""
    if result.repeated_digits or result.repeated_pairs or result.detected_patterns:
        highlight = (
            "\n✨ Номер содержит повторяющиеся элементы, которые делают его "
            "особенно интересным в нашей нумерологической системе."
        )

    lines = [
        "💰 Ваша купюра проанализирована.",
        f"🔢 Главное число: {result.reduced_number}",
        f"💵 Денежная символика: {money_strength}",
        highlight.strip(),
        "",
        f"🏆 Предварительный балл: {result.overall_score}/100",
        "",
        "🔮 Хотите узнать полную персональную интерпретацию — что именно "
        "делает этот номер особенным и какую купюру стоит оставить себе "
        "как денежный талисман?",
    ]
    return "\n".join(line for line in lines if line != "")


def render_fallback_full_report(result: AnalysisResult) -> str:
    """Plain deterministic full report, used if the AI is unavailable so a
    paying user is never left with nothing.
    """
    meaning = DIGIT_MEANINGS[result.reduced_number]

    parts: list[str] = [
        "📜 Полный анализ вашей купюры",
        "",
        f"Номер: {result.normalized_number}",
        f"Главное число: {result.reduced_number} — {meaning}",
        "",
        f"💰 Денежный профиль: {result.money_score}/10",
        f"🍀 Профиль удачи: {result.luck_score}/10",
        f"🌱 Профиль роста: {result.growth_score}/10",
        f"🛡 Профиль стабильности: {result.stability_score}/10",
        f"🏆 Общий балл: {result.overall_score}/100",
        "",
    ]

    if result.repeated_digits:
        digits_str = ", ".join(str(d) for d in result.repeated_digits)
        parts.append(f"🔁 Повторяющиеся цифры: {digits_str}")

    if result.repeated_pairs:
        parts.append(f"🔗 Повторяющиеся пары: {', '.join(result.repeated_pairs)}")

    if result.detected_patterns:
        parts.append("🧩 Обнаруженные узоры:")
        for p in result.detected_patterns:
            parts.append(f"  • {p.description}")

    parts += [
        "",
        "💎 Денежный талисман: эту купюру символически стоит оставить себе — "
        "в нашей развлекательной системе она несёт заметную энергетику числа "
        f"{result.reduced_number}.",
        "",
        "ℹ️ Напоминаем: это развлекательная нумерологическая интерпретация, "
        "она не является финансовым советом и не гарантирует реальных событий.",
    ]

    return "\n".join(parts)
