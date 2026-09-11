from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from app.ai import client as ai_client
from app.ai import report_generator
from app.analysis.engine import analyze
from app.config import settings


@pytest.fixture
def sample_result():
    return analyze("2200373")


async def test_generate_report_raises_when_openai_not_configured(sample_result, monkeypatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "")
    with pytest.raises(report_generator.ReportGenerationError):
        await report_generator.generate_report(sample_result)


async def test_generate_report_returns_ai_text_on_success(sample_result, monkeypatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")

    async def fake_complete_chat(system_prompt: str, user_prompt: str) -> str:
        assert "2200373" in user_prompt
        return "Полный отчёт по вашей купюре."

    monkeypatch.setattr(report_generator, "complete_chat", fake_complete_chat)

    text = await report_generator.generate_report(sample_result)
    assert text == "Полный отчёт по вашей купюре."


async def test_generate_report_wraps_sdk_errors(sample_result, monkeypatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")

    async def failing_complete_chat(system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("boom: upstream unavailable")

    monkeypatch.setattr(report_generator, "complete_chat", failing_complete_chat)

    with pytest.raises(report_generator.ReportGenerationError):
        await report_generator.generate_report(sample_result)


async def test_generate_report_with_fallback_never_raises(sample_result, monkeypatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "")

    text = await report_generator.generate_report_with_fallback(sample_result)
    assert "2200373" in text
    # Product direction: no "entertainment only" / "not financial advice"
    # disclaimer footer in the report text.
    assert "развлекательная" not in text.lower()
    assert "не является финансовым" not in text.lower()
    # But it must still read as a full, structured report, not a bare stub.
    assert "Денежный профиль" in text
    assert "Итог" in text
    assert f"{sample_result.overall_score}/100" in text


async def test_generate_report_wraps_timeout(sample_result, monkeypatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")

    async def timeout_complete_chat(system_prompt: str, user_prompt: str) -> str:
        raise TimeoutError("OpenAI request timed out")

    monkeypatch.setattr(report_generator, "complete_chat", timeout_complete_chat)

    with pytest.raises(report_generator.ReportGenerationError):
        await report_generator.generate_report(sample_result)


_FAKE_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


@pytest.mark.parametrize(
    "make_exc",
    [
        lambda: openai.AuthenticationError(
            "Invalid API key", response=httpx.Response(401, request=_FAKE_REQUEST), body=None
        ),
        lambda: openai.RateLimitError(
            "Rate limit exceeded", response=httpx.Response(429, request=_FAKE_REQUEST), body=None
        ),
        lambda: openai.APIConnectionError(request=_FAKE_REQUEST),
        lambda: openai.APITimeoutError(request=_FAKE_REQUEST),
    ],
    ids=["auth_error", "rate_limit_error", "connection_error", "timeout_error"],
)
async def test_generate_report_wraps_real_openai_sdk_error_types(
    sample_result, monkeypatch, make_exc
) -> None:
    """Uses the SDK's actual exception classes (not a generic RuntimeError
    stand-in) to prove the failure path genuinely handles what the OpenAI
    SDK raises for auth failures, rate limits, connection errors, and
    timeouts — each has its own constructor shape, so a generic mock could
    hide a crash (e.g. in error logging) that only a real instance exposes.
    """
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    fake_client = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(side_effect=make_exc())
    monkeypatch.setattr(ai_client, "get_openai_client", lambda: fake_client)

    with pytest.raises(report_generator.ReportGenerationError):
        await report_generator.generate_report(sample_result)


async def test_generate_report_retry_succeeds_after_prior_failure(sample_result, monkeypatch) -> None:
    """A failed attempt must not poison future attempts — the next call with
    the same input can still succeed (the "retry" the bot's retry button
    relies on).
    """
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    calls = {"count": 0}

    async def flaky_complete_chat(system_prompt: str, user_prompt: str) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("transient upstream failure")
        return "Отчёт готов после повторной попытки."

    monkeypatch.setattr(report_generator, "complete_chat", flaky_complete_chat)

    with pytest.raises(report_generator.ReportGenerationError):
        await report_generator.generate_report(sample_result)

    text = await report_generator.generate_report(sample_result)
    assert text == "Отчёт готов после повторной попытки."
    assert calls["count"] == 2


async def test_complete_chat_raises_on_empty_string_content(monkeypatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    fake_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])
    fake_client = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_response)
    monkeypatch.setattr(ai_client, "get_openai_client", lambda: fake_client)

    with pytest.raises(RuntimeError, match="empty"):
        await ai_client.complete_chat("system", "user")


async def test_complete_chat_raises_on_none_content(monkeypatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    fake_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None))])
    fake_client = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_response)
    monkeypatch.setattr(ai_client, "get_openai_client", lambda: fake_client)

    with pytest.raises(RuntimeError, match="empty"):
        await ai_client.complete_chat("system", "user")


async def test_generate_report_wraps_malformed_response_with_no_choices(
    sample_result, monkeypatch
) -> None:
    """A response with an empty `choices` list (malformed/unexpected shape)
    must be converted to ReportGenerationError, never crash the handler.
    """
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    fake_response = SimpleNamespace(choices=[])
    fake_client = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_response)
    monkeypatch.setattr(ai_client, "get_openai_client", lambda: fake_client)

    with pytest.raises(report_generator.ReportGenerationError):
        await report_generator.generate_report(sample_result)


# --- Regression coverage for the max_tokens -> max_completion_tokens fix --
#
# Real failure: switching OPENAI_MODEL to a newer model (e.g. gpt-5.4-mini)
# made every report generation fail with
#   BadRequestError: Unsupported parameter: 'max_tokens' is not supported
#   with this model. Use 'max_completion_tokens' instead.
# Verified empirically (a live, minimal call against the configured model)
# that `max_completion_tokens` is accepted by gpt-5.4-mini *and* by the
# older models this project has used as defaults (gpt-3.5-turbo,
# gpt-4o-mini) — so this is a straight swap, not a per-model branch.


async def test_complete_chat_requests_max_completion_tokens_not_max_tokens(monkeypatch) -> None:
    """The exact parameter name OpenAI's newer models reject if used."""
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    captured_kwargs: dict = {}

    async def fake_create(**kwargs):
        captured_kwargs.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok"),
                    finish_reason="stop",
                )
            ]
        )

    fake_client = AsyncMock()
    fake_client.chat.completions.create = fake_create
    monkeypatch.setattr(ai_client, "get_openai_client", lambda: fake_client)

    await ai_client.complete_chat("system", "user")

    assert "max_completion_tokens" in captured_kwargs
    assert "max_tokens" not in captured_kwargs
    assert isinstance(captured_kwargs["max_completion_tokens"], int)
    assert captured_kwargs["max_completion_tokens"] > 0


async def test_complete_chat_succeeds_when_finish_reason_is_stop(monkeypatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    fake_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="Готовый отчёт."),
                finish_reason="stop",
            )
        ]
    )
    fake_client = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_response)
    monkeypatch.setattr(ai_client, "get_openai_client", lambda: fake_client)

    text = await ai_client.complete_chat("system", "user")
    assert text == "Готовый отчёт."


async def test_complete_chat_raises_when_truncated_by_token_limit(monkeypatch) -> None:
    """finish_reason="length" means the model was cut off mid-report by the
    token budget (including, on reasoning-capable models, budget silently
    consumed by hidden reasoning tokens) — must not be delivered as if it
    were a complete report.
    """
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    fake_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="Отчёт обрывается на полусл"),
                finish_reason="length",
            )
        ]
    )
    fake_client = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_response)
    monkeypatch.setattr(ai_client, "get_openai_client", lambda: fake_client)

    with pytest.raises(RuntimeError, match="truncated"):
        await ai_client.complete_chat("system", "user")


async def test_generate_report_recovers_from_truncated_completion(sample_result, monkeypatch) -> None:
    """The higher-level generate_report() must convert a truncation into the
    same retryable ReportGenerationError as any other failure — the paid
    report handler's existing retry button relies on this.
    """
    monkeypatch.setattr(settings, "openai_api_key", "test-key")

    async def truncated_complete_chat(system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("OpenAI response was truncated before completing")

    monkeypatch.setattr(report_generator, "complete_chat", truncated_complete_chat)

    with pytest.raises(report_generator.ReportGenerationError):
        await report_generator.generate_report(sample_result)


def test_complete_chat_max_completion_tokens_is_configured_reasonably() -> None:
    """Sanity bound on the token budget: generous enough for the ~1500-2500
    (up to ~3000 for exceptional numbers) character Russian report target
    (see app.ai.prompts.SYSTEM_PROMPT — the "fewer facts, more
    personalization" redesign), but capped well short of "unbounded", so a
    misbehaving model can't silently rack up an oversized bill on a single
    report. 1400 was set from live measurements: real reports at this
    target used 768-828 completion tokens per call.
    """
    import inspect

    source = inspect.getsource(ai_client.complete_chat)
    assert "max_completion_tokens=1400" in source


async def test_get_openai_client_configures_an_explicit_bounded_timeout(monkeypatch) -> None:
    """Regression test: the SDK's own default (600s) would leave a user
    waiting up to 10 minutes with zero feedback if a request ever hangs —
    an explicit, bounded timeout is required so a stuck call fails fast
    enough to trigger the existing retry path instead.
    """
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(ai_client, "_client", None)

    client = ai_client.get_openai_client()

    assert client.timeout == ai_client.OPENAI_REQUEST_TIMEOUT_SECONDS
    assert client.timeout < 600  # meaningfully below the SDK default
