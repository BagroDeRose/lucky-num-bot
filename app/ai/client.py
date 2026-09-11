"""Thin wrapper around the OpenAI SDK.

Isolates the rest of the app from the SDK's API surface and keeps API-key
handling in one place.
"""

from __future__ import annotations

from openai import AsyncOpenAI

from app.config import settings
from app.logging import get_logger

logger = get_logger(__name__)

_client: AsyncOpenAI | None = None


#  The SDK's own default (600s) is far too long for a bot the user is
#  actively waiting on — a hung request would leave them staring at
#  nothing for up to 10 minutes with no feedback. An explicit, bounded
#  timeout means a stuck call fails predictably and quickly enough to
#  trigger the existing graceful-degradation path (ReportGenerationError ->
#  retry button) instead. 60s is generous for a genuine ~1500-2500
#  character generation while still failing well before the user gives up.
OPENAI_REQUEST_TIMEOUT_SECONDS = 60.0


def get_openai_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        if not settings.openai_configured:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        _client = AsyncOpenAI(
            api_key=settings.openai_api_key, timeout=OPENAI_REQUEST_TIMEOUT_SECONDS
        )
    return _client


async def complete_chat(system_prompt: str, user_prompt: str) -> str:
    """Single non-streaming chat completion. Raises on failure — callers are
    responsible for graceful degradation (see app.ai.report_generator).
    """
    client = get_openai_client()
    response = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        # `max_completion_tokens`, not the older `max_tokens`: OpenAI's Chat
        # Completions API rejects `max_tokens` outright for newer models
        # (confirmed against gpt-5.4-mini: "Unsupported parameter: 'max_tokens'
        # is not supported with this model. Use 'max_completion_tokens'
        # instead."), while `max_completion_tokens` is accepted by both that
        # and every older model this project has used (gpt-3.5-turbo,
        # gpt-4o-mini) — verified empirically, not just per changelog.
        #
        # The report prompt now targets ~1500-2500 Russian characters
        # ("fewer facts, more personalization" — see app.ai.prompts.
        # SYSTEM_PROMPT), down from the earlier ~2500-3800 target. Live
        # calls against the configured model at that target used 768-828
        # completion tokens per report (~3.1-3.2 chars/token for this
        # content). 1400 leaves ~40% headroom above the largest number
        # observed (a "dominant single digit" report, the kind most likely
        # to run long) without provisioning for a report far bigger than
        # the prompt actually asks for — lower than 2200 mainly because the
        # target length itself came down, not because of anything model-
        # specific.
        max_completion_tokens=1400,
    )
    choice = response.choices[0]
    content = choice.message.content
    if not content:
        raise RuntimeError("OpenAI returned an empty response")
    if choice.finish_reason == "length":
        # The model was cut off mid-report rather than finishing naturally
        # (on a reasoning-capable model this can happen even with content
        # present, if hidden reasoning tokens ate into the same budget).
        # Surfacing this as a failure is safer than silently sending a
        # truncated/malformed paid report — the caller's existing retry
        # path (ReportGenerationError) handles it the same as any other
        # generation failure.
        logger.warning("OpenAI response truncated by max_completion_tokens (finish_reason=length)")
        raise RuntimeError("OpenAI response was truncated before completing")
    return content.strip()
