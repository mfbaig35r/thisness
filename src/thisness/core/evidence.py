"""Typed scores and evidence (PRD §10-11, design 02 §6.2)."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from thisness.core.base import Model
from thisness.core.types import EvidenceId, Name, NodeId, NonEmpty, RunId, StageId

CALIBRATED_PROBABILITY = "calibrated_match_probability"


class Score(Model):
    """A number with its meaning attached. There is no universal confidence float.

    Only a score with a calibration reference may call itself a calibrated
    match probability.
    """

    kind: Name
    value: float | str
    calibration_ref: str | None = None
    model_ref: str | None = None

    @model_validator(mode="after")
    def _calibrated_probability_is_earned(self) -> Self:
        if self.kind == CALIBRATED_PROBABILITY:
            if self.calibration_ref is None:
                raise ValueError(f"{CALIBRATED_PROBABILITY} requires a calibration_ref")
            if not isinstance(self.value, float) or not 0.0 <= self.value <= 1.0:
                raise ValueError(f"{CALIBRATED_PROBABILITY} must be a float in [0, 1]")
        return self


class EvidenceItem(Model):
    evidence_id: EvidenceId
    left_id: NodeId
    right_id: NodeId
    kind: Name
    attribute: str | None = None
    level: str | None = None  # comparison level; Fellegi-Sunter consumes this
    direction: Literal["supports", "contradicts", "neutral"]
    strength: Literal["weak", "moderate", "strong", "decisive"] | None = None
    value: JsonValue = None
    score: Score | None = None
    method_ref: NonEmpty
    inputs: dict[str, JsonValue] = Field(default_factory=dict)  # filtered by log_policy
    run_id: RunId | None = None
    stage_id: StageId | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _two_different_nodes(self) -> Self:
        if self.left_id == self.right_id:
            raise ValueError("evidence compares two different nodes")
        return self
