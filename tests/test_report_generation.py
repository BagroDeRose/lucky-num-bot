from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    assert "Денежный потенциал" in text
    assert "Общий показатель" in text
    assert f"{sample_result.overall_score}/100" in text


async def test_generate_report_wraps_timeout(sample_result, monkeypatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")

    async def timeout_complete_chat(system_prompt: str, user_prompt: str) -> str:
        raise TimeoutError("OpenAI request timed out")

    monkeypatch.setattr(report_generator, "complete_chat", timeout_complete_chat)

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
