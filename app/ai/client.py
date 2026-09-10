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


def get_openai_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        if not settings.openai_configured:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
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
        # The full paid report has ~10 sections targeting ~2500-3800 Russian
        # characters (see app.ai.prompts.SYSTEM_PROMPT); Cyrillic tokenizes
        # less efficiently than English, so this needs meaningfully more
        # headroom than a short reply.
        max_tokens=2200,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("OpenAI returned an empty response")
    return content.strip()
