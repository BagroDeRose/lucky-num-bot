from __future__ import annotations

import pytest

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
    assert "развлекательная" in text.lower()
