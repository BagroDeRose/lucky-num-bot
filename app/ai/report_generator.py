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
from app.config import settings
from app.logging import get_logger

logger = get_logger(__name__)


class ReportGenerationError(Exception):
    """Raised when the AI report could not be generated (caller may retry)."""


async def generate_report(result: AnalysisResult) -> str:
    """Generate the full personalized report text.

    Raises ReportGenerationError if OpenAI is unreachable/misconfigured so the
    caller can decide to retry or serve the deterministic fallback. Callers
    that always want a result (never fail the user) should use
    `generate_report_with_fallback` instead.
    """
    if not settings.openai_configured:
        raise ReportGenerationError("OPENAI_API_KEY is not configured")

    analysis_json = json.dumps(result.model_dump_public(), ensure_ascii=False, indent=2)
    user_prompt = REPORT_USER_TEMPLATE.format(analysis_json=analysis_json)

    try:
        return await complete_chat(SYSTEM_PROMPT, user_prompt)
    except Exception as exc:  # noqa: BLE001 - convert any SDK error into our error type
        logger.warning("OpenAI report generation failed: %s", type(exc).__name__)
        raise ReportGenerationError(str(exc)) from exc


async def generate_report_with_fallback(result: AnalysisResult) -> str:
    """Best-effort report generation: never raises. Falls back to the
    deterministic template if the AI call fails for any reason.
    """
    try:
        return await generate_report(result)
    except ReportGenerationError:
        return render_fallback_full_report(result)
