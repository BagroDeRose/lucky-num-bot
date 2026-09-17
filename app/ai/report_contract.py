"""The structured contract between the AI and the application for the paid
report.

WHY THIS EXISTS

The model used to write the entire report, including every score-bearing
heading, by filling a template like "💰 <b>ДЕНЕЖНЫЙ ПРОФИЛЬ — {money_score}/10</b>".
The application then passed that text to the user after HTML sanitization
only. Nothing checked the headings, so label drift reached users: a real
report labelled the money section "💰 ПРОФИЛЬ УДАЧИ — 3/10" (money's correct
value) right above "🍀 ПРОФИЛЬ УДАЧИ — 4/10" — two different numbers under one
category, even though the application had computed exactly one luck score.

THE CONTRACT

The model returns a JSON object of prose *bodies* only (see SECTION_KEYS).
The application owns everything that states a canonical fact: every heading,
every score, the serial number, the main number, the birth number and the
digit-sum arithmetic are rendered here from the AnalysisResult. The model
therefore has no place to write a heading or a score, and each category
heading is emitted exactly once by construction.

Bodies are still validated, because prose can smuggle numbers back in: a body
may not restate a score that differs from the canonical one, may not contain
a section heading anchor, and may not leak internal field names. Any
violation raises ReportContractError, which the caller converts into the
existing retryable ReportGenerationError — a malformed report is never shown.
"""

from __future__ import annotations

import json
import re

from app.analysis.interpreter import (
    render_digit_sum_calculation,
    render_personal_section,
    render_special_section,
)
from app.analysis.models import AnalysisResult
from app.formatting import to_telegram_html, truncate_telegram_html


class ReportContractError(Exception):
    """The model's output does not satisfy the report contract."""


# Every key the model is asked to return. Optional sections may be empty; the
# application decides whether they are rendered at all (see render_report).
REQUIRED_SECTIONS = (
    "opening",
    "main_number",
    "digit_story",
    "money",
    "luck",
    "growth",
    "stability",
    "summary",
    "verdict",
)
OPTIONAL_SECTIONS = ("special", "birth")
SECTION_KEYS = REQUIRED_SECTIONS + OPTIONAL_SECTIONS

# The profile sections and the canonical score each one is allowed to state.
PROFILE_SCORE_FIELDS = {
    "money": "money_score",
    "luck": "luck_score",
    "growth": "growth_score",
    "stability": "stability_score",
}

# Headings are rendered by the application only. These are the exact
# strings users see, and the single source of truth for them.
PROFILE_HEADINGS = {
    "money": ("💰", "ДЕНЕЖНЫЙ ПРОФИЛЬ"),
    "luck": ("🍀", "ПРОФИЛЬ УДАЧИ"),
    "growth": ("🌱", "ПРОФИЛЬ РОСТА"),
    "stability": ("🛡", "ПРОФИЛЬ СТАБИЛЬНОСТИ"),
}

# Anything that looks like a section heading inside a body would render as a
# second, application-unchecked heading — exactly the bug this module fixes.
_HEADING_ANCHORS = ("🔮", "🔢", "🔎", "✨", "🎂", "💰", "🍀", "🌱", "🛡", "⭐", "💥")
_HEADING_LABELS = (
    "ДЕНЕЖНЫЙ РАЗБОР",
    "ГЛАВНОЕ ЧИСЛО",
    "ИСТОРИЯ ЦИФР",
    "ОСОБЫЕ СОЧЕТАНИЯ",
    "ЧИСЛО РОЖДЕНИЯ",
    "ДЕНЕЖНЫЙ ПРОФИЛЬ",
    "ПРОФИЛЬ УДАЧИ",
    "ПРОФИЛЬ РОСТА",
    "ПРОФИЛЬ СТАБИЛЬНОСТИ",
    "ИТОГ",
    "ВЕРДИКТ ЖМЫХА",
)

# "4/10", "4 / 10", "50/100", "4 из 10". Scores stated in prose are checked
# against the canonical values; nothing else in the report uses these forms.
_SCORE_MENTION_RE = re.compile(r"(\d{1,3})\s*(?:/|из)\s*(10|100)(?!\d)")

# Internal identifiers the model must never echo to a user: snake_case field
# names, and the enum codes the payload uses for birth resonance.
_INTERNAL_IDENTIFIER_RE = re.compile(
    r"\b[a-z]+_[a-z_]+\b|\b(?:same_number|present|absent|json|null)\b",
    re.IGNORECASE,
)

# Headings in a finished report, used to prove each category appears once
# with its canonical value. Matches both the AI report's uppercase headings
# and the deterministic fallback's sentence-case ones.
SCORE_HEADING_RE = re.compile(
    r"<b>\s*(ДЕНЕЖНЫЙ ПРОФИЛЬ|ПРОФИЛЬ УДАЧИ|ПРОФИЛЬ РОСТА|ПРОФИЛЬ СТАБИЛЬНОСТИ|ИТОГ)"
    r"\s*—\s*(\d{1,3})\s*/\s*(10|100)\s*</b>",
    re.IGNORECASE,
)
_HEADING_TO_FIELD = {
    "ДЕНЕЖНЫЙ ПРОФИЛЬ": "money_score",
    "ПРОФИЛЬ УДАЧИ": "luck_score",
    "ПРОФИЛЬ РОСТА": "growth_score",
    "ПРОФИЛЬ СТАБИЛЬНОСТИ": "stability_score",
    "ИТОГ": "overall_score",
}


def parse_sections(raw: str) -> dict[str, str]:
    """Parse the model's JSON object into validated section bodies.

    Missing optional sections become empty strings. A missing or empty
    required section, a non-string value, or non-JSON output is a contract
    violation — never silently papered over.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ReportContractError("model output is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ReportContractError("model output is not a JSON object")

    sections: dict[str, str] = {}
    for key in SECTION_KEYS:
        value = data.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ReportContractError(f"section {key!r} is not a string")
        value = value.strip()
        if key in REQUIRED_SECTIONS and not value:
            raise ReportContractError(f"required section {key!r} is missing or empty")
        sections[key] = value
    return sections


def validate_sections(sections: dict[str, str], result: AnalysisResult) -> None:
    """Reject bodies that could contradict the canonical analysis.

    - A profile body may only state its own canonical score ("4/10" in the
      luck body when luck is 4); any other /10 mention, anywhere, is rejected.
    - A /100 mention anywhere must equal the canonical overall score.
    - No body may contain a section heading anchor or heading label.
    - No body may leak an internal field name or enum code.
    """
    for key, body in sections.items():
        if not body:
            continue

        for match in _SCORE_MENTION_RE.finditer(body):
            value, scale = int(match.group(1)), match.group(2)
            if scale == "100":
                if value != result.overall_score:
                    raise ReportContractError(
                        f"section {key!r} states overall {value}/100, "
                        f"canonical is {result.overall_score}"
                    )
                continue
            own_field = PROFILE_SCORE_FIELDS.get(key)
            if own_field is None or value != getattr(result, own_field):
                raise ReportContractError(
                    f"section {key!r} states a /10 score ({value}) it does not own"
                )

        for anchor in _HEADING_ANCHORS:
            if anchor in body:
                raise ReportContractError(f"section {key!r} contains a heading anchor")
        upper = body.upper()
        for label in _HEADING_LABELS:
            if label in upper and _looks_like_heading(upper, label):
                raise ReportContractError(f"section {key!r} contains heading {label!r}")

        leaked = _INTERNAL_IDENTIFIER_RE.search(body)
        if leaked:
            raise ReportContractError(f"section {key!r} leaks internal identifier")


def _looks_like_heading(upper_body: str, label: str) -> bool:
    """A heading label counts only in heading form — bold, alone on its line,
    or followed by a dash ("ИТОГ — 50/100"). Ordinary prose that merely
    starts with the same words ("Итог простой: …") must not be rejected,
    since a false rejection costs the user a paid generation attempt.
    """
    if f"<B>{label}" in upper_body:
        return True
    for line in upper_body.splitlines():
        stripped = line.strip()
        if stripped == label:
            return True
        if stripped.startswith(label) and stripped[len(label) :].lstrip().startswith("—"):
            return True
    return False


# Models reach for markup line breaks that Telegram HTML does not support.
# Unconverted, the sanitizer escapes them into visible junk — a live report
# ended its opening with a literal "</n>". A literal backslash-n (JSON
# double-escaping) is the same mistake in another form.
_LINE_BREAK_RE = re.compile(r"<\s*/?\s*(?:br|p|n)\s*/?\s*>|\\n", re.IGNORECASE)
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _body(text: str) -> str:
    # Line-break markup becomes real newlines first; then each body is
    # sanitized on its own, so an unbalanced tag in one body degrades only
    # that body to plain text instead of stripping formatting report-wide.
    text = _LINE_BREAK_RE.sub("\n", text)
    text = _EXCESS_BLANK_LINES_RE.sub("\n\n", text).strip()
    return to_telegram_html(text)


def render_report(sections: dict[str, str], result: AnalysisResult) -> str:
    """Assemble the final report. Every heading and every number comes from
    `result`; the model contributes prose between them.
    """
    parts = [
        "🔮 <b>ДЕНЕЖНЫЙ РАЗБОР</b>",
        f"Серийный номер: <b>{result.normalized_number}</b>",
        "",
        _body(sections["opening"]),
        "",
        f"🔢 <b>ГЛАВНОЕ ЧИСЛО — {result.reduced_number}</b>",
        render_digit_sum_calculation(result),
        "",
        _body(sections["main_number"]),
        "",
        "🔎 <b>ИСТОРИЯ ЦИФР</b>",
        _body(sections["digit_story"]),
    ]

    # Rendered only when the analysis actually found something, whatever the
    # model returned. An empty model body falls back to deterministic copy so
    # a real finding is never silently dropped.
    if result.repeated_digits or result.repeated_pairs or result.detected_patterns:
        special = _body(sections["special"]) if sections["special"] else render_special_section(result)
        if special:
            parts += ["", "✨ <b>ОСОБЫЕ СОЧЕТАНИЯ</b>", special]

    # Same rule for personalization: present only when the analysis was
    # computed with a birth date; legacy and skipped analyses never show it.
    if result.birth_number is not None:
        personal = _body(sections["birth"]) if sections["birth"] else render_personal_section(result)
        if personal:
            parts += ["", f"🎂 <b>ВАШЕ ЧИСЛО РОЖДЕНИЯ — {result.birth_number}</b>", personal]

    for key, (emoji, label) in PROFILE_HEADINGS.items():
        score = getattr(result, PROFILE_SCORE_FIELDS[key])
        parts += ["", f"{emoji} <b>{label} — {score}/10</b>", _body(sections[key])]

    parts += [
        "",
        f"⭐ <b>ИТОГ — {result.overall_score}/100</b>",
        _body(sections["summary"]),
        "",
        "💥 <b>ВЕРДИКТ ЖМЫХА</b>",
        _body(sections["verdict"]),
    ]
    return truncate_telegram_html("\n".join(parts))


def score_headings(report: str) -> dict[str, list[int]]:
    """Every score heading in a finished report, grouped by canonical field."""
    found: dict[str, list[int]] = {}
    for label, value, _scale in SCORE_HEADING_RE.findall(report):
        found.setdefault(_HEADING_TO_FIELD[label.upper()], []).append(int(value))
    return found


def verify_canonical_scores(report: str, result: AnalysisResult) -> None:
    """Final guard on an assembled report: no category may appear more than
    once, and every score heading must equal the canonical value.
    """
    for field, values in score_headings(report).items():
        if len(values) > 1:
            raise ReportContractError(f"{field} heading appears {len(values)} times")
        if values[0] != getattr(result, field):
            raise ReportContractError(
                f"{field} heading shows {values[0]}, canonical is {getattr(result, field)}"
            )


def build_report(raw: str, result: AnalysisResult) -> str:
    """Parse, validate, render and verify in one step."""
    sections = parse_sections(raw)
    validate_sections(sections, result)
    report = render_report(sections, result)
    verify_canonical_scores(report, result)
    return report
