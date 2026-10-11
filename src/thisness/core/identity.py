"""Identity state: entities, snapshots, memberships, relationships (design 02 §6.3).

These records change only through ``commit()``. Validity is recorded as ledger
sequence numbers, so point-in-time questions have exact answers.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from thisness.core.base import Model
from thisness.core.types import (
    ConstraintId,
    DecisionId,
    EntityId,
    EntityType,
    EvidenceId,
    LedgerSeq,
    MembershipId,
    Name,
    NonEmpty,
    ObservationId,
    RelationshipId,
)

# Identity is never a relationship (invariant 6).
RESERVED_RELATIONS = frozenset({"same_entity", "same_as"})


class EntityState(StrEnum):
    ACTIVE = "active"
    PROVISIONAL = "provisional"
    RETIRED = "retired"


class Entity(Model):
    entity_id: EntityId
    entity_type: EntityType
    state: EntityState
    version: int = Field(ge=1)  # +1 per commit that touches the entity
    created_seq: LedgerSeq
    retired_seq: LedgerSeq | None = None
    merged_into: EntityId | None = None  # historical; resolution is derived

    @model_validator(mode="after")
    def _retirement_is_consistent(self) -> Self:
        retired = self.state is EntityState.RETIRED
        if retired != (self.retired_seq is not None):
            raise ValueError("retired_seq is set exactly when the entity is retired")
        if self.merged_into is not None and not retired:
            raise ValueError("only a retired entity can have merged_into")
        if self.merged_into == self.entity_id:
            raise ValueError("an entity cannot be merged into itself")
        return self


class EntitySnapshot(Model):
    entity_id: EntityId
    version: int = Field(ge=1)  # equals the entity version it was built at
    attributes: dict[str, JsonValue]
    derived_from: tuple[ObservationId, ...]
    canonicalizer_ref: NonEmpty
    created_seq: LedgerSeq


class IdentityMembership(Model):
    membership_id: MembershipId
    observation_id: ObservationId
    entity_id: EntityId
    basis_decision_id: DecisionId
    valid_from_seq: LedgerSeq
    valid_to_seq: LedgerSeq | None = None  # None = current

    @model_validator(mode="after")
    def _valid_interval(self) -> Self:
        if self.valid_to_seq is not None and self.valid_to_seq <= self.valid_from_seq:
            raise ValueError("valid_to_seq must be after valid_from_seq")
        return self

    def is_valid_at(self, seq: int) -> bool:
        return self.valid_from_seq <= seq and (self.valid_to_seq is None or seq < self.valid_to_seq)


class PairConstraint(Model):
    """A reviewer's verdict made durable (design 04 §2.5). Anchored on observations,
    which never move, so it survives later merges and splits. A cannot-link is hard:
    no commit may put its two observations in one entity."""

    constraint_id: ConstraintId
    kind: Literal["must_link", "cannot_link"]
    left_id: ObservationId
    right_id: ObservationId
    basis_decision_id: DecisionId
    valid_from_seq: LedgerSeq
    valid_to_seq: LedgerSeq | None = None

    @model_validator(mode="after")
    def _two_observations(self) -> Self:
        if self.left_id >= self.right_id:
            raise ValueError("left_id must sort before right_id")
        return self


class RelationshipAssertion(Model):
    relationship_id: RelationshipId
    relation: Name
    subject_entity_id: EntityId
    object_entity_id: EntityId
    basis_decision_id: DecisionId
    evidence_ids: tuple[EvidenceId, ...] = ()
    effective_from: date | None = None  # domain time: when it was true
    effective_to: date | None = None
    valid_from_seq: LedgerSeq  # system time: when we believed it
    valid_to_seq: LedgerSeq | None = None

    @model_validator(mode="after")
    def _not_identity(self) -> Self:
        if self.relation in RESERVED_RELATIONS:
            raise ValueError(f"{self.relation!r} is identity, not a relationship")
        if self.subject_entity_id == self.object_entity_id:
            raise ValueError("a relationship links two different entities")
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError("effective_to is before effective_from")
        if self.valid_to_seq is not None and self.valid_to_seq <= self.valid_from_seq:
            raise ValueError("valid_to_seq must be after valid_from_seq")
        return self
