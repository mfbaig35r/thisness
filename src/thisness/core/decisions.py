"""Decisions (design 01 §4 and §7.6, design 02 §6.2).

All decision kinds share one id space and one table, so every
``basis_decision_id`` resolves in one place. ``decision_type`` discriminates.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from thisness.core.base import Model
from thisness.core.evidence import Score
from thisness.core.types import (
    DecisionId,
    EntityId,
    EvidenceId,
    InvocationId,
    NodeId,
    NonEmpty,
    ReviewId,
    RunId,
    StageId,
)


class Actor(Model):
    kind: Literal["resolver", "human", "system", "agent"]
    id: NonEmpty


class Verdict(StrEnum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    POSSIBLE_MATCH = "POSSIBLE_MATCH"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


class ConsensusOutcome(StrEnum):
    CREATE_ENTITY = "CREATE_ENTITY"
    CREATE_PROVISIONAL = "CREATE_PROVISIONAL"
    ATTACH = "ATTACH"
    MERGE_PROVISIONAL = "MERGE_PROVISIONAL"
    PROPOSE_MERGE = "PROPOSE_MERGE"
    REVIEW_CLUSTER = "REVIEW_CLUSTER"
    SPLIT_REVIEW = "SPLIT_REVIEW"
    BOUNDARY_REVIEW = "BOUNDARY_REVIEW"  # same entity, or related? (design 01 §7.4)
    NO_CHANGE = "NO_CHANGE"


_NEEDS_TARGET = {ConsensusOutcome.ATTACH, ConsensusOutcome.MERGE_PROVISIONAL}


class OperatorAction(StrEnum):
    MERGE = "MERGE"
    SPLIT = "SPLIT"
    PROMOTE = "PROMOTE"
    RELATE = "RELATE"
    REVOKE_RELATIONSHIP = "REVOKE_RELATIONSHIP"
    REVOKE_CONSTRAINT = "REVOKE_CONSTRAINT"
    REDACT = "REDACT"
    LABEL = "LABEL"
    CLEAR_LABEL = "CLEAR_LABEL"


class _DecisionBase(Model):
    decision_id: DecisionId
    actor: Actor
    evidence_ids: tuple[EvidenceId, ...] = ()
    component_refs: tuple[NonEmpty, ...] = ()
    invocation_id: InvocationId | None = None  # the model call behind it, if any
    created_at: AwareDatetime


class PairJudgment(_DecisionBase):
    """A resolver's judgment on one pair: observation or entity on either side."""

    decision_type: Literal["pair"] = "pair"
    run_id: RunId | None = None
    stage_id: StageId | None = None
    left_id: NodeId
    right_id: NodeId
    verdict: Verdict
    scores: tuple[Score, ...] = ()

    @model_validator(mode="after")
    def _two_different_nodes(self) -> Self:
        if self.left_id == self.right_id:
            raise ValueError("a pair judgment compares two different nodes")
        return self


class SuggestedCut(Model):
    """A reviewer aid from a min-disagreement partition. Never applied automatically."""

    keep: tuple[NodeId, ...]
    move: tuple[NodeId, ...]
    disagreements: int = Field(ge=0)


class ConsensusDecision(_DecisionBase):
    decision_type: Literal["consensus"] = "consensus"
    run_id: RunId
    members: tuple[NodeId, ...] = Field(min_length=1)
    outcome: ConsensusOutcome
    target_entity_id: EntityId | None = None
    flags: dict[str, JsonValue] = Field(default_factory=dict)
    suggested_cut: SuggestedCut | None = None

    @model_validator(mode="after")
    def _target_matches_outcome(self) -> Self:
        if self.outcome in _NEEDS_TARGET and self.target_entity_id is None:
            raise ValueError(f"{self.outcome} requires target_entity_id")
        if len(set(self.members)) != len(self.members):
            raise ValueError("members must be unique")
        return self


class OperatorDecision(_DecisionBase):
    """An explicit action by a person, an agent or the system: merge, split, promote."""

    decision_type: Literal["operator"] = "operator"
    action: OperatorAction
    subject_ids: tuple[NonEmpty, ...] = Field(min_length=1)
    reason: NonEmpty


ReviewResolution = Literal["groups", "same", "different", "related", "approve", "reject", "dismiss"]


class ReviewDecision(_DecisionBase):
    """A person's answer to a review (design 04 §2.4). ``groups`` records the partition of
    the review's members the answer means (every answer but a dismissal has one; for
    ``groups`` it is the reviewer's own); ``relation`` names a relationship."""

    decision_type: Literal["review"] = "review"
    review_id: ReviewId
    verdict: Verdict
    resolution: ReviewResolution = "dismiss"
    groups: tuple[tuple[NodeId, ...], ...] | None = None
    relation: str | None = None
    reason: NonEmpty
    # The model's recommendation the reviewer was shown, if any (design 05 §5).
    recommendation_id: DecisionId | None = None

    @model_validator(mode="after")
    def _shape_matches_resolution(self) -> Self:
        if (self.resolution == "dismiss") == (self.groups is not None):
            raise ValueError("groups are given exactly when the resolution is not 'dismiss'")
        if (self.resolution == "related") != (self.relation is not None):
            raise ValueError("a relation is given exactly when the resolution is 'related'")
        if self.groups is not None:
            seen = [n for g in self.groups for n in g]
            if len(seen) != len(set(seen)) or any(not g for g in self.groups):
                raise ValueError("groups must be non-empty and disjoint")
        return self


class ReviewRecommendation(_DecisionBase):
    """A model's suggested answer to a review (design 05 §5). It never closes a review or
    changes identity state, and it is not a label: a person's answer is. ``groups``
    partitions the review's members; ``evidence_ids`` cites the judgments it relied on."""

    decision_type: Literal["recommendation"] = "recommendation"
    review_id: ReviewId
    resolution: Literal["same", "different", "groups", "related", "unsure"]
    groups: tuple[tuple[NodeId, ...], ...] | None = None
    relation: str | None = None
    reason_codes: tuple[str, ...] = ()
    rationale: str | None = None
    cited_judgments: tuple[DecisionId, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.resolution == "unsure") == (self.groups is not None):
            raise ValueError("groups are given exactly when the model answered")
        if (self.resolution == "related") != (self.relation is not None):
            raise ValueError("a relation is given exactly when the resolution is 'related'")
        return self


class LabelSuggestion(_DecisionBase):
    """A model's suggested display name for an entity (design 08 §5.2). It changes nothing:
    a person accepting it does, by a labeling commit that cites it. ``name`` is checked to
    be a spelling of one of the entity's own names before it is recorded."""

    decision_type: Literal["label_suggestion"] = "label_suggestion"
    entity_id: EntityId
    name: NonEmpty
    rationale: str | None = None


Decision = Annotated[
    PairJudgment
    | ConsensusDecision
    | OperatorDecision
    | ReviewDecision
    | ReviewRecommendation
    | LabelSuggestion,
    Field(discriminator="decision_type"),
]
