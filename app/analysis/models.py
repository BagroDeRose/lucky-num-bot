"""Typed data structures produced by the deterministic analysis engine.

These models are the contract between the analysis engine and everything
downstream (Telegram handlers, database storage, the AI report generator).
The AI layer must never compute values that belong here — it only narrates
what this module has already calculated.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ScoreFactor(BaseModel):
    """One explainable contribution to a score.

    `effect` is a signed integer string such as "+2" or "-1" applied to the
    relevant sub-score, kept human-readable for direct display if desired.
    """

    factor: str
    effect: str
    reason: str


class ScoreBreakdown(BaseModel):
    money: list[ScoreFactor] = Field(default_factory=list)
    luck: list[ScoreFactor] = Field(default_factory=list)
    growth: list[ScoreFactor] = Field(default_factory=list)
    stability: list[ScoreFactor] = Field(default_factory=list)


class DetectedPattern(BaseModel):
    name: str
    description: str


class AnalysisResult(BaseModel):
    """Complete deterministic analysis of a single banknote serial number."""

    algorithm_version: str

    normalized_number: str
    digits: list[int]
    digit_sum: int
    reduced_number: int

    digit_frequency: dict[int, int]
    repeated_digits: list[int]
    repeated_pairs: list[str]
    detected_patterns: list[DetectedPattern]

    money_score: int
    luck_score: int
    growth_score: int
    stability_score: int
    overall_score: int

    score_breakdown: ScoreBreakdown

    def model_dump_public(self) -> dict:
        """A JSON-friendly dict (dict keys as strings) for storage/AI input."""
        data = self.model_dump()
        data["digit_frequency"] = {str(k): v for k, v in data["digit_frequency"].items()}
        return data
