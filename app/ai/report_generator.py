"""Turns a deterministic AnalysisResult into a polished natural-language report.

The AI performs zero calculation — it only narrates the structured data it is
given. If OpenAI is unavailable or misconfigured, falls back to a plain
deterministic report so a paying user is never left empty-handed.
"""

from __future__ import annotations

import json

from app.ai.client import complete_chat
from app.ai.prompts import REPORT_USER_TEMPLATE, SYSTEM_PROMPT
from app.analysis.interpreter import render_fallback_full_report
from app.analysis.models import AnalysisResult
from app.analysis.rules import DIGIT_MEANINGS
from app.config import settings
from app.formatting import to_telegram_html, truncate_telegram_html
from app.logging import get_logger

logger = get_logger(__name__)


class ReportGenerationError(Exception):
    """Raised when the AI report could not be generated (caller may retry)."""


def _build_ai_payload(result: AnalysisResult) -> dict:
    """Curated, narrative-friendly facts for the AI prompt.

    Deliberately excludes `score_breakdown` and `algorithm_version` — the
    breakdown holds raw scoring mechanics ("+6", "+1", internal factor
    names) that the report must never expose to the user, so the simplest
    way to guarantee that is to never send them to the model in the first
    place, rather than relying solely on a prompt instruction.

    The same rule governs the birth date: only the *derived* life-path
    numbers are sent, never the date itself, so the model structurally
    cannot repeat a user's date of birth back to them.
    """
    digits_present = sorted(set(result.digits))
    payload = {
        "serial_number": result.normalized_number,
        "digits": result.digits,
        "digit_sum": result.digit_sum,
        "reduced_number": result.reduced_number,
        "reduced_number_meaning": DIGIT_MEANINGS[result.reduced_number],
        "digit_meanings": {str(d): DIGIT_MEANINGS[d] for d in digits_present},
        "digit_frequency": {str(k): v for k, v in result.digit_frequency.items()},
        "repeated_digits": result.repeated_digits,
        "repeated_pairs": result.repeated_pairs,
        "detected_patterns": [
            {"name": p.name, "description": p.description} for p in result.detected_patterns
        ],
        "money_score": result.money_score,
        "luck_score": result.luck_score,
        "growth_score": result.growth_score,
        "stability_score": result.stability_score,
        "overall_score": result.overall_score,
    }

    # Birth-date personalization, added only when the analysis actually has
    # it — a serial-only or pre-feature analysis sends a payload identical to
    # what it always sent. The raw birth date is deliberately NOT included:
    # the model needs the derived life-path digit, its meaning and how it
    # meets the serial, and nothing about the actual date can improve that.
    if result.birth_number is not None:
        payload["birth_number"] = result.birth_number
        payload["birth_number_meaning"] = result.birth_number_meaning
        payload["birth_resonance"] = result.birth_resonance
        payload["birth_digit_in_serial_count"] = result.birth_digit_in_serial_count

    return payload


async def generate_report(result: AnalysisResult) -> str:
    """Generate the full personalized report text, ready to send as-is under
    Telegram's HTML parse mode (already sanitized and length-capped).

    Raises ReportGenerationError if OpenAI is unreachable/misconfigured so the
    caller can decide to retry or serve the deterministic fallback. Callers
    that always want a result (never fail the user) should use
    `generate_report_with_fallback` instead.
    """
    if not settings.openai_configured:
        raise ReportGenerationError("OPENAI_API_KEY is not configured")

    analysis_json = json.dumps(_build_ai_payload(result), ensure_ascii=False, indent=2)
    user_prompt = REPORT_USER_TEMPLATE.format(analysis_json=analysis_json)

    try:
        raw_text = await complete_chat(SYSTEM_PROMPT, user_prompt)
    except Exception as exc:  # noqa: BLE001 - convert any SDK error into our error type
        logger.warning("OpenAI report generation failed: %s", type(exc).__name__)
        raise ReportGenerationError(str(exc)) from exc

    return truncate_telegram_html(to_telegram_html(raw_text))


async def generate_report_with_fallback(result: AnalysisResult) -> str:
    """Best-effort report generation: never raises. Falls back to the
    deterministic template if the AI call fails for any reason.
    """
    try:
        return await generate_report(result)
    except ReportGenerationError:
        return render_fallback_full_report(result)
