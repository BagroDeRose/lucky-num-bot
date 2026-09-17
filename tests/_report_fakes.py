"""Shared test doubles for the AI report contract (app.ai.report_contract).

The model now returns a JSON object of prose bodies; these helpers produce
valid bodies — no headings, no scores, no internal identifiers — so tests
exercising payment/report flows go through the real parse/validate/render
path instead of bypassing it.
"""

from __future__ import annotations

import json


def valid_report_json(marker: str = "Готовый разбор.", **overrides: str) -> str:
    """A contract-valid model response. `marker` is placed in the opening so
    a test can find it in the rendered report; any section can be overridden.
    """
    sections = {
        "opening": marker,
        "main_number": "Главная цифра задаёт этому номеру понятный характер.",
        "digit_story": "Цифры идут одной линией, от начала к завершению.",
        "special": "Повтор здесь заметен сразу.",
        "birth": "Число рождения ложится на номер по-своему.",
        "money": "Про деньги номер говорит сдержанно.",
        "luck": "Удача здесь приходит в нужный момент.",
        "growth": "Движение вперёд читается ясно.",
        "stability": "Опора в номере ощутимая.",
        "summary": "Сильнее всего звучит рост, тише всего — деньги.",
        "verdict": "Этот номер не про суету.",
    }
    sections.update(overrides)
    return json.dumps(sections, ensure_ascii=False)
