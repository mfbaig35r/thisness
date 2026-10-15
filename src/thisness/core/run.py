"""Resolution runs, their artifacts, and change sets (design 01 §3-4, §8; design 02 §6.2, §7).

A ``ChangeSet`` is the only way identity state changes. Every operation in it
cites the decision that justifies it.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from thisness.core.base import Model
from thisness.core.identity import RESERVED_RELATIONS
from thisness.core.review import ReviewKind
from thisness.core.types import (
    ConstraintId,
    DecisionId,
    EntityId,
    EntityType,
    EvidenceId,
    LedgerSeq,
    Name,
    NodeId,
    NonEmpty,
    ObservationId,
    ProposalId,
    RelationshipId,
    ReviewId,
    RunId,
    StageId,
)


class RunStatus(StrEnum):
    PLANNED = "planned"
    EXECUTING = "executing"
    PROPOSED = "proposed"
    COMMITTED = "committed"
    ABANDONED = "abandoned"


class ResolutionRun(Model):
    run_id: RunId
    entity_type: EntityType
    profile_ref: NonEmpty
    base_seq: LedgerSeq  # the ledger position the run reads from
    status: RunStatus
    plan: dict[str, JsonValue]
    created_at: AwareDatetime
    committed_seq: LedgerSeq | None = None

    @model_validator(mode="after")
    def _commit_is_consistent(self) -> Self:
        if (self.status is RunStatus.COMMITTED) != (self.committed_seq is not None):
            raise ValueError("committed_seq is set exactly when the run is committed")
        if self.committed_seq is not None and self.committed_seq <= self.base_seq:
            raise ValueError("a run commits after its base")
        return self


StageKind = Literal["candidates", "evidence", "pair_resolver", "group_proposer", "verify", "human"]


class StageExecution(Model):
    stage_id: StageId
    run_id: RunId
    kind: StageKind
    producer_ref: NonEmpty
    family: NonEmpty  # independence class (design 01 §5.4)
    view: Literal["raw", "derived"]
    conditioned_on: tuple[StageId, ...] = ()
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None


class ProposalMember(Model):
    node_id: NodeId
    shown: str  # exactly what the producer saw
    group_no: int | None = Field(default=None, ge=0)  # None = kept apart (singleton)
    label_out: str | None = None
    uncertain: bool = False


class GroupProposal(Model):
    proposal_id: ProposalId
    run_id: RunId
    stage_id: StageId
    batch_id: NonEmpty
    members: tuple[ProposalMember, ...] = Field(min_length=1)
    invocation_id: str | None = None

    @model_validator(mode="after")
    def _members_unique(self) -> Self:
        ids = [m.node_id for m in self.members]
        if len(set(ids)) != len(ids):
            raise ValueError("a node appears at most once in a proposal")
        return self


# ---------------------------------------------------------------------------
# Change sets
# ---------------------------------------------------------------------------


class CreateEntity(Model):
    op: Literal["create_entity"] = "create_entity"
    entity_id: EntityId
    entity_type: EntityType
    state: Literal["active", "provisional"]
    members: tuple[ObservationId, ...] = ()
    decision_id: DecisionId


class Attach(Model):
    op: Literal["attach"] = "attach"
    observation_id: ObservationId
    entity_id: EntityId
    decision_id: DecisionId


class Merge(Model):
    """Move the merged entities' memberships to the survivor and retire them."""

    op: Literal["merge"] = "merge"
    survivor_id: EntityId
    merged_ids: tuple[EntityId, ...] = Field(min_length=1)
    decision_id: DecisionId
    expected_versions: dict[EntityId, int] | None = None

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if self.survivor_id in self.merged_ids:
            raise ValueError("the survivor cannot also be merged")
        if len(set(self.merged_ids)) != len(self.merged_ids):
            raise ValueError("merged_ids must be unique")
        return self


class Split(Model):
    """Move observations out of ``source_id``. A target that is a retired entity is
    reactivated (an unmerge); any other target is created."""

    op: Literal["split"] = "split"
    source_id: EntityId
    targets: dict[EntityId, tuple[ObservationId, ...]] = Field(min_length=1)
    decision_id: DecisionId
    expected_version: int | None = None

    @model_validator(mode="after")
    def _disjoint_targets(self) -> Self:
        if self.source_id in self.targets:
            raise ValueError("the source cannot be a split target")
        seen: set[str] = set()
        for target, observations in self.targets.items():
            if not observations:
                raise ValueError(f"split target {target} has no observations")
            overlap = seen.intersection(observations)
            if overlap:
                raise ValueError(f"observations assigned to two targets: {sorted(overlap)}")
            seen.update(observations)
        return self


class Promote(Model):
    op: Literal["promote"] = "promote"
    entity_id: EntityId
    decision_id: DecisionId


class Label(Model):
    """Set an entity's display name (design 08 §5.2); ``suggestion_id`` cites the model's
    suggestion it accepts, if any."""

    op: Literal["label"] = "label"
    entity_id: EntityId
    name: NonEmpty
    decision_id: DecisionId
    suggestion_id: DecisionId | None = None


class ClearLabel(Model):
    op: Literal["clear_label"] = "clear_label"
    entity_id: EntityId
    decision_id: DecisionId


class Relate(Model):
    op: Literal["relate"] = "relate"
    relationship_id: RelationshipId
    relation: Name
    subject_entity_id: EntityId
    object_entity_id: EntityId
    evidence_ids: tuple[EvidenceId, ...] = ()
    effective_from: date | None = None
    effective_to: date | None = None
    decision_id: DecisionId

    @model_validator(mode="after")
    def _not_identity(self) -> Self:
        if self.relation in RESERVED_RELATIONS:
            raise ValueError(f"{self.relation!r} is identity, not a relationship")
        if self.subject_entity_id == self.object_entity_id:
            raise ValueError("a relationship links two different entities")
        return self


class RevokeRelationship(Model):
    op: Literal["revoke_relationship"] = "revoke_relationship"
    relationship_id: RelationshipId
    decision_id: DecisionId


class OpenReview(Model):
    op: Literal["open_review"] = "open_review"
    review_id: ReviewId
    kind: ReviewKind
    members: tuple[NodeId, ...] = Field(min_length=1)
    decision_id: DecisionId


class AddConstraint(Model):
    op: Literal["add_constraint"] = "add_constraint"
    constraint_id: ConstraintId
    kind: Literal["must_link", "cannot_link"]
    left_id: ObservationId
    right_id: ObservationId
    decision_id: DecisionId

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.left_id >= self.right_id:
            raise ValueError("left_id must sort before right_id")
        return self


class RevokeConstraint(Model):
    op: Literal["revoke_constraint"] = "revoke_constraint"
    constraint_id: ConstraintId
    decision_id: DecisionId


class CloseReview(Model):
    op: Literal["close_review"] = "close_review"
    review_id: ReviewId
    status: Literal["resolved", "dismissed"]
    decision_id: DecisionId


Operation = Annotated[
    CreateEntity
    | Attach
    | Merge
    | Split
    | Promote
    | Label
    | ClearLabel
    | Relate
    | RevokeRelationship
    | OpenReview
    | CloseReview
    | AddConstraint
    | RevokeConstraint,
    Field(discriminator="op"),
]


class ReadSet(Model):
    """What a run read, for the conflict check at commit (design 01 §8)."""

    entity_ids: frozenset[EntityId] = frozenset()
    blocking_keys: frozenset[str] = frozenset()


class ChangeSet(Model):
    run_id: RunId | None = None  # None for operator actions outside a run
    base_seq: LedgerSeq
    ops: tuple[Operation, ...] = Field(min_length=1)
    read_set: ReadSet = ReadSet()

    @property
    def decision_ids(self) -> frozenset[str]:
        return frozenset(op.decision_id for op in self.ops)
